#!/usr/bin/env python3
"""Start the App Factory, or check that it is ready to start.

    python3 run.py                                  # serve on 127.0.0.1:8765
    python3 run.py --models config/models.mock.toml  # no API key needed
    python3 run.py --check                          # validate config and exit
    python3 run.py --port 9000 --verbose trace      # louder Window 3

Two deliberate properties:

  * `--check` never imports the web layer, so you can validate your provider
    configuration and your API key before installing FastAPI or uvicorn. It
    is the fastest way to answer "is my key wired up correctly?".
  * The server is only imported when actually serving. Importing `server.app`
    pulls in FastAPI; keeping that out of the check path means a
    configuration typo reports as a configuration error rather than an
    ImportError.

The factory is model-agnostic: which provider and model each of the seven
roles uses is decided entirely in `config/models.toml`, never in code. See
`config/providers.presets.toml` for the preset catalogue.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields as dataclass_fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.config import (  # noqa: E402
    DEFAULT_GOVERNOR_FILE,
    DEFAULT_MODELS_FILE,
    ConfigError,
    ConfigStore,
)
from core.parser import Verbosity  # noqa: E402
from core.schemas.common import AgentRole, Stack  # noqa: E402

WEB_ASSETS = ("index.html", "app.js", "styles.css")
CHARTER_NAME = "system.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Start the App Factory (3-window operator console).",
    )
    parser.add_argument(
        "--models",
        default=None,
        help=f"model/provider bindings (default: {DEFAULT_MODELS_FILE})",
    )
    parser.add_argument(
        "--governor",
        default=None,
        help=f"budgets, gates and rules (default: {DEFAULT_GOVERNOR_FILE})",
    )
    parser.add_argument(
        "--env", default=None, help="dotenv file holding API keys (default: .env)"
    )
    parser.add_argument(
        "--runs", default=None, help="workspace root for projects (default: ./projects)"
    )
    parser.add_argument(
        "--web", default=None, help="directory holding the front-end (default: ./web)"
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8765, help="bind port")
    parser.add_argument(
        "--verbose",
        choices=["quiet", "normal", "trace"],
        default="normal",
        help="initial Window 3 verbosity; change it live with !verbose",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration, charters and assets, then exit",
    )
    parser.add_argument(
        "--export",
        default=None,
        metavar="DIR",
        help="export a build into DIR as an IDE-ready project, then exit",
    )
    parser.add_argument(
        "--from",
        dest="source",
        default=None,
        metavar="DIR",
        help="source build directory to export (used with --export)",
    )
    parser.add_argument(
        "--stack",
        default="web",
        choices=[s.value for s in Stack],
        help="stack profile for --export (default: web)",
    )
    parser.add_argument(
        "--entrypoint",
        default=None,
        help="entrypoint path recorded in the export (used with --export)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow --export to write into a non-empty directory",
    )
    return parser


def resolve(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else (ROOT / path)


def load_store(args: argparse.Namespace) -> ConfigStore:
    models = resolve(args.models) or DEFAULT_MODELS_FILE
    governor = resolve(args.governor) or DEFAULT_GOVERNOR_FILE

    if not Path(models).exists():
        mock = ROOT / "config" / "models.mock.toml"
        hint = (
            f"\n  A mock configuration exists at {mock.relative_to(ROOT)} and needs no"
            f"\n  API key. Start with:  python3 run.py --models {mock.relative_to(ROOT)}"
            if mock.exists()
            else ""
        )
        raise ConfigError(f"no model configuration at {models}{hint}")
    if not Path(governor).exists():
        raise ConfigError(f"no governor configuration at {governor}")

    env = resolve(args.env)
    if env is None:
        default_env = ROOT / ".env"
        env = default_env if default_env.exists() else None

    return ConfigStore.from_paths(
        Path(models),
        Path(governor),
        env_path=env,
        runs_dir=resolve(args.runs) or (ROOT / "projects"),
    )


def check(args: argparse.Namespace) -> int:
    """Validate everything the factory needs before it will accept a command."""

    problems: list[str] = []

    try:
        store = load_store(args)
    except ConfigError as exc:
        print(f"config: FAILED\n  {exc}")
        return 1

    config = store.current
    print(f"config: ok\n  models   {config.models_path}\n  governor {config.governor_path}")

    # -- providers ------------------------------------------------------

    print("\nproviders:")
    for name in sorted({config.role(role).provider for role in AgentRole}):
        provider = config.provider(name)
        credential = "mock" if provider.is_mock else ("key set" if provider.api_key else "no key")
        print(f"  {name:<16} {provider.kind:<22} {provider.base_url or '-':<44} {credential}")

    # -- roles ----------------------------------------------------------

    print("\nroles:")
    for role in AgentRole:
        binding = config.role(role)
        chain = " -> ".join(p.name for p in config.provider_chain(role))
        shape = "json" if binding.structured else "files"
        print(f"  {role.value:<16} {binding.model:<28} {shape:<6} {chain}")

    # -- charters -------------------------------------------------------

    charters = sorted(
        path.parent.name for path in Path(config.agents_dir).glob(f"*/{CHARTER_NAME}")
    )
    print(f"\ncharters: {len(charters)} found in {config.agents_dir}")
    for name in charters:
        size = (Path(config.agents_dir) / name / CHARTER_NAME).stat().st_size
        print(f"  {name:<16} {size:>6} bytes")
    if len(charters) != len(AgentRole):
        problems.append(
            f"expected {len(AgentRole)} agent charters, found {len(charters)}"
        )
    for name in charters:
        if (Path(config.agents_dir) / name / CHARTER_NAME).stat().st_size == 0:
            problems.append(f"charter for {name} is empty")

    # -- front-end ------------------------------------------------------

    web_dir = resolve(args.web) or config.web_dir
    print(f"\nfront-end: {web_dir}")
    for asset in WEB_ASSETS:
        path = Path(web_dir) / asset
        state = f"{path.stat().st_size} bytes" if path.exists() else "MISSING"
        print(f"  {asset:<16} {state}")
        if not path.exists():
            problems.append(f"missing front-end asset: {asset}")

    # -- governor -------------------------------------------------------
    #
    # Printed generically from the dataclass fields, so a new knob in
    # config/governor.toml shows up here without anyone editing this file.

    print("")
    print("governor:")
    for section_name, section in (
        ("budgets", config.governor.budgets),
        ("gates", config.governor.gates),
        ("rules", config.governor.rules),
        ("context", config.governor.context),
    ):
        print(f"  [{section_name}]")
        for field in dataclass_fields(section):
            value = getattr(section, field.name)
            if isinstance(value, (list, tuple, set, frozenset)):
                shown = ", ".join(str(getattr(item, "value", item)) for item in value) or "none"
            elif value is None:
                shown = "unset"
            else:
                shown = str(getattr(value, "value", value))
            print(f"    {field.name:<26} {shown}")

    # -- web dependencies (reported, not required) ----------------------

    print("\nweb dependencies:")
    missing: list[str] = []
    for module in ("fastapi", "uvicorn"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
            print(f"  {module:<16} MISSING")
        else:
            print(f"  {module:<16} ok")
    if missing:
        print(
            "  the console cannot serve until these are installed:"
            "\n    pip install -r requirements.txt"
            "\n  the deterministic layers (parser, state bus, governor, pipeline)"
            "\n  are fully testable without them:  python3 tests/run_all.py"
        )

    if problems:
        print("\ncheck: FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("\ncheck: ok" + (" (install web dependencies to serve)" if missing else ""))
    return 0


def serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed, so the console cannot be served.\n"
            "  pip install -r requirements.txt\n"
            "Meanwhile, `python3 run.py --check` validates your configuration and\n"
            "`python3 tests/run_all.py` exercises the whole pipeline offline.",
            file=sys.stderr,
        )
        return 1

    try:
        from server.app import create_app
    except ImportError as exc:
        print(f"the web layer could not be imported: {exc}", file=sys.stderr)
        print("  pip install -r requirements.txt", file=sys.stderr)
        return 1

    # Fail on a bad configuration before binding the port, so a typo in
    # models.toml does not present itself as a dead web page.
    try:
        load_store(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    verbosity = Verbosity[args.verbose.upper()]
    try:
        app = create_app(
            models_path=resolve(args.models),
            governor_path=resolve(args.governor),
            env_path=resolve(args.env),
            runs_dir=resolve(args.runs),
            web_dir=resolve(args.web),
            verbosity=verbosity,
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    print(
        "App Factory listening on http://" + args.host + ":" + str(args.port) + "\n"
        f"  Window 1  command line      (type `!status` to confirm readiness)\n"
        f"  Window 2  live sandbox      (renders each build)\n"
        f"  Window 3  agent chatter     (verbosity: {args.verbose})\n"
        "Press Ctrl+C to stop."
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def export_cmd(args: argparse.Namespace) -> int:
    """Materialize a build into an IDE-ready directory tree, then exit.

    The deterministic output end of the pipeline: no model, no API key, no web
    layer. It reads a build directory and writes a portable project any IDE
    can open, with a generated dependency manifest and IDE_EXPORT.md.
    """
    from core.artifacts import ArtifactError
    from core.export import export_build, read_source_tree

    if not args.source:
        print("--export requires --from <build_dir>", file=sys.stderr)
        return 2
    source = resolve(args.source)
    target = resolve(args.export)
    if source is None or not source.is_dir():
        print(f"no such build directory: {args.source}", file=sys.stderr)
        return 1

    try:
        files = read_source_tree(source, stack=args.stack)
        if not files:
            print(f"no {args.stack} source files under {source}", file=sys.stderr)
            return 1
        result = export_build(
            files,
            target,
            stack=args.stack,
            entrypoint=args.entrypoint,
            overwrite=args.overwrite,
        )
    except ArtifactError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1

    print(f"exported {len(result.written)} file(s) to {result.target_dir}")
    print(f"  stack       {result.stack.value}")
    if result.entrypoint:
        print(f"  entrypoint  {result.entrypoint}")
    print("  languages:")
    for report in result.languages:
        libs = ", ".join(report.libraries) if report.libraries else "-"
        print(f"    {report.language:<12} {len(report.files):>2} file(s)   libs: {libs}")
    if result.generated:
        print("  generated:  " + ", ".join(result.generated))
    print("\nOpen the target directory in any IDE.")
    return 0


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.export is not None:
        return export_cmd(args)
    if args.check:
        return check(args)
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
