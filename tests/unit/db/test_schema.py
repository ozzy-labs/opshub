"""Tests for opshub.db.schema."""

from __future__ import annotations

from typing import cast

from opshub.db.schema import metadata


def test_metadata_is_initially_empty() -> None:
    # Step 7 will register Table objects here; until then the registry stays
    # empty so autogenerate sees a clean baseline.
    assert metadata.tables == {}


def test_naming_convention_is_set() -> None:
    # SQLAlchemy types `naming_convention` as a TypedDict with all-optional
    # keys; we know our concrete dict has every key set, so cast to plain
    # dict[str, str] for the subscript reads in this test.
    convention = cast(dict[str, str], metadata.naming_convention)
    assert convention["pk"] == "pk_%(table_name)s"
    assert "%(table_name)s" in convention["ix"]
    assert "%(table_name)s" in convention["uq"]
    assert "%(table_name)s" in convention["fk"]
    assert "%(table_name)s" in convention["ck"]
