"""FastAPI composition root.

This is the only module allowed to know about every layer at once. core/
never imports agents/, and neither imports server/; assembly happens here so
that the dependency direction stays one-way and testable.

The wire protocol is deliberately two frames and nothing else:

    {"type": "event",  "payload": Event.to_dict()}
    {"type": "status", "payload": pipeline status payload}

One socket, one writer. After the initial replay a single pump task owns the
socket, so frames can never interleave mid-write. Commands arriving from the
browser are dispatched as tracked tasks rather than awaited inline, which is
what lets `!status` and `!log` answer while a build is running.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from agents.prompts import PromptLibrary
from core import __version__ as CORE_VERSION
from core.budgeter import Budgeter
from core.commands import CommandRouter
from core.config import ConfigStore
from core.events import Channel, Event, EventBus, Verbosity
from core.llm import AgentRunner
from core.pipeline import Pipeline
from core.workspace import Workspace

APP_ROOT = Path(__file__).resolve().parents[1]

#: Longest history a new tab receives. Window 1 lines are never dropped by
#: verbosity, only by this cap, so the operator always sees recent replies.
REPLAY_LIMIT = 400

#: Explicit allowlist. The web directory is not a static mount, because a
#: build directory should never become reachable through it by accident.
ASSETS = frozenset({"app.js", "styles.css"})

_MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
_RUN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}Z-[0-9a-f]{4}$")
_SNAP_RE = re.compile(r"^it-\d{3}$")
_BUILD_RE = re.compile(r"^b-\d{3}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _command_line(raw: str) -> str | None:
    """Pull the command line out of a socket frame, untouched.

    No trimming, no normalising, no validation. The Python parser is the only
    authority on what a command means, and it treats a stray space as an
    error on purpose - silently cleaning input here would make the UI and the
    documented grammar disagree.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        line = parsed.get("line")
        if isinstance(line, str):
            return line
    return None


def _check_segments(slug: str, run_id: str, snap_id: str, bld_id: str) -> None:
    """Validate every path segment before it reaches the filesystem."""
    for value, pattern, label in (
        (slug, _SLUG_RE, "project"),
        (run_id, _RUN_RE, "run id"),
        (snap_id, _SNAP_RE, "snapshot id"),
        (bld_id, _BUILD_RE, "build id"),
    ):
        if not pattern.match(value):
            raise HTTPException(status_code=404, detail=f"bad {label}")


def _frame(kind: str, payload: Any) -> str:
    return json.dumps({"type": kind, "payload": payload})


