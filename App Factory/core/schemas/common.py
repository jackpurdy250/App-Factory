"""App Factory - shared schema primitives.

Enums, identifier types, the Issue model, and the fingerprint contract.
Imported by state.py (the bus) and envelope.py (agent output).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.2.0"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Strict(BaseModel):
    """Base for every App Factory model.

    extra='forbid' is the point: an LLM that invents a field fails validation
    instead of silently writing garbage into the bus.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class AgentRole(StrEnum):
    COMMANDER = "commander"
    OPTIMIZER = "optimizer"
    OBSERVER = "observer"
    IMPLEMENTER = "implementer"
    QC = "qc"
    DESIGN = "design"
    PROMPT_ENGINEER = "prompt_engineer"


class Stage(StrEnum):
    INTAKE = "S0_INTAKE"
    OPTIMIZE = "S1_OPTIMIZE"
    SPEC = "S2_SPEC"
    BUILD = "S3_BUILD"
    REVIEW = "S4_REVIEW"
    ADJUDICATE = "S5_ADJUDICATE"
    GATE = "S6_GATE"
    RULE_WRITE = "S7_RULE_WRITE"
    AWAITING_REVIEW = "S8_AWAITING_REVIEW"
    SHIPPED = "S9_SHIPPED"


class PipelineStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    SHIPPED = "shipped"


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


#: Higher wins when two critics report the same fingerprint (G5 merge).
SEVERITY_RANK: dict[Severity, int] = {
    Severity.BLOCKER: 3,
    Severity.MAJOR: 2,
    Severity.MINOR: 1,
    Severity.NIT: 0,
}

#: Only these severities hold a build (G3). Mirrors governor.toml.
BLOCKING_SEVERITIES: frozenset[Severity] = frozenset({Severity.BLOCKER})


class IssueCategory(StrEnum):
    CORRECTNESS = "correctness"
    SPEC_FIDELITY = "spec_fidelity"
    SECURITY = "security"
    ACCESSIBILITY = "accessibility"
    UX_POLISH = "ux_polish"


#: G6 deterministic precedence. Index = priority; lower index wins conflicts.
PRECEDENCE_ORDER: tuple[IssueCategory, ...] = (
    IssueCategory.CORRECTNESS,
    IssueCategory.SPEC_FIDELITY,
    IssueCategory.SECURITY,
    IssueCategory.ACCESSIBILITY,
    IssueCategory.UX_POLISH,
)


class IssueState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ACCEPTED_DEBT = "accepted_debt"
    ESCALATED = "escalated"


class OutputStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    REFUSED = "refused"
    ERROR = "error"


class ContextAction(StrEnum):
    NONE = "none"
    SUMMARIZED = "summarized"
    TRUNCATED = "truncated"


class GateDecision(StrEnum):
    PASS = "pass"
    LOOP = "loop"
    ESCALATE = "escalate"


class Priority(StrEnum):
    MUST = "must"
    SHOULD = "should"
    COULD = "could"


class Stack(StrEnum):
    """Declared target language or runtime, chosen at intake.

    Fixed for the life of the project: changing the stack changes the spec,
    so it is a new project rather than an edit.
    """

    WEB = "web"
    PYTHON = "python"
    GO = "go"
    CPP = "cpp"
    RUST = "rust"
    NODE = "node"
    JAVA = "java"
    CSHARP = "csharp"
    SHELL = "shell"
    OTHER = "other"


class ProjectType(StrEnum):
    """What shape of thing is being built. Informs the spec, not a toolchain."""

    APP = "app"
    CLI = "cli"
    LIBRARY = "library"
    SERVICE = "service"
    SCRIPT = "script"


class PreviewMode(StrEnum):
    """How Window 2 renders a build.

    IFRAME executes the artifact in a sandboxed frame. SOURCE shows a
    read-only source view. Nothing outside the web stack is ever executed:
    the App Factory installs no toolchains and runs no compilers.
    """

    IFRAME = "iframe"
    SOURCE = "source"


# --------------------------------------------------------------------------
# Identifier types
# --------------------------------------------------------------------------

ReqId = Annotated[str, Field(pattern=r"^R-\d{3}$")]
AcId = Annotated[str, Field(pattern=r"^AC-\d{3}$")]
IssueId = Annotated[str, Field(pattern=r"^(QC|DES|OBS)-\d{4}$")]
RuleId = Annotated[str, Field(pattern=r"^PE-\d{3}$")]
RunId = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{4}Z-[0-9a-f]{4}$")]
BuildId = Annotated[str, Field(pattern=r"^b-\d{3}$")]
SnapshotId = Annotated[str, Field(pattern=r"^it-\d{3}$")]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
StateHash = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
ProjectSlug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")]


