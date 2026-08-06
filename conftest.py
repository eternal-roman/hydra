"""Repo-root pytest config.

Without this, a bare `pytest tests/` fails on every file with
`ModuleNotFoundError: No module named 'hydra_agent'` — only `python -m pytest`
worked, because that form injects CWD into sys.path. About half the test files
paper over it with their own `sys.path.insert` preamble and half do not, so the
failure depended on which file pytest happened to import first.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
