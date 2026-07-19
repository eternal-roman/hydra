"""Structured error types for the publishable heartbeat package."""

from __future__ import annotations

import pytest

from heartbeat.errors import (
    HeartbeatError,
    InvalidDatasetError,
    MissingDatasetError,
)


def test_missing_dataset_error_code_and_hint():
    err = MissingDatasetError(
        "dataset not found: /no/such/file.csv",
        hint="required columns: ts|timestamp|time, price, qty|quantity|size|volume, side|aggressor",
    )
    assert isinstance(err, HeartbeatError)
    assert isinstance(err, Exception)
    assert err.code == "missing_dataset"
    assert "not found" in str(err).lower() or "dataset" in str(err).lower()
    assert err.hint
    assert "side" in err.hint


def test_invalid_dataset_error_code():
    err = InvalidDatasetError("bad side value 'maybe'", hint="use buy/sell/b/s/1/-1")
    assert isinstance(err, HeartbeatError)
    assert err.code == "invalid_dataset"
    assert "bad side" in str(err)
    assert err.hint


def test_errors_default_hint_none():
    m = MissingDatasetError("gone")
    i = InvalidDatasetError("bad")
    assert m.hint is None or isinstance(m.hint, str)
    assert i.hint is None or isinstance(i.hint, str)
    assert m.code == "missing_dataset"
    assert i.code == "invalid_dataset"


def test_errors_are_catchable_as_heartbeat_error():
    with pytest.raises(HeartbeatError) as ei:
        raise MissingDatasetError("x")
    assert ei.value.code == "missing_dataset"

    with pytest.raises(HeartbeatError) as ei2:
        raise InvalidDatasetError("y")
    assert ei2.value.code == "invalid_dataset"
