"""Handlers for the `!` command family.

Every function in this module is local and deterministic. A `!` command never
reaches a model, never spends a token, and never mutates the state bus. The
two exceptions that do change something change only operator-facing settings:
`!verbose` moves the Window 3 threshold and `!reload` stages a config re-read.

Each handler returns a `StateReply`: one closed-vocabulary line for Window 1
and a structured payload for Window 3. Rendering is the caller's job, so this
module stays testable without a bus or a socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path, PurePath
from typing import Any

from .config import ConfigError, ConfigStore
from .events import EventBus
from .parser import REGISTRY, StateCommand, StateVerb
from .schemas import (
    OWNED_REGIONS,
    CliResponse,
    PipelineStatus,
    ProjectState,
    ProjectType,
    Stack,
    profile_for,
)
from .store import SnapshotStore
from .workspace import Workspace, WorkspaceError

#: How many recent log events `!log` returns.
LOG_TAIL = 40

#: Evidence strings are trimmed before they reach Window 3.
EVIDENCE_LIMIT = 240

#: Pipeline status -> the Window 1 word for `!status`.
_STATUS_WORDS: dict[PipelineStatus, CliResponse] = {
    PipelineStatus.IDLE: CliResponse.IDLE,
    PipelineStatus.RUNNING: CliResponse.PIPELINE_BUSY,
    PipelineStatus.AWAITING_REVIEW: CliResponse.READY_FOR_REVIEW,
    PipelineStatus.BLOCKED: CliResponse.NEEDS_HUMAN,
    PipelineStatus.NEEDS_HUMAN: CliResponse.NEEDS_HUMAN,
    PipelineStatus.SHIPPED: CliResponse.SHIPPED,
}


@dataclass(frozen=True, slots=True)
class StateReply:
    """What a `!` command produced."""

    cli: str
    label: str
    detail: dict[str, Any] | None = None
    ok: bool = True
    #: Written only by the `!stack` / `!type` setters. The router hands this
    #: to the state manager, the one writer allowed to touch `meta`.
    meta_update: dict[str, Any] | None = None


def _jsonable(value: Any) -> Any:
    """Convert state fragments into something a socket can carry."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (PurePath, Path)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return str(value)


def _region_value(state: ProjectState, dotted: str) -> Any:
    """Read one owned region, `review.qc_result` included."""

    value: Any = state
    for part in dotted.split("."):
        value = getattr(value, part)
    return _jsonable(value)


def _trim(text: str | None, limit: int = EVIDENCE_LIMIT) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "\u2026"


def _active_record(workspace: Workspace):
    try:
        return workspace.active()
    except WorkspaceError:
        return None


def _no_state(label: str, workspace: Workspace) -> StateReply:
    active = _active_record(workspace)
    return StateReply(
        cli=CliResponse.NO_STATE.value,
        label=label,
        detail={"project": active.slug if active else None, "run": None},
    )


# --------------------------------------------------------------------------
# Individual handlers
# --------------------------------------------------------------------------


def handle_state(
    command: StateCommand, state: ProjectState | None, workspace: Workspace
) -> StateReply:
    spec = REGISTRY[command.target] if command.target is not None else None
    if spec is None:
        return StateReply(
            cli=CliResponse.INVALID_COMMAND.value, label="state", ok=False
        )
    if state is None:
        return _no_state(f"state:{spec.role.value}", workspace)
    regions = OWNED_REGIONS.get(spec.role, frozenset())
    detail: dict[str, Any] = {
        "role": spec.role.value,
        "regions": sorted(regions),
        "state_hash": state.meta.state_hash,
    }
    for region in sorted(regions):
        detail[region] = _region_value(state, region)
    return StateReply(
        cli=CliResponse.ACK.value, label=f"state:{spec.role.value}", detail=detail
    )


def handle_status(state: ProjectState | None, workspace: Workspace) -> StateReply:
    if state is None:
        active = _active_record(workspace)
        return StateReply(
            cli=CliResponse.IDLE.value,
            label="status",
            detail={
                "project": active.slug if active else None,
                "stack": active.stack.value if active else None,
                "run": None,
                "stage": None,
            },
        )
    pipeline = state.pipeline
    response = _STATUS_WORDS.get(pipeline.status, CliResponse.IDLE)
    return StateReply(
        cli=response.value,
        label="status",
        detail={
            "project": state.meta.project_slug,
            "type": state.meta.project_type.value,
            "stack": state.meta.stack.value,
            "run": pipeline.run_id,
            "status": pipeline.status.value,
            "stage": pipeline.current_stage.value,
            "iteration": pipeline.iteration,
            "head_snapshot": pipeline.head_snapshot,
            "last_gate_decision": (
                pipeline.last_gate_decision.value
                if pipeline.last_gate_decision
                else None
            ),
            "needs_human_reason": pipeline.needs_human_reason,
            "spec_version": state.spec.spec_version,
            "open_blockers": len(state.review.open_blockers),
            "sealed": state.artifacts.sealed,
            "state_hash": state.meta.state_hash,
        },
    )


