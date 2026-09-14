"""Prompt assembly.

Each role's standing instructions live in `agents/<role>/system.md`. This
module loads them, appends the Prompt Engineer's active rules, appends the
output contract, and renders the projection as the user message.

Nothing here knows which provider or model will receive the prompt. That
binding is resolved in `core/config.py` from `config/models.toml`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path, PurePath
from typing import Any, Mapping, Sequence

from core.schemas import PAYLOAD_SCHEMAS, AgentRole, Rule

#: Directory per role under `agents/`.
ROLE_DIRNAMES: dict[AgentRole, str] = {
    AgentRole.COMMANDER: "commander",
    AgentRole.OPTIMIZER: "optimizer",
    AgentRole.OBSERVER: "observer",
    AgentRole.IMPLEMENTER: "implementer",
    AgentRole.QC: "qc",
    AgentRole.DESIGN: "design",
    AgentRole.PROMPT_ENGINEER: "prompt_engineer",
}

SYSTEM_FILENAME = "system.md"

#: The code writer is the one role that does not answer in JSON. Its product
#: is files on disk, emitted as <file> blocks and parsed by core.artifacts.
TEXT_ROLES = frozenset({AgentRole.IMPLEMENTER})

RULES_HEADING = "## Appended rules (active this iteration)"
REPAIR_HEADING = "## Repair required"
CONTRACT_HEADING = "## Output contract"

_JSON_CONTRACT = """{heading}

Return exactly one JSON object. Nothing before it, nothing after it. No prose,
no Markdown, no code fence.

The object must validate against this schema:

{schema}

Hard rules:

- Field names must match the schema exactly. Unknown fields are rejected and
  the whole reply is discarded.
- Use null for an optional value you do not know. Never invent a placeholder
  string, a fake identifier, or a guessed number.
- Identifiers follow their patterns exactly: requirements `R-001`, acceptance
  criteria `AC-001`, QC issues `QC-0001`, design issues `DES-0001`, observer
  issues `OBS-0001`, rules `PE-001`.
- Stay inside your role. Do not write another agent's region of the state bus
  and do not restate the projection back to the caller.
- Two optional top-level keys are accepted alongside the payload fields:
  `"requests"`, an array of `{{"what": "...", "why": "...", "blocking": false}}`,
  and `"assumptions"`, an array of
  `{{"statement": "...", "confidence": 0.5, "req_id": null}}`. Use `requests`
  when you needed something you were not given; the Commander decides whether
  to surface it. Use `assumptions` when you proceeded anyway.
"""

_IMPLEMENTER_CONTRACT = """{heading}

Reply with these blocks and nothing else. No prose outside the blocks.

<plan>
{{
  "plan_summary": "one sentence",
  "plan": ["ordered build steps, one short line each"],
  "components": [
    {{"name": "...", "responsibility": "...", "depends_on": []}}
  ],
  "files": [
    {{"path": "relative/path.ext", "purpose": "...", "component": null}}
  ],
  "dependencies": [
    {{"name": "...", "version": null, "reason": "..."}}
  ],
  "deviations": [
    {{"req_id": "R-001", "description": "...", "justification": "..."}}
  ],
  "entrypoint": "relative/path.ext"
}}
</plan>

<file path="relative/path.ext">
the complete contents of that file
</file>

<notes>
Optional. One or two lines for the reviewers. Omit the block if you have
nothing to say.
</notes>

Hard rules:

- The `<plan>` block contains one JSON object, exactly the keys above. It is
  parsed strictly; a missing brace discards the whole build.
- One `<file>` block per file, with the path in the opening tag. Repeat the
  block for every file. Paths are relative, no leading slash, no `..`, no
  dotfiles, at most six segments.
- Write every file in full, every time. Never write "unchanged", never elide
  with `...`, never leave a TODO. A file you omit is a file that does not
  exist in the build.
- Only write paths whose extension appears in `task.allowed_suffixes`. Any
  other extension is refused and the build is rejected.
- `entrypoint` must be one of the paths you wrote.
- Ceilings: 60 files, 200000 bytes per file, 2000000 bytes in total.
- You are given the spec, not the conversation. Build what the spec says. If
  the spec is wrong, say so in `<notes>` and build it anyway.
"""


class PromptError(RuntimeError):
    """Raised when a role's prompt assets are missing or unusable."""


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """One ready-to-send prompt pair."""

    role: AgentRole
    system: str
    user: str
    json_schema: dict[str, Any] | None = None


