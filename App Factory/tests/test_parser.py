"""The command grammar.

The parser is the one component that must never consult a model, so it is
also the one component that can be tested exhaustively. Every rejection code
in the manual appears below with the exact input that produces it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import TypeAdapter, ValidationError  # noqa: E402

from core.parser import (  # noqa: E402
    BROADCAST,
    REGISTRY,
    ExecCommand,
    ExecVerb,
    Rejection,
    StateCommand,
    StateVerb,
    Target,
    Verbosity,
    _SLUG_RE,
    _STACK_VALUES,
    parse,
)
from core.schemas.common import (  # noqa: E402
    AgentRole,
    ProjectSlug,
    Stack,
    Stage,
)
from core.schemas.envelope import CliResponse, ReasonCode  # noqa: E402
from tests.harness import Checker  # noqa: E402


def run() -> tuple[int, int]:
    check = Checker("test_parser")

    # -- execution commands ------------------------------------------------

    check.section("# execution commands")

    routed = parse("#route QC add a CSV header filter")
    check.check("#route parses to an ExecCommand", isinstance(routed, ExecCommand))
    if isinstance(routed, ExecCommand):
        check.same("#route verb is route", routed.verb, ExecVerb.ROUTE)
        check.same("#route target is QC", routed.target, Target.QC)
        check.equal(
            "#route instruction is kept verbatim",
            routed.instruction,
            "add a CSV header filter",
        )
        check.falsy("#route to one target is not a broadcast", routed.broadcast)
        spec = routed.spec
        check.same(
            "#route QC resolves to the QC role",
            spec.role if spec else None,
            AgentRole.QC,
        )

    everyone = parse("#route ALL tighten the empty states")
    check.check("#route ALL parses", isinstance(everyone, ExecCommand))
    if isinstance(everyone, ExecCommand):
        check.truthy("#route ALL sets broadcast", everyone.broadcast)
        check.same("#route ALL has no single target", everyone.target, None)
        check.equal(
            "#route ALL keeps its instruction",
            everyone.instruction,
            "tighten the empty states",
        )
        check.same("a broadcast has no target spec", everyone.spec, None)

    check.equal("broadcast token is ALL", BROADCAST, "ALL")

    shipped = parse("#ship")
    check.check("#ship parses", isinstance(shipped, ExecCommand))
    if isinstance(shipped, ExecCommand):
        check.same("#ship verb is ship", shipped.verb, ExecVerb.SHIP)
        check.equal("#ship takes no instruction", shipped.instruction, "")
        check.same("#ship takes no target", shipped.target, None)

    revised = parse("#revise IMPLEMENTER fix the sticky header")
    check.check("#revise parses", isinstance(revised, ExecCommand))
    if isinstance(revised, ExecCommand):
        check.same("#revise verb is revise", revised.verb, ExecVerb.REVISE)
        check.same("#revise target is IMPLEMENTER", revised.target, Target.IMPLEMENTER)
        check.equal(
            "#revise keeps its instruction",
            revised.instruction,
            "fix the sticky header",
        )

    multi = parse("#route IMPLEMENTER add a  double space and, punctuation.")
    check.equal(
        "instruction whitespace and punctuation survive intact",
        multi.instruction if isinstance(multi, ExecCommand) else None,
        "add a  double space and, punctuation.",
    )

    # -- aliases -----------------------------------------------------------

    check.section("target aliases")

    alias_cases = [
        ("obs", AgentRole.OBSERVER),
        ("brain", AgentRole.OBSERVER),
        ("po", AgentRole.OPTIMIZER),
        ("prompt-optimizer", AgentRole.OPTIMIZER),
        ("impl", AgentRole.IMPLEMENTER),
        ("builder", AgentRole.IMPLEMENTER),
        ("qa", AgentRole.QC),
        ("gatekeeper", AgentRole.QC),
        ("design-critic", AgentRole.DESIGN),
        ("ux", AgentRole.DESIGN),
        ("pe", AgentRole.PROMPT_ENGINEER),
    ]
    for alias, role in alias_cases:
        parsed = parse(f"#route {alias} do the thing")
        resolved = (
            parsed.spec.role
            if isinstance(parsed, ExecCommand) and parsed.spec
            else None
        )
        check.same(f"alias {alias!r} resolves to {role.value}", resolved, role)

    # -- target registry invariants ---------------------------------------

    check.section("target registry")

    check.equal("every target has a registry entry", len(REGISTRY), len(Target))
    for target in Target:
        check.check(f"{target.value} is registered", target in REGISTRY)

    check.falsy(
        "COMMANDER is not routable", REGISTRY[Target.COMMANDER].routable
    )
    for target in (
        Target.OBSERVER,
        Target.OPTIMIZER,
        Target.IMPLEMENTER,
        Target.QC,
        Target.DESIGN,
        Target.PROMPT_ENGINEER,
    ):
        check.truthy(f"{target.value} is routable", REGISTRY[target].routable)

    reentry = {
        Target.OPTIMIZER: Stage.OPTIMIZE,
        Target.OBSERVER: Stage.SPEC,
        Target.IMPLEMENTER: Stage.BUILD,
        Target.QC: Stage.REVIEW,
        Target.DESIGN: Stage.REVIEW,
        Target.PROMPT_ENGINEER: Stage.BUILD,
        Target.COMMANDER: Stage.INTAKE,
    }
    # reentry_stage is declared `str` in core/parser.py, not Stage, so the
    # comparison has to be against the enum value rather than the member.
    for target, stage in reentry.items():
        check.same(
            f"{target.value} re-enters at {stage.value}",
            REGISTRY[target].reentry_stage,
            stage.value,
        )

    seen: dict[str, str] = {}
    collisions: list[str] = []
    for target, spec in REGISTRY.items():
        for token in (target.value, *spec.aliases):
            key = token.lower()
            if key in seen:
                collisions.append(f"{token} -> {seen[key]} and {target.value}")
            seen[key] = target.value
    check.equal("no alias collides with another target", collisions, [])

    # -- state commands ----------------------------------------------------

    check.section("! state commands")

    inspected = parse("!state OBSERVER")
    check.check("!state parses to a StateCommand", isinstance(inspected, StateCommand))
    if isinstance(inspected, StateCommand):
        check.same("!state verb", inspected.verb, StateVerb.STATE)
        check.same("!state target", inspected.target, Target.OBSERVER)

    aliased_state = parse("!state obs")
    check.same(
        "!state accepts an alias",
        aliased_state.target if isinstance(aliased_state, StateCommand) else None,
        Target.OBSERVER,
    )

    simple_verbs = [
        ("!log", StateVerb.LOG),
        ("!status", StateVerb.STATUS),
        ("!budget", StateVerb.BUDGET),
        ("!issues", StateVerb.ISSUES),
        ("!rules", StateVerb.RULES),
        ("!snapshots", StateVerb.SNAPSHOTS),
        ("!reload", StateVerb.RELOAD),
        ("!stack", StateVerb.STACK),
        ("!type", StateVerb.TYPE),
        ("!project", StateVerb.PROJECT),
    ]
    for line, verb in simple_verbs:
        parsed = parse(line)
        check.same(
            f"{line} parses to {verb.value}",
            parsed.verb if isinstance(parsed, StateCommand) else None,
            verb,
        )

    for token, expected in (
        ("quiet", Verbosity.QUIET),
        ("normal", Verbosity.NORMAL),
        ("trace", Verbosity.TRACE),
    ):
        parsed = parse(f"!verbose {token}")
        check.same(
            f"!verbose {token} parses",
            parsed.verbosity if isinstance(parsed, StateCommand) else None,
            expected,
        )

    bare_project = parse("!project")
    check.equal(
        "bare !project carries no arguments",
        bare_project.args if isinstance(bare_project, StateCommand) else None,
        (),
    )

    new_project = parse("!project new my-app go")
    check.equal(
        "!project new keeps slug and stack",
        new_project.args if isinstance(new_project, StateCommand) else None,
        ("new", "my-app", "go"),
    )
    check.equal(
        "!project new subcommand",
        new_project.subcommand if isinstance(new_project, StateCommand) else None,
        "new",
    )

    use_project = parse("!project use my-app")
    check.equal(
        "!project use keeps the slug",
        use_project.args if isinstance(use_project, StateCommand) else None,
        ("use", "my-app"),
    )

    # -- rejections --------------------------------------------------------

    check.section("rejections")

    matrix = [
        ("route QC add a filter", ReasonCode.E_SIGIL, "no sigil"),
        ("", ReasonCode.E_SIGIL, "empty line"),
        ("   ", ReasonCode.E_SIGIL, "whitespace only"),
        ("&pip install requests", ReasonCode.E_SIGIL, "reserved & sigil"),
        ("#route QC one\ntwo", ReasonCode.E_SIGIL, "embedded newline"),
        ("#", ReasonCode.E_VERB, "sigil with no verb"),
        ("#deploy now", ReasonCode.E_VERB, "unknown verb"),
        ("#ship now", ReasonCode.E_TRAILING, "argument after #ship"),
        ("#route QC", ReasonCode.E_EMPTY_ARG, "target with no instruction"),
        ("#revise ALL fix it", ReasonCode.E_TARGET_SCOPE, "#revise cannot broadcast"),
        ("!state commander", ReasonCode.E_TARGET, "commander is not inspectable"),
        ("#route nobody do it", ReasonCode.E_TARGET, "unknown target"),
        ("#route implementor go", ReasonCode.E_TARGET, "misspelled target"),
        ("!verbose loud", ReasonCode.E_VALUE, "unknown verbosity"),
        ("!project new My_App web", ReasonCode.E_VALUE, "illegal slug"),
        ("!project new demo klingon", ReasonCode.E_VALUE, "unknown stack"),
    ]
    for line, code, why in matrix:
        parsed = parse(line)
        if not isinstance(parsed, Rejection):
            check.check(f"{why}: {line!r} is rejected", False, f"got {parsed!r}")
            continue
        check.same(f"{why}: {line!r} -> {code.value}", parsed.code, code)
        check.equal(
            f"{why}: Window 1 sees only 'invalid command'",
            parsed.response.value,
            "invalid command",
        )

    for line in ("#route COMMANDER do it", "#route ALL", "#revise QC"):
        check.check(
            f"{line!r} is refused", isinstance(parse(line), Rejection)
        )

    check.same(
        "rejection response defaults to the closed vocabulary",
        Rejection(
            code=ReasonCode.E_VERB, detail="x", raw="#x"
        ).response,
        CliResponse.INVALID_COMMAND,
    )

    # -- duplicated patterns cannot drift ---------------------------------

    check.section("pattern agreement")

    slug_adapter = TypeAdapter(ProjectSlug)

    def slug_ok(value: str) -> bool:
        try:
            slug_adapter.validate_python(value)
        except ValidationError:
            return False
        return True

    for candidate in (
        "my-app",
        "a1",
        "go-service-2",
        "My_App",
        "-leading",
        "trailing-",
        "a",
        "has space",
        "UPPER",
    ):
        check.equal(
            f"parser and schema agree on slug {candidate!r}",
            bool(_SLUG_RE.match(candidate)),
            slug_ok(candidate),
        )

    check.equal(
        "every Stack value is an accepted !project new token",
        _STACK_VALUES,
        frozenset(stack.value for stack in Stack),
    )

    # -- !stack / !type are setters, not zero-arg reads ---------------------

    check.section("!stack and !type arity")

    # Imported here so the shared import block stays untouched.
    from core.parser import StateCommand as _StateCommand
    from core.parser import _PROJECT_TYPE_VALUES
    from core.schemas.common import ProjectType

    for token, value, verb in (
        ("stack", "go", StateVerb.STACK),
        ("type", "cli", StateVerb.TYPE),
    ):
        bare = parse(f"!{token}")
        check.check(
            f"!{token} with no argument still reports",
            isinstance(bare, _StateCommand) and bare.args == (),
            f"got {bare!r}",
        )
        setter = parse(f"!{token} {value}")
        if not isinstance(setter, _StateCommand):
            check.check(f"!{token} {value} parses", False, f"got {setter!r}")
            continue
        check.same(f"!{token} {value} keeps its verb", setter.verb, verb)
        check.equal(
            f"!{token} {value} carries the declared value",
            setter.subcommand,
            value,
        )
        check.equal(
            f"!{token} {value.upper()} is folded to lower case",
            getattr(parse(f"!{token} {value.upper()}"), "subcommand", None),
            value,
        )

    for line, code, why in (
        ("!stack klingon", ReasonCode.E_VALUE, "unknown stack"),
        ("!type widget", ReasonCode.E_VALUE, "unknown project type"),
        ("!stack go web", ReasonCode.E_TRAILING, "two stacks at once"),
        ("!type cli app", ReasonCode.E_TRAILING, "two types at once"),
    ):
        parsed = parse(line)
        if not isinstance(parsed, Rejection):
            check.check(f"{why}: {line!r} is rejected", False, f"got {parsed!r}")
            continue
        check.same(f"{why}: {line!r} -> {code.value}", parsed.code, code)
        check.equal(
            f"{why}: Window 1 sees only 'invalid command'",
            parsed.response.value,
            "invalid command",
        )

    check.equal(
        "every ProjectType value is an accepted !type token",
        _PROJECT_TYPE_VALUES,
        frozenset(item.value for item in ProjectType),
    )

    return check.report()


if __name__ == "__main__":
    passed, total = run()
    raise SystemExit(0 if passed == total else 1)
