"""The agent runner: the only place that talks to a model.

Everything provider-shaped stops here. The pipeline asks for a role and a
projection and gets back a validated envelope; it never learns which provider
answered, which model was used, or how many times the call was retried.

What this module guarantees:

  * **Nothing is trusted.** A reply is parsed, schema-validated, and only then
    wrapped in an envelope. A model that invents a field fails validation
    rather than poisoning the state bus.

  * **The envelope is stamped, not requested.** Agents return payload JSON
    only. `run_id`, `step_id`, `iteration`, `input_state_hash` and telemetry
    are filled in from what the pipeline knows, so a model cannot claim to
    have read a state it never saw.

  * **Failure is bounded.** Each provider gets `max_attempts` tries, the
    second carrying a repair instruction. When the chain is exhausted the
    runner raises instead of returning something half-valid.

The code writer is the one exception to JSON: its reply is raw text carrying
`<file>` blocks, and this module hands that text back untouched for
`core.artifacts` to extract. It is identified by its prompt having no JSON
schema, not by a role check, so `core` never imports `agents` at runtime.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from pydantic import ValidationError

from .budgeter import Budgeter, TrimResult
from .config import ConfigStore, ProviderConfig, RoleConfig
from .events import EventBus
from .providers import BaseProvider, ProviderError, ProviderResult, build_provider
from .schemas import (
    AgentRole,
    CommanderOutput,
    ContextActionRecord,
    DesignOutput,
    EnvelopeBase,
    ImplementerOutput,
    ObserverOutput,
    OptimizerOutput,
    OutputStatus,
    PromptEngineerOutput,
    QcOutput,
    Rule,
    Telemetry,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agents.prompts import PromptLibrary, RenderedPrompt


#: Role to concrete envelope class. The discriminated union could infer this
#: from a reply, but the runner already knows who it called, and trusting the
#: model to name itself is exactly the kind of drift the envelope prevents.
ENVELOPE_CLASSES: dict[AgentRole, type[EnvelopeBase]] = {
    AgentRole.OPTIMIZER: OptimizerOutput,
    AgentRole.OBSERVER: ObserverOutput,
    AgentRole.IMPLEMENTER: ImplementerOutput,
    AgentRole.QC: QcOutput,
    AgentRole.DESIGN: DesignOutput,
    AgentRole.PROMPT_ENGINEER: PromptEngineerOutput,
    AgentRole.COMMANDER: CommanderOutput,
}

#: Envelope-level fields an agent is allowed to volunteer. Everything else on
#: the envelope is stamped by the runner.
ENVELOPE_EXTRA_KEYS = ("requests", "assumptions")

#: Envelope fields a model sometimes echoes back. Dropped rather than
#: rejected: the payload is what matters, and `extra="forbid"` would fail the
#: whole call over a harmless copy of `run_id`.
_STAMPED_KEYS = frozenset(
    {
        "agent",
        "schema_version",
        "run_id",
        "step_id",
        "iteration",
        "input_state_hash",
        "emitted_at",
        "telemetry",
        "status",
        "error",
    }
)

#: Event codes emitted on the log channel (Window 3).
CODE_CTX_TRIM = "CTX_TRIM"
CODE_CALL = "AGENT_CALL"
CODE_RETRY = "AGENT_RETRY"
CODE_ERROR = "AGENT_ERROR"
CODE_INVALID = "AGENT_INVALID"
CODE_OK = "AGENT_OK"
CODE_FALLBACK = "AGENT_FALLBACK"
CODE_FAIL = "AGENT_FAIL"

_REPAIR_TEMPLATE = (
    "Your previous reply was rejected: {reason}\n"
    "Return one JSON object matching the contract exactly. "
    "No prose, no code fences, no fields outside the schema."
)


class AgentFailure(RuntimeError):
    """Raised when every provider in a role's chain has been exhausted."""

    def __init__(
        self,
        message: str,
        *,
        role: AgentRole,
        attempts: int = 0,
        last_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.role = role
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True, slots=True)
class AgentRun:
    """One completed agent call.

    `envelope` is None for the code writer, whose reply is raw text; the
    pipeline extracts files from `raw` and stamps the envelope itself.
    """

    role: AgentRole
    raw: str
    telemetry: Telemetry
    attempts: int
    envelope: EnvelopeBase | None = None
    trim: TrimResult | None = None
    context_action: ContextActionRecord | None = None

    @property
    def is_text(self) -> bool:
        return self.envelope is None