# --------------------------------------------------------------------------
# Stack profiles
# --------------------------------------------------------------------------

#: Suffixes every stack may write: documentation, data, and configuration.
COMMON_SUFFIXES: frozenset[str] = frozenset(
    {".md", ".txt", ".json", ".toml", ".yml", ".yaml", ".csv"}
)


@dataclass(frozen=True, slots=True)
class StackProfile:
    """Per-stack artifact policy.

    Consulted by the artifact writer (G8) to decide what the Implementer is
    allowed to write, and by Window 2 to decide whether a build can be
    previewed or only read. This is internal policy, never serialized into
    the bus, which is why it is a dataclass rather than a Strict model.
    """

    stack: Stack
    suffixes: frozenset[str]
    entrypoint_preference: tuple[str, ...]
    preview: PreviewMode
    bare_filenames: frozenset[str] = frozenset()
    entrypoint_required: bool = False

    @property
    def allowed_suffixes(self) -> frozenset[str]:
        return self.suffixes | COMMON_SUFFIXES

    def permits(self, path: str) -> bool:
        """True when this stack is allowed to write `path`.

        Matches a bare filename first (CMakeLists.txt has no useful suffix),
        then the extension. An extensionless path is refused unless it was
        declared, which keeps stray binaries out of a build directory.
        """
        name = path.rsplit("/", 1)[-1]
        if name in self.bare_filenames:
            return True
        dot = name.rfind(".")
        if dot <= 0:
            return False
        return name[dot:].lower() in self.allowed_suffixes


STACK_PROFILES: dict[Stack, StackProfile] = {
    Stack.WEB: StackProfile(
        stack=Stack.WEB,
        suffixes=frozenset({".html", ".htm", ".css", ".js", ".mjs", ".svg"}),
        entrypoint_preference=("index.html", "main.html", "app.html", "index.htm"),
        preview=PreviewMode.IFRAME,
    ),
    Stack.PYTHON: StackProfile(
        stack=Stack.PYTHON,
        suffixes=frozenset({".py", ".pyi", ".cfg", ".ini"}),
        entrypoint_preference=("main.py", "__main__.py", "app.py"),
        preview=PreviewMode.SOURCE,
    ),
    Stack.GO: StackProfile(
        stack=Stack.GO,
        suffixes=frozenset({".go", ".mod", ".sum"}),
        entrypoint_preference=("main.go", "cmd/main.go"),
        preview=PreviewMode.SOURCE,
    ),
    Stack.CPP: StackProfile(
        stack=Stack.CPP,
        suffixes=frozenset({".cpp", ".cc", ".h", ".hpp", ".cmake"}),
        entrypoint_preference=("main.cpp", "src/main.cpp"),
        preview=PreviewMode.SOURCE,
        bare_filenames=frozenset({"CMakeLists.txt", "Makefile"}),
    ),
    Stack.RUST: StackProfile(
        stack=Stack.RUST,
        suffixes=frozenset({".rs"}),
        entrypoint_preference=("src/main.rs",),
        preview=PreviewMode.SOURCE,
    ),
    Stack.NODE: StackProfile(
        stack=Stack.NODE,
        suffixes=frozenset({".js", ".mjs", ".cjs", ".ts"}),
        entrypoint_preference=("index.js", "main.js", "server.js"),
        preview=PreviewMode.SOURCE,
    ),
    Stack.JAVA: StackProfile(
        stack=Stack.JAVA,
        suffixes=frozenset({".java", ".gradle", ".properties"}),
        entrypoint_preference=("Main.java", "src/Main.java"),
        preview=PreviewMode.SOURCE,
    ),
    Stack.CSHARP: StackProfile(
        stack=Stack.CSHARP,
        suffixes=frozenset({".cs", ".csproj"}),
        entrypoint_preference=("Program.cs",),
        preview=PreviewMode.SOURCE,
    ),
    Stack.SHELL: StackProfile(
        stack=Stack.SHELL,
        suffixes=frozenset({".sh", ".bash"}),
        entrypoint_preference=("main.sh", "run.sh"),
        preview=PreviewMode.SOURCE,
    ),
    Stack.OTHER: StackProfile(
        stack=Stack.OTHER,
        suffixes=frozenset(),
        entrypoint_preference=(),
        preview=PreviewMode.SOURCE,
        entrypoint_required=True,
    ),
}

