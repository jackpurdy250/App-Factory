"""App Factory - [agent]_output.json, the shared envelope.

Every LLM reply lands here first. The envelope is identical for all agents;
only `payload` differs, and it is selected by a discriminated union on
`agent`, so a QC payload can never be accepted from the Implementer.

The Implementer is the only role whose real product is not JSON: its code goes
to disk, and its payload carries only the manifest describing what it wrote.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Optional, Union

from pydantic import Field, TypeAdapter

from .common import (
    SCHEMA_VERSION,
    AgentRole,
    Ambiguity,
    ContextAction,
    Fingerprint,
    GateDecision,
    Issue,
    IssueId,
    OutputStatus,
    ProjectSlug,
    ReqId,
    RubricScores,
    RunId,
    ScopeVerdict,
    Strict,
    Telemetry,
    utcnow,
)
from .state import (
    ArtifactFile,
    Component,
    Dependency,
    Deviation,
    Spec,
)

StepId = Annotated[str, Field(pattern=r"^S\d-\d{3}$")]
StateHashRef = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]


# --------------------------------------------------------------------------
# Window 1 closed vocabulary
# --------------------------------------------------------------------------


class CliResponse(StrEnum):
    """The only strings that may ever reach Window 1."""

    INVALID_COMMAND = "invalid command"
    PIPELINE_BUSY = "pipeline busy"
    NO_BUILD_AWAITING_REVIEW = "no build awaiting review"
    NO_STATE = "no state"
    IDLE = "idle"
    NEEDS_HUMAN = "needs human"
    ACK = "ack"
    READY_FOR_REVIEW = "ready for review"
    SHIPPED = "shipped"
    NO_SUCH_PROJECT = "no such project"
    PROJECT_EXISTS = "project exists"


class ReasonCode(StrEnum):
    """Window 3 only. Never rendered in the CLI."""

    E_SIGIL = "E_SIGIL"
    E_VERB = "E_VERB"
    E_ARITY = "E_ARITY"
    E_TARGET = "E_TARGET"
    E_TARGET_SCOPE = "E_TARGET_SCOPE"
    E_EMPTY_ARG = "E_EMPTY_ARG"
    E_TRAILING = "E_TRAILING"
    E_VALUE = "E_VALUE"  # well-formed command, unacceptable argument value


# --------------------------------------------------------------------------
# Shared envelope members
# --------------------------------------------------------------------------


class Request(Strict):
    """Something the agent needed but was not given. Never a question to the
    user directly - the Commander decides whether to surface it."""

    what: str = Field(min_length=1)
    why: Optional[str] = None
    blocking: bool = False


class Assumption(Strict):
    statement: str = Field(min_length=1)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    req_id: Optional[ReqId] = None


class EnvelopeBase(Strict):
    schema_version: str = SCHEMA_VERSION
    run_id: RunId
    step_id: StepId
    iteration: int = Field(ge=0)
    input_state_hash: StateHashRef
    status: OutputStatus = OutputStatus.OK
    emitted_at: datetime = Field(default_factory=utcnow)
    requests: list[Request] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    telemetry: Optional[Telemetry] = None
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Payloads
# --------------------------------------------------------------------------


class OptimizerPayload(Strict):
    optimized_prompt: str = Field(min_length=1)
    inferred_requirements: list[str] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class ObserverSpecPayload(Strict):
    """S2. The Observer authors the spec."""

    kind: Literal["spec"] = "spec"
    spec: Spec
    scope_verdicts: list[ScopeVerdict] = Field(default_factory=list)
    context_action: ContextAction = ContextAction.NONE
    digest: Optional[str] = None


class ObserverAdjudicationPayload(Strict):
    """S5. The Observer merges two critic reports into one issue set."""

    kind: Literal["adjudication"] = "adjudication"
    merged_issues: list[Issue] = Field(default_factory=list)
    blocker_fingerprints: list[Fingerprint] = Field(default_factory=list)
    escalations: list[IssueId] = Field(default_factory=list)
    precedence_notes: list[str] = Field(default_factory=list)
    gate_recommendation: GateDecision = GateDecision.PASS
    scope_verdicts: list[ScopeVerdict] = Field(default_factory=list)
    context_action: ContextAction = ContextAction.NONE


class ObserverContextSavePayload(Strict):
    """S2 and S6. The Observer refreshes this project's registry record in
    saved-project-context.json.

    Descriptive fields only. The mechanical ones - head run, head snapshot,
    timestamps, shipped builds - are written by deterministic Python at every
    stage boundary, so a failed LLM call can never lose the operator's place.
    """

    kind: Literal["context_save"] = "context_save"
    slug: ProjectSlug
    title: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    pinned_spec_version: int = Field(ge=0)
    open_blocker_count: int = Field(default=0, ge=0)
    context_action: ContextAction = ContextAction.NONE


ObserverPayload = Annotated[
    Union[
        ObserverSpecPayload,
        ObserverAdjudicationPayload,
        ObserverContextSavePayload,
    ],
    Field(discriminator="kind"),
]


class ImplementerPayload(Strict):
    """Free-form code goes to disk; only the manifest travels in JSON."""

    plan: list[str] = Field(default_factory=list)
    components: list[Component] = Field(default_factory=list)
    files: list[ArtifactFile] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    deviations: list[Deviation] = Field(default_factory=list)
    entrypoint: Optional[str] = None


class QcPayload(Strict):
    issues: list[Issue] = Field(default_factory=list)
    spec_coverage: list[ReqId] = Field(default_factory=list)
    summary: Optional[str] = None
    passed: bool = False


class DesignPayload(Strict):
    issues: list[Issue] = Field(default_factory=list)
    rubric_scores: RubricScores
    ui_surfaces_reviewed: list[str] = Field(default_factory=list)
    summary: Optional[str] = None
    passed: bool = False


class RuleDraft(Strict):
    rule_text: str = Field(min_length=1)
    origin_issue: IssueId  # G7: no orphan rules
    scope: AgentRole = AgentRole.IMPLEMENTER
    ttl_iterations: int = Field(default=3, ge=1, le=10)


class PromptEngineerPayload(Strict):
    analysis: str = Field(min_length=1)
    rules: list[RuleDraft] = Field(default_factory=list, max_length=5)


class CommanderPayload(Strict):
    """`detail` supplies the suffix for NEEDS_HUMAN and SHIPPED only."""

    response: CliResponse
    detail: Optional[str] = None
    reason_code: Optional[ReasonCode] = None

    def render(self) -> str:
        if self.response in (CliResponse.NEEDS_HUMAN, CliResponse.SHIPPED) and self.detail:
            joiner = ": " if self.response is CliResponse.NEEDS_HUMAN else " "
            return f"{self.response.value}{joiner}{self.detail}"
        return self.response.value


# --------------------------------------------------------------------------
# Concrete envelopes
# --------------------------------------------------------------------------


class OptimizerOutput(EnvelopeBase):
    agent: Literal[AgentRole.OPTIMIZER] = AgentRole.OPTIMIZER
    payload: OptimizerPayload


class ObserverOutput(EnvelopeBase):
    agent: Literal[AgentRole.OBSERVER] = AgentRole.OBSERVER
    payload: ObserverPayload


class ImplementerOutput(EnvelopeBase):
    agent: Literal[AgentRole.IMPLEMENTER] = AgentRole.IMPLEMENTER
    payload: ImplementerPayload


class QcOutput(EnvelopeBase):
    agent: Literal[AgentRole.QC] = AgentRole.QC
    payload: QcPayload


class DesignOutput(EnvelopeBase):
    agent: Literal[AgentRole.DESIGN] = AgentRole.DESIGN
    payload: DesignPayload


class PromptEngineerOutput(EnvelopeBase):
    agent: Literal[AgentRole.PROMPT_ENGINEER] = AgentRole.PROMPT_ENGINEER
    payload: PromptEngineerPayload


class CommanderOutput(EnvelopeBase):
    agent: Literal[AgentRole.COMMANDER] = AgentRole.COMMANDER
    payload: CommanderPayload


AgentOutput = Annotated[
    Union[
        OptimizerOutput,
        ObserverOutput,
        ImplementerOutput,
        QcOutput,
        DesignOutput,
        PromptEngineerOutput,
        CommanderOutput,
    ],
    Field(discriminator="agent"),
]

#: Parse any agent reply: AgentOutputAdapter.validate_json(raw)
AgentOutputAdapter: TypeAdapter[AgentOutput] = TypeAdapter(AgentOutput)

#: Per-role adapter for provider-native structured output. Pass
#: PAYLOAD_SCHEMAS[role].json_schema() as the response format.
PAYLOAD_SCHEMAS: dict[AgentRole, TypeAdapter] = {
    AgentRole.OPTIMIZER: TypeAdapter(OptimizerPayload),
    AgentRole.OBSERVER: TypeAdapter(ObserverPayload),
    AgentRole.IMPLEMENTER: TypeAdapter(ImplementerPayload),
    AgentRole.QC: TypeAdapter(QcPayload),
    AgentRole.DESIGN: TypeAdapter(DesignPayload),
    AgentRole.PROMPT_ENGINEER: TypeAdapter(PromptEngineerPayload),
    AgentRole.COMMANDER: TypeAdapter(CommanderPayload),
}
