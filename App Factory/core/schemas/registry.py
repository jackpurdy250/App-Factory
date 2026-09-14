"""App Factory - saved-project-context.json, the workspace registry.

One file at the workspace root listing every project the operator has
started. It is what makes `!project use <slug>` possible: the registry holds
enough per-project state to resume a build mid-stream without reading any
other project's files.

Ownership is split, deliberately:

  Observer (via a context_save envelope)   title, digest, pinned_spec_version,
                                           open_blocker_count
  Deterministic Python                     head_run_id, head_snapshot, status,
                                           shipped_build_ids, timestamps,
                                           runs_dir, active_project

The Observer describes; Python records. A failed or refused LLM call can
therefore never lose the operator's place in a project.

Layout, one tree per project, so switching never overwrites a build:

    saved-project-context.json
    projects/<slug>/runs/<run_id>/snapshots/it-NNN/
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Optional

from pydantic import Field, model_validator

from .common import (
    SCHEMA_VERSION,
    BuildId,
    PreviewMode,
    ProjectSlug,
    ProjectType,
    RunId,
    SnapshotId,
    Stack,
    Strict,
    profile_for,
    utcnow,
)

#: Filename at the workspace root. Managed by the Observer, written by Python.
REGISTRY_FILENAME = "saved-project-context.json"

#: Directory holding one subtree per project.
PROJECTS_DIRNAME = "projects"


def default_runs_dir(slug: str) -> str:
    """Workspace-relative runs directory for a project.

    Relative, not absolute: a workspace stays portable if it is moved or
    copied, and nothing in the registry then depends on one machine's paths.
    """
    return f"{PROJECTS_DIRNAME}/{slug}/runs"


class ProjectStatus(StrEnum):
    """Project-level status, distinct from the per-run PipelineStatus.

    A project is ACTIVE only while it is the selected one; everything else
    that has work in flight is IDLE. That distinction is what keeps
    "concurrent projects" honest: they are concurrently *maintained*, never
    concurrently executing.
    """

    ACTIVE = "active"
    IDLE = "idle"
    BLOCKED = "blocked"
    SHIPPED = "shipped"
    ARCHIVED = "archived"


class ProjectRecord(Strict):
    """Everything needed to resume one project without touching another."""

    slug: ProjectSlug
    title: str = Field(min_length=1)
    project_type: ProjectType = ProjectType.APP
    stack: Stack = Stack.WEB
    created_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow)
    runs_dir: str = Field(min_length=1)
    head_run_id: Optional[RunId] = None
    head_snapshot: Optional[SnapshotId] = None
    status: ProjectStatus = ProjectStatus.IDLE
    pinned_spec_version: Optional[int] = Field(default=None, ge=0)
    open_blocker_count: int = Field(default=0, ge=0)
    shipped_build_ids: list[BuildId] = Field(default_factory=list)
    digest: Optional[str] = None

    @property
    def preview_mode(self) -> PreviewMode:
        """Derived, never stored: the stack decides how Window 2 renders."""
        return profile_for(self.stack).preview

    @property
    def has_history(self) -> bool:
        return self.head_run_id is not None


class ProjectRegistry(Strict):
    """The whole workspace. Exactly one project may be active at a time."""

    schema_version: str = SCHEMA_VERSION
    active_project: Optional[ProjectSlug] = None
    updated_at: datetime = Field(default_factory=utcnow)
    projects: dict[str, ProjectRecord] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> "ProjectRegistry":
        for key, record in self.projects.items():
            if key != record.slug:
                raise ValueError(
                    f"registry key {key!r} does not match record slug {record.slug!r}"
                )
        if self.active_project is not None and self.active_project not in self.projects:
            raise ValueError(
                f"active_project {self.active_project!r} is not in the registry"
            )
        return self

    # -- reads ------------------------------------------------------------

    def has(self, slug: str) -> bool:
        return slug in self.projects

    def record(self, slug: str) -> ProjectRecord:
        try:
            return self.projects[slug]
        except KeyError:
            raise KeyError(f"no such project: {slug!r}") from None

    @property
    def active(self) -> Optional[ProjectRecord]:
        if self.active_project is None:
            return None
        return self.projects[self.active_project]

    def slugs(self) -> list[str]:
        """Most recently active first, which is the order `!project` lists."""
        return [
            record.slug
            for record in sorted(
                self.projects.values(),
                key=lambda r: r.last_active_at,
                reverse=True,
            )
        ]

    # -- writes -----------------------------------------------------------
    #
    # Each returns self so a caller can chain, and each reassigns the whole
    # field rather than mutating in place: with validate_assignment on, that
    # is what re-runs the consistency check above.

    def upsert(self, record: ProjectRecord) -> "ProjectRegistry":
        projects = dict(self.projects)
        projects[record.slug] = record
        self.projects = projects
        self.updated_at = utcnow()
        return self

    def activate(self, slug: str) -> "ProjectRegistry":
        """Select a project. Demotes whoever was active; never runs anything."""
        if slug not in self.projects:
            raise KeyError(f"no such project: {slug!r}")
        projects = dict(self.projects)
        for key, record in projects.items():
            if record.status is ProjectStatus.ACTIVE:
                projects[key] = record.model_copy(
                    update={"status": ProjectStatus.IDLE}
                )
        projects[slug] = projects[slug].model_copy(
            update={"status": ProjectStatus.ACTIVE, "last_active_at": utcnow()}
        )
        self.projects = projects
        self.active_project = slug
        self.updated_at = utcnow()
        return self

    def touch(self, slug: str, **updates: object) -> "ProjectRegistry":
        """Patch one record's mechanical fields and stamp it."""
        record = self.record(slug)
        merged = {"last_active_at": utcnow(), **updates}
        projects = dict(self.projects)
        projects[slug] = record.model_copy(update=merged)
        self.projects = projects
        self.updated_at = utcnow()
        return self

    # -- serialization ----------------------------------------------------

    def canonical_json(self) -> str:
        """Deterministic form, excluding `updated_at`.

        Excluded so an unchanged registry hashes identically across saves and
        the workspace manager can skip a pointless write.
        """
        data = json.loads(self.model_dump_json())
        data.pop("updated_at", None)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:16]
