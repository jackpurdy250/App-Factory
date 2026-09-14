"""The single writer for `project_state.json`.

No agent ever mutates the bus. An agent returns a validated envelope, and
this module merges its payload into the one region that agent owns (G8). Any
attempt to write outside that region is refused here, in Python, rather than
discouraged in a prompt.

Three properties this module is responsible for:

  1. **Ownership.** `OWNED_REGIONS` says who may author what. Regions listed
     in `STATE_MANAGER_REGIONS` have no agent owner at all and are written
     only by deterministic code.

  2. **Integrity.** Every write restamps `meta.updated_at` and recomputes
     `meta.state_hash`. Agents echo that hash back as `input_state_hash`,
     which is how a reply written against a stale spec is detected instead of
     silently merged.

  3. **History.** Snapshots, never rollback. Each iteration writes an
     immutable directory and moves HEAD; nothing in the system reverses a
     write, so a bad iteration is a new snapshot rather than a lost one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from .schemas import (
    OWNED_REGIONS,
    STATE_MANAGER_REGIONS,
    AgentRole,
    Budgets,
    GateDecision,
    Meta,
    Pipeline,
    PipelineStatus,
    ProjectState,
    ProjectType,
    Spec,
    Stack,
    Stage,
    StageTransition,
    utcnow,
)
from .store import SnapshotStore, snapshot_id

#: Region keys are at most `parent.child`. Anything deeper would mean an agent
#: is reaching into a specific field, which is a prompt problem, not a merge.
MAX_KEY_DEPTH = 2

#: Stage history is an audit trail, not a growth surface. A build that somehow
#: produced more transitions than this has a loop the governor should have
#: caught; the cap keeps one runaway run from producing an unreadable file.
MAX_STAGE_HISTORY = 400


class StateError(RuntimeError):
    """Raised when a write is unauthorized, malformed, or would corrupt state."""


@dataclass(frozen=True, slots=True)
class Commit:
    """The result of persisting one snapshot."""

    snapshot: str
    path: Path
    state_hash: str
    iteration: int


def regions_for(role: AgentRole) -> frozenset[str]:
    """Regions an agent may author. Empty for a role with no write rights."""

    return OWNED_REGIONS.get(role, frozenset())


class StateManager:
    """Owns one run's state bus."""

    def __init__(self, store: SnapshotStore, run_id: str) -> None:
        self._store = store
        self._run_id = run_id

    @property
    def store(self) -> SnapshotStore:
        return self._store

    @property
    def run_id(self) -> str:
        return self._run_id

    # -- construction -----------------------------------------------------

    def create(
        self,
        *,
        project_id: str,
        project_slug: str,
        project_type: ProjectType = ProjectType.APP,
        stack: Stack = Stack.WEB,
        raw_input: str = "",
        budgets: Budgets | None = None,
    ) -> ProjectState:
        """Build the initial state for a run and create its run directory.

        `spec.target_stack` is seeded from `meta.stack` because the state
        model refuses to hold two different answers to "what are we
        building".
        """

        self._store.create_run(self._run_id)
        try:
            state = ProjectState(
                meta=Meta(
                    project_id=project_id,
                    project_slug=project_slug,
                    project_type=project_type,
                    stack=stack,
                ),
                pipeline=Pipeline(run_id=self._run_id),
                spec=Spec(target_stack=stack),
                budgets=budgets or Budgets(),
            )
        except ValidationError as exc:
            raise StateError(f"cannot create state for {project_slug!r}: {exc}") from exc

        state.intent.raw_input = raw_input
        return self._restamp(state)

    def adopt(self, state: ProjectState) -> ProjectState:
        """Take ownership of an externally loaded state.

        Used when `!project use` resumes a build: the run id on the pipeline
        must agree with the manager's, otherwise two runs would be writing
        into one directory.
        """

        if state.pipeline.run_id != self._run_id:
            raise StateError(
                f"state belongs to run {state.pipeline.run_id!r}, "
                f"not {self._run_id!r}"
            )
        return state

    def load_snapshot(self, snap_id: str) -> ProjectState:
        return self._store.read_state(self._run_id, snap_id)

    def load_head(self) -> ProjectState | None:
        """The most recent committed state, or None for a run with no writes."""

        head = self._store.head(self._run_id)
        if head is None:
            return None
        return self._store.read_state(self._run_id, head)

    # -- authorization ----------------------------------------------------

    def _authorize(self, role: AgentRole | None, keys: list[str]) -> None:
        """G8. Refuse any write outside the caller's region."""

        if not keys:
            raise StateError("a write must name at least one region")

        for key in keys:
            if not key or key.startswith(".") or key.endswith("."):
                raise StateError(f"malformed region key: {key!r}")
            if len(key.split(".")) > MAX_KEY_DEPTH:
                raise StateError(
                    f"region key {key!r} is deeper than {MAX_KEY_DEPTH} levels"
                )

        if role is None:
            unauthorized = [key for key in keys if key not in STATE_MANAGER_REGIONS]
            if unauthorized:
                raise StateError(
                    "the state manager may not author "
                    + ", ".join(sorted(unauthorized))
                )
            return

        owned = regions_for(role)
        if not owned:
            raise StateError(f"{role.value} owns no region of the state bus")

        unauthorized = [key for key in keys if key not in owned]
        if unauthorized:
            raise StateError(
                f"{role.value} may not write "
                + ", ".join(sorted(unauthorized))
                + "; it owns "
                + ", ".join(sorted(owned))
            )

    # -- merging ----------------------------------------------------------

    def _assign(self, state: ProjectState, dotted: str, value: Any) -> None:
        """Set one region on a working copy.

        Assignment rather than reconstruction: `validate_assignment` is on for
        every model in the bus, so a bad value raises here instead of landing
        and being discovered three stages later.
        """

        parts = dotted.split(".")
        target: Any = state
        for part in parts[:-1]:
            target = getattr(target, part, None)
            if target is None:
                raise StateError(f"no such region: {dotted!r}")
        leaf = parts[-1]
        if not hasattr(target, leaf):
            raise StateError(f"no such region: {dotted!r}")
        try:
            setattr(target, leaf, value)
        except ValidationError as exc:
            raise StateError(f"rejected write to {dotted}: {exc}") from exc

    def _merge(
        self,
        state: ProjectState,
        updates: Mapping[str, Any],
        *,
        role: AgentRole | None,
    ) -> ProjectState:
        self._authorize(role, list(updates))

        working = state.model_copy(deep=True)
        for dotted, value in updates.items():
            self._assign(working, dotted, value)

        # Nested assignment does not re-run ProjectState's own validators, so
        # the whole object is revalidated. This is what catches an Observer
        # writing a spec whose target_stack contradicts meta.stack.
        try:
            revalidated = ProjectState.model_validate(working.model_dump())
        except ValidationError as exc:
            raise StateError(f"write would corrupt the state bus: {exc}") from exc

        return self._restamp(revalidated)

    def apply(
        self,
        state: ProjectState,
        *,
        role: AgentRole,
        updates: Mapping[str, Any],
    ) -> ProjectState:
        """Merge an agent's output into the region that agent owns."""

        return self._merge(state, updates, role=role)

    def apply_system(
        self, state: ProjectState, updates: Mapping[str, Any]
    ) -> ProjectState:
        """Write a region that no agent owns."""

        return self._merge(state, updates, role=None)

    # -- pipeline (Commander's region) ------------------------------------

    def transition(
        self,
        state: ProjectState,
        stage: Stage,
        *,
        actor: AgentRole | None = None,
        note: str | None = None,
    ) -> ProjectState:
        """Move the pipeline to a stage and record the transition."""

        pipeline = state.pipeline.model_copy(deep=True)
        history = list(pipeline.stage_history)
        history.append(
            StageTransition(
                stage=stage,
                iteration=pipeline.iteration,
                actor=actor,
                note=note,
            )
        )
        if len(history) > MAX_STAGE_HISTORY:
            history = history[-MAX_STAGE_HISTORY:]
        pipeline.stage_history = history
        pipeline.current_stage = stage
        return self._merge(state, {"pipeline": pipeline}, role=AgentRole.COMMANDER)

    def set_status(
        self,
        state: ProjectState,
        status: PipelineStatus,
        *,
        reason: str | None = None,
        gate_decision: GateDecision | None = None,
    ) -> ProjectState:
        """Set the run's status, and the reason when a human is needed."""

        pipeline = state.pipeline.model_copy(deep=True)
        pipeline.status = status
        if gate_decision is not None:
            pipeline.last_gate_decision = gate_decision
        if status in (PipelineStatus.BLOCKED, PipelineStatus.NEEDS_HUMAN):
            pipeline.needs_human_reason = reason
        elif reason is None:
            pipeline.needs_human_reason = None
        return self._merge(state, {"pipeline": pipeline}, role=AgentRole.COMMANDER)

    def bump_iteration(self, state: ProjectState) -> ProjectState:
        """Start the next iteration and charge it against the budget.

        Both counters move together on purpose: an iteration the pipeline ran
        but did not charge is exactly how a loop limit gets bypassed.
        """

        pipeline = state.pipeline.model_copy(deep=True)
        pipeline.iteration += 1

        budgets = state.budgets.model_copy(deep=True)
        budgets.iterations_used += 1

        after = self._merge(state, {"pipeline": pipeline}, role=AgentRole.COMMANDER)
        return self.apply_system(after, {"budgets": budgets})

    # -- integrity --------------------------------------------------------

    def _restamp(self, state: ProjectState) -> ProjectState:
        """Stamp `updated_at` and recompute `state_hash`.

        `canonical_json` excludes the hash field itself, so the value is a
        function of everything except itself and recomputing it is stable.
        """

        state.meta.updated_at = utcnow()
        state.meta.state_hash = state.compute_state_hash()
        return state

    def rehash(self, state: ProjectState) -> ProjectState:
        return self._restamp(state)

    def verify_hash(self, state: ProjectState) -> bool:
        """True when the recorded hash matches the content."""

        return state.meta.state_hash == state.compute_state_hash()

    # -- persistence ------------------------------------------------------

    def commit(self, state: ProjectState, *, note: str | None = None) -> Commit:
        """Write an immutable snapshot for the current iteration and move HEAD."""

        snap_id = snapshot_id(state.pipeline.iteration)

        if self._store.is_sealed(self._run_id, snap_id):
            raise StateError(f"snapshot {snap_id} is sealed and cannot be rewritten")

        pipeline = state.pipeline.model_copy(deep=True)
        pipeline.head_snapshot = snap_id
        if note:
            history = list(pipeline.stage_history)
            history.append(
                StageTransition(
                    stage=pipeline.current_stage,
                    iteration=pipeline.iteration,
                    note=note,
                )
            )
            pipeline.stage_history = history[-MAX_STAGE_HISTORY:]

        final = self._merge(state, {"pipeline": pipeline}, role=AgentRole.COMMANDER)

        # The snapshot directory usually exists already, because this
        # iteration's build was written into it before the state was
        # committed. write_state() creates it when missing and enforces the
        # guarantee that actually matters: project_state.json is written once
        # and never overwritten.
        path = self._store.write_state(self._run_id, snap_id, final)
        self._store.set_head(self._run_id, snap_id)

        state_hash = final.meta.state_hash or final.compute_state_hash()
        return Commit(
            snapshot=snap_id,
            path=path,
            state_hash=state_hash,
            iteration=final.pipeline.iteration,
        )

    def seal(self, state: ProjectState, *, bld_id: str) -> dict[str, object]:
        """Mark the head snapshot shipped. Irreversible by design."""

        head = self._store.head(self._run_id)
        if head is None:
            raise StateError("nothing to seal: this run has no snapshot")
        return self._store.seal(self._run_id, head, bld_id=bld_id)

    def snapshot_index(self) -> list[dict[str, object]]:
        return self._store.snapshot_index(self._run_id)
