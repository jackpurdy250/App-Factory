"""The multi-project workspace.

One file at the workspace root, `saved-project-context.json`, records every
project the operator has started. It is what makes `!project use <slug>`
possible: each record carries enough resumable state (head run, head snapshot,
pinned spec version, open blocker count) to pick a build back up without
reading any other project's files.

Two rules hold the design together:

  1. Exactly one project is active at a time. "Concurrent projects" means
     concurrently *maintained*, never concurrently executing. A second build
     cannot start while one is running, so there is never a question of which
     project a bare command applies to.

  2. Build directories never overlap. Every project owns
     `projects/<slug>/runs/`, and the path stored in the registry is relative,
     so moving or copying a workspace does not break it.

The registry is the Observer's memory across projects; this module is the only
thing that writes it, and it writes atomically.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .schemas import (
    PROJECTS_DIRNAME,
    REGISTRY_FILENAME,
    ProjectRecord,
    ProjectRegistry,
    ProjectStatus,
    ProjectType,
    Stack,
    default_runs_dir,
    profile_for,
    utcnow,
)
from .store import SnapshotStore

#: Used when a workspace is opened for the first time and nothing exists yet.
DEFAULT_SLUG = "default"
DEFAULT_TITLE = "Default project"


class WorkspaceError(RuntimeError):
    """Raised for any workspace-level refusal.

    Carries the offending slug when there is one so the caller can put it in
    the Window 3 payload without re-parsing the message.
    """

    def __init__(self, message: str, *, slug: str | None = None) -> None:
        super().__init__(message)
        self.slug = slug


def _atomic_write(path: Path, text: str) -> None:
    """Write a file so a crash mid-write cannot corrupt it.

    The registry is the one file whose loss would orphan every project's
    history, so it is written to a temporary file in the same directory,
    flushed to disk, and then moved into place. `os.replace` is atomic on
    every platform we target.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class ProjectHandle:
    """Everything the pipeline needs to work on one project."""

    record: ProjectRecord
    runs_dir: Path
    store: SnapshotStore

    @property
    def slug(self) -> str:
        return self.record.slug

    @property
    def stack(self) -> Stack:
        return self.record.stack

    @property
    def project_type(self) -> ProjectType:
        return self.record.project_type


