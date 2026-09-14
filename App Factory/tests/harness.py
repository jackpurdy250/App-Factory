"""Dependency-free test harness for App Factory.

No pytest, no plugins, no network. `python3 tests/run_all.py` is the whole
story. That constraint is deliberate: the deterministic core is the part that
must be provable, and it has to stay provable on a machine where nothing can
be installed and no API key exists.

Rules this suite keeps:

* tests/ may import core/ and agents/. It must NEVER import server/, because
  server/ needs fastapi and the suite has to run without it.
* every run uses config/models.mock.toml, so no test can reach the network.
* every run gets a fresh temp workspace, so no test can see another's state.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.prompts import PromptLibrary  # noqa: E402
from core.budgeter import Budgeter  # noqa: E402
from core.commands import CommandRouter  # noqa: E402
from core.config import AppConfig, ConfigStore  # noqa: E402
from core.events import Channel, Event, EventBus  # noqa: E402
from core.llm import AgentRunner  # noqa: E402
from core.parser import Verbosity  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.workspace import Workspace  # noqa: E402

CONFIG_DIR = ROOT / "config"
MOCK_MODELS = CONFIG_DIR / "models.mock.toml"
REAL_MODELS = CONFIG_DIR / "models.toml"
GOVERNOR = CONFIG_DIR / "governor.toml"
PRESETS = CONFIG_DIR / "providers.presets.toml"


# ---------------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------------


class Checker:
    """Collects results for one module and prints each as it happens.

    Printing immediately rather than at the end matters when a check hangs or
    crashes: the last line printed names the check that did it.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.passed = 0
        self.failures: list[str] = []

    # -- output ------------------------------------------------------------

    def section(self, title: str) -> None:
        print(f"\n  -- {title}")

    def check(self, label: str, ok: bool, detail: Any = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  PASS  {label}")
            return True
        self.failures.append(label)
        suffix = f"   {detail}" if detail != "" else ""
        print(f"  FAIL  {label}{suffix}")
        return False

    # -- assertions --------------------------------------------------------

    def equal(self, label: str, actual: Any, expected: Any) -> bool:
        return self.check(
            label, actual == expected, f"got {actual!r}, want {expected!r}"
        )

    def same(self, label: str, actual: Any, expected: Any) -> bool:
        """Identity, for enum members and sentinels."""
        return self.check(
            label, actual is expected, f"got {actual!r}, want {expected!r}"
        )

    def truthy(self, label: str, value: Any) -> bool:
        return self.check(label, bool(value), f"got {value!r}")

    def falsy(self, label: str, value: Any) -> bool:
        return self.check(label, not value, f"got {value!r}")

    def contains(self, label: str, haystack: Any, needle: Any) -> bool:
        try:
            ok = needle in haystack
        except TypeError as exc:
            return self.check(label, False, f"not containable: {exc}")
        return self.check(label, ok, f"{needle!r} not found in {haystack!r}")

    def excludes(self, label: str, haystack: Any, needle: Any) -> bool:
        try:
            ok = needle not in haystack
        except TypeError as exc:
            return self.check(label, False, f"not containable: {exc}")
        return self.check(label, ok, f"{needle!r} unexpectedly present")

    def raises(
        self,
        label: str,
        exception: type[BaseException],
        call: Callable[[], Any],
    ) -> bool:
        try:
            call()
        except exception:
            return self.check(label, True)
        except BaseException as other:  # noqa: BLE001 - report, do not mask
            return self.check(
                label, False, f"raised {type(other).__name__}: {other}"
            )
        return self.check(label, False, "no exception raised")

    # -- reporting ---------------------------------------------------------

    @property
    def total(self) -> int:
        return self.passed + len(self.failures)

    def report(self) -> tuple[int, int]:
        print(f"\n  {self.name}: {self.passed}/{self.total}")
        for label in self.failures:
            print(f"    failed: {label}")
        return self.passed, self.total


# ---------------------------------------------------------------------------
# Temp workspaces
# ---------------------------------------------------------------------------


def temp_root(prefix: str = "af-test-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# System assembly
#
# This mirrors server/app.py's create_app deliberately. If the two drift, the
# suite stops proving anything about what actually runs, so any change to one
# belongs in the other.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class System:
    root: Path
    config_store: ConfigStore
    bus: EventBus
    workspace: Workspace
    prompts: PromptLibrary
    budgeter: Budgeter
    runner: AgentRunner
    pipeline: Pipeline
    router: CommandRouter

    @property
    def config(self) -> AppConfig:
        return self.config_store.current


def build_system(
    root: Path,
    *,
    verbosity: Verbosity = Verbosity.QUIET,
    models_path: Path = MOCK_MODELS,
) -> System:
    """Assemble a complete, network-free system rooted at `root`."""
    config_store = ConfigStore.from_paths(
        models_path=models_path,
        governor_path=GOVERNOR,
        env_path=None,
        runs_dir=root / "projects",
    )
    config = config_store.current
    bus = EventBus(verbosity=verbosity)
    workspace = Workspace(root)
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
    return System(
        root=root,
        config_store=config_store,
        bus=bus,
        workspace=workspace,
        prompts=prompts,
        budgeter=budgeter,
        runner=runner,
        pipeline=pipeline,
        router=router,
    )


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def events_on(bus: EventBus, channel: Channel) -> list[Event]:
    return [event for event in bus.replay() if event.channel is channel]


def cli_lines(bus: EventBus) -> list[str]:
    """Exactly what Window 1 showed, in order."""
    return [event.text for event in events_on(bus, Channel.CLI)]


def log_codes(bus: EventBus) -> list[str]:
    return [event.code for event in events_on(bus, Channel.LOG) if event.code]