def _sendable(bus: EventBus, event: Event) -> bool:
    """Only LOG traffic is verbosity-filtered.

    Window 1 replies, previews, and status frames are structural: dropping
    them at `!verbose quiet` would silently break the UI rather than make it
    calmer.
    """
    if event.channel is Channel.LOG:
        return bus.visible(event)
    return True


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    models_path: Path | str | None = None,
    governor_path: Path | str | None = None,
    env_path: Path | str | None = None,
    runs_dir: Path | str | None = None,
    web_dir: Path | str | None = None,
    verbosity: Verbosity = Verbosity.NORMAL,
) -> FastAPI:
    """Build the application. There is deliberately no module-level instance:
    importing this module must not read config or touch the filesystem."""
    default_env = APP_ROOT / ".env"
    config_store = ConfigStore.from_paths(
        Path(models_path) if models_path else APP_ROOT / "config" / "models.toml",
        Path(governor_path)
        if governor_path
        else APP_ROOT / "config" / "governor.toml",
        env_path=Path(env_path)
        if env_path
        else (default_env if default_env.exists() else None),
        runs_dir=Path(runs_dir) if runs_dir else APP_ROOT / "projects",
    )
    config = config_store.current

    bus = EventBus(verbosity=verbosity)
    workspace = Workspace(APP_ROOT)
    prompts = PromptLibrary(config.agents_dir)
    budgeter = Budgeter(config.governor.budgets, config.governor.context)
    # AgentRunner and Pipeline take the STORE, not the snapshot: both call
    # `.current` on every use so that `!reload` swaps config mid-session.
    # PromptLibrary and Budgeter take plain values and keep the snapshot.
    runner = AgentRunner(config_store, bus, budgeter, prompts)
    pipeline = Pipeline(
        config=config_store,
        workspace=workspace,
        bus=bus,
        runner=runner,
        budgeter=budgeter,
    )
    router = CommandRouter(
        pipeline=pipeline,
        workspace=workspace,
        config_store=config_store,
        bus=bus,
    )

    root = Path(web_dir) if web_dir else Path(config.web_dir)

    app = FastAPI(title="App Factory", docs_url=None, redoc_url=None)
    app.state.router = router
    app.state.pipeline = pipeline
    app.state.bus = bus
    app.state.workspace = workspace
    app.state.config_store = config_store
    app.state.web_dir = root

    # -- static shell ------------------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        page = root / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail="web/index.html is missing")
        return FileResponse(page, media_type="text/html; charset=utf-8")

    @app.get("/assets/{name}")
    async def asset(name: str) -> FileResponse:
        if name not in ASSETS:
            raise HTTPException(status_code=404, detail="unknown asset")
        target = root / name
        if not target.is_file():
            raise HTTPException(status_code=404, detail="unknown asset")
        return FileResponse(
            target, media_type=_MEDIA_TYPES.get(target.suffix, "text/plain")
        )

    # -- read-only API -----------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "version": CORE_VERSION})

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(router.status_payload())

    @app.get("/api/projects")
    async def api_projects() -> JSONResponse:
        return JSONResponse({"projects": workspace.summary()})

    @app.get("/api/events")
    async def api_events(
        limit: int = Query(default=REPLAY_LIMIT, ge=1, le=5000),
    ) -> JSONResponse:
        history = [
            event.to_dict() for event in bus.replay() if _sendable(bus, event)
        ]
        return JSONResponse({"events": history[-limit:]})

    # -- build preview -----------------------------------------------------

    @app.get("/preview/{slug}/{run_id}/{snap_id}/{bld_id}/{asset_path:path}")
    async def preview(
        slug: str, run_id: str, snap_id: str, bld_id: str, asset_path: str
    ) -> FileResponse:
        _check_segments(slug, run_id, snap_id, bld_id)
        try:
            store = workspace.store_for(slug)
        except Exception as exc:  # workspace errors are 404s to the browser
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        base = store.build_dir(run_id, snap_id, bld_id).resolve()
        target = (base / asset_path).resolve()
        # Containment check first: asset_path is attacker-shaped input.
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(status_code=404, detail="no such build file")

        return FileResponse(
            target,
            headers={"Cache-Control": "no-store"},
        )

    # -- socket ------------------------------------------------------------

    @app.websocket("/ws")
    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = bus.subscribe()
        pending: set[asyncio.Task[Any]] = set()
        pump_task: asyncio.Task[None] | None = None

        async def pump() -> None:
            """The only writer once replay has finished."""
            while True:
                event = await queue.get()
                if not _sendable(bus, event):
                    continue
                await websocket.send_text(_frame("event", event.to_dict()))
                # A Window 1 reply is the one moment the header is always
                # stale, so status follows it immediately.
                if event.channel is Channel.CLI:
                    await websocket.send_text(
                        _frame("status", router.status_payload())
                    )

        try:
            history = [event for event in bus.replay() if _sendable(bus, event)]
            for event in history[-REPLAY_LIMIT:]:
                await websocket.send_text(_frame("event", event.to_dict()))
            await websocket.send_text(_frame("status", router.status_payload()))

            pump_task = asyncio.create_task(pump())

            while True:
                raw = await websocket.receive_text()
                line = _command_line(raw)
                if line is None:
                    continue
                # Dispatched, not awaited: a running build must not block the
                # socket from accepting !status or !log.
                task = asyncio.create_task(router.submit(line))
                pending.add(task)
                task.add_done_callback(pending.discard)
        except WebSocketDisconnect:
            pass
        finally:
            if pump_task is not None:
                pump_task.cancel()
            bus.unsubscribe(queue)

    return app