def handle_budget(state: ProjectState | None, workspace: Workspace) -> StateReply:
    if state is None:
        return _no_state("budget", workspace)
    budgets = state.budgets
    return StateReply(
        cli=CliResponse.ACK.value,
        label="budget",
        detail={
            "iterations": {
                "used": budgets.iterations_used,
                "max": budgets.max_iterations,
                "remaining": max(0, budgets.max_iterations - budgets.iterations_used),
            },
            "tokens": {
                "used": budgets.tokens_used,
                "max": budgets.max_tokens,
                "remaining": max(0, budgets.max_tokens - budgets.tokens_used),
            },
            "cost_usd": {
                "used": round(budgets.cost_usd_used, 6),
                "max": budgets.max_cost_usd,
                "tracked": budgets.max_cost_usd is not None,
            },
            "wall_clock_seconds": {
                "used": round(budgets.wall_clock_seconds_used, 1),
                "max": budgets.max_wall_clock_seconds,
            },
            "exhausted": budgets.exhausted,
        },
    )


def handle_issues(state: ProjectState | None, workspace: Workspace) -> StateReply:
    if state is None:
        return _no_state("issues", workspace)
    review = state.review
    severities: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for issue in review.open_issues:
        severities[issue.severity.value] = severities.get(issue.severity.value, 0) + 1
        rows.append(
            {
                "issue_id": issue.issue_id,
                "severity": issue.severity.value,
                "category": issue.category.value,
                "raised_by": issue.raised_by.value,
                "file_path": issue.file_path,
                "req_id": issue.req_id,
                "evidence": _trim(issue.evidence),
                "suggested_fix": _trim(issue.suggested_fix),
                "repeat_count": issue.repeat_count,
                "first_seen_iteration": issue.first_seen_iteration,
                "fingerprint": issue.fingerprint,
                "blocking": issue.is_blocking,
            }
        )
    return StateReply(
        cli=CliResponse.ACK.value,
        label="issues",
        detail={
            "open": rows,
            "counts": severities,
            "blockers": len(review.open_blockers),
            "accepted_debt": [issue.issue_id for issue in review.accepted_debt],
            "resolved": [issue.issue_id for issue in review.resolved_issues],
            "escalated": [issue.issue_id for issue in review.escalated],
            "blocker_fingerprints": sorted(review.blocker_fingerprints),
            "made_progress": review.made_progress,
        },
    )


def handle_rules(state: ProjectState | None, workspace: Workspace) -> StateReply:
    if state is None:
        return _no_state("rules", workspace)
    ruleset = state.rules
    return StateReply(
        cli=CliResponse.ACK.value,
        label="rules",
        detail={
            "max_active": ruleset.max_active,
            "active": [
                {
                    "rule_id": rule.rule_id,
                    "scope": rule.scope.value,
                    "rule_text": rule.rule_text,
                    "origin_issue": rule.origin_issue,
                    "created_iteration": rule.created_iteration,
                    "expires_iteration": rule.expires_iteration,
                }
                for rule in ruleset.active
            ],
            "retired": [rule.rule_id for rule in ruleset.retired],
        },
    )


def handle_snapshots(
    state: ProjectState | None,
    workspace: Workspace,
    store: SnapshotStore | None,
    run_id: str | None,
) -> StateReply:
    if store is None or run_id is None:
        return _no_state("snapshots", workspace)
    index = store.snapshot_index(run_id)
    head = store.head(run_id)
    return StateReply(
        cli=CliResponse.ACK.value,
        label="snapshots",
        detail={
            "run": run_id,
            "head": head,
            "count": len(index),
            "snapshots": _jsonable(index),
            "sealed": store.is_sealed(run_id, head) if head else False,
        },
    )