_UNPROFILED = set(Stack) - set(STACK_PROFILES)
if _UNPROFILED:  # pragma: no cover - structural guard, checked at import
    raise RuntimeError(f"stacks without a profile: {sorted(_UNPROFILED)}")


def profile_for(stack: Stack | str) -> StackProfile:
    """Total lookup. Every Stack member is guaranteed to have a profile."""
    return STACK_PROFILES[Stack(stack)]


# --------------------------------------------------------------------------
# Fingerprint contract
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_LINE_REF = re.compile(r"(?:\bline\s+\d+|:\d+(?::\d+)?)", re.IGNORECASE)
_QUOTED = re.compile(r"[\"'`]([^\"'`]+)[\"'`]")
_NOISE = re.compile(r"[^a-z0-9 _./-]")
_EDGE_PUNCT = "._-/"


def normalize_evidence(text: str) -> str:
    """Collapse two descriptions of one defect onto the same string.

    Strips line/column references, lowercases quoted identifiers, drops
    punctuation, trims token-edge punctuation so a trailing period cannot
    fork a fingerprint, and collapses whitespace. Deliberately lossy.
    """
    s = text.lower()
    s = _LINE_REF.sub(" ", s)
    s = _QUOTED.sub(lambda m: m.group(1).lower(), s)
    s = _NOISE.sub(" ", s)
    tokens = (t.strip(_EDGE_PUNCT) for t in _WS.sub(" ", s).split())
    return " ".join(t for t in tokens if t)


def compute_fingerprint(
    file_path: Optional[str],
    category: IssueCategory | str,
    evidence: str,
) -> str:
    """Stable 16-hex identity for one defect, across iterations and critics."""
    parts = (
        (file_path or "<global>").strip().lower(),
        str(getattr(category, "value", category)),
        normalize_evidence(evidence),
    )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


# --------------------------------------------------------------------------
# Shared models
# --------------------------------------------------------------------------


class Telemetry(Strict):
    provider: str
    model: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    attempt: int = Field(default=1, ge=1)
    fallback_used: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Issue(Strict):
    """One defect. Emitted by QC or Design, merged by the Observer."""

    issue_id: IssueId
    fingerprint: Fingerprint = ""  # always derived; a supplied value is ignored
    raised_by: AgentRole
    severity: Severity
    category: IssueCategory
    evidence: str = Field(min_length=1)
    req_id: Optional[ReqId] = None
    file_path: Optional[str] = None
    suggested_fix: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    state: IssueState = IssueState.OPEN
    first_seen_iteration: int = Field(default=0, ge=0)
    repeat_count: int = Field(default=1, ge=1)
    merged_from: list[IssueId] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _derive_fingerprint(cls, data: object) -> object:
        if isinstance(data, dict):
            data = dict(data)
            data["fingerprint"] = compute_fingerprint(
                data.get("file_path"),
                data.get("category", ""),
                str(data.get("evidence", "")),
            )
        return data

    @property
    def is_blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    @property
    def precedence(self) -> int:
        return PRECEDENCE_ORDER.index(self.category)


class Ambiguity(Strict):
    ambiguity_id: str = Field(pattern=r"^AMB-\d{3}$")
    question: str = Field(min_length=1)
    assumed_answer: Optional[str] = None
    resolved: bool = False
    resolved_by_operator: bool = False


class ScopeVerdict(Strict):
    """Observer's advisory role-boundary check. Enforcement is in Python (G8)."""

    agent: AgentRole
    in_scope: bool
    evidence: Optional[str] = None


class RubricScores(Strict):
    """Design Critic scores on a fixed 0-10 rubric. Bounded by construction,
    which is what makes 'is this good enough' a decidable question."""

    hierarchy: float = Field(ge=0.0, le=10.0)
    density: float = Field(ge=0.0, le=10.0)
    originality: float = Field(ge=0.0, le=10.0)
    affordance: float = Field(ge=0.0, le=10.0)

    @property
    def overall(self) -> float:
        return round(
            (self.hierarchy + self.density + self.originality + self.affordance) / 4, 2
        )


#: The rubric axes are fixed so scores stay comparable across projects; what
#: each axis *means* is re-read per stack. A CLI has no visual hierarchy, but
#: it has a module structure, and that is the same question asked of different
#: material. Supplied to the Design Critic as part of its projection.
RUBRIC_INTERPRETATION: dict[str, str] = {
    "hierarchy": "visual hierarchy for web; module and package structure otherwise",
    "density": "information density for web; public API surface size otherwise",
    "originality": "distinctiveness for web; idiomatic fit to the language otherwise",
    "affordance": "discoverability for web; errors, help text, and naming otherwise",
}
