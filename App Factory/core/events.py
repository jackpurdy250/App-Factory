"""App Factory - the event bus behind Window 1 and Window 3.

Window 3 is a live view of the audit log, not a separate thing: every event is
appended to runs/<run_id>/events.jsonl regardless of verbosity, and the
verbosity setting only filters what is streamed to the browser. That way
turning the log down never loses history.

Window 1 events carry a closed-vocabulary string (see CliResponse). Nothing
else may ever be routed to the CLI channel.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

from .parser import Verbosity

_VERBOSITY_RANK: dict[Verbosity, int] = {
    Verbosity.QUIET: 0,
    Verbosity.NORMAL: 1,
    Verbosity.TRACE: 2,
}


class Channel(StrEnum):
    #: Window 1. Closed vocabulary only.
    CLI = "cli"
    #: Window 3.
    LOG = "log"
    #: Window 2 control: load a build into a channel.
    PREVIEW = "preview"
    #: Structured status for the UI header.
    STATUS = "status"


@dataclass(slots=True)
class Event:
    channel: Channel
    text: str = ""
    ts: float = field(default_factory=time.time)
    #: Lowest verbosity at which this event is streamed to Window 3.
    min_verbosity: Verbosity = Verbosity.NORMAL
    #: Governor decisions set this. They are printed at every level, because a
    #: silently applied budget cut is the hardest class of bug to diagnose.
    always: bool = False
    #: Left-hand code, e.g. "S4" for a stage or "G4" for a governor rule.
    code: str | None = None
    #: Label printed after the code, e.g. "REVIEW" or "MONOTONIC".
    label: str | None = None
    iteration: int | None = None
    actor: str | None = None
    status: str | None = None
    tokens: int | None = None
    latency_ms: int | None = None
    #: Rendered as a "└─" continuation line under the previous entry.
    continuation: bool = False
    #: Full payloads, attached at trace verbosity.
    detail: dict[str, Any] | None = None

    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.ts))

    def render(self) -> str:
        """The Window 3 line format."""
        if self.channel is Channel.CLI:
            return self.text

        stamp = f"[{self.clock()}]"

        if self.continuation:
            return f"{stamp}   └─ {self.text}"

        head = f"{stamp} {self.code or '  '} {(self.label or ''):<11}"
        iteration = f" it={self.iteration}" if self.iteration is not None else ""

        if self.actor is not None:
            tokens = f"{self.tokens:>5} tok" if self.tokens is not None else " " * 9
            latency = (
                f" {self.latency_ms / 1000:>5.1f}s" if self.latency_ms is not None else ""
            )
            return (
                f"{head}{iteration}  {self.actor:<15} "
                f"{(self.status or ''):<5} {tokens}{latency}"
            ).rstrip()

        body = f"  {self.text}" if self.text else ""
        return f"{head}{iteration}{body}".rstrip()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ts": round(self.ts, 3),
            "channel": self.channel.value,
            "text": self.text,
            "rendered": self.render(),
            "min_verbosity": self.min_verbosity.value,
            "always": self.always,
        }
        for key in (
            "code",
            "label",
            "iteration",
            "actor",
            "status",
            "tokens",
            "latency_ms",
            "detail",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.continuation:
            payload["continuation"] = True
        return payload


class EventBus:
    """Fan-out to connected UI sockets plus an append-only JSONL audit log.

    `emit` is synchronous and non-blocking so that deterministic code paths
    (the parser, the gate) can log without being async.
    """

    def __init__(self, *, verbosity: Verbosity = Verbosity.NORMAL) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._verbosity = verbosity
        self._jsonl_path: Path | None = None
        self._history: list[Event] = []
        self._history_limit = 500

    # -- configuration -----------------------------------------------------

    @property
    def verbosity(self) -> Verbosity:
        return self._verbosity

    def set_verbosity(self, level: Verbosity) -> None:
        self._verbosity = level

    def bind_log(self, path: Path) -> None:
        """Point the audit log at a run directory."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = path

    @property
    def log_path(self) -> Path | None:
        return self._jsonl_path

    # -- subscriptions -----------------------------------------------------

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def replay(self) -> Iterator[Event]:
        """Recent events, so a newly connected window is not blank."""
        return iter(tuple(self._history))

    # -- emission ----------------------------------------------------------

    def visible(self, event: Event) -> bool:
        if event.channel is not Channel.LOG:
            return True
        if event.always:
            return True
        return _VERBOSITY_RANK[event.min_verbosity] <= _VERBOSITY_RANK[self._verbosity]

    def emit(self, event: Event) -> Event:
        self._append_jsonl(event)

        if not self.visible(event):
            return event

        self._history.append(event)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]

        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled browser tab must never stall the pipeline. Drop the
                # oldest event for that subscriber and keep going; the JSONL
                # log is still complete.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return event

    def _append_jsonl(self, event: Event) -> None:
        if self._jsonl_path is None:
            return
        try:
            with self._jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        except OSError:
            # Losing the audit log must not take the pipeline down, but it must
            # be visible, so surface it on the log channel without recursing.
            fallback = Event(
                channel=Channel.LOG,
                code="!!",
                label="AUDIT",
                text=f"could not append to {self._jsonl_path}",
                always=True,
            )
            self._history.append(fallback)

    # -- convenience emitters ---------------------------------------------

    def cli(self, text: str) -> Event:
        """Window 1. Callers must pass a closed-vocabulary string."""
        return self.emit(Event(channel=Channel.CLI, text=text))

    def stage(
        self,
        *,
        code: str,
        label: str,
        iteration: int | None = None,
        text: str = "",
        min_verbosity: Verbosity = Verbosity.QUIET,
    ) -> Event:
        return self.emit(
            Event(
                channel=Channel.LOG,
                code=code,
                label=label,
                iteration=iteration,
                text=text,
                min_verbosity=min_verbosity,
            )
        )

    def agent(
        self,
        *,
        code: str,
        label: str,
        actor: str,
        status: str,
        iteration: int | None = None,
        tokens: int | None = None,
        latency_ms: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Event:
        return self.emit(
            Event(
                channel=Channel.LOG,
                code=code,
                label=label,
                actor=actor,
                status=status,
                iteration=iteration,
                tokens=tokens,
                latency_ms=latency_ms,
                detail=detail,
                min_verbosity=Verbosity.NORMAL,
            )
        )

    def note(
        self, text: str, *, iteration: int | None = None, always: bool = False
    ) -> Event:
        return self.emit(
            Event(
                channel=Channel.LOG,
                text=text,
                iteration=iteration,
                continuation=True,
                always=always,
                min_verbosity=Verbosity.NORMAL,
            )
        )

    def governor(
        self,
        *,
        code: str,
        label: str,
        text: str,
        iteration: int | None = None,
    ) -> Event:
        """Governor decisions print at every verbosity level."""
        return self.emit(
            Event(
                channel=Channel.LOG,
                code=code,
                label=label,
                text=text,
                iteration=iteration,
                always=True,
                min_verbosity=Verbosity.QUIET,
            )
        )

    def trace(self, label: str, detail: dict[str, Any]) -> Event:
        return self.emit(
            Event(
                channel=Channel.LOG,
                code="..",
                label=label,
                text="payload",
                detail=detail,
                min_verbosity=Verbosity.TRACE,
            )
        )

    def preview(
        self,
        *,
        url: str,
        build_id: str,
        channel: str,
        entrypoint: str | None = None,
        manifest: list[dict[str, Any]] | None = None,
        surface: str = "preview",
    ) -> Event:
        """Tell Window 2 to load a build.

        `channel` is how to render it (iframe or source); `surface` is which
        of Window 2's two channels it belongs to: the live preview, or the
        frozen shipped build.
        """
        return self.emit(
            Event(
                channel=Channel.PREVIEW,
                text=url,
                detail={
                    "build_id": build_id,
                    "channel": channel,
                    "url": url,
                    "entrypoint": entrypoint,
                    "surface": surface,
                    "manifest": list(manifest or []),
                },
            )
        )

    def status(self, payload: dict[str, Any]) -> Event:
        return self.emit(Event(channel=Channel.STATUS, text="status", detail=payload))