"""S0 to S9 orchestration for one App Factory build.

The pipeline is the only component that moves a run forward. It owns the
stage machine, it is the single caller of the agent runner, and it is the
place where every governor rule is actually applied to a live build.

Three invariants shape this file:

* **One snapshot per iteration.** `SnapshotStore` is write-once, so the
  pipeline commits exactly once per iteration, at the end of the cycle. A
  `#revise` starts a new iteration rather than rewriting a sealed history.
* **The pipeline charges the budget, not the runner.** `AgentRunner`
  deliberately does not enforce ceilings; it cannot know whether a call is
  part of a build. Every invocation here is preceded by
  `assert_within_budget` (G2) and followed by `charge`.
* **No agent writes outside its region.** Every state write goes through
  `StateManager`, which refuses cross-region writes (G8). Regions that no
  agent owns are written with `apply_system`.

This module imports nothing from `agents`. The prompt library reaches it
only as an already-constructed `AgentRunner`, which is what keeps `core`
independent of prompt content.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .artifacts import (
    ArtifactError,
    choose_entrypoint,
    extract_files,
    extract_notes,
    extract_plan,
    write_build,
)
from .budgeter import BudgetExhausted, Budgeter
from .config import ConfigStore
from .events import EventBus
from .governor import (
    GateResult,
    blocking_issues,
    evaluate_gate,
    fingerprints,
    merge_issues,
    partition_debt,
    promote_rules,
    repeat_offenders,
    resolved_since,
    sort_by_precedence,
)
from .llm import AgentFailure, AgentRun, AgentRunner
from .projection import (
    ProjectionError,
    adjudication_projection,
    assert_critic_isolation,
    design_projection,
    implementer_projection,
    observer_spec_projection,
    optimizer_projection,
    prompt_engineer_projection,
    qc_projection,
    strip_cross_critic_issues,
)
from .schemas import utcnow
from .schemas.common import (
    AgentRole,
    GateDecision,
    Issue,
    PipelineStatus,
    ProjectType,
    Stack,
    Stage,
    profile_for,
)
from .schemas.envelope import CliResponse
from .schemas.registry import ProjectStatus
from .schemas.state import (
    Architecture,
    ArtifactFile,
    Artifacts,
    Component,
    ContextActionRecord,
    CriticResult,
    Dependency,
    Deviation,
    FilePlan,
    Memory,
    ProjectState,
)
from .state_manager import StateError, StateManager
from .store import SnapshotStore, build_id, new_run_id, snapshot_id
from .workspace import ProjectHandle, Workspace, WorkspaceError

__all__ = [
    "BuildOutcome",
    "Pipeline",
    "PipelineBusy",
    "PipelineError",
    "preview_url",
]

#: How many context-trim records the Observer keeps in memory.
MAX_MEMORY_ACTIONS = 50

#: How many iteration numbers `memory.recent_iterations` carries.
MAX_RECENT_ITERATIONS = 10


class PipelineError(RuntimeError):
    """Raised when a build cannot proceed for a structural reason."""


class PipelineBusy(PipelineError):
    """Raised when a command arrives while a build is already running."""


def preview_url(
    slug: str, run_id: str, snap_id: str, bld_id: str, entrypoint: str
) -> str:
    """The URL Window 2 loads for a build.

    The server mounts builds under this exact shape, so the contract lives
    here rather than being rebuilt by string concatenation in two places.
    """
    return f"/preview/{slug}/{run_id}/{snap_id}/{bld_id}/{entrypoint}"


def build_manifest(state: "ProjectState") -> list[dict[str, Any]]:
    """The artifact file tree Window 2 renders beside a source preview.

    Size and sha256 are already recorded per artifact, so the UI never has
    to stat the build directory or hash anything itself.
    """
    return [
        {
            "path": item.path,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
            "purpose": item.purpose,
        }
        for item in state.artifacts.files
    ]


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    """What a `#route`, `#revise`, or `#ship` produced."""

    run_id: str
    slug: str
    status: PipelineStatus
    stage: Stage
    iteration: int
    cli: str
    snapshot: str | None = None
    build: str | None = None
    entrypoint: str | None = None
    preview: str | None = None
    decision: GateDecision | None = None
    reason: str | None = None
    open_blockers: int = 0


class Pipeline:
    """Drives one project's build through the stage machine."""

    def __init__(
        self,
        *,
        config: ConfigStore,
        workspace: Workspace,
        bus: EventBus,
        runner: AgentRunner,
        budgeter: Budgeter,
    ) -> None:
        self._config = config
        self._workspace = workspace
        self._bus = bus
        self._runner = runner
        self._budgeter = budgeter

        self._lock = asyncio.Lock()
        self._busy = False

        self._handle: ProjectHandle | None = None
        self._manager: StateManager | None = None
        self._state: ProjectState | None = None
        self._build_dir: Path | None = None
        self._operator: dict[str, Any] | None = None
        self._pending_context: list[ContextActionRecord] = []
        self._merged: list[Issue] = []
        self._last_gate: GateResult | None = None

    # -- inspection --------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def state(self) -> ProjectState | None:
        return self._state

    @property
    def manager(self) -> StateManager | None:
        return self._manager

    @property
    def store(self) -> SnapshotStore | None:
        return self._handle.store if self._handle is not None else None

    @property
    def run_id(self) -> str | None:
        return self._state.pipeline.run_id if self._state is not None else None

    @property
    def slug(self) -> str | None:
        return self._handle.slug if self._handle is not None else None

    @property
    def build_dir(self) -> Path | None:
        return self._build_dir

    def status_payload(self) -> dict[str, Any]:
        """The header payload Window 1 and the UI chrome render."""

        if self._state is None:
            return {
                "slug": self._workspace.active_slug(),
                "status": PipelineStatus.IDLE.value,
                "stage": None,
                "iteration": 0,
                "run_id": None,
                "busy": self._busy,
                "open_blockers": 0,
                "tokens_used": 0,
                "max_tokens": self._config.current.governor.budgets.max_tokens_per_build,
                "build": None,
            }

        state = self._state
        return {
            "slug": self.slug,
            "status": state.pipeline.status.value,
            "stage": state.pipeline.current_stage.value,
            "iteration": state.pipeline.iteration,
            "run_id": state.pipeline.run_id,
            "busy": self._busy,
            "open_blockers": len(state.review.open_blockers),
            "tokens_used": state.budgets.tokens_used,
            "max_tokens": state.budgets.max_tokens,
            "build": state.artifacts.build_id,
        }

    # -- entry points ------------------------------------------------------

    async def start(
        self,
        raw_input: str,
        *,
        target: AgentRole | None = None,
        project_type: ProjectType = ProjectType.APP,
        stack: Stack = Stack.WEB,
    ) -> BuildOutcome:
        """Begin a new run from intake. This is `#route` on a cold pipeline."""

        if self._busy:
            raise PipelineBusy(CliResponse.PIPELINE_BUSY.value)

        async with self._lock:
            self._busy = True
            try:
                self._intake(raw_input, project_type=project_type, stack=stack)
                self._operator = {
                    "raw_input": raw_input,
                    "target": target.value if target is not None else "ALL",
                    "instruction": raw_input,
                }
                return await self._guarded(Stage.OPTIMIZE)
            finally:
                self._busy = False
                self._bus.status(self.status_payload())

    async def resume(
        self, *, entry_stage: Stage, target: AgentRole, instruction: str
    ) -> BuildOutcome:
        """Re-enter a live run at an agent's stage. `#route` and `#revise`.

        The iteration advances first. Snapshots are write-once, so a second
        pass has to be a new iteration rather than an overwrite of the one
        the operator has already seen.
        """

        if self._busy:
            raise PipelineBusy(CliResponse.PIPELINE_BUSY.value)
        if self._state is None or self._manager is None:
            raise PipelineError(CliResponse.NO_STATE.value)

        async with self._lock:
            self._busy = True
            try:
                self._operator = {
                    "target": target.value,
                    "instruction": instruction,
                    "reentry_stage": entry_stage.value,
                }
                self._state = self._manager.bump_iteration(self._state)
                self._bus.stage(
                    code="REENTRY",
                    label=entry_stage.value,
                    iteration=self._state.pipeline.iteration,
                    text=f"{target.value}: {instruction}",
                )
                self._state = self._manager.set_status(
                    self._state, PipelineStatus.RUNNING
                )
                return await self._guarded(entry_stage)
            finally:
                self._busy = False
                self._bus.status(self.status_payload())

    async def ship(self) -> BuildOutcome:
        """Seal the reviewed build. Irreversible by design.

        Sealing freezes the head snapshot and records a hash of every
        artifact. The state file inside that snapshot is not rewritten,
        because a sealed snapshot is exactly what the operator reviewed; the
        `SEALED.json` marker and the project registry carry the shipped fact.
        """

        if self._busy:
            raise PipelineBusy(CliResponse.PIPELINE_BUSY.value)
        if self._state is None or self._manager is None:
            raise PipelineError(CliResponse.NO_STATE.value)

        state = self._state
        if state.pipeline.status is not PipelineStatus.AWAITING_REVIEW:
            raise PipelineError(CliResponse.NO_BUILD_AWAITING_REVIEW.value)

        bld = state.artifacts.build_id
        if bld is None:
            raise PipelineError(CliResponse.NO_BUILD_AWAITING_REVIEW.value)

        async with self._lock:
            self._busy = True
            try:
                receipt = self._manager.seal(state, bld_id=bld)
                artifacts = state.artifacts.model_copy(
                    update={"sealed": True, "sealed_at": utcnow()}
                )
                state = self._manager.apply_system(state, {"artifacts": artifacts})
                state = self._manager.transition(
                    state, Stage.SHIPPED, actor=AgentRole.COMMANDER, note="shipped"
                )
                self._state = self._manager.set_status(state, PipelineStatus.SHIPPED)

                self._workspace.record_progress(
                    self.slug or "",
                    status=ProjectStatus.SHIPPED,
                    shipped_build_id=bld,
                    snapshot_id=self._state.pipeline.head_snapshot,
                    run_id=self._state.pipeline.run_id,
                    open_blocker_count=len(self._state.review.open_blockers),
                )
                self._bus.governor(
                    code="SHIP",
                    label="sealed",
                    text=f"{bld} sealed with {len(self._state.artifacts.files)} files",
                    iteration=self._state.pipeline.iteration,
                )
                self._bus.trace("seal", dict(receipt))

                # Promote Window 2 to its Shipped channel. The sealed build
                # keeps its own URL, so switching back to the preview channel
                # still shows exactly what was reviewed.
                shipped_entry = self._state.artifacts.entrypoint
                shipped_snap = self._state.pipeline.head_snapshot
                if shipped_entry is not None and shipped_snap is not None:
                    shipped_mode = self._state.artifacts.preview_mode
                    self._bus.preview(
                        url=preview_url(
                            self.slug or "",
                            self._state.pipeline.run_id,
                            shipped_snap,
                            bld,
                            shipped_entry,
                        ),
                        build_id=bld,
                        channel=(
                            shipped_mode.value if shipped_mode is not None else "source"
                        ),
                        entrypoint=shipped_entry,
                        manifest=build_manifest(self._state),
                        surface="shipped",
                    )
                cli = f"{CliResponse.SHIPPED.value} {bld}"
                self._bus.cli(cli)
                return self._outcome(cli=cli, decision=GateDecision.PASS)
            finally:
                self._busy = False
                self._bus.status(self.status_payload())

    def set_meta(
        self,
        *,
        stack: Stack | None = None,
        project_type: ProjectType | None = None,
    ) -> ProjectState | None:
        """Apply a declared stack or project-type change to live state.

        `meta` is state-manager owned, so this goes through the manager
        instead of mutating the model in place. With no live run there is
        nothing to update: the registry already holds the new value and the
        next run reads it from there.
        """

        state = self._state
        if state is None:
            return None
        updates: dict[str, Any] = {}
        if project_type is not None:
            updates["project_type"] = project_type
        if stack is not None and state.spec.target_stack is stack:
            # ProjectState refuses a meta.stack that contradicts
            # spec.target_stack, and `spec` belongs to the Observer, so the
            # system writer cannot retarget it. A declaration that differs
            # from the pinned spec stays in the registry instead and is
            # adopted when the next run derives its spec from it.
            updates["stack"] = stack
        if not updates:
            return state
        meta = state.meta.model_copy(update=updates)
        self._state = self._manager.apply_system(state, {"meta": meta})
        return self._state

    def attach(self, slug: str) -> ProjectState | None:
        """Point the pipeline at another project's most recent run.

        Used by `!project use`. Returns the resumed state, or None when that
        project has never been built.
        """

        if self._busy:
            raise PipelineBusy(CliResponse.PIPELINE_BUSY.value)

        handle = self._workspace.handle(slug)
        run_id = handle.record.head_run_id
        self._handle = handle
        self._build_dir = None
        self._operator = None
        self._pending_context = []
        self._merged = []
        self._last_gate = None

        if run_id is None:
            self._manager = None
            self._state = None
            return None

        manager = StateManager(handle.store, run_id)
        state = manager.load_head()
        self._manager = manager
        self._state = manager.adopt(state) if state is not None else None
        if self._state is not None:
            self._bus.bind_log(handle.store.events_path(run_id))
            self._build_dir = self._resolve_build_dir(self._state)
        return self._state

    def detach(self) -> None:
        """Forget the active run without touching anything on disk."""

        self._handle = None
        self._manager = None
        self._state = None
        self._build_dir = None
        self._operator = None
        self._pending_context = []
        self._merged = []
        self._last_gate = None

    # -- stage machine -----------------------------------------------------

    async def _guarded(self, entry: Stage) -> BuildOutcome:
        """Run the machine, converting every expected failure into a halt.

        A build that stops must always leave the operator with a state they
        can inspect, which is why nothing below is allowed to escape as a
        bare exception.
        """

        try:
            return await self._drive(entry)
        except BudgetExhausted as exc:
            return self._halt(str(exc), decision=GateDecision.ESCALATE)
        except AgentFailure as exc:
            return self._halt(f"{exc.role.value} failed: {exc}")
        except (ArtifactError, ProjectionError, StateError, WorkspaceError) as exc:
            return self._halt(f"{type(exc).__name__}: {exc}")

    async def _drive(self, entry: Stage) -> BuildOutcome:
        stage = entry
        while True:
            if stage is Stage.OPTIMIZE:
                await self._s1_optimize()
                stage = Stage.SPEC
            elif stage is Stage.SPEC:
                await self._s2_spec()
                stage = Stage.BUILD
            elif stage is Stage.BUILD:
                await self._s3_build()
                stage = Stage.REVIEW
            elif stage is Stage.REVIEW:
                await self._s4_review()
                stage = Stage.ADJUDICATE
            elif stage is Stage.ADJUDICATE:
                await self._s5_adjudicate()
                stage = Stage.GATE
            elif stage is Stage.GATE:
                gate = self._s6_gate()
                if gate.decision is GateDecision.PASS:
                    return self._s8_awaiting(gate)
                if gate.decision is GateDecision.ESCALATE:
                    return self._escalate(gate)
                stage = Stage.RULE_WRITE
            elif stage is Stage.RULE_WRITE:
                await self._s7_rules()
                stage = Stage.BUILD
            else:
                raise PipelineError(f"cannot enter the machine at {stage.value}")

    # -- S0 ----------------------------------------------------------------

    def _intake(
        self, raw_input: str, *, project_type: ProjectType, stack: Stack
    ) -> None:
        record = self._workspace.ensure_active(
            project_type=project_type, stack=stack
        )
        handle = self._workspace.handle(record.slug)
        run_id = new_run_id()

        # StateManager.create() creates the run directory itself; calling
        # create_run() here as well would trip the write-once guard.
        manager = StateManager(handle.store, run_id)
        state = manager.create(
            project_id=f"{record.slug}:{run_id}",
            project_slug=record.slug,
            project_type=record.project_type,
            stack=record.stack,
            raw_input=raw_input,
            budgets=self._budgeter.initial(),
        )
        self._bus.bind_log(handle.store.events_path(run_id))
        self._budgeter.start()
        state = manager.transition(
            state, Stage.INTAKE, actor=AgentRole.COMMANDER, note="intake"
        )
        state = manager.set_status(state, PipelineStatus.RUNNING)

        self._handle = handle
        self._manager = manager
        self._state = state
        self._build_dir = None
        self._pending_context = []
        self._merged = []
        self._last_gate = None

        self._bus.stage(
            code="S0",
            label=Stage.INTAKE.value,
            iteration=0,
            text=f"{record.slug} [{record.project_type.value}/{record.stack.value}] {raw_input}",
        )
        self._bus.status(self.status_payload())

    # -- S1 ----------------------------------------------------------------

    async def _s1_optimize(self) -> None:
        state = self._enter(Stage.OPTIMIZE, AgentRole.OPTIMIZER)
        projection = optimizer_projection(
            state, limits=self._ctx, operator=self._operator
        )
        run = await self._invoke(
            role=AgentRole.OPTIMIZER, projection=projection, stage=Stage.OPTIMIZE
        )
        payload = self._payload(run)

        state = self._require_state()
        intent = state.intent.model_copy(
            update={
                "optimized_prompt": payload.optimized_prompt,
                "inferred_requirements": list(payload.inferred_requirements),
                "ambiguities": list(payload.ambiguities),
                "confidence": payload.confidence,
                "optimized_at": utcnow(),
            }
        )
        self._state = self._manager_ref().apply(
            state, role=AgentRole.OPTIMIZER, updates={"intent": intent}
        )
        self._bus.note(
            f"{len(intent.inferred_requirements)} inferred requirements, "
            f"{len(intent.ambiguities)} ambiguities",
            iteration=state.pipeline.iteration,
        )

    # -- S2 ----------------------------------------------------------------

    async def _s2_spec(self) -> None:
        state = self._enter(Stage.SPEC, AgentRole.OBSERVER)
        projection = observer_spec_projection(
            state, limits=self._ctx, operator=self._operator
        )
        run = await self._invoke(
            role=AgentRole.OBSERVER, projection=projection, stage=Stage.SPEC
        )
        payload = self._payload(run)

        state = self._require_state()
        spec = payload.spec.model_copy(
            update={
                "spec_version": state.spec.spec_version + 1,
                # The Observer does not get to retarget the build. The state
                # model refuses to hold two answers to "what are we
                # building", and meta.stack is the operator's declaration.
                "target_stack": state.meta.stack,
                "frozen_at": utcnow(),
            }
        )
        memory = self._memory(
            state, digest=payload.digest, pinned_spec_version=spec.spec_version
        )
        self._state = self._manager_ref().apply(
            state,
            role=AgentRole.OBSERVER,
            updates={"spec": spec, "memory": memory},
        )
        self._record_scope(payload)
        self._bus.note(
            f"spec v{spec.spec_version}: {len(spec.requirements)} requirements, "
            f"{len(spec.acceptance_criteria)} acceptance criteria",
            iteration=state.pipeline.iteration,
        )

    # -- S3 ----------------------------------------------------------------

    async def _s3_build(self) -> None:
        state = self._enter(Stage.BUILD, AgentRole.IMPLEMENTER)
        rules = [
            rule
            for rule in state.rules.active
            if rule.active and rule.scope is AgentRole.IMPLEMENTER
        ]
        projection = implementer_projection(
            state,
            limits=self._ctx,
            build_dir=self._build_dir,
            operator=self._operator,
        )
        run = await self._invoke(
            role=AgentRole.IMPLEMENTER,
            projection=projection,
            stage=Stage.BUILD,
            rules=rules,
        )

        state = self._require_state()
        stack = state.meta.stack
        plan = extract_plan(run.raw)
        notes = extract_notes(run.raw)
        files = extract_files(run.raw, stack=stack)

        iteration = state.pipeline.iteration
        snap = snapshot_id(iteration)
        bld = build_id(iteration)
        build_dir = self._store_ref().build_dir(state.pipeline.run_id, snap, bld)
        manifest = write_build(build_dir, files)
        entrypoint = choose_entrypoint(plan.get("entrypoint"), manifest, stack=stack)

        architecture = Architecture(
            plan_summary=self._plan_summary(plan, notes),
            components=self._components(plan),
            files=self._file_plans(plan, manifest),
            dependencies=self._dependencies(plan),
            deviations=self._deviations(plan),
            planned_at=utcnow(),
        )
        artifacts = Artifacts(
            build_id=bld,
            entrypoint=entrypoint,
            preview_mode=profile_for(stack).preview,
            files=manifest,
            sealed=False,
        )

        manager = self._manager_ref()
        state = manager.apply(
            state, role=AgentRole.IMPLEMENTER, updates={"architecture": architecture}
        )
        self._state = manager.apply_system(state, {"artifacts": artifacts})
        self._build_dir = build_dir

        if notes:
            self._bus.note(notes, iteration=iteration)
        self._bus.stage(
            code="BUILD",
            label=bld,
            iteration=iteration,
            text=f"{len(manifest)} files, entrypoint {entrypoint}",
        )

    # -- S4 ----------------------------------------------------------------

    async def _s4_review(self) -> None:
        state = self._enter(Stage.REVIEW, AgentRole.QC)
        build_dir = self._build_dir
        if build_dir is None:
            raise PipelineError("review requested before anything was built")

        qc_projection_data = self._critic_projection(state, AgentRole.QC, build_dir)
        design_projection_data = self._critic_projection(
            state, AgentRole.DESIGN, build_dir
        )

        # G2 is checked once before the pair: the two critics are one
        # logical step and are billed together below.
        self._budgeter.assert_within_budget(state.budgets)
        iteration = state.pipeline.iteration
        qc_run, design_run = await asyncio.gather(
            self._runner.invoke(
                role=AgentRole.QC,
                projection=qc_projection_data,
                run_id=state.pipeline.run_id,
                step_id=self._step(Stage.REVIEW, iteration),
                iteration=iteration,
                input_state_hash=state.meta.state_hash,
            ),
            self._runner.invoke(
                role=AgentRole.DESIGN,
                projection=design_projection_data,
                run_id=state.pipeline.run_id,
                step_id=self._step(Stage.REVIEW, iteration),
                iteration=iteration,
                input_state_hash=state.meta.state_hash,
            ),
        )

        # State writes are serialized even though the calls were not.
        for run in (qc_run, design_run):
            self._settle(run)

        manager = self._manager_ref()
        qc_payload = self._payload(qc_run)
        design_payload = self._payload(design_run)

        qc_result = CriticResult(
            agent=AgentRole.QC,
            iteration=iteration,
            issues=list(qc_payload.issues),
            summary=qc_payload.summary,
            passed=qc_payload.passed,
            spec_coverage=list(qc_payload.spec_coverage),
            reviewed_at=utcnow(),
        )
        design_result = CriticResult(
            agent=AgentRole.DESIGN,
            iteration=iteration,
            issues=list(design_payload.issues),
            summary=design_payload.summary,
            passed=design_payload.passed,
            rubric_scores=design_payload.rubric_scores,
            reviewed_at=utcnow(),
        )

        state = manager.apply(
            self._require_state(),
            role=AgentRole.QC,
            updates={"review.qc_result": qc_result},
        )
        self._state = manager.apply(
            state,
            role=AgentRole.DESIGN,
            updates={"review.design_result": design_result},
        )

        self._bus.stage(
            code="REVIEW",
            label="critics",
            iteration=iteration,
            text=(
                f"qc {len(qc_result.issues)} issues, "
                f"design {len(design_result.issues)} issues "
                f"(overall {design_result.rubric_scores.overall if design_result.rubric_scores else 0.0})"
            ),
        )

    def _critic_projection(
        self, state: ProjectState, role: AgentRole, build_dir: Path
    ) -> dict[str, Any]:
        """Build a critic projection and prove G1 before it leaves."""

        if role is AgentRole.QC:
            projection = qc_projection(
                state, limits=self._ctx, build_dir=build_dir, operator=self._operator
            )
        else:
            projection = design_projection(
                state, limits=self._ctx, build_dir=build_dir, operator=self._operator
            )
        projection = strip_cross_critic_issues(projection, role)
        assert_critic_isolation(projection, role)
        return projection

    # -- S5 ----------------------------------------------------------------

    async def _s5_adjudicate(self) -> None:
        state = self._enter(Stage.ADJUDICATE, AgentRole.OBSERVER)
        gates = self._config.current.governor.gates
        iteration = state.pipeline.iteration

        qc_issues = list(state.review.qc_result.issues) if state.review.qc_result else []
        design_issues = (
            list(state.review.design_result.issues) if state.review.design_result else []
        )
        previous = list(state.review.open_issues)

        # The merge, the dedupe, and the precedence order are computed here,
        # in Python. The Observer adjudicates them; it does not recompute
        # them, and its own merged_issues echo is advisory.
        merged = sort_by_precedence(
            merge_issues(
                qc_issues, design_issues, iteration=iteration, previous=previous
            )
        )
        self._merged = merged

        still_open, new_debt = partition_debt(merged, gates=gates)
        resolved = resolved_since(previous, merged)
        blockers = blocking_issues(still_open, gates)
        repeats = repeat_offenders(still_open, gates=gates)

        projection = adjudication_projection(
            state,
            limits=self._ctx,
            merged=[issue.model_dump(mode="json") for issue in merged],
            operator=self._operator,
        )
        run = await self._invoke(
            role=AgentRole.OBSERVER, projection=projection, stage=Stage.ADJUDICATE
        )
        payload = self._payload(run)

        state = self._require_state()
        manager = self._manager_ref()
        updates: dict[str, Any] = {
            "review.open_issues": still_open,
            "review.accepted_debt": self._dedupe(
                state.review.accepted_debt, new_debt
            ),
            "review.resolved_issues": self._dedupe(
                state.review.resolved_issues, resolved
            ),
            "review.escalated": sorted(
                {*state.review.escalated, *payload.escalations}
            ),
            "review.previous_blocker_fingerprints": list(
                state.review.blocker_fingerprints
            ),
            "review.blocker_fingerprints": fingerprints(blockers),
        }
        state = manager.apply_system(state, updates)
        self._state = manager.apply(
            state,
            role=AgentRole.OBSERVER,
            updates={"memory": self._memory(state)},
        )
        self._record_scope(payload)

        self._bus.stage(
            code="ADJUDICATE",
            label="merged",
            iteration=iteration,
            text=(
                f"{len(merged)} issues, {len(blockers)} blocking, "
                f"{len(new_debt)} accepted as debt, {len(resolved)} resolved, "
                f"{len(repeats)} repeat offenders"
            ),
        )
        if repeats:
            self._bus.governor(
                code="G5",
                label="repeat offender",
                text=", ".join(sorted(issue.issue_id for issue in repeats)),
                iteration=iteration,
            )

    # -- S6 ----------------------------------------------------------------

    def _s6_gate(self) -> GateResult:
        state = self._enter(Stage.GATE, AgentRole.COMMANDER)
        gates = self._config.current.governor.gates
        design_result = state.review.design_result
        overall = (
            design_result.rubric_scores.overall
            if design_result is not None and design_result.rubric_scores is not None
            else None
        )
        gate = evaluate_gate(
            state,
            gates=gates,
            budget_reason=self._budgeter.breach(state.budgets),
            iterations_remaining=self._budgeter.iterations_remaining(state.budgets),
            design_overall=overall,
        )
        self._last_gate = gate
        self._bus.governor(
            code=gate.decision.value.upper(),
            label=gate.rule,
            text=gate.reason,
            iteration=state.pipeline.iteration,
        )
        if gate.detail:
            self._bus.trace("gate", dict(gate.detail))
        return gate

    # -- S7 ----------------------------------------------------------------

    async def _s7_rules(self) -> None:
        gate = self._last_gate
        if gate is None:
            raise PipelineError("rule writing requested before the gate ran")

        state = self._enter(Stage.RULE_WRITE, AgentRole.PROMPT_ENGINEER)
        projection = prompt_engineer_projection(
            state, limits=self._ctx, gate_reason=gate.reason, operator=self._operator
        )
        run = await self._invoke(
            role=AgentRole.PROMPT_ENGINEER,
            projection=projection,
            stage=Stage.RULE_WRITE,
        )
        payload = self._payload(run)

        state = self._require_state()
        manager = self._manager_ref()
        known = self._known_issue_ids(state)
        ruleset = promote_rules(
            state.rules,
            list(payload.rules),
            iteration=state.pipeline.iteration,
            limits=self._config.current.governor.rules,
            known_issue_ids=known,
        )
        state = manager.apply(
            state, role=AgentRole.PROMPT_ENGINEER, updates={"rules": ruleset}
        )
        self._state = state

        self._bus.note(payload.analysis, iteration=state.pipeline.iteration)
        self._bus.governor(
            code="G7",
            label="rules",
            text=(
                f"{len(ruleset.active)} active, "
                f"{len(ruleset.retired)} retired"
            ),
            iteration=state.pipeline.iteration,
        )

        # The iteration is complete: snapshot it, then advance. Committing
        # before the bump is what keeps one snapshot per iteration.
        self._commit(note=f"loop: {gate.reason}")
        self._state = manager.bump_iteration(self._require_state())
        self._bus.status(self.status_payload())

    # -- S8 / escalation ---------------------------------------------------

    def _s8_awaiting(self, gate: GateResult) -> BuildOutcome:
        manager = self._manager_ref()
        state = manager.transition(
            self._require_state(),
            Stage.AWAITING_REVIEW,
            actor=AgentRole.COMMANDER,
            note=gate.reason,
        )
        self._state = manager.set_status(
            state,
            PipelineStatus.AWAITING_REVIEW,
            gate_decision=GateDecision.PASS,
        )
        commit = self._commit(note="gate passed")
        self._sync_registry(ProjectStatus.ACTIVE)

        state = self._require_state()
        url = None
        if (
            state.artifacts.build_id is not None
            and state.artifacts.entrypoint is not None
            and commit is not None
        ):
            url = preview_url(
                self.slug or "",
                state.pipeline.run_id,
                commit,
                state.artifacts.build_id,
                state.artifacts.entrypoint,
            )
            mode = state.artifacts.preview_mode
            self._bus.preview(
                url=url,
                build_id=state.artifacts.build_id,
                channel=mode.value if mode is not None else "source",
                entrypoint=state.artifacts.entrypoint,
                manifest=build_manifest(state),
                surface="preview",
            )

        cli = CliResponse.READY_FOR_REVIEW.value
        self._bus.cli(cli)
        return self._outcome(
            cli=cli, decision=GateDecision.PASS, reason=gate.reason, preview=url
        )

    def _escalate(self, gate: GateResult) -> BuildOutcome:
        return self._halt(
            gate.reason, decision=GateDecision.ESCALATE, rule=gate.rule
        )

    def _halt(
        self,
        reason: str,
        *,
        decision: GateDecision | None = None,
        rule: str = "halt",
    ) -> BuildOutcome:
        """Stop the build and hand it back to the operator, intact."""

        if self._manager is None or self._state is None:
            raise PipelineError(reason)

        manager = self._manager
        state = manager.set_status(
            self._state,
            PipelineStatus.NEEDS_HUMAN,
            reason=reason,
            gate_decision=decision,
        )
        self._state = manager.transition(
            state, state.pipeline.current_stage, actor=AgentRole.COMMANDER, note=reason
        )
        self._commit(note=f"halt: {reason}")
        self._sync_registry(ProjectStatus.BLOCKED)

        self._bus.governor(
            code="HALT",
            label=rule,
            text=reason,
            iteration=self._require_state().pipeline.iteration,
        )
        cli = f"{CliResponse.NEEDS_HUMAN.value}: {reason}"
        self._bus.cli(cli)
        return self._outcome(cli=cli, decision=decision, reason=reason)

    # -- agent plumbing ----------------------------------------------------

    async def _invoke(
        self,
        *,
        role: AgentRole,
        projection: Mapping[str, Any],
        stage: Stage,
        rules: Sequence[Any] = (),
    ) -> AgentRun:
        state = self._require_state()
        # G2. The runner does not police ceilings; a call that would breach
        # one never leaves this method.
        self._budgeter.assert_within_budget(state.budgets)
        run = await self._runner.invoke(
            role=role,
            projection=projection,
            run_id=state.pipeline.run_id,
            step_id=self._step(stage, state.pipeline.iteration),
            iteration=state.pipeline.iteration,
            input_state_hash=state.meta.state_hash,
            rules=rules,
        )
        self._settle(run)
        return run

    def _settle(self, run: AgentRun) -> None:
        """Charge a completed call and queue any context action it produced."""

        state = self._require_state()
        budgets = self._budgeter.charge(state.budgets, run.telemetry)
        self._state = self._manager_ref().apply_system(state, {"budgets": budgets})
        if run.context_action is not None:
            # `memory` belongs to the Observer (G8), so trims are queued here
            # and committed the next time the Observer writes.
            self._pending_context.append(run.context_action)

    def _payload(self, run: AgentRun) -> Any:
        if run.envelope is None:
            raise PipelineError(
                f"{run.role.value} returned no structured envelope"
            )
        return run.envelope.payload

    def _record_scope(self, payload: Any) -> None:
        """Surface the Observer's role-breach findings in Window 3."""

        verdicts = getattr(payload, "scope_verdicts", None) or []
        for verdict in verdicts:
            if verdict.in_scope:
                continue
            self._bus.governor(
                code="G8",
                label=f"scope: {verdict.agent.value}",
                text=verdict.evidence or "out of scope",
                iteration=self._require_state().pipeline.iteration,
            )

    # -- state helpers -----------------------------------------------------

    def _enter(self, stage: Stage, actor: AgentRole) -> ProjectState:
        """Move to a stage and return the current state.

        Config changes staged by `!reload` are adopted here, at a stage
        boundary, never in the middle of a call.
        """

        self._refresh_bindings()
        manager = self._manager_ref()
        self._state = manager.transition(
            self._require_state(), stage, actor=actor
        )
        self._bus.status(self.status_payload())
        return self._require_state()

    def _refresh_bindings(self) -> None:
        if not self._config.has_pending:
            return
        self._config.apply_pending()
        self._runner.reload()
        self._bus.note("reloaded provider bindings at a stage boundary", always=True)

    def _commit(self, *, note: str | None = None) -> str | None:
        manager = self._manager_ref()
        commit = manager.commit(self._require_state(), note=note)
        # commit() writes a merged state that it does not hand back. Reading
        # the snapshot again is what keeps the in-memory state and the file
        # on disk byte-identical, including head_snapshot and the note.
        self._state = manager.load_snapshot(commit.snapshot)
        self._bus.stage(
            code="SNAPSHOT",
            label=commit.snapshot,
            iteration=commit.iteration,
            text=str(commit.path),
        )
        return commit.snapshot

    def _sync_registry(self, status: ProjectStatus) -> None:
        state = self._require_state()
        self._workspace.record_progress(
            self.slug or "",
            run_id=state.pipeline.run_id,
            snapshot_id=state.pipeline.head_snapshot,
            status=status,
            open_blocker_count=len(state.review.open_blockers),
            pinned_spec_version=state.memory.pinned_spec_version,
            digest=state.memory.digest,
        )

    def _memory(
        self,
        state: ProjectState,
        *,
        digest: str | None = None,
        pinned_spec_version: int | None = None,
    ) -> Memory:
        """Fold queued context actions into the Observer's memory region."""

        actions = list(state.memory.actions) + self._pending_context
        self._pending_context = []
        actions = actions[-MAX_MEMORY_ACTIONS:]

        recent = list(state.memory.recent_iterations)
        if state.pipeline.iteration not in recent:
            recent.append(state.pipeline.iteration)
        recent = recent[-MAX_RECENT_ITERATIONS:]

        token_estimate = (
            actions[-1].tokens_after if actions else state.memory.token_estimate
        )
        return state.memory.model_copy(
            update={
                "actions": actions,
                "recent_iterations": recent,
                "digest": digest if digest is not None else state.memory.digest,
                "pinned_spec_version": (
                    pinned_spec_version
                    if pinned_spec_version is not None
                    else state.memory.pinned_spec_version
                ),
                "pinned_rule_ids": [
                    rule.rule_id for rule in state.rules.active if rule.active
                ],
                "token_estimate": token_estimate,
            }
        )

    def _known_issue_ids(self, state: ProjectState) -> list[str]:
        seen: list[str] = []
        for bucket in (
            self._merged,
            state.review.open_issues,
            state.review.accepted_debt,
            state.review.resolved_issues,
        ):
            for issue in bucket:
                if issue.issue_id not in seen:
                    seen.append(issue.issue_id)
        return seen

    @staticmethod
    def _dedupe(existing: Sequence[Issue], incoming: Iterable[Issue]) -> list[Issue]:
        merged: dict[str, Issue] = {issue.fingerprint: issue for issue in existing}
        for issue in incoming:
            merged[issue.fingerprint] = issue
        return list(merged.values())

    def _resolve_build_dir(self, state: ProjectState) -> Path | None:
        snap = state.pipeline.head_snapshot
        bld = state.artifacts.build_id
        if snap is None or bld is None:
            return None
        candidate = self._store_ref().build_dir(state.pipeline.run_id, snap, bld)
        return candidate if candidate.exists() else None

    def _outcome(
        self,
        *,
        cli: str,
        decision: GateDecision | None = None,
        reason: str | None = None,
        preview: str | None = None,
    ) -> BuildOutcome:
        state = self._require_state()
        return BuildOutcome(
            run_id=state.pipeline.run_id,
            slug=self.slug or "",
            status=state.pipeline.status,
            stage=state.pipeline.current_stage,
            iteration=state.pipeline.iteration,
            cli=cli,
            snapshot=state.pipeline.head_snapshot,
            build=state.artifacts.build_id,
            entrypoint=state.artifacts.entrypoint,
            preview=preview,
            decision=decision,
            reason=reason,
            open_blockers=len(state.review.open_blockers),
        )

    # -- plan parsing ------------------------------------------------------
    #
    # The implementer is the one text-mode role, so its <plan> is JSON by
    # convention rather than by schema. Malformed entries are dropped with a
    # note instead of failing a build whose files are otherwise valid.

    def _plan_summary(self, plan: Mapping[str, Any], notes: str | None) -> str | None:
        summary = plan.get("plan_summary") or plan.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        steps = plan.get("plan") or plan.get("steps")
        if isinstance(steps, list):
            rendered = "; ".join(str(step) for step in steps if str(step).strip())
            if rendered:
                return rendered
        return notes

    def _components(self, plan: Mapping[str, Any]) -> list[Component]:
        return self._coerce(plan.get("components"), Component, "component")

    def _dependencies(self, plan: Mapping[str, Any]) -> list[Dependency]:
        return self._coerce(plan.get("dependencies"), Dependency, "dependency")

    def _deviations(self, plan: Mapping[str, Any]) -> list[Deviation]:
        return self._coerce(plan.get("deviations"), Deviation, "deviation")

    def _file_plans(
        self, plan: Mapping[str, Any], manifest: Sequence[ArtifactFile]
    ) -> list[FilePlan]:
        planned = self._coerce(plan.get("files"), FilePlan, "file plan")
        if planned:
            return planned
        return [
            FilePlan(path=item.path, purpose=item.purpose or "artifact")
            for item in manifest
        ]

    def _coerce(self, raw: Any, model: Any, label: str) -> list[Any]:
        if not isinstance(raw, list):
            return []
        built: list[Any] = []
        skipped = 0
        for item in raw:
            try:
                built.append(model.model_validate(item))
            except Exception:
                skipped += 1
        if skipped:
            self._bus.note(
                f"dropped {skipped} malformed {label} entries from the plan",
                iteration=self._require_state().pipeline.iteration,
            )
        return built

    # -- small accessors ---------------------------------------------------

    @property
    def _ctx(self) -> Any:
        return self._config.current.governor.context

    @staticmethod
    def _step(stage: Stage, iteration: int) -> str:
        """Step ids look like S3-004: the stage number and the iteration."""
        return f"{stage.value[:2]}-{iteration:03d}"

    def _require_state(self) -> ProjectState:
        if self._state is None:
            raise PipelineError(CliResponse.NO_STATE.value)
        return self._state

    def _manager_ref(self) -> StateManager:
        if self._manager is None:
            raise PipelineError(CliResponse.NO_STATE.value)
        return self._manager

    def _store_ref(self) -> SnapshotStore:
        if self._handle is None:
            raise PipelineError(CliResponse.NO_STATE.value)
        return self._handle.store
