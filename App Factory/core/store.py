"""App Factory - the snapshot store.

Snapshots instead of rollback. Every iteration writes an immutable directory;
nothing in the system ever reverses a write.

    runs/<run_id>/
      snapshots/it-000/project_state.json
      snapshots/it-000/builds/b-000/...
      snapshots/it-001/...
      events.jsonl
      HEAD

Rollback has to correctly reverse partial writes across a state file, a build
directory, an issue ledger, and a rule set. Forking forward has no reverse
operation to get wrong, and it preserves the failure history that makes the
monotonic-progress check (G4) possible.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .schemas.state import ProjectState

STATE_FILENAME = "project_state.json"
HEAD_FILENAME = "HEAD"
EVENTS_FILENAME = "events.jsonl"
SEALED_FILENAME = "SEALED.json"
SNAPSHOTS_DIRNAME = "snapshots"
BUILDS_DIRNAME = "builds"

_RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}Z-[0-9a-f]{4}$")
_SNAPSHOT_ID_RE = re.compile(r"^it-\d{3}$")
_BUILD_ID_RE = re.compile(r"^b-\d{3}$")


class StoreError(RuntimeError):
    """Raised on any violation of the write-once contract."""


def new_run_id(now: datetime | None = None) -> str:
    """Generate a run id matching the RunId schema pattern."""
    moment = now or datetime.now(timezone.utc)
    return f"{moment.strftime('%Y-%m-%dT%H%MZ')}-{secrets.token_hex(2)}"


def snapshot_id(iteration: int) -> str:
    if iteration < 0 or iteration > 999:
        raise StoreError(f"iteration out of range for a snapshot id: {iteration}")
    return f"it-{iteration:03d}"


def build_id(iteration: int) -> str:
    if iteration < 0 or iteration > 999:
        raise StoreError(f"iteration out of range for a build id: {iteration}")
    return f"b-{iteration:03d}"


class SnapshotStore:
    """Filesystem layout owner. The only component that writes run directories."""

    def __init__(self, runs_root: Path | str) -> None:
        self.runs_root = Path(runs_root)

    # -- paths -------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        if not _RUN_ID_RE.match(run_id):
            raise StoreError(f"malformed run id: {run_id!r}")
        return self.runs_root / run_id

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / EVENTS_FILENAME

    def head_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / HEAD_FILENAME

    def snapshots_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / SNAPSHOTS_DIRNAME

    def snapshot_dir(self, run_id: str, snap_id: str) -> Path:
        if not _SNAPSHOT_ID_RE.match(snap_id):
            raise StoreError(f"malformed snapshot id: {snap_id!r}")
        return self.snapshots_dir(run_id) / snap_id

    def state_path(self, run_id: str, snap_id: str) -> Path:
        return self.snapshot_dir(run_id, snap_id) / STATE_FILENAME

    def build_dir(self, run_id: str, snap_id: str, bld_id: str) -> Path:
        if not _BUILD_ID_RE.match(bld_id):
            raise StoreError(f"malformed build id: {bld_id!r}")
        return self.snapshot_dir(run_id, snap_id) / BUILDS_DIRNAME / bld_id

    # -- run lifecycle -----------------------------------------------------

    def create_run(self, run_id: str) -> Path:
        directory = self.run_dir(run_id)
        if directory.exists():
            raise StoreError(f"run directory already exists: {directory}")
        (directory / SNAPSHOTS_DIRNAME).mkdir(parents=True)
        return directory

    def list_runs(self) -> list[str]:
        if not self.runs_root.exists():
            return []
        return sorted(
            entry.name
            for entry in self.runs_root.iterdir()
            if entry.is_dir() and _RUN_ID_RE.match(entry.name)
        )

    # -- snapshots ---------------------------------------------------------

    def prepare_snapshot(self, run_id: str, snap_id: str) -> Path:
        """Create an empty snapshot directory. Fails if it already exists."""
        directory = self.snapshot_dir(run_id, snap_id)
        if directory.exists():
            raise StoreError(f"snapshot {snap_id} already exists (write-once)")
        (directory / BUILDS_DIRNAME).mkdir(parents=True)
        return directory

    def write_state(self, run_id: str, snap_id: str, state: ProjectState) -> Path:
        """Write project_state.json into a snapshot. Write-once: an existing
        state file is never overwritten."""
        directory = self.snapshot_dir(run_id, snap_id)
        if not directory.exists():
            self.prepare_snapshot(run_id, snap_id)
        target = self.state_path(run_id, snap_id)
        if target.exists():
            raise StoreError(
                f"{STATE_FILENAME} already written for {snap_id} (write-once); "
                "fork a new snapshot instead"
            )
        payload = json.loads(state.model_dump_json())
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp, target)
        return target

    def read_state(self, run_id: str, snap_id: str) -> ProjectState:
        target = self.state_path(run_id, snap_id)
        if not target.exists():
            raise StoreError(f"no {STATE_FILENAME} for {run_id}/{snap_id}")
        return ProjectState.model_validate_json(target.read_text(encoding="utf-8"))

    def list_snapshots(self, run_id: str) -> list[str]:
        directory = self.snapshots_dir(run_id)
        if not directory.exists():
            return []
        return sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_dir() and _SNAPSHOT_ID_RE.match(entry.name)
        )

    def snapshot_index(self, run_id: str) -> list[dict[str, object]]:
        """What `!snapshots` prints."""
        head = self.head(run_id)
        index: list[dict[str, object]] = []
        for snap in self.list_snapshots(run_id):
            state_file = self.state_path(run_id, snap)
            builds_dir = self.snapshot_dir(run_id, snap) / BUILDS_DIRNAME
            builds = (
                sorted(entry.name for entry in builds_dir.iterdir() if entry.is_dir())
                if builds_dir.exists()
                else []
            )
            index.append(
                {
                    "snapshot_id": snap,
                    "head": snap == head,
                    "sealed": self.is_sealed(run_id, snap),
                    "state_written": state_file.exists(),
                    "builds": builds,
                }
            )
        return index

    # -- HEAD --------------------------------------------------------------

    def head(self, run_id: str) -> str | None:
        path = self.head_path(run_id)
        if not path.exists():
            return None
        value = path.read_text(encoding="utf-8").strip()
        return value or None

    def set_head(self, run_id: str, snap_id: str) -> None:
        if not _SNAPSHOT_ID_RE.match(snap_id):
            raise StoreError(f"malformed snapshot id: {snap_id!r}")
        if not self.snapshot_dir(run_id, snap_id).exists():
            raise StoreError(f"cannot point HEAD at missing snapshot {snap_id}")
        path = self.head_path(run_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(snap_id + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # -- sealing -----------------------------------------------------------

    def is_sealed(self, run_id: str, snap_id: str) -> bool:
        return (self.snapshot_dir(run_id, snap_id) / SEALED_FILENAME).exists()

    def seal(self, run_id: str, snap_id: str, *, bld_id: str) -> dict[str, object]:
        """Freeze a snapshot on #ship and record the hash of every artifact.

        A sealed snapshot is permanently addressable: the recorded hashes are
        what let you prove later that a shipped build has not been edited.
        """
        directory = self.snapshot_dir(run_id, snap_id)
        if not directory.exists():
            raise StoreError(f"cannot seal missing snapshot {snap_id}")
        marker = directory / SEALED_FILENAME
        if marker.exists():
            raise StoreError(f"snapshot {snap_id} is already sealed")

        build_path = self.build_dir(run_id, snap_id, bld_id)
        hashes: dict[str, str] = {}
        if build_path.exists():
            for path in sorted(build_path.rglob("*")):
                if path.is_file():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    hashes[str(path.relative_to(build_path))] = digest

        record: dict[str, object] = {
            "run_id": run_id,
            "snapshot_id": snap_id,
            "build_id": bld_id,
            "sealed_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(hashes),
            "hashes": hashes,
        }
        marker.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        # Make the sealed build read-only. Belt and braces alongside the marker:
        # the marker is the contract, this is the reminder.
        if build_path.exists():
            for path in build_path.rglob("*"):
                if path.is_file():
                    path.chmod(0o444)
        return record