"""The state vocabulary: identities, enums, ownership, and stack policy.

These are the invariants the rest of the system quietly assumes. Two of them
earn their place by having actually broken:

  * Fingerprints must survive rewording. An early version hashed raw evidence,
    so "...header row is empty" and "...header row is empty." were different
    defects, which defeated repeat-offender detection entirely.
  * A supplied fingerprint must be ignored. An agent that invents one must not
    be able to disguise a repeat defect as a new one.

Nothing here touches disk or the network.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import TypeAdapter, ValidationError  # noqa: E402

from core.schemas.common import (  # noqa: E402
    BLOCKING_SEVERITIES,
    COMMON_SUFFIXES,
    PRECEDENCE_ORDER,
    SCHEMA_VERSION,
    STACK_PROFILES,
    AgentRole,
    BuildId,
    Fingerprint,
    GateDecision,
    Issue,
    IssueCategory,
    IssueState,
    PipelineStatus,
    PreviewMode,
    ProjectSlug,
    ProjectType,
    RunId,
    Severity,
    SnapshotId,
    Stack,
    Stage,
    StateHash,
    compute_fingerprint,
)
from core.schemas.envelope import CliResponse, ReasonCode  # noqa: E402
from core.schemas.registry import (  # noqa: E402
    PROJECTS_DIRNAME,
    REGISTRY_FILENAME,
    ProjectStatus,
    default_runs_dir,
)
from core.schemas.state import (  # noqa: E402
    OWNED_REGIONS,
    STATE_MANAGER_REGIONS,
    Budgets,
    RuleSet,
)
from tests.harness import Checker  # noqa: E402

HEX16 = re.compile(r"^[0-9a-f]{16}$")


def issue(**overrides) -> Issue:
    fields = {
        "issue_id": "QC-0001",
        "raised_by": AgentRole.QC,
        "severity": Severity.BLOCKER,
        "category": IssueCategory.CORRECTNESS,
        "evidence": "CSV import crashes when the header row is empty",
    }
    fields.update(overrides)
    return Issue(**fields)


def accepts(adapter: TypeAdapter, value: str) -> bool:
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


def run() -> tuple[int, int]:
    check = Checker("test_schemas")

    # -- version and identities ---------------------------------------------

    check.section("identities")

    check.equal("schema version", SCHEMA_VERSION, "0.2.0")

    cases = [
        ("SnapshotId", SnapshotId, ["it-000", "it-999"], ["it-1", "it-0000", "IT-001", ""]),
        ("BuildId", BuildId, ["b-000", "b-012"], ["b-1", "B-001", "build-001"]),
        (
            "RunId",
            RunId,
            ["2026-09-13T2244Z-8777"],
            ["2026-09-13T2244Z-877", "2026-09-13-2244Z-8777", "2026-09-13T2244Z-QQQQ"],
        ),
        ("Fingerprint", Fingerprint, ["12e10c9294a887b6"], ["12E10C9294A887B6", "abc", ""]),
        ("StateHash", StateHash, ["e7a87889b23bb605"], ["nothex0000000000"]),
        (
            "ProjectSlug",
            ProjectSlug,
            ["default", "go-svc", "a1"],
            ["My_App", "-lead", "trail-", "A", ""],
        ),
    ]
    for name, alias, good, bad in cases:
        adapter = TypeAdapter(alias)
        for value in good:
            check.truthy(f"{name} accepts {value!r}", accepts(adapter, value))
        for value in bad:
            check.falsy(f"{name} rejects {value!r}", accepts(adapter, value))

    check.equal(
        "a project's runs directory stays workspace-relative",
        default_runs_dir("go-svc"),
        "projects/go-svc/runs",
    )
    check.equal(
        "the registry filename is the documented one",
        REGISTRY_FILENAME,
        "saved-project-context.json",
    )
    check.equal("projects live under one directory", PROJECTS_DIRNAME, "projects")

    # -- enums ---------------------------------------------------------------

    check.section("enums")

    check.equal("ten pipeline stages", len(Stage), 10)
    for index, stage in enumerate(Stage):
        check.truthy(
            f"{stage.name} is numbered S{index}", stage.value.startswith(f"S{index}_")
        )
    check.equal("the last stage is shipped", Stage.SHIPPED.value, "S9_SHIPPED")

    check.equal("seven agent roles", len(AgentRole), 7)
    check.equal("six pipeline statuses", len(PipelineStatus), 6)
    check.equal("three gate decisions", len(GateDecision), 3)
    check.equal(
        "the gate can pass, loop, or escalate",
        {decision.value for decision in GateDecision},
        {"pass", "loop", "escalate"},
    )
    check.equal("four severities", len(Severity), 4)
    check.equal("four issue states", len(IssueState), 4)
    check.equal("five issue categories", len(IssueCategory), 5)
    check.equal("ten stacks", len(Stack), 10)
    check.equal("five project types", len(ProjectType), 5)
    check.equal("two preview modes", len(PreviewMode), 2)
    check.equal("five project statuses", len(ProjectStatus), 5)

    check.equal(
        "precedence covers every category", set(PRECEDENCE_ORDER), set(IssueCategory)
    )
    check.equal(
        "precedence lists each category once",
        len(PRECEDENCE_ORDER),
        len(set(PRECEDENCE_ORDER)),
    )
    check.equal(
        "correctness outranks polish",
        PRECEDENCE_ORDER.index(IssueCategory.CORRECTNESS)
        < PRECEDENCE_ORDER.index(IssueCategory.UX_POLISH),
        True,
    )
    check.contains("only blockers block", BLOCKING_SEVERITIES, Severity.BLOCKER)
    check.excludes("a nit never blocks", BLOCKING_SEVERITIES, Severity.NIT)

    # -- the operator's closed vocabulary -------------------------------------

    check.section("operator vocabulary")

    expected_replies = {
        "INVALID_COMMAND": "invalid command",
        "PIPELINE_BUSY": "pipeline busy",
        "NO_BUILD_AWAITING_REVIEW": "no build awaiting review",
        "NO_STATE": "no state",
        "NEEDS_HUMAN": "needs human",
        "READY_FOR_REVIEW": "ready for review",
        "NO_SUCH_PROJECT": "no such project",
        "PROJECT_EXISTS": "project exists",
    }
    for name, text in expected_replies.items():
        check.equal(f"CliResponse.{name}", getattr(CliResponse, name).value, text)
    check.equal("eight rejection reasons", len(ReasonCode), 8)
    check.equal(
        "rejection reasons are the documented set",
        {code.name for code in ReasonCode},
        {
            "E_SIGIL",
            "E_VERB",
            "E_ARITY",
            "E_TARGET",
            "E_TARGET_SCOPE",
            "E_EMPTY_ARG",
            "E_TRAILING",
            "E_VALUE",
        },
    )

    # -- fingerprints ----------------------------------------------------------

    check.section("fingerprints")

    base = compute_fingerprint(
        "src/import.py",
        IssueCategory.CORRECTNESS,
        "CSV import crashes when the header row is empty",
    )
    check.truthy("a fingerprint is 16 hex characters", bool(HEX16.match(base)))
    check.equal(
        "the same defect hashes the same twice",
        compute_fingerprint(
            "src/import.py",
            IssueCategory.CORRECTNESS,
            "CSV import crashes when the header row is empty",
        ),
        base,
    )
    check.equal(
        "trailing punctuation does not create a new defect",
        compute_fingerprint(
            "src/import.py",
            IssueCategory.CORRECTNESS,
            "CSV import crashes when the header row is empty.",
        ),
        base,
    )
    check.equal(
        "whitespace drift does not create a new defect",
        compute_fingerprint(
            "src/import.py",
            IssueCategory.CORRECTNESS,
            "  CSV   import crashes\n  when the header row is empty  ",
        ),
        base,
    )
    check.equal(
        "path case does not create a new defect",
        compute_fingerprint(
            "SRC/Import.py",
            IssueCategory.CORRECTNESS,
            "CSV import crashes when the header row is empty",
        ),
        base,
    )
    check.check(
        "a different category is a different defect",
        compute_fingerprint(
            "src/import.py",
            IssueCategory.SECURITY,
            "CSV import crashes when the header row is empty",
        )
        != base,
        "security and correctness collided",
    )
    check.check(
        "a different file is a different defect",
        compute_fingerprint(
            "src/export.py",
            IssueCategory.CORRECTNESS,
            "CSV import crashes when the header row is empty",
        )
        != base,
        "two files collided",
    )
    check.equal(
        "a fileless defect is scoped to <global>",
        compute_fingerprint(None, IssueCategory.SECURITY, "no CSP header is set"),
        compute_fingerprint("<global>", IssueCategory.SECURITY, "no CSP header is set"),
    )
    check.equal(
        "a category passed as a bare string hashes identically",
        compute_fingerprint("src/import.py", "correctness", "CSV import crashes when the header row is empty"),
        base,
    )

    # -- issues -----------------------------------------------------------------

    check.section("issues")

    derived = issue(file_path="src/import.py")
    check.equal("an issue derives its own fingerprint", derived.fingerprint, base)
    forged = issue(file_path="src/import.py", fingerprint="0000000000000000")
    check.equal(
        "a supplied fingerprint is ignored, not trusted", forged.fingerprint, base
    )
    check.truthy("a blocker blocks", issue().is_blocking)
    check.falsy("a minor does not block", issue(severity=Severity.MINOR).is_blocking)
    check.equal(
        "precedence follows the category order",
        issue(category=IssueCategory.UX_POLISH).precedence,
        PRECEDENCE_ORDER.index(IssueCategory.UX_POLISH),
    )
    check.equal("a new issue starts open", issue().state, IssueState.OPEN)
    check.equal("a new issue has been seen once", issue().repeat_count, 1)
    check.raises(
        "an issue with no evidence is refused",
        ValidationError,
        lambda: issue(evidence=""),
    )
    check.raises(
        "an issue id must be well formed",
        ValidationError,
        lambda: issue(issue_id="QC-1"),
    )

    # -- budgets and rules --------------------------------------------------------

    check.section("budgets and rules")

    check.falsy("a fresh budget is not exhausted", Budgets().exhausted)
    check.truthy(
        "the iteration ceiling exhausts the budget",
        Budgets(iterations_used=3).exhausted,
    )
    check.truthy(
        "the token ceiling exhausts the budget", Budgets(tokens_used=250_000).exhausted
    )
    check.truthy(
        "the clock exhausts the budget",
        Budgets(wall_clock_seconds_used=900.0).exhausted,
    )
    check.falsy(
        "an unset cost ceiling never exhausts",
        Budgets(cost_usd_used=999.0).exhausted,
    )
    check.truthy(
        "a set cost ceiling does exhaust",
        Budgets(cost_usd_used=1.0, max_cost_usd=1.0).exhausted,
    )
    check.equal("default iteration ceiling", Budgets().max_iterations, 3)
    check.equal("default token ceiling", Budgets().max_tokens, 250_000)
    check.equal("at most five active rules", RuleSet().max_active, 5)
    check.equal("a fresh ruleset is empty", RuleSet().active, [])

    # -- ownership ------------------------------------------------------------------

    check.section("state ownership")

    check.equal("every role owns a region", set(OWNED_REGIONS), set(AgentRole))
    check.equal(
        "the commander owns only the pipeline block",
        set(OWNED_REGIONS[AgentRole.COMMANDER]),
        {"pipeline"},
    )
    check.equal(
        "the observer owns the spec and the memory",
        set(OWNED_REGIONS[AgentRole.OBSERVER]),
        {"spec", "memory"},
    )
    check.equal(
        "each critic owns only its own result",
        (
            set(OWNED_REGIONS[AgentRole.QC]),
            set(OWNED_REGIONS[AgentRole.DESIGN]),
        ),
        ({"review.qc_result"}, {"review.design_result"}),
    )

    owned: list[str] = []
    for regions in OWNED_REGIONS.values():
        owned.extend(regions)
    check.equal(
        "no region has two owners", len(owned), len(set(owned))
    )
    check.equal(
        "agents never own what the state manager writes",
        sorted(set(owned) & set(STATE_MANAGER_REGIONS)),
        [],
    )
    for region in ("meta", "artifacts", "budgets", "review.open_issues"):
        check.contains(
            f"the state manager owns {region}", STATE_MANAGER_REGIONS, region
        )

    # -- stack policy -------------------------------------------------------------------

    check.section("stack policy")

    check.equal("every stack has a profile", set(STACK_PROFILES), set(Stack))
    for stack, profile in STACK_PROFILES.items():
        check.equal(f"{stack.value} profile is self-consistent", profile.stack, stack)
        check.truthy(
            f"{stack.value} inherits the common suffixes",
            COMMON_SUFFIXES <= profile.allowed_suffixes,
        )

    check.equal(
        "only the web stack is previewable in an iframe",
        {stack for stack, profile in STACK_PROFILES.items() if profile.preview is PreviewMode.IFRAME},
        {Stack.WEB},
    )
    for stack in Stack:
        if stack is Stack.WEB:
            continue
        check.equal(
            f"{stack.value} is read as source, not rendered",
            STACK_PROFILES[stack].preview,
            PreviewMode.SOURCE,
        )

    web = STACK_PROFILES[Stack.WEB]
    go = STACK_PROFILES[Stack.GO]
    check.truthy("the web stack may write index.html", web.permits("index.html"))
    check.truthy("the web stack may write a stylesheet", web.permits("styles.css"))
    check.falsy("the web stack may not write a go file", web.permits("main.go"))
    check.truthy("the go stack may write main.go", go.permits("main.go"))
    check.truthy("the go stack may write go.mod", go.permits("go.mod"))
    check.falsy("the go stack may not write index.html", go.permits("index.html"))
    check.truthy("every stack may write a readme", go.permits("README.md"))
    check.truthy(
        "the web stack prefers an entrypoint", bool(web.entrypoint_preference)
    )

    return check.report()


if __name__ == "__main__":
    passed, total = run()
    raise SystemExit(0 if passed == total else 1)
