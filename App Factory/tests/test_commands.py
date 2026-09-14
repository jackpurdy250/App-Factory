"""The command router, end to end, against the mock provider.

This is the highest-value module in the suite. `core/commands.py` had to be
retyped from scratch after a sandbox wipe, and the two bugs it originally
shipped with were both found here rather than by reading it:

  1. `Workspace.active()` raises when there is no active project; it does not
     return None. Treating it as optional crashed the first `!project` call.
  2. `ship()` seals the head snapshot without writing a new one, so a project
     reattached later reloads a state that still reads `awaiting_review`. The
     seal check therefore has to consult disk, not just memory.

Both are asserted below so neither can come back quietly.

Everything runs inside a single asyncio.run: a Pipeline holds async state that
does not survive being driven from two different loops.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.events import Channel  # noqa: E402
from core.schemas.envelope import CliResponse  # noqa: E402
from tests.harness import (  # noqa: E402
    Checker,
    System,
    build_system,
    cli_lines,
    discard,
    events_on,
    temp_root,
)

SHIPPED_RE = re.compile(r"^shipped b-\d{3}$")


def last_cli(system: System) -> str:
    lines = cli_lines(system.bus)
    return lines[-1] if lines else ""


def cli_count(system: System) -> int:
    return len(cli_lines(system.bus))


def tokens(system: System) -> int:
    payload = system.pipeline.status_payload()
    return int(payload.get("tokens_used") or 0)


def plain(value: Any) -> Any:
    return getattr(value, "value", value)


async def exercise(check: Checker) -> None:
    root = temp_root("af-router-")
    try:
        system = build_system(root)
        router = system.router

        # -- refusals are free and quiet ----------------------------------

        check.section("refusals cost nothing")

        refusals = [
            "route QC add a filter",
            "#deploy now",
            "#ship now",
            "#route QC",
            "&pip install requests",
            "",
            "!state commander",
            "!verbose loud",
        ]
        before_lines = cli_count(system)
        for line in refusals:
            outcome = await router.submit(line)
            check.equal(f"{line!r} answers 'invalid command'", outcome.cli, "invalid command")
            check.falsy(f"{line!r} is not ok", outcome.ok)
            check.truthy(f"{line!r} carries a reason code for Window 3", outcome.reason_code)
        check.equal(
            "each refusal printed to Window 1 exactly once",
            cli_count(system) - before_lines,
            len(refusals),
        )
        check.equal("refusals spent no tokens", tokens(system), 0)
        check.falsy("refusals left the pipeline idle", system.pipeline.busy)

        # -- state commands answer on a cold pipeline ---------------------

        check.section("state commands before any build")

        for line in ("!status", "!log", "!budget", "!issues", "!rules", "!snapshots"):
            outcome = await router.submit(line)
            check.truthy(f"{line} answers on a cold pipeline", outcome.cli)
            check.truthy(f"{line} is not an error", outcome.ok)
        check.equal("state commands still spent nothing", tokens(system), 0)

        listing = await router.submit("!project")
        check.truthy("bare !project lists projects without crashing", listing.ok)

        # -- first build ---------------------------------------------------

        check.section("first build")

        outcome = await router.submit(
            "#route IMPLEMENTER build a CSV viewer with a header filter"
        )
        check.truthy("#route is accepted", outcome.ok)
        check.equal("the first run reaches review", last_cli(system), "ready for review")

        detail = outcome.detail or {}
        for key in (
            "run_id",
            "project",
            "status",
            "stage",
            "iteration",
            "snapshot",
            "build",
            "entrypoint",
            "preview",
        ):
            check.contains(f"exec detail reports {key}", detail, key)

        first_run = detail.get("run_id")
        check.truthy("the run has an id", first_run)
        check.equal(
            "status is awaiting review", plain(detail.get("status")), "awaiting_review"
        )
        check.truthy("a build was written", detail.get("build"))
        check.contains(
            "the preview url points at the build",
            str(detail.get("preview")),
            str(detail.get("build")),
        )
        check.truthy("the build cost tokens", tokens(system) > 0)

        preview_events = events_on(system.bus, Channel.PREVIEW)
        check.truthy("Window 2 was told to load the build", preview_events)

        # -- inspection after a build -------------------------------------

        check.section("inspection after a build")

        spent = tokens(system)
        for line in (
            "!state OBSERVER",
            "!state IMPLEMENTER",
            "!state QC",
            "!issues",
            "!rules",
            "!snapshots",
            "!budget",
            "!status",
        ):
            outcome = await router.submit(line)
            check.truthy(f"{line} answers after a build", outcome.cli)
        check.equal("inspection is free", tokens(system), spent)

        # -- ship -----------------------------------------------------------

        check.section("ship")

        shipped = await router.submit("#ship")
        check.truthy("#ship is accepted", shipped.ok)
        check.check(
            "#ship reports the build",
            bool(SHIPPED_RE.match(last_cli(system))),
            f"got {last_cli(system)!r}",
        )
        check.truthy("#ship names the build id", shipped.build)

        # -- the sealed run refuses further work ---------------------------

        check.section("a sealed run is closed")

        before_lines = cli_count(system)
        spent = tokens(system)
        second_ship = await router.submit("#ship")
        check.falsy("a second #ship is refused", second_ship.ok)
        check.equal(
            "a second #ship says no build awaiting review",
            second_ship.cli,
            "no build awaiting review",
        )
        check.equal(
            "the refusal printed once", cli_count(system) - before_lines, 1
        )
        check.equal("the refusal spent nothing", tokens(system), spent)

        revise_sealed = await router.submit("#revise IMPLEMENTER widen the table")
        check.falsy("#revise on a sealed run is refused", revise_sealed.ok)
        check.equal(
            "#revise on a sealed run uses the closed vocabulary",
            revise_sealed.cli,
            "no build awaiting review",
        )
        check.excludes(
            "the operator is never shown the word 'sealed'",
            revise_sealed.cli,
            "sealed",
        )
        check.equal("the refusal spent nothing", tokens(system), spent)

        # -- project switching ----------------------------------------------

        check.section("project switching")

        created = await router.submit("!project new go-svc go")
        check.truthy("!project new succeeds", created.ok)
        check.equal(
            "!project new inherits the declared stack",
            plain(router.active_stack),
            "go",
        )

        ghost = await router.submit("!project use ghost")
        check.falsy("switching to an unknown project is refused", ghost.ok)
        check.equal(
            "an unknown project says so plainly", ghost.cli, "no such project"
        )

        duplicate = await router.submit("!project new go-svc go")
        check.falsy("re-creating a project is refused", duplicate.ok)

        back = await router.submit("!project use default")
        check.truthy("!project use succeeds", back.ok)
        check.equal(
            "!project use restores the original stack",
            plain(router.active_stack),
            "web",
        )

        # The reattached state is the shipped run's head snapshot, which was
        # sealed in place rather than rewritten, so it still reads
        # awaiting_review. The seal lives on disk; the router must consult it.
        status_payload = router.status_payload()
        check.equal(
            "a reattached shipped run still reads awaiting_review",
            plain(status_payload.get("status")),
            "awaiting_review",
        )

        spent = tokens(system)
        revise_reattached = await router.submit("#revise IMPLEMENTER add a footer")
        check.falsy(
            "#revise on a reattached sealed run is still refused",
            revise_reattached.ok,
        )
        check.equal(
            "the disk-backed seal check produces the same refusal",
            revise_reattached.cli,
            "no build awaiting review",
        )
        check.equal("that refusal also spent nothing", tokens(system), spent)

        # -- a new run after shipping ----------------------------------------

        check.section("routing after a ship starts a new run")

        second = await router.submit("#route IMPLEMENTER add CSV export")
        check.truthy("#route after a ship is accepted", second.ok)
        second_detail = second.detail or {}
        check.check(
            "#route after a ship starts a new run",
            second_detail.get("run_id") != first_run,
            f"reused {first_run!r}",
        )
        check.equal(
            "the second run also reaches review", last_cli(system), "ready for review"
        )

        # -- the iteration ceiling stops the loop ----------------------------

        check.section("the iteration ceiling stops revision")

        final = ""
        for _ in range(6):
            await router.submit("#revise IMPLEMENTER keep polishing the header")
            final = last_cli(system)
            if final.startswith("needs human"):
                break
        check.equal(
            "revision stops at the iteration ceiling",
            final,
            "needs human: iteration ceiling reached (3/3)",
        )
        check.falsy("the pipeline is left idle, not stuck busy", system.pipeline.busy)

        # -- declared stack and type are setters ------------------------------

        # The manual tells the operator to type `!stack go`. The parser
        # originally listed both verbs as zero-arg, so the documented form
        # was refused outright. This section is the regression wall.
        check.section("!stack and !type are setters, not just readouts")

        slug = system.workspace.active_slug()
        check.truthy("a project is active before the setters run", slug is not None)
        assert slug is not None
        start_stack = plain(system.workspace.record(slug).stack)
        start_type = plain(system.workspace.record(slug).project_type)
        want_stack = "python" if start_stack == "go" else "go"
        want_type = "app" if start_type == "cli" else "cli"

        reading = await router.submit("!stack")
        check.truthy("bare !stack still reads", reading.ok)
        check.equal(
            "a bare read does not change the stack",
            plain(system.workspace.record(slug).stack),
            start_stack,
        )

        setting = await router.submit("!stack " + want_stack)
        check.equal("!stack <value> is acknowledged", setting.cli, "ack")
        set_detail = setting.detail or {}
        check.equal("the new stack is echoed back", set_detail.get("stack"), want_stack)
        check.equal(
            "the previous stack is reported",
            set_detail.get("changed_from"),
            start_stack,
        )
        check.equal(
            "the preview mode follows the declared stack",
            set_detail.get("preview_mode"),
            "source",
        )
        check.equal(
            "the registry persisted the new stack",
            plain(system.workspace.record(slug).stack),
            want_stack,
        )

        # touch() saves, so the change survives a cold read of
        # saved-project-context.json rather than living only in memory.
        reloaded = system.workspace.__class__(
            system.workspace.root, registry_path=system.workspace.registry_path
        )
        check.equal(
            "the change reached saved-project-context.json",
            plain(reloaded.record(slug).stack),
            want_stack,
        )

        live = system.pipeline.state
        check.truthy("a live run is attached", live is not None)
        # `spec` is the Observer's region and ProjectState forbids a
        # meta.stack that contradicts spec.target_stack, so a declaration
        # made mid-run is held in the registry and adopted by the next spec
        # rather than forced onto a build already in flight.
        check.equal(
            "a mid-run declaration is deferred, not forced",
            set_detail.get("applies"),
            "next run",
        )
        if live is not None:
            check.equal(
                "the running build keeps the stack it was specced for",
                plain(live.meta.stack),
                start_stack,
            )

        typing_reply = await router.submit("!type " + want_type)
        check.equal("!type <value> is acknowledged", typing_reply.cli, "ack")
        type_detail = typing_reply.detail or {}
        check.equal("the new type is echoed back", type_detail.get("type"), want_type)
        check.equal(
            "the registry persisted the new type",
            plain(system.workspace.record(slug).project_type),
            want_type,
        )

        # project_type carries no spec invariant, so the live run adopts it
        # immediately. This is the proof that the write path itself works.
        live_typed = system.pipeline.state
        if live_typed is not None:
            check.equal(
                "live state follows a declaration with no spec conflict",
                plain(live_typed.meta.project_type),
                want_type,
            )

        # Unknown values and extra tokens die in the parser, so they never
        # reach the registry and never cost a model call.
        spent = tokens(system)
        for line in ("!stack klingon", "!type widget", "!stack go web", "!type cli app"):
            bad = await router.submit(line)
            check.equal(f"{line!r} answers 'invalid command'", bad.cli, "invalid command")
            check.truthy(f"{line!r} carries a reason code", bad.reason_code)
        check.equal(
            "the registry survived the rejected values",
            plain(system.workspace.record(slug).stack),
            want_stack,
        )
        check.equal("rejected setters spent no tokens", tokens(system) - spent, 0)

        # Single writer: redeclaring the stack mid-build would swap the
        # artifact allowlist underneath a running writer.
        system.pipeline._busy = True
        try:
            busy = await router.submit("!stack rust")
        finally:
            system.pipeline._busy = False
        check.equal(
            "a setter is refused while the pipeline is busy", busy.cli, "pipeline busy"
        )
        check.falsy("the busy refusal is not ok", busy.ok)
        check.equal(
            "the refused setter changed nothing",
            plain(system.workspace.record(slug).stack),
            want_stack,
        )

        # Reads stay available while a build runs; only writes are held off.
        system.pipeline._busy = True
        try:
            read_busy = await router.submit("!stack")
        finally:
            system.pipeline._busy = False
        check.truthy("a bare !stack still reads while busy", read_busy.ok)

        # -- window 1 discipline ----------------------------------------------

        check.section("Window 1 vocabulary")

        # Derive the vocabulary from the enum instead of restating it. A new
        # CliResponse member is then allowed automatically, while anything
        # that is not in the enum still fails. `shipped b-003` and
        # `needs human: <reason>` extend their base strings, hence startswith.
        allowed_prefixes = tuple(member.value for member in CliResponse)
        check.contains(
            "the closed vocabulary is defined by CliResponse",
            allowed_prefixes,
            "ack",
        )
        stray = [
            line
            for line in cli_lines(system.bus)
            if not line.startswith(allowed_prefixes)
        ]
        check.equal(
            "Window 1 only ever showed closed-vocabulary replies", stray, []
        )
    finally:
        discard(root)


def run() -> tuple[int, int]:
    check = Checker("test_commands")
    asyncio.run(exercise(check))
    return check.report()


if __name__ == "__main__":
    passed, total = run()
    raise SystemExit(0 if passed == total else 1)
