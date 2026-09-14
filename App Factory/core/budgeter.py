"""App Factory - the budgeter (G2) and context throttling.

Two jobs:

  1. Enforce the triple budget. Iterations, tokens, and wall clock each have a
     ceiling. Any single breach escalates to the operator; none of them
     triggers a retry, because retrying a budget failure is how a run turns
     into a bill.

  2. Trim each projection to the per-agent cap before the call, in a fixed
     drop order. Trimming before the call is deliberate: letting the provider
     truncate the reply loses the end of a payload, which is exactly where
     JSON closes.

Token estimation is a deterministic characters/4 heuristic. It is used for
trimming decisions only; charged spend always comes from provider-reported
usage.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .config import BudgetLimits, ContextLimits
from .schemas.common import AgentRole, ContextAction, Telemetry
from .schemas.state import Budgets, ContextActionRecord

CHARS_PER_TOKEN = 4

#: The fixed drop order under context pressure. Nits first because they are
#: preferences; architecture detail last because losing it causes the
#: implementer to re-derive decisions and drift.
DROP_ORDER: tuple[str, ...] = (
    "nits",
    "resolved_issues",
    "old_digest",
    "architecture_detail",
)


class BudgetExhausted(RuntimeError):
    """Raised pre-call when a ceiling is already breached."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class TrimResult:
    projection: dict[str, Any]
    tokens_before: int
    tokens_after: int
    dropped: list[str]

    @property
    def action(self) -> ContextAction:
        if not self.dropped:
            return ContextAction.NONE
        if "old_digest" in self.dropped or "architecture_detail" in self.dropped:
            return ContextAction.TRUNCATED
        return ContextAction.SUMMARIZED


