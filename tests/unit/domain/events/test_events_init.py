"""Tests for the public surface of :mod:`opshub.domain.events`.

Pins the internal API surface of the events package so future commits
cannot accidentally re-introduce per-phase grouping aliases without an
explicit decision. Epic #470 dropped ``Phase2Event`` ... ``Phase8Event``
in favour of a single :data:`AllEvent` union; this test catches any
accidental resurrection (whether at the alias level or via re-export).
"""

from __future__ import annotations

import importlib

import pytest

# The 6 per-phase grouping aliases dropped in epic #470. Phase 7 was
# projection-only and never had a ``Phase7Event``.
_DROPPED_ALIASES = (
    "Phase2Event",
    "Phase3Event",
    "Phase4Event",
    "Phase5Event",
    "Phase6Event",
    "Phase8Event",
)


@pytest.mark.parametrize("name", _DROPPED_ALIASES)
def test_dropped_per_phase_alias_is_not_re_exported(name: str) -> None:
    """``from opshub.domain.events import Phase<N>Event`` must fail.

    The aliases were never part of any production decode path (the
    event store always reached for :data:`AllEvent`) and were dropped
    in epic #470 to keep new event families a one-line addition. This
    test pins the public surface so a future ``__init__.py`` edit
    cannot silently put them back.
    """
    module = importlib.import_module("opshub.domain.events")
    assert name not in module.__all__
    assert not hasattr(module, name)


@pytest.mark.parametrize("name", _DROPPED_ALIASES)
def test_dropped_per_phase_alias_raises_importerror(name: str) -> None:
    """The aliases must raise :class:`ImportError`, not silently resolve."""
    with pytest.raises(ImportError):
        # ``getattr`` against the package would only see the alias if it
        # were re-exported; the ``from ... import`` statement raises
        # ``ImportError`` when the name is missing, which is the
        # specific behaviour callers depend on.
        exec(f"from opshub.domain.events import {name}")
