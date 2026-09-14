"""App Factory - project_state.json, the master bus.

One file, one writer (the state manager). Agents never mutate this; they emit
an envelope (see envelope.py) which is validated and merged into the single
region that agent owns.

Invariant: no source code ever enters this file. Artifacts live on disk and
are referenced here by path + sha256.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Optional

from pydantic import Field, model_validator

from .common import (
    SCHEMA_VERSION,
    AcId,
    AgentRole,
    Ambiguity,
    BuildId,
    ContextAction,
    GateDecision,
    Fingerprint,
    Issue,
    IssueId,
    PreviewMode,
    Priority,
    ProjectSlug,
    ProjectType,
    ReqId,
    RubricScores,
    RuleId,
    RunId,
    Severity,
    Sha256,
    SnapshotId,
    Stack,
    StateHash,
    Stage,
    PipelineStatus,
    Strict,
    profile_for,
    utcnow,
)


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------


class Meta(Strict):
    schema_version: str = SCHEMA_VERSION
    project_id: str = Field(min_length=1)
    project_slug: ProjectSlug  # key into saved-project-context.json
    project_type: ProjectType = ProjectType.APP
    stack: Stack = Stack.WEB  # single source of truth for the target stack
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    state_hash: Optional[StateHash] = None


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------


class StageTransition(Strict):
    stage: Stage
    iteration: int = Field(ge=0)
    actor: AgentRole | None = None
    entered_at: datetime = Field(default_factory=utcnow)
    note: Optional[str] = None


class Pipeline(Strict):
    run_id: RunId
    status: PipelineStatus = PipelineStatus.IDLE
    current_stage: Stage = Stage.INTAKE
    iteration: int = Field(default=0, ge=0)
    head_snapshot: Optional[SnapshotId] = None
    last_gate_decision: Optional[GateDecision] = None
    needs_human_reason: Optional[str] = None
    stage_history: list[StageTransition] = Field(default_factory=list)


# --------------------------------------------------------------------------
# intent  (owner: Prompt Optimizer)
# --------------------------------------------------------------------------


class Intent(Strict):
    raw_input: str = ""
    optimized_prompt: Optional[str] = None
    inferred_requirements: list[str] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    optimized_at: Optional[datetime] = None


# --------------------------------------------------------------------------
# spec  (owner: Observer)
# --------------------------------------------------------------------------


class Requirement(Strict):
    req_id: ReqId
    text: str = Field(min_length=1)
    priority: Priority = Priority.MUST
    rationale: Optional[str] = None
    acceptance_criteria: list[AcId] = Field(default_factory=list)


class AcceptanceCriterion(Strict):
    ac_id: AcId
    req_id: ReqId
    statement: str = Field(min_length=1)
    verifiable_by: str = Field(default="qc")


class Spec(Strict):
    spec_version: int = Field(default=0, ge=0)
    target_stack: Stack = Stack.WEB  # must equal meta.stack; see ProjectState
    language_conventions: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    frozen_at: Optional[datetime] = None


# --------------------------------------------------------------------------
# architecture + artifacts  (owner: Implementer / state manager)
# --------------------------------------------------------------------------


class Component(Strict):
    name: str = Field(min_length=1)
    responsibility: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class FilePlan(Strict):
    path: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    component: Optional[str] = None


class Dependency(Strict):
    name: str = Field(min_length=1)
    version: Optional[str] = None
    reason: Optional[str] = None


class Deviation(Strict):
    req_id: Optional[ReqId] = None
    description: str = Field(min_length=1)
    justification: str = Field(min_length=1)


class Architecture(Strict):
    plan_summary: Optional[str] = None
    components: list[Component] = Field(default_factory=list)
    files: list[FilePlan] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    deviations: list[Deviation] = Field(default_factory=list)
    planned_at: Optional[datetime] = None


class ArtifactFile(Strict):
    path: str = Field(min_length=1)
    sha256: Sha256
    purpose: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)


class Artifacts(Strict):
    build_id: Optional[BuildId] = None
    entrypoint: Optional[str] = None
    preview_mode: Optional[PreviewMode] = None
    files: list[ArtifactFile] = Field(default_factory=list)
    sealed: bool = False
    sealed_at: Optional[datetime] = None


# --------------------------------------------------------------------------
# review  (owner: state manager, via Observer adjudication)
# --------------------------------------------------------------------------


class CriticResult(Strict):
    agent: AgentRole
    iteration: int = Field(ge=0)
    issues: list[Issue] = Field(default_factory=list)
    summary: Optional[str] = None
    passed: bool = False
    rubric_scores: Optional[RubricScores] = None  # Design Critic only
    spec_coverage: list[ReqId] = Field(default_factory=list)  # QC only
    reviewed_at: datetime = Field(default_factory=utcnow)


class Review(Strict):
    qc_result: Optional[CriticResult] = None
    design_result: Optional[CriticResult] = None
    open_issues: list[Issue] = Field(default_factory=list)
    accepted_debt: list[Issue] = Field(default_factory=list)
    resolved_issues: list[Issue] = Field(default_factory=list)
    escalated: list[IssueId] = Field(default_factory=list)
    blocker_fingerprints: list[Fingerprint] = Field(default_factory=list)
    previous_blocker_fingerprints: list[Fingerprint] = Field(default_factory=list)

    @property
    def open_blockers(self) -> list[Issue]:
        return [i for i in self.open_issues if i.severity is Severity.BLOCKER]

    @property
    def made_progress(self) -> bool:
        """G4: the blocker fingerprint set must strictly shrink."""
        now = set(self.blocker_fingerprints)
        before = set(self.previous_blocker_fingerprints)
        if not before:
            return True
        return now < before


# --------------------------------------------------------------------------
# rules  (owner: Prompt Engineer, append-only)
# --------------------------------------------------------------------------


class Rule(Strict):
    rule_id: RuleId
    rule_text: str = Field(min_length=1)
    origin_issue: IssueId  # G7: every rule must cite the defect that caused it
    scope: AgentRole = AgentRole.IMPLEMENTER
    created_iteration: int = Field(ge=0)
    expires_iteration: int = Field(ge=0)
    active: bool = True


class RuleSet(Strict):
    max_active: int = Field(default=5, ge=1)
    active: list[Rule] = Field(default_factory=list)
    retired: list[Rule] = Field(default_factory=list)


# --------------------------------------------------------------------------
# budgets  (owner: budgeter)
# --------------------------------------------------------------------------


class Budgets(Strict):
    iterations_used: int = Field(default=0, ge=0)
    max_iterations: int = Field(default=3, ge=1)
    tokens_used: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=250_000, ge=1)
    cost_usd_used: float = Field(default=0.0, ge=0.0)
    max_cost_usd: Optional[float] = Field(default=None, ge=0.0)
    wall_clock_seconds_used: float = Field(default=0.0, ge=0.0)
    max_wall_clock_seconds: float = Field(default=900.0, ge=1.0)

    @property
    def exhausted(self) -> bool:
        """G2: any one ceiling is enough to escalate."""
        return (
            self.iterations_used >= self.max_iterations
            or self.tokens_used >= self.max_tokens
            or self.wall_clock_seconds_used >= self.max_wall_clock_seconds
            or (
                self.max_cost_usd is not None
                and self.cost_usd_used >= self.max_cost_usd
            )
        )


# --------------------------------------------------------------------------
# memory  (owner: Observer)
# --------------------------------------------------------------------------


class ContextActionRecord(Strict):
    iteration: int = Field(ge=0)
    agent: AgentRole
    action: ContextAction
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    dropped: list[str] = Field(default_factory=list)
    at: datetime = Field(default_factory=utcnow)


class Memory(Strict):
    pinned_spec_version: Optional[int] = Field(default=None, ge=0)
    pinned_rule_ids: list[RuleId] = Field(default_factory=list)
    recent_iterations: list[int] = Field(default_factory=list)
    digest: Optional[str] = None
    token_estimate: int = Field(default=0, ge=0)
    actions: list[ContextActionRecord] = Field(default_factory=list)


# --------------------------------------------------------------------------
# the bus
# --------------------------------------------------------------------------


class ProjectState(Strict):
    meta: Meta
    pipeline: Pipeline
    intent: Intent = Field(default_factory=Intent)
    spec: Spec = Field(default_factory=Spec)
    architecture: Architecture = Field(default_factory=Architecture)
    artifacts: Artifacts = Field(default_factory=Artifacts)
    review: Review = Field(default_factory=Review)
    rules: RuleSet = Field(default_factory=RuleSet)
    budgets: Budgets = Field(default_factory=Budgets)
    memory: Memory = Field(default_factory=Memory)

    def canonical_json(self) -> str:
        """Deterministic serialization, excluding meta.state_hash itself."""
        data = json.loads(self.model_dump_json())
        data.get("meta", {}).pop("state_hash", None)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def compute_state_hash(self) -> str:
        """16-hex identity of this state. Echoed by agents as input_state_hash,
        which is how a reply against a stale spec is detected."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:16]

    @model_validator(mode="after")
    def _stack_agreement(self) -> "ProjectState":
        """The declared stack is single-sourced in `meta`.

        Two ways a polyglot build could go wrong silently, both closed here:
        the Observer retargeting a project by writing a different
        `spec.target_stack`, and Window 2 being told to execute a build that
        the stack profile says is source-only.
        """
        if self.spec.target_stack is not self.meta.stack:
            raise ValueError(
                f"spec.target_stack {self.spec.target_stack.value!r} does not match "
                f"meta.stack {self.meta.stack.value!r}"
            )
        expected = profile_for(self.meta.stack).preview
        actual = self.artifacts.preview_mode
        if actual is not None and actual is not expected:
            raise ValueError(
                f"artifacts.preview_mode {actual.value!r} contradicts the "
                f"{self.meta.stack.value!r} stack profile ({expected.value!r})"
            )
        return self


# --------------------------------------------------------------------------
# G8 write ACL - the only agent permitted to author each region
# --------------------------------------------------------------------------

OWNED_REGIONS: dict[AgentRole, frozenset[str]] = {
    AgentRole.COMMANDER: frozenset({"pipeline"}),
    AgentRole.OPTIMIZER: frozenset({"intent"}),
    AgentRole.OBSERVER: frozenset({"spec", "memory"}),
    AgentRole.IMPLEMENTER: frozenset({"architecture"}),
    AgentRole.QC: frozenset({"review.qc_result"}),
    AgentRole.DESIGN: frozenset({"review.design_result"}),
    AgentRole.PROMPT_ENGINEER: frozenset({"rules"}),
}

#: Regions no agent may author; written only by deterministic Python.
STATE_MANAGER_REGIONS: frozenset[str] = frozenset(
    {
        "meta",
        "artifacts",
        "budgets",
        "review.open_issues",
        "review.accepted_debt",
        "review.resolved_issues",
        "review.escalated",
        "review.blocker_fingerprints",
        "review.previous_blocker_fingerprints",
    }
)
