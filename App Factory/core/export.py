"""App Factory - the universal output line (polyglot IDE export).

The AI chain produces code as text. Turning that text into a project a
developer can open in *any* IDE is deliberately NOT the model's job: IDE
integrations differ between vendors, change often, and break, and asking a
model to target them would put a moving, environment-specific dependency on
the one part of the system that must stay portable.

So the output end of the pipeline is hard-coded and deterministic, and this
module is that section. It takes the exact file text emitted by the
Implementer, identifies each file's language and the third-party libraries it
imports, and writes the whole build out as an ordinary directory tree - the
one interface every IDE already understands: a folder of files.

That is the "more streamlined way": rather than a fragile per-IDE plugin, the
universal interface is the filesystem. One code path materializes a build for
VS Code, PyCharm, IntelliJ, or Neovim alike, with a generated dependency
manifest (requirements.txt / package.json) and an IDE_EXPORT.md explaining how
to open and install it. No model call, no IDE API, no network.

Stack policy is unchanged: the profiles in schemas/common.py remain the single
source of truth for what a stack may write, and this module refuses any path a
profile refuses. With no stack declared, the export defaults to web, exactly
like the rest of the factory.
"""

from __future__ import annotations

import json
import sys
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .artifacts import ArtifactError, safe_relpath, write_build
from .schemas.common import Stack, profile_for


# ---------------------------------------------------------------------------
# Language identification
# ---------------------------------------------------------------------------

#: Suffix -> language label. Identification only; what a stack may WRITE is
#: still governed by the stack profiles in schemas/common.py.
_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".sh": "shell",
    ".bash": "shell",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".svg": "svg",
}


def identify_language(path: str) -> str | None:
    """Return a language label for a file path, or None for data/docs."""
    suffix = PurePosixPath(path).suffix.lower()
    return _LANGUAGE_BY_SUFFIX.get(suffix)


# ---------------------------------------------------------------------------
# Library detection
#
# Each detector reads source text and returns the set of THIRD-PARTY libraries
# it imports. Standard-library and local (in-build) modules are filtered out so
# a generated manifest lists only what a fresh checkout would need to install.
# Detection is regex-based and deliberately conservative: a miss shows up in
# the report as "not detected", never as a wrong dependency in a manifest.
# ---------------------------------------------------------------------------

_PY_IMPORT = re.compile(r"^[ \t]*(?:import|from)[ \t]+([A-Za-z_][\w.]*)", re.MULTILINE)

_NODE_BUILTINS = frozenset({
    "assert", "buffer", "child_process", "cluster", "console", "crypto",
    "dgram", "dns", "domain", "events", "fs", "http", "http2", "https",
    "inspector", "module", "net", "os", "path", "perf_hooks", "process",
    "punycode", "querystring", "readline", "repl", "stream",
    "string_decoder", "timers", "tls", "trace_events", "tty", "url", "util",
    "v8", "vm", "worker_threads", "zlib",
})
_JS_FROM = re.compile(r"""\bfrom\s*['\"]([^'\"]+)['\"]""")
_JS_BARE_IMPORT = re.compile(r"""\bimport\s*['\"]([^'\"]+)['\"]""")
_JS_CALL = re.compile(r"""\b(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)""")

_GO_IMPORT_BLOCK = re.compile(r"import\s*\((?P<body>[^)]*)\)", re.DOTALL)
_GO_SINGLE = re.compile(r'import\s+(?:[\w.]+\s+)?"(?P<path>[^"]+)"')
_GO_STRING = re.compile(r'"([^"]+)"')

_RUST_USE = re.compile(r"^[ \t]*use\s+([A-Za-z_]\w*)", re.MULTILINE)
_RUST_CRATE = re.compile(r"^[ \t]*extern\s+crate\s+([A-Za-z_]\w*)", re.MULTILINE)
_RUST_MOD = re.compile(r"^[ \t]*(?:pub\s+)?mod\s+([A-Za-z_]\w*)\s*;", re.MULTILINE)
_RUST_BUILTIN = frozenset({"std", "core", "alloc", "proc_macro", "test",
                           "crate", "self", "super"})

