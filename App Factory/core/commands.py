"""The command router: the seam between Window 1 and the pipeline.

Three rules shape this module.

1. Parsing is deterministic and happens before anything else, so a malformed
   line costs nothing - no model call, no run, no state write. `parse` has
   already decided; the router only reports the verdict.

2. Window 1 has exactly one writer per command. For `#` verbs that writer is
   the pipeline, which prints `ready for review`, `shipped b-NNN`, or
   `needs human: ...` itself. The router therefore traces exec results to
   Window 3 and stays silent on Window 1 - otherwise every build would print
   twice. The router speaks on Window 1 only when the pipeline never ran:
   parse rejections, `!` replies, and its own refusals.

3. `!` commands never reach an LLM. They are answered from state, the store,
   and the registry by `state_commands.handle`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .config import ConfigStore
from .events import EventBus
from .parser import ExecCommand, ExecVerb, Rejection, StateCommand, StateVerb, parse
from .pipeline import BuildOutcome, Pipeline, PipelineBusy, PipelineError
from .schemas.common import PipelineStatus, Stack, Stage
from .schemas.envelope import CliResponse
from .schemas.registry import ProjectRecord
from .schemas.state import ProjectState
from .state_commands import handle as handle_state_command
from .store import StoreError
from .workspace import Workspace, WorkspaceError

__all__ = ["CommandOutcome", "CommandRouter"]

#: A run in one of these states is stalled: the operator has to intervene.
#: These count as failures for reporting, but they are still legitimate
#: targets for `#revise` - that is how you unstick a halted build.
_STALLED = frozenset({PipelineStatus.NEEDS_HUMAN, PipelineStatus.BLOCKED})

#: States in which a revision makes sense at all.
_REVISABLE = frozenset({PipelineStatus.AWAITING_REVIEW}) | _STALLED

#: `!project` subcommands that change which project is active. After one of
#: these succeeds, the in-memory pipeline points at the wrong project.
_SWITCHING = frozenset({"new", "use"})

#: The only two `!` verbs that write. Everything else on the state side
#: is a read, so only these have to respect the single writer.
_META_SETTERS = frozenset({StateVerb.STACK, StateVerb.TYPE})


def _plain(value: Any) -> Any:
    """Unwrap an enum for logging without caring whether it is one."""

    return getattr(value, "value", value)


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """What one submitted line did.

    `cli` is the exact text that reached Window 1, so callers and tests can
    assert against the operator's view rather than against internal enums.
    """

    cli: str
    label: str
    ok: bool = True
    detail: dict[str, Any] | None = None
    build: str | None = None
    reason_code: Any = None


class CommandRouter:
    """Turns one line of text into at most one pipeline action."""

    def __init__(
        self,
        *,
        pipeline: Pipeline,
        workspace: Workspace,
        config_store: ConfigStore,
        bus: EventBus,
    ) -> None:
        self._pipeline = pipeline
        self._workspace = workspace
        self._config = config_store
        self._bus = bus

    # -- entry point ------------------------------------------------------

    async def submit(self, line: str) -> CommandOutcome:
        """Parse a line and run it. The only public way in."""

        command = parse(line)
        if isinstance(command, Rejection):
            return self._refuse_parse(command)
        if isinstance(command, StateCommand):
            return self._run_state(command)
        return await self._run_exec(command)

    # -- refusals ---------------------------------------------------------

    def _refuse_parse(self, rejection: Rejection) -> CommandOutcome:
        """A line that never became a command. Costs one printed line."""

        detail = {
            "code": _plain(rejection.code),
            "detail": rejection.detail,
            "raw": rejection.raw,
        }
        text = rejection.response.value
        self._bus.trace("reject", detail)
        self._bus.cli(text)
        return CommandOutcome(
            cli=text,
            label="reject",
            ok=False,
            detail=detail,
            reason_code=rejection.code,
        )

    def _refuse(
        self,
        label: str,
        response: CliResponse,
        detail: dict[str, Any],
        *,
        text: str | None = None,
    ) -> CommandOutcome:
        """A well-formed command the router declines to run.

        The pipeline never started, so here the router is the one writer for
        Window 1.
        """

        line = text if text is not None else response.value
        self._bus.trace(label, dict(detail, refused=True, cli=line))
        self._bus.cli(line)
        return CommandOutcome(cli=line, label=label, ok=False, detail=detail)

    # -- state commands ---------------------------------------------------

    def _run_state(self, command: StateCommand) -> CommandOutcome:
        """Answer a `!` command from state on disk. No model is involved."""

        if command.verb in _META_SETTERS and command.args and self._pipeline.busy:
            # Redeclaring the stack mid-build would swap the artifact
            # allowlist under a running writer.
            return self._refuse(
                command.verb.value, CliResponse.PIPELINE_BUSY, {"raw": command.raw}
            )

        reply = handle_state_command(
            command,
            state=self._pipeline.state,
            workspace=self._workspace,
            config_store=self._config,
            bus=self._bus,
            store=self._pipeline.store,
            run_id=self._pipeline.run_id,
        )

        # A successful project switch changes what "the pipeline" means, so
        # re-point it before another command can arrive.
        if command.verb is StateVerb.PROJECT and reply.ok:
            self._follow_project_switch(command)

        # A declared stack/type change has to reach live state too, or the
        # current run keeps building against the old profile.
        if reply.meta_update:
            self._pipeline.set_meta(**reply.meta_update)

        detail = (
            reply.detail
            if isinstance(reply.detail, dict)
            else {"detail": reply.detail}
        )
        self._bus.trace(
            reply.label, {"raw": command.raw, "ok": reply.ok, "detail": reply.detail}
        )
        self._bus.cli(reply.cli)
        return CommandOutcome(
            cli=reply.cli, label=reply.label, ok=reply.ok, detail=detail
        )

    def _follow_project_switch(self, command: StateCommand) -> None:
        """Point the pipeline at whatever project just became active."""

        subcommand = command.subcommand
        if subcommand not in _SWITCHING or len(command.args) < 2:
            return
        slug = command.args[1]
        if subcommand == "use":
            # Reload that project's head snapshot, if it has one.
            self._pipeline.attach(slug)
        else:
            # A brand new project has no run yet. Drop the old state so the
            # next `#route` starts cold instead of editing another project.
            self._pipeline.detach()

    # -- execution commands -----------------------------------------------

    async def _run_exec(self, command: ExecCommand) -> CommandOutcome:
        if self._pipeline.busy:
            # The pipeline raises PipelineBusy as well, but checking first
            # stops the pre-flight guards below from refusing a live run with
            # the wrong reason.
            return self._refuse(
                command.verb.value, CliResponse.PIPELINE_BUSY, {"raw": command.raw}
            )
        if command.verb is ExecVerb.SHIP:
            return await self._ship(command)
        if command.verb is ExecVerb.REVISE:
            return await self._revise(command)
        return await self._route(command)

    async def _route(self, command: ExecCommand) -> CommandOutcome:
        """`#route`. A broadcast or a cold pipeline means a new run; a named
        target on a live run means re-entry at that agent's stage."""

        state = self._pipeline.state
        cold = state is None or self._sealed(state)
        spec = command.spec

        if command.broadcast or cold:
            role = spec.role if spec is not None else None
            record = self._active_record()
            # A new run inherits the active project's declared stack, which is
            # what keeps a Go project from being handed a web spec.
            options: dict[str, Any] = {}
            if record is not None:
                options = {
                    "project_type": record.project_type,
                    "stack": record.stack,
                }
            return await self._drive(
                "route",
                command,
                lambda: self._pipeline.start(
                    command.instruction, target=role, **options
                ),
            )

        if spec is None:
            # Defensive: the parser only permits a target-less #route via ALL.
            return self._refuse(
                "route", CliResponse.INVALID_COMMAND, {"raw": command.raw}
            )

        return await self._drive(
            "route",
            command,
            lambda: self._pipeline.resume(
                entry_stage=Stage(spec.reentry_stage),
                target=spec.role,
                instruction=command.instruction,
            ),
        )

    async def _revise(self, command: ExecCommand) -> CommandOutcome:
        """`#revise`. Re-enter one agent against the build under review."""

        state = self._pipeline.state
        if state is None:
            return self._refuse("revise", CliResponse.NO_STATE, {"raw": command.raw})
        if self._sealed(state):
            return self._refuse(
                "revise",
                CliResponse.NO_BUILD_AWAITING_REVIEW,
                {
                    "raw": command.raw,
                    "reason": "this run is sealed; route a new build instead",
                },
            )
        if state.pipeline.status not in _REVISABLE:
            return self._refuse(
                "revise",
                CliResponse.NO_BUILD_AWAITING_REVIEW,
                {"raw": command.raw, "status": _plain(state.pipeline.status)},
            )

        spec = command.spec
        if spec is None:
            # The parser rejects `#revise ALL`, so this cannot normally happen.
            return self._refuse(
                "revise", CliResponse.INVALID_COMMAND, {"raw": command.raw}
            )

        return await self._drive(
            "revise",
            command,
            lambda: self._pipeline.resume(
                entry_stage=Stage(spec.reentry_stage),
                target=spec.role,
                instruction=command.instruction,
            ),
        )

    async def _ship(self, command: ExecCommand) -> CommandOutcome:
        """`#ship`. Seal the build the operator just reviewed."""

        state = self._pipeline.state
        if state is None:
            return self._refuse("ship", CliResponse.NO_STATE, {"raw": command.raw})
        if (
            self._sealed(state)
            or state.pipeline.status is not PipelineStatus.AWAITING_REVIEW
        ):
            return self._refuse(
                "ship",
                CliResponse.NO_BUILD_AWAITING_REVIEW,
                {"raw": command.raw, "status": _plain(state.pipeline.status)},
            )
        return await self._drive("ship", command, self._pipeline.ship)

    # -- pipeline plumbing -------------------------------------------------

    async def _drive(
        self,
        label: str,
        command: ExecCommand,
        call: Callable[[], Awaitable[BuildOutcome]],
    ) -> CommandOutcome:
        """Run one pipeline call and translate its failures into CLI lines."""

        try:
            outcome = await call()
        except PipelineBusy:
            return self._refuse(label, CliResponse.PIPELINE_BUSY, {"raw": command.raw})
        except PipelineError as exc:
            # The pipeline's own message is more specific than a generic
            # refusal, so it is what the operator sees.
            return self._refuse(
                label,
                CliResponse.NEEDS_HUMAN,
                {"raw": command.raw, "error": str(exc)},
                text=str(exc),
            )
        except WorkspaceError as exc:
            return self._refuse(
                label,
                CliResponse.NEEDS_HUMAN,
                {"raw": command.raw, "error": str(exc)},
            )
        return self._report(label, outcome)

    def _report(self, label: str, outcome: BuildOutcome) -> CommandOutcome:
        """Trace a completed run. Window 1 was already written by the pipeline."""

        detail = {
            "run_id": outcome.run_id,
            "project": outcome.slug,
            "status": _plain(outcome.status),
            "stage": _plain(outcome.stage),
            "iteration": outcome.iteration,
            "snapshot": outcome.snapshot,
            "build": outcome.build,
            "entrypoint": outcome.entrypoint,
            "preview": outcome.preview,
            "decision": _plain(outcome.decision),
            "reason": outcome.reason,
            "open_blockers": outcome.open_blockers,
        }
        self._bus.trace(label, detail)
        status = PipelineStatus(_plain(outcome.status))
        return CommandOutcome(
            cli=outcome.cli,
            label=label,
            ok=status not in _STALLED,
            detail=detail,
            build=outcome.build,
        )

    def _sealed(self, state: ProjectState) -> bool:
        """True when this run's head snapshot has already been shipped.

        In-session `#ship` leaves the status SHIPPED, but shipping writes no
        new snapshot - the seal is a SEALED.json marker on disk. So a project
        reopened with `!project use` loads a state that still reads
        `awaiting_review`, and only the store knows the truth. Without this,
        `#revise` would be refused on a sealed run during the session and
        quietly allowed after a reattach.
        """

        if state.pipeline.status is PipelineStatus.SHIPPED:
            return True
        store = self._pipeline.store
        head = state.pipeline.head_snapshot
        if store is None or not head:
            return False
        try:
            return store.is_sealed(state.pipeline.run_id, head)
        except StoreError:
            return False

    def _active_record(self) -> ProjectRecord | None:
        """The active project, or None when the workspace is still empty.

        `Workspace.active()` raises rather than returning None, and having no
        active project is the normal state on the very first `#route` - not an
        error worth refusing the command over.
        """

        try:
            return self._workspace.active()
        except WorkspaceError:
            return None

    # -- read-only surface for the server ---------------------------------

    def status_payload(self) -> dict[str, Any]:
        return self._pipeline.status_payload()

    @property
    def active_stack(self) -> Stack | None:
        """The stack in force: the live run's, else the active project's."""

        state = self._pipeline.state
        if state is not None:
            return state.meta.stack
        record = self._active_record()
        return record.stack if record is not None else None
