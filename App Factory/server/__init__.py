"""HTTP and WebSocket transport for the App Factory.

The server owns no pipeline logic. It carries raw lines from Window 1 into
the command router, streams bus events out to the three panes, and serves
build artifacts read-only for Window 2.

`create_app` is resolved lazily so that `import server` stays cheap and does
not require FastAPI to be installed - the dependency-free test suite imports
this package without ever building an app.
"""

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
