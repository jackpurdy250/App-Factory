"""App Factory - the governor.

Every rule that guarantees termination lives here, and every one of them is
plain Python. No LLM participates in a gate decision.

  G1  No debate channel          topological; enforced in projection.py
  G2  Triple budget              budgeter.py, checked pre-call and at the gate
  G3  Severity gate              only blocking severities hold a build
  G4  Monotonic progress         the blocker fingerprint set must strictly shrink
  G5  Repeat-offender cap        a twice-seen fingerprint becomes a human decision
  G6  Deterministic precedence   fixed category order, never argued
  G7  Rule decay                 capped, TTL-bound, each citing an origin issue
  G8  Single writer + ACLs       state_manager.py and artifacts.py

The order of checks in `evaluate_gate` is itself part of the contract: budget
before progress, progress before iteration count. A run that is out of budget
and also making no progress should report the budget, because that is the
ceiling the operator has to decide about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .config import GateLimits, RuleLimits
from .schemas.common import (
    PRECEDENCE_ORDER,
    SEVERITY_RANK,
    GateDecision,
    Issue,
    IssueState,
    Severity,
)
from .schemas.state import ProjectState, Rule, RuleSet
from .schemas.envelope import RuleDraft


@dataclass(slots=True)
class GateResult:
    decision: GateDecision
    reason: str
    rule: str
    escalations: tuple[str, ...] = ()
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.decision is GateDecision.PASS


# --------------------------------------------------------------------------
# G6 - precedence
# --------------------------------------------------------------------------


def sort_by_precedence(issues: Iterable[Issue]) -> list[Issue]:
    """Deterministic ordering: severity first, then the fixed category order,
    then issue id as a stable tiebreak.

    A fixed tiebreak is worth more than a smart one, because a smart one can
    change its mind next iteration.
    """
    return sorted(
        issues,
        key=lambda issue: (
            -SEVERITY_RANK[issue.severity],
            PRECEDENCE_ORDER.index(issue.category),
            issue.issue_id,
        ),
    )


# --------------------------------------------------------------------------
# Fingerprint merge (G5 input)
# --------------------------------------------------------------------------


def merge_issues(
    *critic_issues: Sequence[Issue],
    iteration: int,
    previous: Sequence[Issue] = (),
) -> list[Issue]:
    """Collapse every critic's findings into one issue set, keyed by
    fingerprint.

    When two critics flag one problem, the higher severity wins and the other
    id is recorded in `merged_from`, so the operator can still see that both
    found it. Repeat counts carry forward from the previous iteration, which
    is what makes G5 measurable.
    """
    history = {issue.fingerprint: issue for issue in previous}
    groups: dict[str, list[Issue]] = {}
    for batch in critic_issues:
        for issue in batch:
            groups.setdefault(issue.fingerprint, []).append(issue)

    merged: list[Issue] = []
    for fingerprint, group in groups.items():
        primary = max(group, key=lambda issue: SEVERITY_RANK[issue.severity])
        others = [issue.issue_id for issue in group if issue.issue_id != primary.issue_id]

        prior = history.get(fingerprint)
        if prior is not None:
            first_seen = prior.first_seen_iteration
            repeat_count = prior.repeat_count + 1
        else:
            first_seen = iteration
            repeat_count = 1

        merged.append(
            primary.model_copy(
                update={
                    "merged_from": sorted(set(others) | set(primary.merged_from)),
                    "first_seen_iteration": first_seen,
                    "repeat_count": repeat_count,
                    "state": IssueState.OPEN,
                }
            )
        )
    return sort_by_precedence(merged)


def resolved_since(
    previous: Sequence[Issue], current: Sequence[Issue]
) -> list[Issue]:
    """Issues present last iteration and absent now. Recorded, not discarded:
    a resolved issue is evidence that the loop is working."""
    live = {issue.fingerprint for issue in current}
    return [
        issue.model_copy(update={"state": IssueState.RESOLVED})
        for issue in previous
        if issue.fingerprint not in live
    ]


def blocking_issues(issues: Iterable[Issue], gates: GateLimits) -> list[Issue]:
    return [issue for issue in issues if issue.severity in gates.blocking_severities]


def fingerprints(issues: Iterable[Issue]) -> list[str]:
    """Sorted for determinism: this list is compared set-wise by G4 and is
    persisted, so a stable order keeps state hashes stable."""
    return sorted({issue.fingerprint for issue in issues})


# --------------------------------------------------------------------------
# Majors become debt
# --------------------------------------------------------------------------


def partition_debt(
    issues: Sequence[Issue], *, gates: GateLimits
) -> tuple[list[Issue], list[Issue]]:
    """Split issues into (still open, newly accepted debt).

    A major gets `major_fix_attempts` attempts and then converts to accepted
    debt. This is the single rule that kills most perfectionism spirals: only a
    blocking severity can hold a build indefinitely, and everything else ends
    up on a list the operator can read with `!issues`.
    """
    still_open: list[Issue] = []
    debt: list[Issue] = []
    for issue in issues:
        if issue.severity in gates.blocking_severities:
            still_open.append(issue)
            continue
        if issue.severity is Severity.MAJOR:
            if issue.repeat_count > gates.major_fix_attempts:
                debt.append(issue.model_copy(update={"state": IssueState.ACCEPTED_DEBT}))
            else:
                still_open.append(issue)
            continue
        still_open.append(issue)
    return still_open, debt


def repeat_offenders(issues: Sequence[Issue], *, gates: GateLimits) -> list[Issue]:
    """G5. Blocking issues that have survived `repeat_offender_limit` fix
    attempts.

    `repeat_count` counts sightings, and the first sighting precedes any fix,
    so failed attempts are one less than sightings: an issue seen twice has
    been attempted once. Counting attempts rather than sightings is what stops
    this gate from escalating before the implementer has had the tries the
    operator budgeted for it.
    """
    return [
        issue
        for issue in issues
        if issue.severity in gates.blocking_severities
        and issue.repeat_count - 1 >= gates.repeat_offender_limit
    ]


# --------------------------------------------------------------------------
# S6 - the gate
# --------------------------------------------------------------------------


def evaluate_gate(
    state: ProjectState,
    *,
    gates: GateLimits,
    budget_reason: str | None,
    iterations_remaining: int,
    design_overall: float | None,
) -> GateResult:
    """Pass, loop, or escalate. Deterministic and total."""
    review = state.review
    iteration = state.pipeline.iteration
    blockers = blocking_issues(review.open_issues, gates)

    # G2 first: a ceiling is the operator's decision, not the pipeline's.
    if budget_reason is not None:
        return GateResult(
            decision=GateDecision.ESCALATE,
            reason=budget_reason,
            rule="G2",
            detail={"blockers": len(blockers)},
        )

    # G5: an identical third attempt is not worth paying for.
    offenders = repeat_offenders(review.open_issues, gates=gates)
    if offenders:
        return GateResult(
            decision=GateDecision.ESCALATE,
            reason=(
                f"repeat offender after {offenders[0].repeat_count - 1} fix "
                f"attempt(s): {offenders[0].evidence[:120]}"
            ),
            rule="G5",
            escalations=tuple(issue.issue_id for issue in offenders),
            detail={
                "fingerprints": [issue.fingerprint for issue in offenders],
                "limit": gates.repeat_offender_limit,
                "attempts": offenders[0].repeat_count - 1,
            },
        )

    # G3 + G4.
    if blockers:
        if not review.made_progress:
            return GateResult(
                decision=GateDecision.ESCALATE,
                reason=(
                    f"no progress: blocker set did not shrink "
                    f"({len(review.previous_blocker_fingerprints)} "
                    f"-> {len(review.blocker_fingerprints)})"
                ),
                rule="G4",
                escalations=tuple(issue.issue_id for issue in blockers),
                detail={
                    "before": list(review.previous_blocker_fingerprints),
                    "after": list(review.blocker_fingerprints),
                },
            )
        if iterations_remaining <= 0:
            return GateResult(
                decision=GateDecision.ESCALATE,
                reason=(
                    f"{len(blockers)} blocker(s) remain with no iterations left"
                ),
                rule="G2",
                escalations=tuple(issue.issue_id for issue in blockers),
            )
        return GateResult(
            decision=GateDecision.LOOP,
            reason=f"{len(blockers)} blocker(s) open",
            rule="G3",
            detail={"blockers": [issue.issue_id for issue in blockers]},
        )

    # Design polish. Bounded by the same attempt ceiling as majors, because
    # open-ended polish has no fixed point.
    if design_overall is not None and design_overall < gates.design_pass_threshold:
        if iteration < gates.major_fix_attempts and iterations_remaining > 0:
            return GateResult(
                decision=GateDecision.LOOP,
                reason=(
                    f"design rubric {design_overall:.2f} below threshold "
                    f"{gates.design_pass_threshold:.2f}"
                ),
                rule="G3",
                detail={"rubric": design_overall},
            )
        return GateResult(
            decision=GateDecision.PASS,
            reason=(
                f"design rubric {design_overall:.2f} below threshold "
                f"{gates.design_pass_threshold:.2f}; polish attempts spent, "
                "recorded as debt"
            ),
            rule="G3",
            detail={"rubric": design_overall, "polish_debt": True},
        )

    return GateResult(
        decision=GateDecision.PASS,
        reason="no blocking issues",
        rule="G3",
        detail={"rubric": design_overall},
    )


# --------------------------------------------------------------------------
# G7 - rule decay
# --------------------------------------------------------------------------


def promote_rules(
    ruleset: RuleSet,
    drafts: Sequence[RuleDraft],
    *,
    iteration: int,
    limits: RuleLimits,
    known_issue_ids: Iterable[str],
) -> RuleSet:
    """Turn Prompt Engineer drafts into active rules, then decay.

    Uncapped corrective rules are the hidden infinite loop: the set grows, the
    Implementer drifts under instruction weight, old requirements quietly
    break, and new issues appear that look unrelated to anything.

    Three constraints, all enforced here:
      - every rule cites an origin issue that actually exists;
      - duplicate rule text is not added twice;
      - the active set never exceeds `max_active`, oldest retired first.
    """
    known = set(known_issue_ids)
    existing_text = {rule.rule_text.strip().lower() for rule in ruleset.active}
    active = list(ruleset.active)
    retired = list(ruleset.retired)

    # Expire anything past its TTL before admitting new rules.
    still_active: list[Rule] = []
    for rule in active:
        if rule.expires_iteration <= iteration:
            retired.append(rule.model_copy(update={"active": False}))
        else:
            still_active.append(rule)
    active = still_active

    next_index = 1 + max(
        [int(rule.rule_id.split("-")[1]) for rule in (*active, *retired)] or [0]
    )

    for draft in drafts:
        if draft.origin_issue not in known:
            # G7: an orphan rule cannot be audited or expired against
            # anything, so it is refused rather than quietly kept.
            continue
        text_key = draft.rule_text.strip().lower()
        if text_key in existing_text:
            continue
        if next_index > 999:
            break
        active.append(
            Rule(
                rule_id=f"PE-{next_index:03d}",
                rule_text=draft.rule_text,
                origin_issue=draft.origin_issue,
                scope=draft.scope,
                created_iteration=iteration,
                expires_iteration=iteration + (draft.ttl_iterations or limits.default_ttl),
                active=True,
            )
        )
        existing_text.add(text_key)
        next_index += 1

    # Cap the active set: oldest first, because the newest rule is the one
    # addressing the defect that is currently blocking.
    max_active = min(limits.max_active, ruleset.max_active)
    if len(active) > max_active:
        active.sort(key=lambda rule: (rule.created_iteration, rule.rule_id))
        overflow = active[: len(active) - max_active]
        active = active[len(active) - max_active :]
        retired.extend(rule.model_copy(update={"active": False}) for rule in overflow)

    return RuleSet(max_active=ruleset.max_active, active=active, retired=retired)