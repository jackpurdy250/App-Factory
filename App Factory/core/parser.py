"""App Factory - the deterministic command parser.

No LLM sees a command before it is validated here, and no API call is made for
an invalid command. Parsing is regex and string matching only.

The rules, in order of application:
  1. The first character must be '#' or '!'.
  2. The verb is matched against a closed set. No fuzzy matching, no
     "did you mean".
  3. Targets resolve against the registry below, case-insensitively, using
     canonical names and registered aliases only.
  4. Arity is exact. Trailing tokens on a zero-argument command are an error,
     not something to ignore.
  5. An instruction must contain at least one non-whitespace character.

Every rejection returns `invalid command` to Window 1 and a reason code to
Window 3. The reason codes are a closed set; they never reach the CLI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Union

from .schemas.common import AgentRole, ProjectType, Stack
from .schemas.envelope import CliResponse, ReasonCode

BROADCAST = "ALL"

#: Commands are single-line. A newline is a hard syntax error rather than
#: something to silently join, because a pasted multi-line blob is far more
#: likely to be an accident than an intent.
_NEWLINE = re.compile(r"[\r\n]")


class Target(StrEnum):
    """Canonical routing targets."""

    OBSERVER = "OBSERVER"
    OPTIMIZER = "OPTIMIZER"
    IMPLEMENTER = "IMPLEMENTER"
    QC = "QC"
    DESIGN = "DESIGN"
    PROMPT_ENGINEER = "PROMPT_ENGINEER"
    COMMANDER = "COMMANDER"


class ExecVerb(StrEnum):
    ROUTE = "route"
    SHIP = "ship"
    REVISE = "revise"


class StateVerb(StrEnum):
    STATE = "state"
    LOG = "log"
    STATUS = "status"
    BUDGET = "budget"
    ISSUES = "issues"
    RULES = "rules"
    SNAPSHOTS = "snapshots"
    VERBOSE = "verbose"
    RELOAD = "reload"
    PROJECT = "project"
    STACK = "stack"
    TYPE = "type"


class Verbosity(StrEnum):
    QUIET = "quiet"
    NORMAL = "normal"
    TRACE = "trace"


@dataclass(frozen=True, slots=True)
class TargetSpec:
    target: Target
    role: AgentRole
    aliases: tuple[str, ...]
    routable: bool
    revisable: bool
    #: Whether `!state <target>` may address it.
    inspectable: bool
    #: Stage the pipeline re-enters when this target is routed or revised.
    reentry_stage: str


#: The registry. Unregistered spellings are rejected outright - silent
#: near-match correction is how you end up debugging an instruction that
#: quietly went to the wrong agent.
REGISTRY: dict[Target, TargetSpec] = {
    Target.OBSERVER: TargetSpec(
        target=Target.OBSERVER,
        role=AgentRole.OBSERVER,
        aliases=("obs", "brain"),
        routable=True,
        revisable=True,
        inspectable=True,
        reentry_stage="S2_SPEC",
    ),
    Target.OPTIMIZER: TargetSpec(
        target=Target.OPTIMIZER,
        role=AgentRole.OPTIMIZER,
        aliases=("po", "prompt-optimizer"),
        routable=True,
        revisable=True,
        inspectable=True,
        reentry_stage="S1_OPTIMIZE",
    ),
    Target.IMPLEMENTER: TargetSpec(
        target=Target.IMPLEMENTER,
        role=AgentRole.IMPLEMENTER,
        aliases=("impl", "builder"),
        routable=True,
        revisable=True,
        inspectable=True,
        reentry_stage="S3_BUILD",
    ),
    Target.QC: TargetSpec(
        target=Target.QC,
        role=AgentRole.QC,
        aliases=("qa", "gatekeeper"),
        routable=True,
        revisable=True,
        inspectable=True,
        reentry_stage="S4_REVIEW",
    ),
    Target.DESIGN: TargetSpec(
        target=Target.DESIGN,
        role=AgentRole.DESIGN,
        aliases=("design-critic", "ux"),
        routable=True,
        revisable=True,
        inspectable=True,
        reentry_stage="S4_REVIEW",
    ),
    Target.PROMPT_ENGINEER: TargetSpec(
        target=Target.PROMPT_ENGINEER,
        role=AgentRole.PROMPT_ENGINEER,
        aliases=("pe",),
        routable=True,
        revisable=True,
        inspectable=True,
        reentry_stage="S3_BUILD",
    ),
    Target.COMMANDER: TargetSpec(
        target=Target.COMMANDER,
        role=AgentRole.COMMANDER,
        aliases=("cmd",),
        routable=False,
        revisable=False,
        inspectable=False,
        reentry_stage="S0_INTAKE",
    ),
}


def _build_alias_index() -> dict[str, Target]:
    index: dict[str, Target] = {}
    for target, spec in REGISTRY.items():
        index[target.value.lower()] = target
        for alias in spec.aliases:
            alias_key = alias.lower()
            if alias_key in index:
                raise RuntimeError(f"duplicate target alias: {alias!r}")
            index[alias_key] = target
    return index


_ALIAS_INDEX: dict[str, Target] = _build_alias_index()

#: role -> canonical target, for reverse lookups elsewhere in the system.
ROLE_TO_TARGET: dict[AgentRole, Target] = {
    spec.role: target for target, spec in REGISTRY.items()
}


def resolve_target(token: str) -> Target | None:
    """Resolve one token to a canonical target, or None if unregistered."""
    return _ALIAS_INDEX.get(token.strip().lower())


# --------------------------------------------------------------------------
# Parse results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecCommand:
    """A '#' command. Enters the pipeline."""

    verb: ExecVerb
    raw: str
    target: Target | None = None
    broadcast: bool = False
    instruction: str = ""

    @property
    def spec(self) -> TargetSpec | None:
        return REGISTRY[self.target] if self.target is not None else None


@dataclass(frozen=True, slots=True)
class StateCommand:
    """A '!' command. Local reads only; never touches the network."""

    verb: StateVerb
    raw: str
    target: Target | None = None
    verbosity: Verbosity | None = None
    args: tuple[str, ...] = ()

    @property
    def subcommand(self) -> str | None:
        """First positional argument, for verbs that take one (`!project new`)."""

        return self.args[0] if self.args else None


@dataclass(frozen=True, slots=True)
class Rejection:
    """A refusal. `response` is what Window 1 shows; `code` and `detail` go to
    Window 3 only."""

    code: ReasonCode
    detail: str
    raw: str
    response: CliResponse = CliResponse.INVALID_COMMAND


ParseResult = Union[ExecCommand, StateCommand, Rejection]

#: Zero-argument commands, by verb token.
_ZERO_ARG_EXEC = frozenset({ExecVerb.SHIP})
#: Project slugs. Deliberately a duplicate of `ProjectSlug`'s pattern in
#: schemas/common.py rather than an import: the parser must reject a bad slug
#: without constructing a model. tests/test_parser.py asserts the two patterns
#: stay identical, so the duplication cannot drift silently.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")

#: Accepted `!project new` stack tokens, taken from the Stack enum so a new
#: stack becomes available to the command grammar automatically.
_STACK_VALUES = frozenset(stack.value for stack in Stack)

#: Accepted `!type` tokens, derived the same way so a new project type
#: becomes available to the grammar automatically.
_PROJECT_TYPE_VALUES = frozenset(item.value for item in ProjectType)

_ZERO_ARG_STATE = frozenset(
    {
        StateVerb.LOG,
        StateVerb.STATUS,
        StateVerb.BUDGET,
        StateVerb.ISSUES,
        StateVerb.RULES,
        StateVerb.SNAPSHOTS,
        StateVerb.RELOAD,
    }
)

#: `!stack` and `!type` report with no argument and set with exactly one.
#: The manual documents both forms, so their arity is optional, not zero.
_VALUE_STATE = frozenset({StateVerb.STACK, StateVerb.TYPE})


def parse(line: str) -> ParseResult:
    """Parse one operator command. Total function: always returns a result,
    never raises, never calls out."""
    raw = line if isinstance(line, str) else ""

    if _NEWLINE.search(raw):
        return Rejection(
            code=ReasonCode.E_SIGIL,
            detail="commands are single-line; newline found",
            raw=raw,
        )

    text = raw.strip()
    if not text:
        return Rejection(
            code=ReasonCode.E_SIGIL, detail="empty input", raw=raw
        )

    sigil = text[0]
    if sigil not in ("#", "!"):
        return Rejection(
            code=ReasonCode.E_SIGIL,
            detail=f"first character must be '#' or '!', got {sigil!r}",
            raw=raw,
        )

    body = text[1:]
    if not body or body[0].isspace():
        return Rejection(
            code=ReasonCode.E_VERB,
            detail="no verb directly after the sigil",
            raw=raw,
        )

    head, _, tail = body.partition(" ")
    verb_token = head.lower()
    rest = tail.strip()

    if sigil == "#":
        return _parse_exec(verb_token, rest, raw)
    return _parse_state(verb_token, rest, raw)


def _parse_exec(verb_token: str, rest: str, raw: str) -> ParseResult:
    try:
        verb = ExecVerb(verb_token)
    except ValueError:
        return Rejection(
            code=ReasonCode.E_VERB,
            detail=f"unknown execution verb {verb_token!r} "
            f"(valid: {', '.join(v.value for v in ExecVerb)})",
            raw=raw,
        )

    if verb in _ZERO_ARG_EXEC:
        if rest:
            return Rejection(
                code=ReasonCode.E_TRAILING,
                detail=f"#{verb.value} takes no arguments, got {rest!r}",
                raw=raw,
            )
        return ExecCommand(verb=verb, raw=raw)

    # #route and #revise: <target> <instruction>
    if not rest:
        return Rejection(
            code=ReasonCode.E_ARITY,
            detail=f"#{verb.value} requires a target and an instruction",
            raw=raw,
        )

    target_token, _, instruction = rest.partition(" ")
    instruction = instruction.strip()

    is_broadcast = target_token.strip().upper() == BROADCAST
    if is_broadcast and verb is ExecVerb.REVISE:
        return Rejection(
            code=ReasonCode.E_TARGET_SCOPE,
            detail="#revise requires a single target; ALL is not accepted",
            raw=raw,
        )

    target: Target | None = None
    if not is_broadcast:
        target = resolve_target(target_token)
        if target is None:
            return Rejection(
                code=ReasonCode.E_TARGET,
                detail=f"{target_token!r} is not a registered target or alias",
                raw=raw,
            )
        spec = REGISTRY[target]
        if verb is ExecVerb.ROUTE and not spec.routable:
            return Rejection(
                code=ReasonCode.E_TARGET,
                detail=f"{target.value} is not routable",
                raw=raw,
            )
        if verb is ExecVerb.REVISE and not spec.revisable:
            return Rejection(
                code=ReasonCode.E_TARGET,
                detail=f"{target.value} is not revisable",
                raw=raw,
            )

    if not instruction:
        return Rejection(
            code=ReasonCode.E_EMPTY_ARG,
            detail=f"#{verb.value} requires a non-empty instruction",
            raw=raw,
        )

    return ExecCommand(
        verb=verb,
        raw=raw,
        target=target,
        broadcast=is_broadcast,
        instruction=instruction,
    )


def _parse_state(verb_token: str, rest: str, raw: str) -> ParseResult:
    try:
        verb = StateVerb(verb_token)
    except ValueError:
        return Rejection(
            code=ReasonCode.E_VERB,
            detail=f"unknown state verb {verb_token!r} "
            f"(valid: {', '.join(v.value for v in StateVerb)})",
            raw=raw,
        )

    if verb in _ZERO_ARG_STATE:
        if rest:
            return Rejection(
                code=ReasonCode.E_TRAILING,
                detail=f"!{verb.value} takes no arguments, got {rest!r}",
                raw=raw,
            )
        return StateCommand(verb=verb, raw=raw)

    tokens = rest.split()

    if verb is StateVerb.STATE:
        if not tokens:
            return Rejection(
                code=ReasonCode.E_ARITY,
                detail="!state requires exactly one target",
                raw=raw,
            )
        if len(tokens) > 1:
            return Rejection(
                code=ReasonCode.E_TRAILING,
                detail=f"!state takes exactly one target, got {len(tokens)}",
                raw=raw,
            )
        if tokens[0].upper() == BROADCAST:
            return Rejection(
                code=ReasonCode.E_TARGET_SCOPE,
                detail="!state requires a single target; ALL is not accepted",
                raw=raw,
            )
        target = resolve_target(tokens[0])
        if target is None or not REGISTRY[target].inspectable:
            return Rejection(
                code=ReasonCode.E_TARGET,
                detail=f"{tokens[0]!r} has no inspectable state region",
                raw=raw,
            )
        return StateCommand(verb=verb, raw=raw, target=target)

    if verb is StateVerb.VERBOSE:
        if not tokens:
            return Rejection(
                code=ReasonCode.E_ARITY,
                detail="!verbose requires a level",
                raw=raw,
            )
        if len(tokens) > 1:
            return Rejection(
                code=ReasonCode.E_TRAILING,
                detail=f"!verbose takes exactly one level, got {len(tokens)}",
                raw=raw,
            )
        try:
            level = Verbosity(tokens[0].lower())
        except ValueError:
            return Rejection(
                code=ReasonCode.E_VALUE,
                detail=f"{tokens[0]!r} is not a level "
                f"(valid: {', '.join(v.value for v in Verbosity)})",
                raw=raw,
            )
        return StateCommand(verb=verb, raw=raw, verbosity=level)

    if verb is StateVerb.PROJECT:
        # Bare `!project` lists the workspace.
        if not tokens:
            return StateCommand(verb=verb, raw=raw)

        action = tokens[0].lower()

        if action == "new":
            if len(tokens) != 3:
                return Rejection(
                    code=ReasonCode.E_ARITY,
                    detail="!project new takes a slug and a stack, got "
                    f"{len(tokens) - 1} argument(s)",
                    raw=raw,
                )
            slug, stack_token = tokens[1], tokens[2].lower()
            if not _SLUG_RE.match(slug):
                return Rejection(
                    code=ReasonCode.E_VALUE,
                    detail=f"{slug!r} is not a valid slug (lowercase letters, "
                    "digits, and inner hyphens; 2-40 characters)",
                    raw=raw,
                )
            if stack_token not in _STACK_VALUES:
                return Rejection(
                    code=ReasonCode.E_VALUE,
                    detail=f"{tokens[2]!r} is not a stack "
                    f"(valid: {', '.join(sorted(_STACK_VALUES))})",
                    raw=raw,
                )
            return StateCommand(verb=verb, raw=raw, args=("new", slug, stack_token))

        if action == "use":
            if len(tokens) != 2:
                return Rejection(
                    code=ReasonCode.E_ARITY,
                    detail="!project use takes exactly one slug, got "
                    f"{len(tokens) - 1} argument(s)",
                    raw=raw,
                )
            slug = tokens[1]
            if not _SLUG_RE.match(slug):
                return Rejection(
                    code=ReasonCode.E_VALUE,
                    detail=f"{slug!r} is not a valid slug (lowercase letters, "
                    "digits, and inner hyphens; 2-40 characters)",
                    raw=raw,
                )
            return StateCommand(verb=verb, raw=raw, args=("use", slug))

        return Rejection(
            code=ReasonCode.E_VALUE,
            detail=f"{tokens[0]!r} is not a !project action (valid: new, use)",
            raw=raw,
        )

    if verb in _VALUE_STATE:
        is_stack = verb is StateVerb.STACK
        allowed = _STACK_VALUES if is_stack else _PROJECT_TYPE_VALUES
        noun = "stack" if is_stack else "project type"
        if not tokens:
            return StateCommand(verb=verb, raw=raw)
        if len(tokens) > 1:
            return Rejection(
                code=ReasonCode.E_TRAILING,
                detail=f"!{verb.value} takes at most one {noun}, "
                f"got {len(tokens)}",
                raw=raw,
            )
        value = tokens[0].lower()
        if value not in allowed:
            return Rejection(
                code=ReasonCode.E_VALUE,
                detail=f"{tokens[0]!r} is not a {noun} "
                f"(valid: {', '.join(sorted(allowed))})",
                raw=raw,
            )
        return StateCommand(verb=verb, raw=raw, args=(value,))

    # Unreachable: every StateVerb is either zero-arg or handled above.
    raise AssertionError(f"unhandled state verb {verb!r}")