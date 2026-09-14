"""App Factory - artifact extraction and the implementer write ACL (G8).

The Implementer is the only role whose product is not JSON. Its reply is
parsed deterministically here:

    <plan>
    { "plan_summary": "...", "components": [...], "files": [...],
      "dependencies": [...], "deviations": [...], "entrypoint": "index.html" }
    </plan>

    <file path="index.html">
    ...file body...
    </file>

Nothing about this is negotiable at runtime: a reply that does not match is a
validation failure, which the LLM layer gets exactly one repair attempt to fix.

Every extracted path is checked before anything touches the disk. Code never
enters project_state.json - the bus stores the path and the sha256 only.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from .schemas.common import STACK_PROFILES, Stack, StackProfile, profile_for
from .schemas.state import ArtifactFile

#: A generated build is one small project, not a monorepo. These ceilings
#: exist so a runaway reply cannot fill the disk.
MAX_FILES = 60
MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 2_000_000

#: Union of every stack's writable suffixes, consulted only when no stack is
#: supplied. The per-stack profile is the real ACL: a Python project may not
#: write .html, and a web project may not write .go.
ALLOWED_SUFFIXES: frozenset[str] = frozenset().union(
    *(profile.allowed_suffixes for profile in STACK_PROFILES.values())
)

#: Extensionless filenames some stack has declared legal (CMakeLists.txt).
ALLOWED_BARE_FILENAMES: frozenset[str] = frozenset().union(
    *(profile.bare_filenames for profile in STACK_PROFILES.values())
)

#: The web stack's preference list, kept for callers that want the default.
#: Prefer profile_for(stack).entrypoint_preference.
ENTRYPOINT_PREFERENCE = STACK_PROFILES[Stack.WEB].entrypoint_preference

_FILE_BLOCK_RE = re.compile(
    r"<file\s+path=\"(?P<path>[^\"<>\r\n]+)\"\s*>\r?\n(?P<body>.*?)\r?\n?</file>",
    re.DOTALL,
)
_PLAN_BLOCK_RE = re.compile(r"<plan\s*>(?P<body>.*?)</plan>", re.DOTALL)
_NOTES_BLOCK_RE = re.compile(r"<notes\s*>(?P<body>.*?)</notes>", re.DOTALL)


class ArtifactError(RuntimeError):
    """Raised when an implementer reply cannot be turned into a build."""


def safe_relpath(raw: str, *, stack: Stack | str | None = None) -> str:
    """Validate one implementer-supplied path.

    The implementer may only write relative paths inside its own build
    directory. This is the file half of G8; the state-region half lives in
    state_manager.py.

    When `stack` is supplied the path must also be legal for that stack's
    profile. That is what keeps a polyglot workspace honest: an HTML file in
    a Go project is a refused write, not a silently accepted one.
    """
    candidate = (raw or "").strip().replace("\\", "/")
    if not candidate:
        raise ArtifactError("empty file path")
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
        raise ArtifactError(f"absolute paths are not permitted: {raw!r}")
    if "\x00" in candidate:
        raise ArtifactError(f"null byte in path: {raw!r}")

    pure = PurePosixPath(candidate)
    parts = [part for part in pure.parts if part not in (".",)]
    if any(part == ".." for part in parts):
        raise ArtifactError(f"path traversal is not permitted: {raw!r}")
    if not parts:
        raise ArtifactError(f"path resolves to nothing: {raw!r}")
    if any(part.startswith(".") for part in parts):
        raise ArtifactError(f"dotfiles are not permitted: {raw!r}")
    if len(parts) > 6:
        raise ArtifactError(f"path is nested too deeply: {raw!r}")

    normalized = "/".join(parts)

    if stack is not None:
        profile = profile_for(stack)
        if not profile.permits(normalized):
            raise ArtifactError(
                f"{normalized} is not writable for the {profile.stack.value} "
                f"stack (allowed: {', '.join(sorted(profile.allowed_suffixes))})"
            )
        return normalized

    if parts[-1] in ALLOWED_BARE_FILENAMES:
        return normalized
    suffix = PurePosixPath(normalized).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ArtifactError(
            f"file extension {suffix or '(none)'} is not permitted "
            f"(allowed: {', '.join(sorted(ALLOWED_SUFFIXES))})"
        )
    return normalized


def extract_plan(text: str) -> dict[str, object]:
    """Pull the architecture plan out of an implementer reply."""
    match = _PLAN_BLOCK_RE.search(text or "")
    if match is None:
        raise ArtifactError("reply contains no <plan>...</plan> block")
    body = match.group("body").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z0-9_-]*\r?\n", "", body)
        body = re.sub(r"\r?\n?```$", "", body).strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"<plan> is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ArtifactError("<plan> must contain a JSON object")
    return parsed


def extract_notes(text: str) -> str | None:
    match = _NOTES_BLOCK_RE.search(text or "")
    if match is None:
        return None
    notes = match.group("body").strip()
    return notes or None


def extract_files(
    text: str, *, stack: Stack | str | None = None
) -> list[tuple[str, str]]:
    """Pull every <file> block out of an implementer reply.

    Returns [(normalized_path, body)]. Raises if the reply is empty, oversized,
    or contains a duplicate, unsafe, or off-stack path.
    """
    matches = list(_FILE_BLOCK_RE.finditer(text or ""))
    if not matches:
        raise ArtifactError("reply contains no <file path=\"...\"> blocks")
    if len(matches) > MAX_FILES:
        raise ArtifactError(
            f"reply declares {len(matches)} files; the ceiling is {MAX_FILES}"
        )

    seen: dict[str, str] = {}
    total = 0
    for match in matches:
        path = safe_relpath(match.group("path"), stack=stack)
        body = match.group("body")
        size = len(body.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ArtifactError(
                f"{path} is {size} bytes; the per-file ceiling is {MAX_FILE_BYTES}"
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ArtifactError(
                f"build exceeds the total ceiling of {MAX_TOTAL_BYTES} bytes"
            )
        if path in seen:
            raise ArtifactError(f"duplicate file path in one reply: {path}")
        seen[path] = body
    return list(seen.items())


def write_build(build_dir: Path, files: list[tuple[str, str]]) -> list[ArtifactFile]:
    """Write extracted files into a build directory and return the manifest.

    Each resolved path is re-checked against the build root after resolution,
    so a symlink or an unusual path component cannot escape.
    """
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    root = build_dir.resolve()

    manifest: list[ArtifactFile] = []
    for path, body in files:
        target = (root / path).resolve()
        if target != root and root not in target.parents:
            raise ArtifactError(f"refusing to write outside the build root: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = body.encode("utf-8")
        target.write_bytes(data)
        manifest.append(
            ArtifactFile(
                path=path,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
    return manifest


def _entrypoint_suffixes(profile: StackProfile) -> frozenset[str]:
    """Suffixes that may serve as an entrypoint for a stack.

    Derived from the profile's preferred names rather than declared
    separately, so there is one source of truth: a stack whose preferred
    entrypoint is main.py accepts .py and nothing else.
    """
    return frozenset(
        PurePosixPath(name).suffix.lower()
        for name in profile.entrypoint_preference
        if PurePosixPath(name).suffix
    )


def choose_entrypoint(
    declared: object,
    manifest: list[ArtifactFile],
    *,
    stack: Stack | str = Stack.WEB,
) -> str:
    """Resolve the build entrypoint for a stack.

    A declared entrypoint must actually exist in the manifest - that mismatch
    is exactly the bug behind a blank Window 2, so it fails loudly here
    instead of silently there.
    """
    profile = profile_for(stack)
    paths = {item.path for item in manifest}
    if isinstance(declared, str) and declared.strip():
        candidate = declared.strip().replace("\\", "/").lstrip("./")
        if candidate not in paths:
            raise ArtifactError(
                f"declared entrypoint {candidate!r} is not among the written files "
                f"({', '.join(sorted(paths))})"
            )
        return candidate

    if profile.entrypoint_required:
        raise ArtifactError(
            f"the {profile.stack.value} stack requires the plan to declare an "
            "entrypoint explicitly"
        )

    for preferred in profile.entrypoint_preference:
        if preferred in paths:
            return preferred
    suffixes = _entrypoint_suffixes(profile)
    for item in manifest:
        if PurePosixPath(item.path).suffix.lower() in suffixes:
            return item.path
    raise ArtifactError(
        f"no entrypoint: the build contains no "
        f"{' or '.join(sorted(suffixes)) or 'suitable'} file and the plan "
        "declared none"
    )


def read_build_sources(
    build_dir: Path,
    manifest: list[ArtifactFile],
    *,
    suffixes: frozenset[str] | None = None,
    max_bytes: int = 120_000,
) -> dict[str, str]:
    """Load artifact bodies for a reviewer projection.

    The critics need the code, and the code is deliberately not in the state
    bus, so it is read back from disk here at projection time.
    """
    build_dir = Path(build_dir)
    sources: dict[str, str] = {}
    budget = max_bytes
    for item in manifest:
        if suffixes is not None:
            if PurePosixPath(item.path).suffix.lower() not in suffixes:
                continue
        path = build_dir / item.path
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        encoded_len = len(text.encode("utf-8"))
        if encoded_len > budget:
            sources[item.path] = text[: max(budget, 0)] + "\n... [truncated]"
            break
        budget -= encoded_len
        sources[item.path] = text
    return sources