_JAVA_IMPORT = re.compile(r"^[ \t]*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)
_CSHARP_USING = re.compile(r"^[ \t]*using\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)
_CPP_INCLUDE = re.compile(r"^[ \t]*#\s*include\s*<([^>]+)>", re.MULTILINE)
_C_STD_HEADERS = frozenset({
    "stdio.h", "stdlib.h", "string.h", "math.h", "time.h", "assert.h",
    "ctype.h", "errno.h", "stdint.h", "stddef.h", "stdbool.h", "inttypes.h",
    "limits.h", "float.h", "signal.h", "stdarg.h",
})
_CPP_STD = frozenset({
    "algorithm", "array", "atomic", "bitset", "chrono", "cmath", "complex",
    "cstdint", "cstdio", "cstdlib", "cstring", "deque", "exception",
    "filesystem", "fstream", "functional", "future", "iomanip", "ios",
    "iostream", "istream", "iterator", "limits", "list", "locale", "map",
    "memory", "mutex", "new", "numeric", "optional", "ostream", "queue",
    "random", "ratio", "regex", "set", "sstream", "stack", "stdexcept",
    "streambuf", "string", "string_view", "system_error", "thread", "tuple",
    "type_traits", "typeinfo", "unordered_map", "unordered_set", "utility",
    "variant", "vector",
})


def _first_segment(spec: str) -> str:
    spec = spec.strip()
    if spec.startswith("@"):
        parts = spec.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else spec
    return spec.split("/", 1)[0]


def _libs_python(bodies: list[str], local: frozenset[str]) -> set[str]:
    stdlib = sys.stdlib_module_names
    found: set[str] = set()
    for body in bodies:
        for match in _PY_IMPORT.finditer(body):
            top = match.group(1).split(".", 1)[0]
            if not top or top in local or top in stdlib or top == "__future__":
                continue
            found.add(top)
    return found


def _libs_js(bodies: list[str], local: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for body in bodies:
        specs = _JS_FROM.findall(body) + _JS_BARE_IMPORT.findall(body) + _JS_CALL.findall(body)
        for spec in specs:
            spec = spec.strip()
            if not spec or spec.startswith((".", "/")) or spec.startswith("node:"):
                continue
            name = _first_segment(spec)
            if name in _NODE_BUILTINS:
                continue
            found.add(name)
    return found


def _libs_go(bodies: list[str], local: frozenset[str]) -> set[str]:
    paths: list[str] = []
    for body in bodies:
        for block in _GO_IMPORT_BLOCK.finditer(body):
            paths.extend(_GO_STRING.findall(block.group("body")))
        for single in _GO_SINGLE.finditer(body):
            paths.append(single.group("path"))
    found: set[str] = set()
    for path in paths:
        first = path.split("/", 1)[0]
        if "." in first:  # github.com/... ; a stdlib import path has no dot
            found.add(path)
    return found


def _libs_rust(bodies: list[str], local: frozenset[str]) -> set[str]:
    mods: set[str] = set()
    for body in bodies:
        mods.update(_RUST_MOD.findall(body))
    found: set[str] = set()
    for body in bodies:
        for name in _RUST_USE.findall(body) + _RUST_CRATE.findall(body):
            if name in _RUST_BUILTIN or name in mods or name in local:
                continue
            found.add(name)
    return found


def _libs_java(bodies: list[str], local: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for body in bodies:
        for pkg in _JAVA_IMPORT.findall(body):
            if pkg.startswith(("java.", "javax.")):
                continue
            parts = pkg.split(".")
            found.add(".".join(parts[:2]) if len(parts) >= 2 else pkg)
    return found


def _libs_csharp(bodies: list[str], local: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for body in bodies:
        for ns in _CSHARP_USING.findall(body):
            if ns == "System" or ns.startswith("System."):
                continue
            found.add(ns)
    return found


def _libs_cpp(bodies: list[str], local: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for body in bodies:
        for inc in _CPP_INCLUDE.findall(body):
            header = inc.strip()
            stem = header.split("/", 1)[0]
            if header in _CPP_STD or stem in _CPP_STD or header in _C_STD_HEADERS:
                continue
            if "/" not in header and "." not in header:
                # bare, extensionless, unknown -> assume an unenumerated std
                # header rather than guess a third-party dependency.
                continue
            found.add(stem if "/" in header else header)
    return found


_DETECTORS = {
    "python": _libs_python,
    "javascript": _libs_js,
    "typescript": _libs_js,
    "go": _libs_go,
    "rust": _libs_rust,
    "java": _libs_java,
    "csharp": _libs_csharp,
    "cpp": _libs_cpp,
}


# ---------------------------------------------------------------------------
# Report + orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LanguageReport:
    """What one language contributes to a build."""

    language: str
    files: tuple[str, ...]
    libraries: tuple[str, ...]


def _local_names(files: list[tuple[str, str]]) -> frozenset[str]:
    """Module names that live inside the build, so imports of them are local."""
    names: set[str] = set()
    for path, _ in files:
        pure = PurePosixPath(path)
        names.add(pure.stem)
        if pure.parts:
            names.add(PurePosixPath(pure.parts[0]).stem)
    return frozenset(names)


def detect_libraries(files: list[tuple[str, str]]) -> tuple[LanguageReport, ...]:
    """Identify every source language in a build and its third-party libraries."""
    local = _local_names(files)
    lang_files: dict[str, list[str]] = {}
    lang_bodies: dict[str, list[str]] = {}
    for path, body in files:
        lang = identify_language(path)
        if lang is None:
            continue
        lang_files.setdefault(lang, []).append(path)
        lang_bodies.setdefault(lang, []).append(body)
    reports: list[LanguageReport] = []
    for lang in sorted(lang_files):
        detector = _DETECTORS.get(lang)
        libs = detector(lang_bodies[lang], local) if detector else set()
        reports.append(LanguageReport(
            language=lang,
            files=tuple(sorted(lang_files[lang])),
            libraries=tuple(sorted(libs)),
        ))
    return tuple(reports)


# ---------------------------------------------------------------------------
# Generated project files
# ---------------------------------------------------------------------------


def render_dependency_manifests(
    reports: tuple[LanguageReport, ...], *, stack: Stack, existing: set[str]
) -> list[tuple[str, str]]:
    """Synthesize a dependency manifest for stacks with a standard one.

    Only Python (requirements.txt) and Node (package.json) have a single
    obvious manifest, and each is generated only when the build does not
    already ship one. Versions are unpinned: detection knows what is imported,
    not which release. Every other stack is documented in IDE_EXPORT.md.
    """
    index = {report.language: report.libraries for report in reports}
    out: list[tuple[str, str]] = []
    if stack is Stack.PYTHON:
        libs = index.get("python", ())
        if libs and "requirements.txt" not in existing:
            lines = [
                "# Generated by the App Factory universal output line.",
                "# Third-party imports detected in the build; versions are unpinned.",
            ]
            lines.extend(libs)
            out.append(("requirements.txt", "\n".join(lines) + "\n"))
    elif stack is Stack.NODE:
        libs = tuple(sorted(set(index.get("javascript", ())) | set(index.get("typescript", ()))))
        if libs and "package.json" not in existing:
            pkg = {
                "name": "app-factory-export",
                "version": "0.0.0",
                "private": True,
                "dependencies": {lib: "*" for lib in libs},
            }
            out.append(("package.json", json.dumps(pkg, indent=2, sort_keys=True) + "\n"))
    return out


def render_ide_readme(
    stack: Stack,
    entrypoint: str | None,
    reports: tuple[LanguageReport, ...],
    existing: set[str],
) -> tuple[str, str]:
    """The human-facing 'open me in any IDE' note. Always safe to write."""
    name = "IDE_EXPORT.md"
    if name in existing:
        name = "IDE_EXPORT.APP_FACTORY.md"
    lines: list[str] = [
        "# Open this project in your IDE",
        "",
        f"Exported by the App Factory universal output line for the **{stack.value}** stack.",
        "",
        "This is an ordinary directory tree. Open this folder in any IDE",
        "(VS Code, PyCharm, IntelliJ, Neovim, and so on) - there is nothing",
        "IDE-specific to configure. A folder of files is the one interface every",
        "editor already understands.",
        "",
        "## Languages and libraries",
        "",
    ]
    if reports:
        for report in reports:
            libs = ", ".join(report.libraries) if report.libraries else "no third-party imports detected"
            lines.append(f"- **{report.language}** - {len(report.files)} file(s); {libs}")
    else:
        lines.append("- no source languages identified")
    lines.append("")
    if "requirements.txt" in existing:
        lines.extend([
            "## Install (Python)",
            "",
            "```bash",
            "python3 -m venv .venv && . .venv/bin/activate",
            "pip install -r requirements.txt",
            "```",
            "",
        ])
    if "package.json" in existing:
        lines.extend(["## Install (Node)", "", "```bash", "npm install", "```", ""])
    return (name, "\n".join(lines).rstrip() + "\n")


# ---------------------------------------------------------------------------
# The universal output line
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportResult:
    target_dir: str
    stack: Stack
    entrypoint: str | None
    written: tuple[str, ...]
    generated: tuple[str, ...]
    languages: tuple[LanguageReport, ...]

    @property
    def library_index(self) -> dict[str, tuple[str, ...]]:
        return {report.language: report.libraries for report in self.languages}


def export_build(
    files: list[tuple[str, str]],
    target_dir: Path | str,
    *,
    stack: Stack | str = Stack.WEB,
    entrypoint: str | None = None,
    overwrite: bool = False,
) -> ExportResult:
    """Write the AI chain's code text into an IDE-ready directory tree.

    This is the hard-coded output end of the pipeline: it takes plain file
    text, never a live model, so it cannot be perturbed by a changing IDE
    environment. It validates every path against the stack ACL, detects the
    languages and libraries, writes the sources plus a generated manifest and
    IDE_EXPORT.md, and re-resolves each path against the target root so no
    symlink or component can escape.
    """
    stack = Stack(stack)
    acl_stack: Stack | None = None if stack is Stack.OTHER else stack

    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, body in files:
        safe = safe_relpath(path, stack=acl_stack)
        if safe in seen:
            raise ArtifactError(f"duplicate file path in export: {safe}")
        seen.add(safe)
        normalized.append((safe, body))
    if not normalized:
        raise ArtifactError("no files to export")

    if entrypoint is not None:
        want = entrypoint.strip().replace("\\", "/").lstrip("./")
        if want and want not in seen:
            raise ArtifactError(
                f"declared entrypoint {want!r} is not among the exported files"
            )
        entrypoint = want or None

    reports = detect_libraries(normalized)
    manifests = render_dependency_manifests(reports, stack=stack, existing=seen)
    for path, _ in manifests:
        seen.add(path)
    readme = render_ide_readme(stack, entrypoint, reports, seen)
    generated = list(manifests) + [readme]

    target = Path(target_dir)
    if target.exists():
        if target.is_file():
            raise ArtifactError(f"export target is a file, not a directory: {target}")
        if any(target.iterdir()) and not overwrite:
            raise ArtifactError(
                f"export target {target} is not empty; pass overwrite to write into it"
            )

    write_build(target, normalized + generated)

    return ExportResult(
        target_dir=str(target),
        stack=stack,
        entrypoint=entrypoint,
        written=tuple(sorted(path for path, _ in normalized)),
        generated=tuple(sorted(path for path, _ in generated)),
        languages=reports,
    )


def read_source_tree(
    build_dir: Path | str, *, stack: Stack | str = Stack.WEB
) -> list[tuple[str, str]]:
    """Read a build directory into (path, body) pairs for re-export.

    Skips dotfiles, __pycache__, unreadable binaries, and anything the stack
    profile does not permit (OTHER accepts any known suffix).
    """
    build_dir = Path(build_dir)
    if not build_dir.is_dir():
        raise ArtifactError(f"not a source directory: {build_dir}")
    stack = Stack(stack)
    profile = profile_for(stack)
    root = build_dir.resolve()
    out: list[tuple[str, str]] = []
    for path in sorted(build_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue  # a symlink pointing outside the root
        parts = PurePosixPath(rel).parts
        if any(part.startswith(".") for part in parts) or "__pycache__" in parts:
            continue
        if stack is not Stack.OTHER and not profile.permits(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out.append((rel, text))
    return out