def _json_default(value: Any) -> Any:
    """Make projections serializable without losing information."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (PurePath, Path)):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, (tuple, list)):
        return list(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return str(value)


def render_projection(projection: Mapping[str, Any]) -> str:
    """Serialize a projection deterministically.

    Sorted keys matter: a stable rendering means an unchanged projection
    produces an unchanged prompt, which is what makes a repeated agent call
    diagnosable.
    """

    return json.dumps(
        dict(projection), indent=2, sort_keys=True, default=_json_default
    )


@lru_cache(maxsize=None)
def payload_schema(role: AgentRole) -> dict[str, Any] | None:
    """JSON Schema for a role's payload, or None for the code writer."""

    if role in TEXT_ROLES:
        return None
    adapter = PAYLOAD_SCHEMAS.get(role)
    if adapter is None:
        raise PromptError(f"{role.value} has no payload schema")
    try:
        return adapter.json_schema()
    except Exception as exc:  # pragma: no cover - schema generation failure
        raise PromptError(f"cannot build a schema for {role.value}: {exc}") from exc


def output_contract(role: AgentRole) -> str:
    """The output contract appended to a role's standing instructions."""

    if role in TEXT_ROLES:
        return _IMPLEMENTER_CONTRACT.format(heading=CONTRACT_HEADING)
    schema = payload_schema(role)
    return _JSON_CONTRACT.format(
        heading=CONTRACT_HEADING,
        schema=json.dumps(schema, indent=2, sort_keys=True),
    )


def rules_block(role: AgentRole, rules: Sequence[Rule]) -> str:
    """Render the Prompt Engineer's active rules for one role.

    Only rules scoped to this role are appended, and only while active. The
    ordering is by rule id so a rule's position never shifts under the model
    between iterations.
    """

    scoped = sorted(
        (rule for rule in rules if rule.active and rule.scope is role),
        key=lambda rule: rule.rule_id,
    )
    if not scoped:
        return ""
    lines = [RULES_HEADING, ""]
    for rule in scoped:
        origin = f" (from {rule.origin_issue})" if rule.origin_issue else ""
        lines.append(f"- **{rule.rule_id}**{origin}: {rule.rule_text}")
    lines.append("")
    lines.append(
        "These are corrections from earlier iterations of this build. They "
        "are additions to your instructions, not replacements."
    )
    return "\n".join(lines)


class PromptLibrary:
    """Loads and assembles prompts from the `agents/` tree."""

    def __init__(self, agents_dir: Path | str) -> None:
        self._agents_dir = Path(agents_dir)
        self._cache: dict[AgentRole, str] = {}

    @property
    def agents_dir(self) -> Path:
        return self._agents_dir

    def path_for(self, role: AgentRole) -> Path:
        dirname = ROLE_DIRNAMES.get(role)
        if dirname is None:
            raise PromptError(f"{role.value} has no prompt directory")
        return self._agents_dir / dirname / SYSTEM_FILENAME

    def system_text(self, role: AgentRole) -> str:
        """Standing instructions for a role, read once and cached."""

        cached = self._cache.get(role)
        if cached is not None:
            return cached
        path = self.path_for(role)
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise PromptError(f"missing prompt file: {path}") from exc
        except OSError as exc:  # pragma: no cover - filesystem failure
            raise PromptError(f"cannot read {path}: {exc}") from exc
        if not text:
            raise PromptError(f"prompt file is empty: {path}")
        self._cache[role] = text
        return text

    def invalidate(self) -> None:
        """Drop cached prompt text so edited files are picked up."""

        self._cache.clear()

    def missing(self) -> list[AgentRole]:
        """Roles whose prompt file is absent or empty."""

        absent: list[AgentRole] = []
        for role in ROLE_DIRNAMES:
            try:
                self.system_text(role)
            except PromptError:
                absent.append(role)
        return absent

    def check(self) -> None:
        """Readiness check used at startup, before any command is accepted."""

        absent = self.missing()
        if absent:
            raise PromptError(
                "missing or empty prompt files for: "
                + ", ".join(role.value for role in absent)
            )

    def json_schema(self, role: AgentRole) -> dict[str, Any] | None:
        return payload_schema(role)

    def render(
        self,
        *,
        role: AgentRole,
        projection: Mapping[str, Any],
        rules: Sequence[Rule] = (),
        repair: str | None = None,
    ) -> RenderedPrompt:
        """Assemble the prompt pair for one call."""

        sections = [self.system_text(role)]

        appended = rules_block(role, rules)
        if appended:
            sections.append(appended)

        sections.append(output_contract(role))

        if repair:
            sections.append(f"{REPAIR_HEADING}\n\n{repair.strip()}")

        return RenderedPrompt(
            role=role,
            system="\n\n".join(section.strip() for section in sections if section),
            user=render_projection(projection),
            json_schema=payload_schema(role),
        )