# --------------------------------------------------------------------------
# Reply parsing
# --------------------------------------------------------------------------


def strip_code_fences(text: str) -> str:
    """Remove a wrapping ``` fence, which models add despite instructions."""

    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped

    # Drop the opening fence (and any language tag on it).
    lines = lines[1:]
    # Drop the closing fence if present.
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip().startswith("```"):
            lines = lines[:index]
            break
    return "\n".join(lines).strip()


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first complete JSON object out of a reply.

    Brace counting rather than a regex, because payloads nest and contain
    braces inside strings. Quote and escape state is tracked so a `}` inside
    a string value cannot end the scan early.
    """

    candidate = strip_code_fences(text)
    if not candidate:
        raise ValueError("reply was empty")

    start = candidate.find("{")
    if start == -1:
        raise ValueError("reply contained no JSON object")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(candidate)):
        char = candidate[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                blob = candidate[start : index + 1]
                try:
                    parsed = json.loads(blob)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"reply was not valid JSON: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise ValueError("reply JSON was not an object")
                return parsed

    raise ValueError("reply contained an unterminated JSON object")


def split_envelope_extras(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate payload fields from the envelope fields an agent may set.

    Lifted out before validation because `requests` and `assumptions` live on
    the envelope, not the payload, and payloads forbid extra keys.
    """

    working = dict(data)

    # A model that wraps its answer in {"payload": {...}} is obeying the
    # shape it was shown; unwrap rather than fail it.
    inner = working.get("payload")
    if isinstance(inner, dict):
        extras_source = working
        working = dict(inner)
    else:
        extras_source = working

    extras: dict[str, Any] = {}
    for key in ENVELOPE_EXTRA_KEYS:
        for source in (working, extras_source):
            if key in source:
                value = source.pop(key)
                if value:
                    extras[key] = value
                break

    for key in _STAMPED_KEYS:
        working.pop(key, None)

    return working, extras


def build_envelope(
    role: AgentRole,
    payload: Any,
    *,
    run_id: str,
    step_id: str,
    iteration: int,
    input_state_hash: str | None,
    telemetry: Telemetry | None = None,
    extras: Mapping[str, Any] | None = None,
    status: OutputStatus = OutputStatus.OK,
) -> EnvelopeBase:
    """Wrap a validated payload in its envelope."""

    envelope_class = ENVELOPE_CLASSES.get(role)
    if envelope_class is None:
        raise AgentFailure(f"{role.value} has no envelope class", role=role)

    fields: dict[str, Any] = {
        "run_id": run_id,
        "step_id": step_id,
        "iteration": iteration,
        "input_state_hash": input_state_hash,
        "status": status,
        "payload": payload,
    }
    if telemetry is not None:
        fields["telemetry"] = telemetry
    for key in ENVELOPE_EXTRA_KEYS:
        value = (extras or {}).get(key)
        if value:
            fields[key] = value

    try:
        return envelope_class(**fields)
    except ValidationError as exc:
        raise AgentFailure(
            f"{role.value} envelope failed validation: {exc}", role=role
        ) from exc


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


