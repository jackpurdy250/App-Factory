"""The seven-stage pipeline, driven end to end against the mock provider.

This is the module that proves the loop-breakers actually break loops. The
mock provider emits a converging defect sequence (two blockers, then one,
then none), so a run here exercises the real gate, the real monotonic
progress check, the real rule writer, and writes real files to disk.

The invariants asserted below are the ones that stop the factory from either
spinning forever or silently losing work:

  * exactly one snapshot per iteration, committed at the end of the cycle;
  * `ship()` seals the head snapshot and writes no new one;
  * a reattached shipped run still reads `awaiting_review`, because the seal
    lives beside the snapshot rather than inside it;
  * revision stops at the iteration ceiling with a human-readable reason.

Two things worth knowing before editing this file:

  * A converging build spends iterations 0, 1 and 2, which is the whole
    default allowance of three. So a run is shipped from its first pass; the
    revision path is exercised on a second run, where the next `#revise`
    legitimately hits the ceiling. Budget arithmetic here is real, not
    mocked.
  * Everything runs inside a single asyncio.run. A Pipeline cannot be driven
    from two different event loops.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.events import Channel  # noqa: E402
from core.parser import Verbosity  # noqa: E402
from core.pipeline import PipelineBusy, PipelineError  # noqa: E402
from core.schemas.common import (  # noqa: E402
    AgentRole,
    GateDecision,
    PipelineStatus,
    ProjectType,
    Stack,
    Stage,
)
from tests.harness import (  # noqa: E402
    Checker,
    build_system,
    cli_lines,
    discard,
    events_on,
    log_codes,
    temp_root,
)

BUILD_RE = re.compile(r"^b-\d{3}$")
SNAP_RE = re.compile(r"^it-\d{3}$")


def plain(value: Any) -> Any:
    return getattr(value, "value", value)


def error_of(call: Callable[[], Any]) -> BaseException | None:
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the test inspects the type
        return exc
    return None


async def exercise(check: Checker) -> None:
    root = temp_root("af-pipeline-")
    trace_root = temp_root("af-trace-")
    try:
        system = build_system(root)
        pipeline = system.pipeline

        # -- before anything runs -----------------------------------------

        check.section("a cold pipeline")

        check.falsy("nothing is running", pipeline.busy)
        check.same("there is no state", pipeline.state, None)
        check.same("there is no run", pipeline.run_id, None)
        check.same("there is no build directory", pipeline.build_dir, None)

        cold = pipeline.status_payload()
        check.equal("the header reads idle", cold["status"], "idle")
        check.equal("no iterations have been spent", cold["iteration"], 0)
        check.equal("no tokens have been spent", cold["tokens_used"], 0)
        check.equal("the token ceiling is published", cold["max_tokens"], 250_000)
        check.same("there is no build to show", cold["build"], None)
        check.truthy(
            "a busy pipeline reports a pipeline error",
            issubclass(PipelineBusy, PipelineError),
        )

        # -- the first build -------------------------------------------------

        check.section("first build")

        first = await pipeline.start(
            "build a CSV viewer with a header filter",
            target=AgentRole.IMPLEMENTER,
            project_type=ProjectType.APP,
            stack=Stack.WEB,
        )

        check.equal(
            "the run ends awaiting review",
            plain(first.status),
            PipelineStatus.AWAITING_REVIEW.value,
        )
        check.equal(
            "the pipeline parks at S8", plain(first.stage), Stage.AWAITING_REVIEW.value
        )
        check.equal("Window 1 is told once", first.cli, "ready for review")
        check.truthy("a build id was assigned", bool(BUILD_RE.match(first.build or "")))
        check.truthy(
            "a snapshot id was assigned", bool(SNAP_RE.match(first.snapshot or ""))
        )
        check.equal("the gate passed", plain(first.decision), GateDecision.PASS.value)
        check.equal("no blockers remain", first.open_blockers, 0)
        check.equal("the web entrypoint is index.html", first.entrypoint, "index.html")
        check.falsy("the pipeline is idle again", pipeline.busy)

        for fragment in (first.slug, first.run_id, first.snapshot, first.build):
            check.contains(
                f"the preview url carries {fragment}", str(first.preview), str(fragment)
            )

        # -- what landed on disk ------------------------------------------------

        check.section("artifacts on disk")

        build_dir = pipeline.build_dir
        check.truthy("the build directory exists", build_dir and build_dir.is_dir())
        written = sorted(path.name for path in build_dir.iterdir() if path.is_file())
        check.truthy("the build is not empty", written)
        check.contains("the entrypoint was written", written, "index.html")
        check.truthy(
            "the entrypoint has content",
            (build_dir / "index.html").read_text().strip() != "",
        )

        store = pipeline.store
        run_id = pipeline.run_id
        snapshots = store.list_snapshots(run_id)
        check.equal(
            "exactly one snapshot per iteration", len(snapshots), first.iteration + 1
        )
        check.equal("HEAD is the reported snapshot", store.head(run_id), first.snapshot)
        check.equal(
            "the snapshot is readable",
            store.read_state(run_id, first.snapshot).pipeline.run_id,
            run_id,
        )
        check.falsy(
            "the head snapshot is not sealed yet", store.is_sealed(run_id, first.snapshot)
        )
        check.check(
            "converging cost more than one iteration",
            first.iteration >= 1,
            "the mock defects did not force a loop",
        )

        # -- what the operator saw -------------------------------------------------

        check.section("what the windows saw")

        check.equal(
            "Window 1 saw exactly one line for the build",
            [line for line in cli_lines(system.bus) if line == "ready for review"],
            ["ready for review"],
        )
        codes = log_codes(system.bus)
        for code in ("BUILD", "REVIEW", "ADJUDICATE", "SNAPSHOT"):
            check.contains(f"Window 3 logged {code}", codes, code)
        check.contains("the gate's loop decision was logged", codes, "LOOP")
        check.contains("the gate's pass decision was logged", codes, "PASS")
        check.excludes(
            "quiet verbosity withholds per-agent chatter", codes, "AGENT_CALL"
        )
        check.truthy("Window 2 was given a preview", events_on(system.bus, Channel.PREVIEW))
        check.truthy("the status channel was updated", events_on(system.bus, Channel.STATUS))

        # -- shipping --------------------------------------------------------------------

        check.section("shipping")

        before_snapshots = len(store.list_snapshots(run_id))
        shipped = await pipeline.ship()
        check.equal("shipping reports the build", shipped.cli, f"shipped {first.build}")
        check.equal(
            "the run is shipped", plain(shipped.status), PipelineStatus.SHIPPED.value
        )
        check.equal(
            "shipping writes no new snapshot",
            len(store.list_snapshots(run_id)),
            before_snapshots,
        )
        check.truthy(
            "the head snapshot is sealed", store.is_sealed(run_id, first.snapshot)
        )
        check.truthy(
            "the seal is a file beside the snapshot",
            (store.snapshot_dir(run_id, first.snapshot) / "SEALED.json").exists(),
        )
        refused = error_of(lambda: None)
        check.same("sanity: error_of returns None when nothing raises", refused, None)

        # -- detach and reattach ------------------------------------------------------------

        check.section("reattaching a shipped run")

        slug = pipeline.slug
        pipeline.detach()
        check.same("detaching clears the state", pipeline.state, None)
        check.equal(
            "the header reads idle again", pipeline.status_payload()["status"], "idle"
        )

        reattached = pipeline.attach(slug)
        check.truthy("reattaching restores a state", reattached is not None)
        check.equal(
            "the reattached snapshot still reads awaiting_review",
            plain(reattached.pipeline.status),
            PipelineStatus.AWAITING_REVIEW.value,
        )
        check.truthy(
            "but the seal on disk is what decides",
            store.is_sealed(run_id, first.snapshot),
        )

        # -- revision and the iteration ceiling --------------------------------------------------

        check.section("revision and the iteration ceiling")

        second = await pipeline.start(
            "build a settings panel", target=AgentRole.IMPLEMENTER, stack=Stack.WEB
        )
        check.check(
            "a new run starts after shipping",
            second.run_id != run_id,
            "the sealed run was reused",
        )
        check.equal(
            "the new run is awaiting review",
            plain(second.status),
            PipelineStatus.AWAITING_REVIEW.value,
        )

        second_store = pipeline.store
        second_run = pipeline.run_id
        revised = await pipeline.resume(
            entry_stage=Stage.BUILD,
            target=AgentRole.IMPLEMENTER,
            instruction="widen the table and add a footer",
        )
        check.check(
            "revision bumps the iteration first",
            revised.iteration > second.iteration,
            f"{revised.iteration} did not advance past {second.iteration}",
        )
        check.check(
            "revision produces a new snapshot",
            revised.snapshot != second.snapshot,
            "the snapshot was reused",
        )
        check.equal(
            "still one snapshot per iteration",
            len(second_store.list_snapshots(second_run)),
            revised.iteration + 1,
        )
        check.equal("the revision stays in the same run", revised.run_id, second.run_id)

        outcome = revised
        for _ in range(6):
            if plain(outcome.status) == PipelineStatus.NEEDS_HUMAN.value:
                break
            outcome = await pipeline.resume(
                entry_stage=Stage.BUILD,
                target=AgentRole.IMPLEMENTER,
                instruction="keep polishing the header",
            )

        check.equal(
            "revision stops at the ceiling",
            plain(outcome.status),
            PipelineStatus.NEEDS_HUMAN.value,
        )
        check.equal(
            "the operator is told why", outcome.reason, "iteration ceiling reached (3/3)"
        )
        check.equal(
            "Window 1 gets the reason too",
            outcome.cli,
            "needs human: iteration ceiling reached (3/3)",
        )
        check.falsy("a halted pipeline is not left busy", pipeline.busy)
        check.equal(
            "the halt never overspends the allowance",
            pipeline.state.budgets.iterations_used
            <= pipeline.state.budgets.max_iterations,
            True,
        )
        check.contains(
            "Window 1 was told about the halt",
            cli_lines(system.bus),
            "needs human: iteration ceiling reached (3/3)",
        )

        # -- verbosity ------------------------------------------------------------------------------

        check.section("verbosity")

        loud = build_system(trace_root, verbosity=Verbosity.TRACE)
        await loud.pipeline.start(
            "build a one-page dashboard", target=AgentRole.IMPLEMENTER, stack=Stack.WEB
        )
        loud_codes = log_codes(loud.bus)
        for code in ("AGENT_CALL", "AGENT_OK"):
            check.contains(f"trace verbosity reveals {code}", loud_codes, code)
        check.contains("trace still logs the stages", loud_codes, "BUILD")
        check.check(
            "trace is strictly noisier than quiet",
            len(loud_codes) > len(codes),
            f"trace produced {len(loud_codes)} codes, quiet produced {len(codes)}",
        )
    finally:
        discard(root)
        discard(trace_root)


def run() -> tuple[int, int]:
    import asyncio

    check = Checker("test_pipeline")
    asyncio.run(exercise(check))
    return check.report()


if __name__ == "__main__":
    passed, total = run()
    raise SystemExit(0 if passed == total else 1)
