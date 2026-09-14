"""App Factory - projections.

An agent never receives project_state.json. It receives a projection: a
trimmed, role-specific slice, assembled here.

Two rules are enforced structurally rather than by prompt:

  G1 (no debate channel). The QC projection cannot contain the Design
     Critic's output and vice versa. There is no edge for a debate to travel
     on, so neither critic can rebut the other, and `assert_critic_isolation`
     fails loudly if a future change introduces one.

  Code stays out of the bus. Reviewers need source, and source is
     deliberately absent from state, so it is read back from disk at
     projection time.

Tiering follows the manual: `pinned` (spec and active rules, never dropped),
`recent` (verbatim), `digest` (rolling summary).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import read_build_sources
from .config import ContextLimits
from .schemas.common import (
    RUBRIC_INTERPRETATION,
    AgentRole,
    Severity,
    Stack,
    profile_for,
)
from .schemas.state import ProjectState

#: Files the Design Critic is shown for a web build. It reviews interface,
#: not algorithms.
UI_SUFFIXES = frozenset({".html", ".htm", ".css", ".svg"})


def design_surfaces(stack: Stack | str) -> frozenset[str] | None:
    """Which artifact files the Design Critic is shown.

    For web, markup and styling only. Every other stack has no separate
    interface layer, so the critic reads the same source QC does and the
    rubric axes are re-read per stack instead (see RUBRIC_INTERPRETATION).
    """
    return UI_SUFFIXES if Stack(stack) is Stack.WEB else None


class ProjectionError(RuntimeError):
    """Raised when a projection would violate an isolation rule."""


def _pinned(state: ProjectState) -> dict[str, Any]:
    """Never droppable: the spec and the active rules."""
    return {
        "spec": state.spec.model_dump(mode="json"),
        "rules": [
            {
                "rule_id": rule.rule_id,
                "rule_text": rule.rule_text,
                "origin_issue": rule.origin_issue,
                "expires_iteration": rule.expires_iteration,
            }
            for rule in state.rules.active
            if rule.active
        ],
    }


def _recent(state: ProjectState, limits: ContextLimits) -> dict[str, Any]:
    """The last N iterations, verbatim."""
    cutoff = max(0, state.pipeline.iteration - limits.recent_iterations_verbatim)
    return {
        "iterations": [
            transition.model_dump(mode="json")
            for transition in state.pipeline.stage_history
            if transition.iteration >= cutoff
        ][-24:],
        "open_issues": [
            issue.model_dump(mode="json") for issue in state.review.open_issues
        ],
        "accepted_debt": [
            issue.model_dump(mode="json") for issue in state.review.accepted_debt
        ],
        "resolved_issues": [
            issue.model_dump(mode="json") for issue in state.review.resolved_issues
        ],
    }


def base_projection(
    state: ProjectState,
    *,
    role: AgentRole,
    stage: str,
    limits: ContextLimits,
    operator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": state.pipeline.run_id,
        "iteration": state.pipeline.iteration,
        "stage": stage,
        "role": role.value,
        "state_hash": state.compute_state_hash(),
        "project": {
            "slug": state.meta.project_slug,
            "type": state.meta.project_type.value,
            "stack": state.meta.stack.value,
        },
        "pinned": _pinned(state),
        "recent": _recent(state, limits),
        "digest": state.memory.digest,
        "operator": operator or {},
        "task": {},
    }


def optimizer_projection(
    state: ProjectState, *, limits: ContextLimits, operator: dict[str, Any] | None = None
) -> dict[str, Any]:
    projection = base_projection(
        state, role=AgentRole.OPTIMIZER, stage="S1_OPTIMIZE", limits=limits,
        operator=operator,
    )
    projection["task"] = {
        "kind": "optimize",
        "raw_input": state.intent.raw_input,
        "previous_prompt": state.intent.optimized_prompt,
        "unresolved_ambiguities": [
            item.model_dump(mode="json")
            for item in state.intent.ambiguities
            if not item.resolved
        ],
    }
    return projection


def observer_spec_projection(
    state: ProjectState, *, limits: ContextLimits, operator: dict[str, Any] | None = None
) -> dict[str, Any]:
    projection = base_projection(
        state, role=AgentRole.OBSERVER, stage="S2_SPEC", limits=limits,
        operator=operator,
    )
    projection["task"] = {
        "kind": "spec",
        "raw_input": state.intent.raw_input,
        "optimized_prompt": state.intent.optimized_prompt,
        "inferred_requirements": list(state.intent.inferred_requirements),
        "ambiguities": [
            item.model_dump(mode="json") for item in state.intent.ambiguities
        ],
        "previous_spec_version": state.spec.spec_version,
    }
    return projection


def implementer_projection(
    state: ProjectState,
    *,
    limits: ContextLimits,
    build_dir: Path | None = None,
    operator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = base_projection(
        state, role=AgentRole.IMPLEMENTER, stage="S3_BUILD", limits=limits,
        operator=operator,
    )
    # The implementer sees only issues it can act on: blockers and majors.
    # Minors and nits are logged debt, not build instructions.
    actionable = [
        issue.model_dump(mode="json")
        for issue in state.review.open_issues
        if issue.severity in (Severity.BLOCKER, Severity.MAJOR)
    ]
    sources: dict[str, str] = {}
    if build_dir is not None and state.artifacts.files:
        sources = read_build_sources(build_dir, list(state.artifacts.files))

    profile = profile_for(state.meta.stack)
    projection["task"] = {
        "kind": "build",
        "optimized_prompt": state.intent.optimized_prompt,
        "issues_to_fix": actionable,
        "previous_entrypoint": state.artifacts.entrypoint,
        "previous_files": [item.path for item in state.artifacts.files],
        "sources": sources,
        "stack": state.meta.stack.value,
        "project_type": state.meta.project_type.value,
        "language_conventions": list(state.spec.language_conventions),
        "allowed_suffixes": sorted(profile.allowed_suffixes),
        "entrypoint_preference": list(profile.entrypoint_preference),
        "entrypoint_required": profile.entrypoint_required,
        "components": [c.model_dump(mode="json") for c in state.architecture.components],
        "dependencies": [
            d.model_dump(mode="json") for d in state.architecture.dependencies
        ],
    }
    return projection


def qc_projection(
    state: ProjectState,
    *,
    limits: ContextLimits,
    build_dir: Path,
    operator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = base_projection(
        state, role=AgentRole.QC, stage="S4_REVIEW", limits=limits, operator=operator
    )
    projection["task"] = {
        "kind": "qc",
        "requirements": [r.model_dump(mode="json") for r in state.spec.requirements],
        "acceptance_criteria": [
            ac.model_dump(mode="json") for ac in state.spec.acceptance_criteria
        ],
        "out_of_scope": list(state.spec.out_of_scope),
        "stack": state.meta.stack.value,
        "language_conventions": list(state.spec.language_conventions),
        "entrypoint": state.artifacts.entrypoint,
        "sources": read_build_sources(build_dir, list(state.artifacts.files)),
    }
    # The shared `recent` tier is assembled for every role and therefore
    # carries both critics' issues from earlier iterations. Strip the other
    # critic's findings first, then prove G1 on what is actually being sent.
    projection = strip_cross_critic_issues(projection, AgentRole.QC)
    assert_critic_isolation(projection, AgentRole.QC)
    return projection


def design_projection(
    state: ProjectState,
    *,
    limits: ContextLimits,
    build_dir: Path,
    operator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = base_projection(
        state, role=AgentRole.DESIGN, stage="S4_REVIEW", limits=limits,
        operator=operator,
    )
    projection["task"] = {
        "kind": "design",
        "goals": list(state.spec.goals),
        "stack": state.meta.stack.value,
        "project_type": state.meta.project_type.value,
        "rubric": dict(RUBRIC_INTERPRETATION),
        "entrypoint": state.artifacts.entrypoint,
        "sources": read_build_sources(
            build_dir,
            list(state.artifacts.files),
            suffixes=design_surfaces(state.meta.stack),
        ),
    }
    projection = strip_cross_critic_issues(projection, AgentRole.DESIGN)
    assert_critic_isolation(projection, AgentRole.DESIGN)
    return projection


def adjudication_projection(
    state: ProjectState,
    *,
    limits: ContextLimits,
    merged: list[dict[str, Any]],
    operator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """S5. The Observer is the only agent that sees both critic reports.

    It adjudicates; it does not referee a debate. The merge, the dedupe, and
    the precedence order are already computed in Python and passed in as
    `merged`, which is authoritative. The Observer's own `merged_issues` echo
    is advisory and is discarded by the pipeline.
    """
    projection = base_projection(
        state, role=AgentRole.OBSERVER, stage="S5_ADJUDICATE", limits=limits,
        operator=operator,
    )
    qc = state.review.qc_result
    design = state.review.design_result
    projection["task"] = {
        "kind": "adjudication",
        "qc": qc.model_dump(mode="json") if qc else None,
        "design": design.model_dump(mode="json") if design else None,
        "merged": merged,
        "previous_blocker_fingerprints": list(
            state.review.previous_blocker_fingerprints
        ),
    }
    return projection


def prompt_engineer_projection(
    state: ProjectState,
    *,
    limits: ContextLimits,
    gate_reason: str,
    operator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = base_projection(
        state, role=AgentRole.PROMPT_ENGINEER, stage="S7_RULE_WRITE", limits=limits,
        operator=operator,
    )
    projection["task"] = {
        "kind": "rule_write",
        "gate_reason": gate_reason,
        "blockers": [
            issue.model_dump(mode="json") for issue in state.review.open_blockers
        ],
        "existing_rules": [
            rule.model_dump(mode="json") for rule in state.rules.active
        ],
        "max_active": state.rules.max_active,
    }
    return projection


def assert_critic_isolation(projection: dict[str, Any], role: AgentRole) -> None:
    """G1. A critic projection must not carry the other critic's findings."""
    if role not in (AgentRole.QC, AgentRole.DESIGN):
        return

    forbidden_key = "design" if role is AgentRole.QC else "qc"
    forbidden_agent = (
        AgentRole.DESIGN.value if role is AgentRole.QC else AgentRole.QC.value
    )

    task = projection.get("task", {})
    if isinstance(task, dict) and forbidden_key in task:
        raise ProjectionError(
            f"G1 violation: {role.value} projection contains "
            f"'{forbidden_key}' output"
        )

    recent = projection.get("recent", {})
    if isinstance(recent, dict):
        for key in ("open_issues", "accepted_debt", "resolved_issues"):
            for issue in recent.get(key) or []:
                if isinstance(issue, dict) and issue.get("raised_by") == forbidden_agent:
                    raise ProjectionError(
                        f"G1 violation: {role.value} projection exposes an issue "
                        f"raised by {forbidden_agent} via recent.{key}"
                    )


def strip_cross_critic_issues(
    projection: dict[str, Any], role: AgentRole
) -> dict[str, Any]:
    """Remove the other critic's issues from the shared `recent` tier.

    Called before `assert_critic_isolation`: the shared tier is assembled once
    for every role, and the critics are the two roles that must not see all of
    it.
    """
    if role not in (AgentRole.QC, AgentRole.DESIGN):
        return projection
    forbidden = AgentRole.DESIGN.value if role is AgentRole.QC else AgentRole.QC.value
    recent = projection.get("recent")
    if isinstance(recent, dict):
        for key in ("open_issues", "accepted_debt", "resolved_issues"):
            items = recent.get(key)
            if isinstance(items, list):
                recent[key] = [
                    item
                    for item in items
                    if not (isinstance(item, dict) and item.get("raised_by") == forbidden)
                ]
    return projection