def estimate_tokens(value: Any) -> int:
    """Deterministic token estimate for a projection or string."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True, default=str)
    return max(1, len(text) // CHARS_PER_TOKEN)


class Budgeter:
    def __init__(self, limits: BudgetLimits, context: ContextLimits) -> None:
        self.limits = limits
        self.context = context
        self._started_at: float | None = None

    # -- wall clock --------------------------------------------------------

    def start(self) -> None:
        self._started_at = time.monotonic()

    @property
    def started(self) -> bool:
        return self._started_at is not None

    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.monotonic() - self._started_at

    # -- ledger ------------------------------------------------------------

    def initial(self) -> Budgets:
        """A fresh ledger seeded from the configured ceilings."""
        return Budgets(
            max_iterations=self.limits.max_iterations_per_build,
            max_tokens=self.limits.max_tokens_per_build,
            max_wall_clock_seconds=self.limits.max_wall_clock_seconds,
            max_cost_usd=self.limits.max_cost_usd,
        )

    def sync_clock(self, budgets: Budgets) -> Budgets:
        return budgets.model_copy(
            update={"wall_clock_seconds_used": round(self.elapsed(), 3)}
        )

    def charge(self, budgets: Budgets, telemetry: Telemetry) -> Budgets:
        return budgets.model_copy(
            update={
                "tokens_used": budgets.tokens_used + telemetry.total_tokens,
                "cost_usd_used": round(
                    budgets.cost_usd_used + telemetry.cost_usd, 6
                ),
                "wall_clock_seconds_used": round(self.elapsed(), 3),
            }
        )

    def tick_iteration(self, budgets: Budgets) -> Budgets:
        return budgets.model_copy(
            update={"iterations_used": budgets.iterations_used + 1}
        )

    def iterations_remaining(self, budgets: Budgets) -> int:
        return max(0, budgets.max_iterations - budgets.iterations_used)

    # -- G2 ----------------------------------------------------------------

    def breach(self, budgets: Budgets) -> str | None:
        """Return a one-line reason if any ceiling is breached, else None.

        The reason string is what the operator sees after `needs human:`, so
        it names the ceiling and the numbers.
        """
        current = self.sync_clock(budgets)
        if current.iterations_used >= current.max_iterations:
            return (
                f"iteration ceiling reached "
                f"({current.iterations_used}/{current.max_iterations})"
            )
        if current.tokens_used >= current.max_tokens:
            return (
                f"token ceiling reached "
                f"({current.tokens_used}/{current.max_tokens})"
            )
        if current.wall_clock_seconds_used >= current.max_wall_clock_seconds:
            return (
                f"wall clock ceiling reached "
                f"({current.wall_clock_seconds_used:.0f}s/"
                f"{current.max_wall_clock_seconds:.0f}s)"
            )
        if (
            current.max_cost_usd is not None
            and current.cost_usd_used >= current.max_cost_usd
        ):
            return (
                f"cost ceiling reached "
                f"(${current.cost_usd_used:.2f}/${current.max_cost_usd:.2f})"
            )
        return None

    def assert_within_budget(self, budgets: Budgets) -> None:
        reason = self.breach(budgets)
        if reason is not None:
            raise BudgetExhausted(reason)

    # -- context throttling ------------------------------------------------

    def trim(self, projection: dict[str, Any]) -> TrimResult:
        """Trim a projection to the per-agent cap, in the fixed drop order."""
        cap = self.context.per_agent_token_cap
        working = json.loads(json.dumps(projection, default=str))
        before = estimate_tokens(working)
        dropped: list[str] = []

        if before <= cap:
            return TrimResult(working, before, before, dropped)

        for step in DROP_ORDER:
            if estimate_tokens(working) <= cap:
                break
            if _apply_drop(working, step):
                dropped.append(step)

        after = estimate_tokens(working)
        if after > cap:
            # Last resort: the task itself is too large. Truncate the artifact
            # sources, which is the only remaining unbounded field, and record
            # it honestly rather than silently overflowing the call.
            if _truncate_sources(working, cap):
                dropped.append("source_truncation")
                after = estimate_tokens(working)

        return TrimResult(working, before, after, dropped)

    def record(
        self, result: TrimResult, *, role: AgentRole, iteration: int
    ) -> ContextActionRecord | None:
        """Build the memory record for a trim.

        The budgeter does not write it: `memory` belongs to the Observer (G8),
        so the pipeline hands these to the Observer to commit.
        """
        if not result.dropped:
            return None
        return ContextActionRecord(
            iteration=iteration,
            agent=role,
            action=result.action,
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            dropped=list(result.dropped),
        )


# --------------------------------------------------------------------------
# Drop steps
# --------------------------------------------------------------------------


def _apply_drop(projection: dict[str, Any], step: str) -> bool:
    """Apply one drop step in place. Returns True if anything was removed."""
    if step == "nits":
        return _drop_issues_by_severity(projection, {"nit"})
    if step == "resolved_issues":
        return _drop_keys(projection, ("resolved_issues", "accepted_debt"))
    if step == "old_digest":
        return _drop_keys(projection, ("digest",))
    if step == "architecture_detail":
        removed = _drop_keys(projection, ("architecture",))
        task = projection.get("task")
        if isinstance(task, dict):
            for key in ("components", "dependencies", "deviations"):
                if task.pop(key, None) is not None:
                    removed = True
        return removed
    return False


def _walk(node: Any) -> Any:
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _drop_issues_by_severity(projection: dict[str, Any], severities: set[str]) -> bool:
    removed = False
    for node in _walk(projection):
        if not isinstance(node, dict):
            continue
        for key, value in list(node.items()):
            if not isinstance(value, list):
                continue
            kept = [
                item
                for item in value
                if not (isinstance(item, dict) and item.get("severity") in severities)
            ]
            if len(kept) != len(value):
                node[key] = kept
                removed = True
    return removed


def _drop_keys(projection: dict[str, Any], keys: tuple[str, ...]) -> bool:
    removed = False
    for node in _walk(projection):
        if not isinstance(node, dict):
            continue
        for key in keys:
            if key in node and node[key] not in (None, [], {}):
                node[key] = None if not isinstance(node[key], list) else []
                removed = True
    return removed


def _truncate_sources(projection: dict[str, Any], cap: int) -> bool:
    """Shrink artifact source bodies until the projection fits."""
    task = projection.get("task")
    if not isinstance(task, dict):
        return False
    sources = task.get("sources")
    if not isinstance(sources, dict) or not sources:
        return False

    changed = False
    limit = 4000
    while estimate_tokens(projection) > cap and limit >= 250:
        for path, body in list(sources.items()):
            if isinstance(body, str) and len(body) > limit:
                sources[path] = body[:limit] + "\n... [truncated by budgeter]"
                changed = True
        limit //= 2
    return changed