class Workspace:
    """Reads and writes `saved-project-context.json` and the project tree."""

    def __init__(self, root: Path | str, *, registry_path: Path | str | None = None):
        self._root = Path(root)
        self._registry_path = (
            Path(registry_path)
            if registry_path is not None
            else self._root / REGISTRY_FILENAME
        )
        self._registry: ProjectRegistry | None = None
        self._saved_hash: str | None = None

    # -- paths ------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def registry_path(self) -> Path:
        return self._registry_path

    @property
    def projects_dir(self) -> Path:
        return self._root / PROJECTS_DIRNAME

    def runs_path(self, slug: str) -> Path:
        """Absolute runs directory for a project.

        The registry stores this relative; it is resolved against the
        workspace root here and nowhere else.
        """

        record = self.record(slug)
        return self._root / record.runs_dir

    def store_for(self, slug: str) -> SnapshotStore:
        return SnapshotStore(self.runs_path(slug))

    # -- registry io ------------------------------------------------------

    def load(self) -> ProjectRegistry:
        """Read the registry, creating an empty one if the file is absent."""

        if self._registry is not None:
            return self._registry

        if not self._registry_path.exists():
            self._registry = ProjectRegistry()
            self._saved_hash = None
            return self._registry

        try:
            raw = self._registry_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkspaceError(f"cannot read {self._registry_path}: {exc}") from exc

        try:
            registry = ProjectRegistry.model_validate_json(raw)
        except ValidationError as exc:
            raise WorkspaceError(
                f"{REGISTRY_FILENAME} is not a valid workspace registry: {exc}"
            ) from exc

        self._registry = registry
        self._saved_hash = registry.compute_hash()
        return registry

    def save(self) -> bool:
        """Persist the registry if anything actually changed.

        Returns True when a write happened. The hash excludes `updated_at`, so
        re-saving an unchanged registry is a no-op rather than a timestamp
        churn that would make the file look edited.
        """

        registry = self.load()
        current = registry.compute_hash()
        if current == self._saved_hash:
            return False
        _atomic_write(self._registry_path, registry.model_dump_json(indent=2))
        self._saved_hash = current
        return True

    def reload(self) -> ProjectRegistry:
        """Drop the cached registry and read it again from disk."""

        self._registry = None
        self._saved_hash = None
        return self.load()

    # -- reads ------------------------------------------------------------

    def slugs(self) -> list[str]:
        return self.load().slugs()

    def has(self, slug: str) -> bool:
        return self.load().has(slug)

    def record(self, slug: str) -> ProjectRecord:
        try:
            return self.load().record(slug)
        except KeyError as exc:
            raise WorkspaceError(f"no such project: {slug!r}", slug=slug) from exc

    def active(self) -> ProjectRecord:
        """The selected project, or a refusal if nothing is selected."""

        record = self.load().active
        if record is None:
            raise WorkspaceError("no active project")
        return record

    def active_slug(self) -> str | None:
        record = self.load().active
        return record.slug if record is not None else None

    def summary(self) -> list[dict[str, Any]]:
        """One row per project, most recently active first.

        This is exactly what `!project` prints, so it stays flat and
        JSON-safe.
        """

        registry = self.load()
        rows: list[dict[str, Any]] = []
        for slug in registry.slugs():
            record = registry.record(slug)
            rows.append(
                {
                    "slug": record.slug,
                    "title": record.title,
                    "active": registry.active_project == record.slug,
                    "type": record.project_type.value,
                    "stack": record.stack.value,
                    "preview_mode": record.preview_mode.value,
                    "status": record.status.value,
                    "has_history": record.has_history,
                    "head_run_id": record.head_run_id,
                    "head_snapshot": record.head_snapshot,
                    "open_blockers": record.open_blocker_count,
                    "pinned_spec_version": record.pinned_spec_version,
                    "shipped": len(record.shipped_build_ids),
                    "last_active_at": record.last_active_at.isoformat(),
                    "runs_dir": record.runs_dir,
                }
            )
        return rows

    # -- writes -----------------------------------------------------------

    def create(
        self,
        slug: str,
        *,
        title: str | None = None,
        project_type: ProjectType = ProjectType.APP,
        stack: Stack = Stack.WEB,
        activate: bool = True,
    ) -> ProjectRecord:
        """Register a new project and create its build directory."""

        registry = self.load()
        if registry.has(slug):
            raise WorkspaceError(f"project already exists: {slug!r}", slug=slug)

        try:
            record = ProjectRecord(
                slug=slug,
                title=title or slug,
                project_type=project_type,
                stack=stack,
                runs_dir=default_runs_dir(slug),
            )
        except ValidationError as exc:
            raise WorkspaceError(f"{slug!r} is not a usable project: {exc}", slug=slug) from exc

        # Touching the profile here fails fast on a stack with no artifact
        # policy, rather than at the first build.
        profile_for(record.stack)

        runs_dir = self._root / record.runs_dir
        try:
            runs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(
                f"cannot create build directory for {slug!r}: {exc}", slug=slug
            ) from exc

        registry.upsert(record)
        if activate:
            registry.activate(slug)
        self.save()
        return registry.record(slug)

    def activate(self, slug: str) -> ProjectRecord:
        """Select a project. Never starts anything."""

        registry = self.load()
        try:
            registry.activate(slug)
        except KeyError as exc:
            raise WorkspaceError(f"no such project: {slug!r}", slug=slug) from exc
        self.save()
        return registry.record(slug)

    def touch(self, slug: str, **updates: object) -> ProjectRecord:
        """Patch one record's mechanical fields."""

        registry = self.load()
        if not registry.has(slug):
            raise WorkspaceError(f"no such project: {slug!r}", slug=slug)
        try:
            registry.touch(slug, **updates)
        except ValidationError as exc:
            raise WorkspaceError(
                f"cannot update {slug!r}: {exc}", slug=slug
            ) from exc
        self.save()
        return registry.record(slug)

    def ensure_active(
        self,
        *,
        project_type: ProjectType = ProjectType.APP,
        stack: Stack = Stack.WEB,
    ) -> ProjectRecord:
        """Guarantee there is a project to work on.

        A brand-new workspace gets one default project so the operator can
        type `#route` immediately instead of being made to run `!project new`
        before anything at all works.
        """

        registry = self.load()
        current = registry.active
        if current is not None:
            return current
        if registry.projects:
            return self.activate(registry.slugs()[0])
        return self.create(
            DEFAULT_SLUG,
            title=DEFAULT_TITLE,
            project_type=project_type,
            stack=stack,
        )

    def handle(self, slug: str | None = None) -> ProjectHandle:
        """Bundle a record with its resolved paths and snapshot store."""

        record = self.record(slug) if slug is not None else self.active()
        runs_dir = self._root / record.runs_dir
        runs_dir.mkdir(parents=True, exist_ok=True)
        return ProjectHandle(
            record=record, runs_dir=runs_dir, store=SnapshotStore(runs_dir)
        )

    def record_progress(
        self,
        slug: str,
        *,
        run_id: str | None = None,
        snapshot_id: str | None = None,
        status: ProjectStatus | None = None,
        open_blocker_count: int | None = None,
        pinned_spec_version: int | None = None,
        digest: str | None = None,
        shipped_build_id: str | None = None,
    ) -> ProjectRecord:
        """Write back what a run achieved, so `!project use` can resume it.

        Called at stage boundaries rather than on every write: the registry is
        a resumption index, not a second copy of the state bus.
        """

        registry = self.load()
        if not registry.has(slug):
            raise WorkspaceError(f"no such project: {slug!r}", slug=slug)

        updates: dict[str, object] = {}
        if run_id is not None:
            updates["head_run_id"] = run_id
        if snapshot_id is not None:
            updates["head_snapshot"] = snapshot_id
        if status is not None:
            updates["status"] = status
        if open_blocker_count is not None:
            updates["open_blocker_count"] = max(0, open_blocker_count)
        if pinned_spec_version is not None:
            updates["pinned_spec_version"] = pinned_spec_version
        if digest is not None:
            updates["digest"] = digest
        if shipped_build_id is not None:
            existing = list(registry.record(slug).shipped_build_ids)
            if shipped_build_id not in existing:
                existing.append(shipped_build_id)
            updates["shipped_build_ids"] = existing

        if not updates:
            return registry.record(slug)

        updates["last_active_at"] = utcnow()
        return self.touch(slug, **updates)