def handle_log(bus: EventBus) -> StateReply:
    events = [event.to_dict() for event in bus.replay()]
    return StateReply(
        cli=CliResponse.ACK.value,
        label="log",
        detail={
            "verbosity": bus.verbosity.value,
            "path": str(bus.log_path) if bus.log_path else None,
            "count": len(events),
            "events": events[-LOG_TAIL:],
        },
    )


def handle_verbose(command: StateCommand, bus: EventBus) -> StateReply:
    if command.verbosity is None:
        return StateReply(
            cli=CliResponse.INVALID_COMMAND.value, label="verbose", ok=False
        )
    previous = bus.verbosity
    bus.set_verbosity(command.verbosity)
    return StateReply(
        cli=CliResponse.ACK.value,
        label="verbose",
        detail={"from": previous.value, "to": command.verbosity.value},
    )


def handle_reload(state: ProjectState | None, config_store: ConfigStore) -> StateReply:
    """Validate both config files, then stage or apply the result.

    A reload is never applied underneath a running pipeline: the staged config
    waits until the run is idle so one build cannot be half-built under two
    different sets of limits.
    """

    try:
        config_store.stage_reload()
    except ConfigError as exc:
        return StateReply(
            cli=CliResponse.NEEDS_HUMAN.value,
            label="reload",
            detail={"error": str(exc), "applied": False},
            ok=False,
        )

    running = state is not None and state.pipeline.status is PipelineStatus.RUNNING
    applied = False if running else config_store.apply_pending()
    return StateReply(
        cli=CliResponse.ACK.value,
        label="reload",
        detail={
            "applied": applied,
            "pending": config_store.has_pending,
            "deferred_reason": "pipeline running" if running else None,
        },
    )


def handle_project(
    command: StateCommand,
    state: ProjectState | None,
    workspace: Workspace,
) -> StateReply:
    action = command.subcommand

    if action is None:
        rows = workspace.summary()
        active = next((row["slug"] for row in rows if row["active"]), None)
        return StateReply(
            cli=CliResponse.ACK.value,
            label="project",
            detail={"active": active, "count": len(rows), "projects": rows},
        )

    running = state is not None and state.pipeline.status is PipelineStatus.RUNNING
    if running:
        return StateReply(
            cli=CliResponse.PIPELINE_BUSY.value,
            label="project",
            detail={"action": action, "reason": "a build is running"},
            ok=False,
        )

    if action == "new":
        _, slug, stack_token = command.args
        if workspace.has(slug):
            return StateReply(
                cli=CliResponse.PROJECT_EXISTS.value,
                label="project",
                detail={"slug": slug},
                ok=False,
            )
        try:
            record = workspace.create(slug, stack=Stack(stack_token))
        except WorkspaceError as exc:
            return StateReply(
                cli=CliResponse.NEEDS_HUMAN.value,
                label="project",
                detail={"slug": slug, "error": str(exc)},
                ok=False,
            )
        return StateReply(
            cli=CliResponse.ACK.value,
            label="project",
            detail={
                "created": record.slug,
                "stack": record.stack.value,
                "type": record.project_type.value,
                "runs_dir": record.runs_dir,
                "active": True,
            },
        )

    if action == "use":
        slug = command.args[1]
        if not workspace.has(slug):
            return StateReply(
                cli=CliResponse.NO_SUCH_PROJECT.value,
                label="project",
                detail={"slug": slug, "known": workspace.slugs()},
                ok=False,
            )
        record = workspace.activate(slug)
        return StateReply(
            cli=CliResponse.ACK.value,
            label="project",
            detail={
                "active": record.slug,
                "stack": record.stack.value,
                "type": record.project_type.value,
                "head_run_id": record.head_run_id,
                "head_snapshot": record.head_snapshot,
                "open_blockers": record.open_blocker_count,
            },
        )

    return StateReply(cli=CliResponse.INVALID_COMMAND.value, label="project", ok=False)


