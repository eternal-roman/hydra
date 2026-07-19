"""Structured errors for the publishable heartbeat package.

Callers (CLI, agent tools, MCP) should branch on `.code` rather than
parsing message text. `.hint` is optional human guidance (e.g. required
columns) and may be None.
"""

from __future__ import annotations

from typing import Optional


class HeartbeatError(Exception):
    """Base for all structured heartbeat failures."""

    code: str = "heartbeat_error"

    def __init__(self, message: str, *, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.hint = hint


class MissingDatasetError(HeartbeatError):
    """Empty path, missing file, or otherwise absent dataset input."""

    code = "missing_dataset"


class InvalidDatasetError(HeartbeatError):
    """Parse/schema failure, bad side values, or zero rows after parse."""

    code = "invalid_dataset"
