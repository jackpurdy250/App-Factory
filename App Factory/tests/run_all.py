#!/usr/bin/env python3
"""Run every suite in one process and report a single verdict.

    python3 tests/run_all.py              # everything
    python3 tests/run_all.py pipeline     # only suites whose name matches
    python3 tests/run_all.py --list       # show what would run

Why this exists rather than pytest: the sandbox this was built in had no
network and no pytest, and the suites need to run on a bare Python 3.11+
install with pydantic as the only third-party import. Each suite exposes
`run() -> (passed, total)` and prints its own detail, so this file only has
to sequence them and add up the score.

The suites are ordered foundation-first: a schema break should be the first
thing you see, not the hundredth. `tests.test_pipeline` goes last because it
is the only one that drives a full asyncio build and writes files to a
temporary directory.

This module deliberately never imports `server`, which needs FastAPI and
uvicorn. The suites validate the parser, the state bus, the governor and the
pipeline without a web server; `run.py --check` is what verifies the server
layer once its dependencies are installed.
"""

from __future__ import annotations

import importlib
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SUITES: tuple[str, ...] = (
    "tests.test_schemas",
    "tests.test_state",
    "tests.test_config",
    "tests.test_parser",
    "tests.test_commands",
    "tests.test_export",
    "tests.test_pipeline",
)


def select(patterns: list[str]) -> list[str]:
    """Pick suites by substring, so `run_all.py state config` works."""

    if not patterns:
        return list(SUITES)
    chosen: list[str] = []
    for name in SUITES:
        short = name.rsplit(".", 1)[-1].removeprefix("test_")
        if any(pattern.lower() in (name, short) or pattern.lower() in short for pattern in patterns):
            chosen.append(name)
    return chosen


def main(argv: list[str]) -> int:
    patterns = [arg for arg in argv if not arg.startswith("-")]
    listing = "--list" in argv
    suites = select(patterns)

    if not suites:
        print(f"no suite matches {patterns}")
        print("available: " + ", ".join(name.rsplit('.', 1)[-1] for name in SUITES))
        return 2

    if listing:
        for name in suites:
            print(name)
        return 0

    results: list[tuple[str, int, int, float, str | None]] = []
    started_all = time.monotonic()

    for name in suites:
        print(f"\n{'=' * 68}\n{name}\n{'=' * 68}")
        started = time.monotonic()
        try:
            module = importlib.import_module(name)
            passed, total = module.run()
            crash: str | None = None
        except Exception:  # noqa: BLE001 - a crashed suite is a failed suite
            passed, total = 0, 0
            crash = traceback.format_exc()
            print(crash, end="")
        results.append((name, passed, total, time.monotonic() - started, crash))

    # -- verdict --------------------------------------------------------

    width = max(len(name) for name, *_ in results)
    passed_all = sum(row[1] for row in results)
    total_all = sum(row[2] for row in results)
    crashed = [row[0] for row in results if row[4] is not None]

    print(f"\n{'=' * 68}\nsummary\n{'=' * 68}")
    for name, passed, total, seconds, crash in results:
        if crash is not None:
            verdict = "CRASH"
        elif total and passed == total:
            verdict = "ok"
        else:
            verdict = "FAIL"
        score = f"{passed}/{total}" if total else "-"
        print(f"  {name:<{width}}  {score:>9}  {seconds:6.2f}s  {verdict}")

    elapsed = time.monotonic() - started_all
    print(f"\n  total: {passed_all}/{total_all} checks in {elapsed:.2f}s")

    if "server" in sys.modules:
        # A suite that reaches for the web layer would make the whole test
        # run depend on FastAPI being installed. Fail loudly instead.
        print("  WARNING: a suite imported `server`; the suites must stay dependency-free")
        return 1

    if crashed:
        print("  crashed: " + ", ".join(crashed))
        return 1
    if total_all == 0:
        print("  nothing ran")
        return 1
    if passed_all != total_all:
        print(f"  {total_all - passed_all} check(s) failed")
        return 1

    print("  all suites green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
