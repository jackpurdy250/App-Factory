"""App Factory test suite.

Run everything with `python3 tests/run_all.py` from the project root.

The suite imports core/ and agents/ only. It deliberately never imports
server/, so that the deterministic parts of the system stay provable on a
machine with no third-party packages installed and no API key configured.
"""
