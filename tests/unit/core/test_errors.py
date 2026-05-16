"""Tests for opshub.core.errors."""

from __future__ import annotations

import pytest

from opshub.core.errors import (
    ConfigError,
    ConflictError,
    NotFoundError,
    OpsHubError,
    ValidationError,
)


@pytest.mark.parametrize(
    "subclass",
    [ConfigError, ValidationError, NotFoundError, ConflictError],
)
def test_subclasses_inherit_from_opshub_error(subclass: type[Exception]) -> None:
    assert issubclass(subclass, OpsHubError)


def test_opshub_error_is_distinct_from_value_error() -> None:
    assert not issubclass(OpsHubError, ValueError)


def test_opshub_error_carries_message() -> None:
    err = ConfigError("missing field")
    assert str(err) == "missing field"
