"""App Factory core.

Deterministic machinery: command parsing, routing, the state bus, snapshots,
budgeting, and the governor. LLMs live behind `llm.py` and are never allowed
to influence control flow.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"