class AgentRunner:
    """Calls agents. Owns retries, fallback, and telemetry."""

    def __init__(
        self,
        config: ConfigStore,
        bus: EventBus,
        budgeter: Budgeter,
        prompts: "PromptLibrary",
        *,
        max_attempts: int = 2,
    ) -> None:
        self._config = config
        self._bus = bus
        self._budgeter = budgeter
        self._prompts = prompts
        self._max_attempts = max(1, max_attempts)
        self._providers: dict[tuple[str, str], BaseProvider] = {}

    # -- configuration ----------------------------------------------------

    def reload(self) -> None:
        """Drop cached providers after `!reload` swapped the bindings."""

        self._providers.clear()
        self._prompts.invalidate()

    def provider(self, config: ProviderConfig) -> BaseProvider:
        """Cached adapter for one provider entry.

        Keyed by name and kind so a `!reload` that changes a provider's kind
        cannot be served a stale adapter.
        """

        key = (config.name, config.kind)
        cached = self._providers.get(key)
        if cached is None:
            cached = build_provider(config)
            self._providers[key] = cached
        return cached

    def chain(self, role: AgentRole) -> tuple[ProviderConfig, ...]:
        """Primary provider then declared fallbacks."""

        return self._config.current.provider_chain(role)

    def binding(self, role: AgentRole) -> RoleConfig:
        return self._config.current.role(role)

    # -- the call ---------------------------------------------------------

    async def invoke(
        self,
        *,
        role: AgentRole,
        projection: Mapping[str, Any],
        run_id: str,
        step_id: str,
        iteration: int,
        input_state_hash: str | None = None,
        rules: Sequence[Rule] = (),
    ) -> AgentRun:
        """Run one agent to a validated result, or raise `AgentFailure`."""

        binding = self.binding(role)

        trim = self._budgeter.trim(dict(projection))
        if trim.dropped:
            self._bus.agent(
                code=CODE_CTX_TRIM,
                label="context trimmed",
                actor=role.value,
                status=trim.action.value,
                iteration=iteration,
                tokens=trim.tokens_after,
                detail={
                    "tokens_before": trim.tokens_before,
                    "tokens_after": trim.tokens_after,
                    "dropped": list(trim.dropped),
                },
            )
        context_action = self._budgeter.record(trim, role=role, iteration=iteration)

        attempts = 0
        repair: str | None = None
        last_error: str | None = None
        chain = self.chain(role)

        for provider_index, provider_config in enumerate(chain):
            adapter = self.provider(provider_config)
            fallback_used = provider_index > 0

            if fallback_used:
                self._bus.agent(
                    code=CODE_FALLBACK,
                    label=f"falling back to {provider_config.name}",
                    actor=role.value,
                    status="fallback",
                    iteration=iteration,
                    detail={"provider": provider_config.name, "after": last_error},
                )

            for attempt in range(1, self._max_attempts + 1):
                attempts += 1
                rendered = self._prompts.render(
                    role=role,
                    projection=trim.projection,
                    rules=rules,
                    repair=repair,
                )

                self._bus.agent(
                    code=CODE_CALL,
                    label=f"calling {role.value}",
                    actor=role.value,
                    status="running",
                    iteration=iteration,
                    detail={
                        "provider": provider_config.name,
                        "attempt": attempt,
                        "structured": rendered.json_schema is not None
                        and binding.structured,
                        "step_id": step_id,
                    },
                )

                try:
                    result = await self._call(adapter, rendered, binding, role=role)
                except ProviderError as exc:
                    last_error = str(exc)
                    self._bus.agent(
                        code=CODE_ERROR,
                        label=f"{provider_config.name} error",
                        actor=role.value,
                        status="error",
                        iteration=iteration,
                        detail={
                            "provider": provider_config.name,
                            "attempt": attempt,
                            "status": exc.status,
                            "retryable": exc.retryable,
                            "error": str(exc),
                        },
                    )
                    if exc.retryable and attempt < self._max_attempts:
                        self._bus.agent(
                            code=CODE_RETRY,
                            label="retrying",
                            actor=role.value,
                            status="retry",
                            iteration=iteration,
                            detail={"reason": "provider error"},
                        )
                        continue
                    break  # move to the next provider

                telemetry = self._telemetry(
                    result,
                    binding,
                    attempt=attempt,
                    fallback_used=fallback_used,
                )

                # The code writer returns prose with <file> blocks; there is
                # nothing to validate here and everything to lose by trying.
                if rendered.json_schema is None:
                    if not result.text.strip():
                        last_error = "empty reply"
                    else:
                        self._bus.agent(
                            code=CODE_OK,
                            label=f"{role.value} replied",
                            actor=role.value,
                            status="ok",
                            iteration=iteration,
                            tokens=telemetry.total_tokens,
                            latency_ms=telemetry.latency_ms,
                            detail={
                                "provider": telemetry.provider,
                                "bytes": len(result.text),
                                "format": "text",
                            },
                        )
                        return AgentRun(
                            role=role,
                            raw=result.text,
                            telemetry=telemetry,
                            attempts=attempts,
                            envelope=None,
                            trim=trim,
                            context_action=context_action,
                        )
                else:
                    try:
                        payload, extras = self._validate(role, result.text)
                    except ValueError as exc:
                        last_error = str(exc)
                        self._bus.agent(
                            code=CODE_INVALID,
                            label=f"{role.value} reply rejected",
                            actor=role.value,
                            status="invalid",
                            iteration=iteration,
                            detail={
                                "provider": provider_config.name,
                                "attempt": attempt,
                                "error": str(exc)[:400],
                            },
                        )
                        repair = _REPAIR_TEMPLATE.format(reason=str(exc)[:400])
                        if attempt < self._max_attempts:
                            self._bus.agent(
                                code=CODE_RETRY,
                                label="retrying with repair instruction",
                                actor=role.value,
                                status="retry",
                                iteration=iteration,
                                detail={"reason": "schema violation"},
                            )
                            continue
                        break  # move to the next provider

                    envelope = build_envelope(
                        role,
                        payload,
                        run_id=run_id,
                        step_id=step_id,
                        iteration=iteration,
                        input_state_hash=input_state_hash,
                        telemetry=telemetry,
                        extras=extras,
                    )

                    self._bus.agent(
                        code=CODE_OK,
                        label=f"{role.value} replied",
                        actor=role.value,
                        status="ok",
                        iteration=iteration,
                        tokens=telemetry.total_tokens,
                        latency_ms=telemetry.latency_ms,
                        detail={
                            "provider": telemetry.provider,
                            "cost_usd": telemetry.cost_usd,
                            "format": "json",
                        },
                    )
                    return AgentRun(
                        role=role,
                        raw=result.text,
                        telemetry=telemetry,
                        attempts=attempts,
                        envelope=envelope,
                        trim=trim,
                        context_action=context_action,
                    )

        self._bus.agent(
            code=CODE_FAIL,
            label=f"{role.value} could not be reached",
            actor=role.value,
            status="failed",
            iteration=iteration,
            detail={"attempts": attempts, "error": last_error},
        )
        raise AgentFailure(
            f"{role.value} failed after {attempts} attempt(s): {last_error}",
            role=role,
            attempts=attempts,
            last_error=last_error,
        )

    async def _call(
        self,
        adapter: BaseProvider,
        rendered: "RenderedPrompt",
        binding: RoleConfig,
        *,
        role: AgentRole,
    ) -> ProviderResult:
        """One provider round trip.

        The schema is passed only when the role is bound to structured
        output; a provider that cannot honour it still receives the contract
        in the prompt text.
        """

        schema = rendered.json_schema if binding.structured else None
        started = time.monotonic()
        result = await adapter.complete(
            role=role,
            system=rendered.system,
            user=rendered.user,
            model=binding.model,
            temperature=binding.temperature,
            max_tokens=binding.max_tokens,
            json_schema=schema,
        )
        if result.latency_ms <= 0:
            elapsed = int((time.monotonic() - started) * 1000)
            result = ProviderResult(
                text=result.text,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                provider=result.provider,
                model=result.model,
                latency_ms=max(0, elapsed),
            )
        return result

    def _validate(
        self, role: AgentRole, text: str
    ) -> tuple[Any, dict[str, Any]]:
        """Parse and schema-check a payload reply.

        Raises `ValueError` with a message specific enough to be useful as a
        repair instruction on the retry.
        """

        from .schemas import PAYLOAD_SCHEMAS  # local: keeps import cost off startup

        data = extract_json_object(text)
        payload_data, extras = split_envelope_extras(data)

        adapter = PAYLOAD_SCHEMAS.get(role)
        if adapter is None:
            raise ValueError(f"{role.value} has no payload schema")

        try:
            payload = adapter.validate_python(payload_data)
        except ValidationError as exc:
            errors = exc.errors()[:4]
            detail = "; ".join(
                f"{'.'.join(str(part) for part in item.get('loc', ()))}: "
                f"{item.get('msg', 'invalid')}"
                for item in errors
            )
            raise ValueError(detail or str(exc)) from exc

        return payload, extras

    def _telemetry(
        self,
        result: ProviderResult,
        binding: RoleConfig,
        *,
        attempt: int,
        fallback_used: bool,
    ) -> Telemetry:
        """Cost is computed from the binding, so swapping a model reprices it."""

        return Telemetry(
            provider=result.provider,
            model=result.model,
            prompt_tokens=max(0, result.prompt_tokens),
            completion_tokens=max(0, result.completion_tokens),
            latency_ms=max(0, result.latency_ms),
            cost_usd=binding.cost_for(result.prompt_tokens, result.completion_tokens),
            attempt=attempt,
            fallback_used=fallback_used,
        )
