"""Suite: the universal output line (core/export.py).

Proves the hard-coded, model-free output end of the pipeline: language and
library identification, and the deterministic write of a build into an
IDE-ready directory tree with a generated dependency manifest.
"""

from __future__ import annotations

import json

from tests.harness import Checker, discard, temp_root

from core.artifacts import ArtifactError
from core.export import (
    ExportResult,
    detect_libraries,
    export_build,
    identify_language,
    read_source_tree,
)
from core.schemas.common import Stack


def _libs(files: list[tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    return {report.language: report.libraries for report in detect_libraries(files)}


def run() -> tuple[int, int]:
    c = Checker("export")

    # -- language identification -------------------------------------------
    c.section("language identification")
    c.equal("py -> python", identify_language("main.py"), "python")
    c.equal("ts -> typescript", identify_language("src/app.ts"), "typescript")
    c.equal("go -> go", identify_language("cmd/main.go"), "go")
    c.equal("rs -> rust", identify_language("src/main.rs"), "rust")
    c.equal("html -> html", identify_language("index.html"), "html")
    c.same("md -> None", identify_language("README.md"), None)

    # -- python detection --------------------------------------------------
    c.section("python detection")
    py = _libs([
        ("main.py", "import os\nimport requests\nfrom flask import Flask\nfrom . import helper\nimport utils\n"),
        ("utils.py", "import json\nimport numpy as np\n"),
    ])["python"]
    c.contains("detects requests", py, "requests")
    c.contains("detects flask", py, "flask")
    c.contains("detects numpy", py, "numpy")
    c.excludes("stdlib os excluded", py, "os")
    c.excludes("stdlib json excluded", py, "json")
    c.excludes("local utils excluded", py, "utils")
    c.excludes("relative import excluded", py, "helper")

    # -- javascript / typescript detection ---------------------------------
    c.section("javascript detection")
    js = _libs([
        ("app.js", "import React from 'react'\nconst fs = require('fs')\nimport './local.js'\nconst _ = await import('lodash')\nimport {a} from '@scope/pkg'\n"),
    ])["javascript"]
    c.contains("detects react (from)", js, "react")
    c.contains("detects lodash (dynamic import)", js, "lodash")
    c.contains("scoped pkg kept whole", js, "@scope/pkg")
    c.excludes("node builtin fs excluded", js, "fs")
    c.excludes("relative specifier excluded", js, "./local.js")

    # -- go detection ------------------------------------------------------
    c.section("go detection")
    go = _libs([
        ("main.go", 'package main\nimport (\n  "fmt"\n  "github.com/gin-gonic/gin"\n)\nimport "net/http"\n'),
    ])["go"]
    c.contains("detects gin (external, dotted host)", go, "github.com/gin-gonic/gin")
    c.excludes("stdlib fmt excluded", go, "fmt")
    c.excludes("stdlib net/http excluded", go, "net/http")

    # -- rust detection ----------------------------------------------------
    c.section("rust detection")
    rs = _libs([
        ("src/main.rs", "use serde::Serialize;\nuse std::collections::HashMap;\nuse crate::thing;\nmod thing;\nextern crate rand;\n"),
    ])["rust"]
    c.contains("detects serde", rs, "serde")
    c.contains("detects rand (extern crate)", rs, "rand")
    c.excludes("std excluded", rs, "std")
    c.excludes("local mod excluded", rs, "thing")

    # -- export_build: python project --------------------------------------
    c.section("export_build (python)")
    root = temp_root()
    try:
        target = root / "ide"
        result = export_build(
            [("main.py", "import requests\nprint('hi')\n")],
            target,
            stack=Stack.PYTHON,
            entrypoint="main.py",
        )
        c.truthy("returns ExportResult", isinstance(result, ExportResult))
        c.same("stack recorded", result.stack, Stack.PYTHON)
        c.equal("entrypoint recorded", result.entrypoint, "main.py")
        c.truthy("main.py written", (target / "main.py").exists())
        req = target / "requirements.txt"
        c.truthy("requirements.txt generated", req.exists())
        c.contains("requirements lists requests", req.read_text(), "requests")
        readme = target / "IDE_EXPORT.md"
        c.truthy("IDE_EXPORT.md generated", readme.exists())
        c.contains("readme names the stack", readme.read_text(), "python")
        c.contains("readme has install hint", readme.read_text(), "pip install -r requirements.txt")
    finally:
        discard(root)

    # -- export_build: node package.json -----------------------------------
    c.section("export_build (node)")
    root = temp_root()
    try:
        target = root / "ide"
        export_build([("index.js", "import express from 'express'\n")], target, stack=Stack.NODE)
        pkg = target / "package.json"
        c.truthy("package.json generated", pkg.exists())
        data = json.loads(pkg.read_text())
        c.contains("express in dependencies", data.get("dependencies", {}), "express")
    finally:
        discard(root)

    # -- export safety -----------------------------------------------------
    c.section("export safety (the ACL still holds on the way out)")
    root = temp_root()
    try:
        c.raises(
            "off-stack file refused",
            ArtifactError,
            lambda: export_build([("index.html", "<x>")], root / "a", stack=Stack.PYTHON),
        )
        c.raises(
            "path traversal refused",
            ArtifactError,
            lambda: export_build([("../evil.py", "x")], root / "b", stack=Stack.PYTHON),
        )
        full = root / "full"
        full.mkdir()
        (full / "keep.py").write_text("x = 1\n")
        c.raises(
            "non-empty target refused",
            ArtifactError,
            lambda: export_build([("main.py", "import os\n")], full, stack=Stack.PYTHON),
        )
        export_build([("main.py", "import os\n")], full, stack=Stack.PYTHON, overwrite=True)
        c.truthy("overwrite writes new file", (full / "main.py").exists())
        c.truthy("overwrite keeps existing file", (full / "keep.py").exists())
    finally:
        discard(root)

    # -- read_source_tree --------------------------------------------------
    c.section("read_source_tree")
    root = temp_root()
    try:
        bd = root / "build"
        bd.mkdir()
        (bd / "main.py").write_text("import os\n")
        (bd / "notes.md").write_text("hi\n")
        (bd / "index.html").write_text("<x>\n")
        cache = bd / "__pycache__"
        cache.mkdir()
        (cache / "x.cpython-313.pyc").write_bytes(b"\x00\x01")
        found = dict(read_source_tree(bd, stack=Stack.PYTHON))
        c.contains("reads main.py", found, "main.py")
        c.contains("reads common notes.md", found, "notes.md")
        c.excludes("skips off-stack index.html", found, "index.html")
        c.falsy("skips __pycache__", [k for k in found if "__pycache__" in k])
    finally:
        discard(root)

    # -- defaults + determinism -------------------------------------------
    c.section("default stack and determinism")
    root = temp_root()
    try:
        res = export_build([("index.html", "<html></html>\n")], root / "w")
        c.same("defaults to web", res.stack, Stack.WEB)
        c.truthy("html written", (root / "w" / "index.html").exists())
        payload = [("main.py", "import requests\n")]
        export_build(payload, root / "d1", stack=Stack.PYTHON)
        export_build(payload, root / "d2", stack=Stack.PYTHON)
        c.equal(
            "identical requirements bytes",
            (root / "d1" / "requirements.txt").read_bytes(),
            (root / "d2" / "requirements.txt").read_bytes(),
        )
    finally:
        discard(root)

    return c.report()


if __name__ == "__main__":
    passed, total = run()
    raise SystemExit(0 if passed == total else 1)