def handle_stack(
    command: StateCommand, state: ProjectState | None, workspace: Workspace
) -> StateReply:
    stack: Stack | None = None
    slug: str | None = None
    if state is not None:
        stack = state.meta.stack
        slug = state.meta.project_slug
    else:
        active = _active_record(workspace)
        if active is not None:
            stack = active.stack
            slug = active.slug
    if stack is None or slug is None:
        return StateReply(
            cli=CliResponse.NO_SUCH_PROJECT.value,
            label="stack",
            detail={"known": workspace.slugs()},
            ok=False,
        )

    changed_from: str | None = None
    meta_update: dict[str, Any] | None = None
    requested = command.subcommand
    if requested is not None:
        target = Stack(requested)
        if target is not stack:
            try:
                workspace.touch(slug, stack=target)
            except WorkspaceError as exc:
                return StateReply(
                    cli=CliResponse.NO_SUCH_PROJECT.value,
                    label="stack",
                    detail={"project": slug, "error": str(exc)},
                    ok=False,
                )
            changed_from = stack.value
            meta_update = {"stack": target}
        stack = target

    profile = profile_for(stack)
    detail: dict[str, Any] = {
        "project": slug,
        "stack": stack.value,
        "preview_mode": profile.preview.value,
        "entrypoint_preference": list(profile.entrypoint_preference),
        "entrypoint_required": profile.entrypoint_required,
        "allowed_suffixes": sorted(profile.allowed_suffixes),
        "bare_filenames": sorted(profile.bare_filenames),
    }
    if changed_from is not None:
        detail["changed_from"] = changed_from
        if state is not None and state.spec.target_stack is not stack:
            # The running build was specced for the old stack; retargeting it
            # mid-flight would invalidate artifacts already written under the
            # old allowlist. The registry holds the declaration until the
            # next spec is derived from it.
            detail["applies"] = "next run"
    return StateReply(
        cli=CliResponse.ACK.value,
        label="stack",
        detail=detail,
        meta_update=meta_update,
    )


def handle_type(
    command: StateCommand, state: ProjectState | None, workspace: Workspace
) -> StateReply:
    slug: str | None = None
    project_type: ProjectType | None = None
    stack: Stack | None = None
    title: str | None = None
    if state is not None:
        slug = state.meta.project_slug
        project_type = state.meta.project_type
        stack = state.meta.stack
    else:
        active = _active_record(workspace)
        if active is not None:
            slug = active.slug
            project_type = active.project_type
            stack = active.stack
            title = active.title
    if slug is None or project_type is None or stack is None:
        return StateReply(
            cli=CliResponse.NO_SUCH_PROJECT.value,
            label="type",
            detail={"known": workspace.slugs()},
            ok=False,
        )

    changed_from: str | None = None
    meta_update: dict[str, Any] | None = None
    requested = command.subcommand
    if requested is not None:
        target = ProjectType(requested)
        if target is not project_type:
            try:
                workspace.touch(slug, project_type=target)
            except WorkspaceError as exc:
                return StateReply(
                    cli=CliResponse.NO_SUCH_PROJECT.value,
                    label="type",
                    detail={"project": slug, "error": str(exc)},
                    ok=False,
                )
            changed_from = project_type.value
            meta_update = {"project_type": target}
        project_type = target

    detail: dict[str, Any] = {"project": slug}
    if title is not None:
        detail["title"] = title
    detail["type"] = project_type.value
    detail["stack"] = stack.value
    if changed_from is not None:
        detail["changed_from"] = changed_from
    return StateReply(
        cli=CliResponse.ACK.value,
        label="type",
        detail=detail,
        meta_update=meta_update,
    )


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def handle(
    command: StateCommand,
    *,
    state: ProjectState | None,
    workspace: Workspace,
    config_store: ConfigStore,
    bus: EventBus,
    store: SnapshotStore | None = None,
    run_id: str | None = None,
) -> StateReply:
    """Dispatch one parsed `!` command."""

    verb = command.verb

    if verb is StateVerb.STATE:
        return handle_state(command, state, workspace)
    if verb is StateVerb.STATUS:
        return handle_status(state, workspace)
    if verb is StateVerb.BUDGET:
        return handle_budget(state, workspace)
    if verb is StateVerb.ISSUES:
        return handle_issues(state, workspace)
    if verb is StateVerb.RULES:
        return handle_rules(state, workspace)
    if verb is StateVerb.SNAPSHOTS:
        return handle_snapshots(state, workspace, store, run_id)
    if verb is StateVerb.LOG:
        return handle_log(bus)
    if verb is StateVerb.VERBOSE:
        return handle_verbose(command, bus)
    if verb is StateVerb.RELOAD:
        return handle_reload(state, config_store)
    if verb is StateVerb.PROJECT:
        return handle_project(command, state, workspace)
    if verb is StateVerb.STACK:
        return handle_stack(command, state, workspace)
    if verb is StateVerb.TYPE:
        return handle_type(command, state, workspace)

    # Only reachable if a verb is added to the parser without a handler.
    return StateReply(
        cli=CliResponse.INVALID_COMMAND.value,
        label="state_command",
        detail={"verb": verb.value, "error": "no handler"},
        ok=False,
    )
