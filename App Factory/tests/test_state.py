"""The state bus: snapshot store, single-writer state manager, workspace.

Three properties matter here, and all three are load-bearing elsewhere:

  * Snapshots are write-once. An iteration's state file is never rewritten,
    which is what makes `!snapshots` an audit trail rather than a guess.
  * Every region of the state has exactly one writer. An agent that strays
    outside its region is refused by the state manager, not by prompt
    etiquette.
  * Projects are isolated on disk. Switching projects must never let one
    build's files land in another project's tree.

No network, no model calls: this module drives the storage layer directly.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.schemas.common import (  # noqa: E402
    AgentRole,
    PipelineStatus,
    PreviewMode,
    ProjectType,
    Stack,
    Stage,
)
from core.schemas.registry import REGISTRY_FILENAME, ProjectStatus  # noqa: E402
from core.state_manager import StateError, StateManager  # noqa: E402
from core.store import SnapshotStore, StoreError  # noqa: E402
from core.workspace import Workspace, WorkspaceError  # noqa: E402
from tests.harness import Checker, discard, temp_root  # noqa: E402

HEX16 = re.compile(r"^[0-9a-f]{16}$")
# The store probe gets its own run id: create_run is write-once, so
# sharing an id with the state manager below would collide.
RUN_PROBE = "2026-09-13T2200Z-0001"
RUN_A = "2026-09-13T2244Z-8777"
RUN_B = "2026-09-13T2255Z-1a2b"


def error_of(call: Callable[[], Any]) -> BaseException | None:
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the test inspects the type
        return exc
    return None


def run() -> tuple[int, int]:
    check = Checker("test_state")
    root = temp_root("af-state-")

    try:
        # -- the snapshot store ------------------------------------------

        check.section("snapshot store")

        store = SnapshotStore(root / "runs")
        run_dir = store.create_run(RUN_PROBE)
        check.truthy("a run directory is created", run_dir.is_dir())
        check.equal("runs are listed", store.list_runs(), [RUN_PROBE])
        check.same("a fresh run has no head", store.head(RUN_PROBE), None)
        check.equal("a fresh run has no snapshots", store.list_snapshots(RUN_PROBE), [])

        duplicate = error_of(lambda: store.create_run(RUN_PROBE))
        check.truthy(
            "creating the same run twice is refused", isinstance(duplicate, StoreError)
        )

        check.contains(
            "a build lives under its snapshot",
            str(store.build_dir(RUN_PROBE, "it-000", "b-000")),
            f"{RUN_PROBE}",
        )
        check.truthy(
            "the events log belongs to the run",
            str(store.events_path(RUN_PROBE)).endswith("events.jsonl"),
        )

        # -- creating and committing state --------------------------------

        check.section("state manager")

        manager = StateManager(store, RUN_A)
        check.equal("the manager knows its run", manager.run_id, RUN_A)
        check.same("nothing is committed yet", manager.load_head(), None)

        state = manager.create(
            project_id="p-001",
            project_slug="default",
            project_type=ProjectType.APP,
            stack=Stack.WEB,
            raw_input="build a CSV viewer",
        )
        check.equal("the run id is stamped into the state", state.pipeline.run_id, RUN_A)
        check.equal("the declared stack is recorded", state.meta.stack, Stack.WEB)
        check.equal(
            "the spec cannot disagree with the declared stack",
            state.spec.target_stack,
            Stack.WEB,
        )
        check.equal(
            "the operator's words are preserved verbatim",
            state.intent.raw_input,
            "build a CSV viewer",
        )
        check.truthy(
            "the state hash is a 16-hex digest",
            bool(HEX16.match(state.compute_state_hash())),
        )
        check.truthy("a freshly stamped state verifies", manager.verify_hash(state))

        # -- region ownership ------------------------------------------------

        check.section("single writer per region")

        allowed = manager.apply(
            state, role=AgentRole.PROMPT_ENGINEER, updates={"rules": state.rules}
        )
        check.truthy("an agent may write its own region", allowed is not None)

        denied = error_of(
            lambda: manager.apply(
                state, role=AgentRole.QC, updates={"rules": state.rules}
            )
        )
        check.truthy(
            "an agent may not write another agent's region",
            isinstance(denied, StateError),
        )

        cross_critic = error_of(
            lambda: manager.apply(
                state,
                role=AgentRole.QC,
                updates={"review.design_result": state.review.design_result},
            )
        )
        check.truthy(
            "one critic may not write the other's verdict",
            isinstance(cross_critic, StateError),
        )

        system_region = error_of(
            lambda: manager.apply(
                state, role=AgentRole.OBSERVER, updates={"budgets": state.budgets}
            )
        )
        check.truthy(
            "no agent may write the budget", isinstance(system_region, StateError)
        )
        check.truthy(
            "the state manager may write the budget",
            manager.apply_system(state, {"budgets": state.budgets}) is not None,
        )

        # -- commits are write-once ---------------------------------------------

        check.section("write-once snapshots")

        state = manager.transition(state, Stage.BUILD, actor=AgentRole.COMMANDER)
        check.equal("the pipeline moved", state.pipeline.current_stage, Stage.BUILD)
        check.truthy("the move was recorded", len(state.pipeline.stage_history) >= 1)

        first = manager.commit(state, note="first pass")
        check.equal("iteration 0 commits as it-000", first.snapshot, "it-000")
        check.equal("the commit reports its iteration", first.iteration, 0)
        check.truthy("the snapshot file exists", Path(first.path).exists())
        check.truthy(
            "the commit reports a state hash", bool(HEX16.match(first.state_hash))
        )
        check.equal("HEAD moved to the commit", store.head(RUN_A), "it-000")

        reloaded = manager.load_head()
        check.truthy("the head reloads", reloaded is not None)
        check.equal(
            "the reloaded state is the committed one",
            reloaded.compute_state_hash(),
            first.state_hash,
        )
        check.equal(
            "the snapshot records its own head", reloaded.pipeline.head_snapshot, "it-000"
        )

        rewrite = error_of(lambda: manager.commit(state))
        check.truthy(
            "an iteration cannot be committed twice",
            isinstance(rewrite, (StoreError, StateError)),
        )

        state = manager.bump_iteration(state)
        check.equal("the iteration advanced", state.pipeline.iteration, 1)
        second = manager.commit(state)
        check.equal("iteration 1 commits as it-001", second.snapshot, "it-001")
        check.equal("HEAD followed the new commit", store.head(RUN_A), "it-001")
        check.equal(
            "both snapshots are listed", store.list_snapshots(RUN_A), ["it-000", "it-001"]
        )
        check.equal("the index has one row per snapshot", len(manager.snapshot_index()), 2)
        check.equal(
            "an older snapshot is still readable",
            store.read_state(RUN_A, "it-000").pipeline.iteration,
            0,
        )

        # -- sealing --------------------------------------------------------------

        check.section("sealing a shipped snapshot")

        check.falsy("a live snapshot is not sealed", store.is_sealed(RUN_A, "it-001"))
        receipt = manager.seal(state, bld_id="b-001")
        check.truthy("sealing returns a receipt", isinstance(receipt, dict))
        check.truthy("the snapshot is now sealed", store.is_sealed(RUN_A, "it-001"))
        seal_file = store.snapshot_dir(RUN_A, "it-001") / "SEALED.json"
        check.truthy("the seal is recorded on disk", seal_file.exists())
        check.contains(
            "the seal names the shipped build", json.loads(seal_file.read_text()).values(), "b-001"
        )
        sealed_write = error_of(lambda: manager.commit(state))
        check.truthy(
            "a sealed snapshot cannot be rewritten",
            isinstance(sealed_write, (StoreError, StateError)),
        )

        # -- status and reasons ------------------------------------------------------

        check.section("status")

        stalled = manager.set_status(
            state, PipelineStatus.NEEDS_HUMAN, reason="iteration ceiling reached (3/3)"
        )
        check.equal(
            "the status is set", stalled.pipeline.status, PipelineStatus.NEEDS_HUMAN
        )
        check.contains(
            "the reason travels with the status",
            str(stalled.pipeline.model_dump()),
            "iteration ceiling reached",
        )

        # -- the workspace -------------------------------------------------------------

        check.section("workspace")

        ws_root = root / "workspace"
        ws_root.mkdir(parents=True, exist_ok=True)
        workspace = Workspace(ws_root)

        check.equal(
            "the registry has the documented filename",
            workspace.registry_path.name,
            REGISTRY_FILENAME,
        )
        check.same("an empty workspace has no active slug", workspace.active_slug(), None)
        no_active = error_of(workspace.active)
        check.truthy(
            "asking for the active project of an empty workspace raises",
            isinstance(no_active, WorkspaceError),
        )

        default = workspace.ensure_active()
        check.equal("a default project is created on demand", default.slug, "default")
        check.equal("the default project is web", default.stack, Stack.WEB)
        check.equal(
            "a web project previews in an iframe",
            default.preview_mode,
            PreviewMode.IFRAME,
        )
        check.falsy("a new project has no history", default.has_history)
        check.equal("ensure_active is idempotent", workspace.ensure_active().slug, "default")

        go = workspace.create("go-svc", stack=Stack.GO, project_type=ProjectType.SERVICE)
        check.equal("a second project can declare another stack", go.stack, Stack.GO)
        check.equal(
            "a non-web project is read as source", go.preview_mode, PreviewMode.SOURCE
        )
        check.equal(
            "its runs directory stays workspace-relative",
            go.runs_dir,
            "projects/go-svc/runs",
        )
        check.truthy(
            "its build directory exists on disk", (ws_root / go.runs_dir).is_dir()
        )
        check.equal("creating a project activates it", workspace.active_slug(), "go-svc")
        check.equal("both projects are registered", sorted(workspace.slugs()), ["default", "go-svc"])

        exists = error_of(lambda: workspace.create("go-svc"))
        check.truthy(
            "a duplicate project is refused", isinstance(exists, WorkspaceError)
        )
        bad_slug = error_of(lambda: workspace.create("My_App"))
        check.truthy(
            "an unusable slug is refused", isinstance(bad_slug, WorkspaceError)
        )
        ghost = error_of(lambda: workspace.activate("ghost"))
        check.truthy(
            "activating an unknown project is refused", isinstance(ghost, WorkspaceError)
        )

        check.falsy("saving an unchanged registry is a no-op", workspace.save())

        # -- isolation and resumption -----------------------------------------------------

        check.section("project isolation")

        default_handle = workspace.handle("default")
        go_handle = workspace.handle("go-svc")
        check.equal("a handle knows its stack", go_handle.stack, Stack.GO)
        check.equal("a handle knows its type", go_handle.project_type, ProjectType.SERVICE)
        check.check(
            "two projects never share a runs directory",
            default_handle.runs_dir != go_handle.runs_dir,
            "both projects resolved to the same directory",
        )

        default_handle.store.create_run(RUN_A)
        go_handle.store.create_run(RUN_B)
        check.equal("the default project sees only its run", default_handle.store.list_runs(), [RUN_A])
        check.equal("the go project sees only its run", go_handle.store.list_runs(), [RUN_B])

        workspace.record_progress(
            "go-svc",
            run_id=RUN_B,
            snapshot_id="it-002",
            status=ProjectStatus.SHIPPED,
            open_blocker_count=0,
            shipped_build_id="b-002",
        )
        record = workspace.record("go-svc")
        check.equal("the head run is remembered", record.head_run_id, RUN_B)
        check.equal("the head snapshot is remembered", record.head_snapshot, "it-002")
        check.contains(
            "the shipped build is remembered", record.shipped_build_ids, "b-002"
        )
        check.truthy("the project now has history", record.has_history)

        missing = error_of(lambda: workspace.record_progress("ghost", run_id=RUN_B))
        check.truthy(
            "progress cannot be recorded for an unknown project",
            isinstance(missing, WorkspaceError),
        )

        reopened = Workspace(ws_root)
        check.equal(
            "the registry survives a restart",
            reopened.record("go-svc").shipped_build_ids,
            ["b-002"],
        )
        check.equal(
            "the active project survives a restart", reopened.active_slug(), "go-svc"
        )

        rows = reopened.summary()
        check.equal("the summary has one row per project", len(rows), 2)
        check.equal(
            "exactly one project is active",
            sum(1 for row in rows if row["active"]),
            1,
        )
        for key in ("slug", "stack", "status", "preview_mode", "shipped", "runs_dir"):
            check.contains(f"the summary reports {key}", rows[0], key)

        return check.report()
    finally:
        discard(root)


if __name__ == "__main__":
    passed, total = run()
    raise SystemExit(0 if passed == total else 1)
