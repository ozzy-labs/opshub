"""Tests for :mod:`opshub.cli._actor`.

Covers both the original :func:`resolve_owner` precedence rules and the
state-file helpers added in Phase 2 step 6 (``current-session`` tracking).
All tests isolate ``$HOME`` / ``$XDG_STATE_HOME`` via ``monkeypatch`` so
the developer's real ``~/.local/state`` is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.cli._actor import (
    clear_current_session,
    get_current_session_id,
    resolve_owner,
    set_current_session_id,
    state_file_path,
)


def _isolate_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point both ``HOME`` and ``XDG_STATE_HOME`` inside ``tmp_path``."""
    state_home = tmp_path / "state"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    return state_home


# ---- resolve_owner precedence (pre-existing) -----------------------------


def test_resolve_owner_defaults_when_nothing_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    monkeypatch.delenv("OPSHUB_ACTOR", raising=False)
    monkeypatch.delenv("OPSHUB_WORK_SESSION_ID", raising=False)

    owner = resolve_owner()
    assert owner.actor == "cli:default"
    assert owner.work_session_id is None


def test_resolve_owner_env_overrides_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_ACTOR", "agent:claude")
    monkeypatch.setenv("OPSHUB_WORK_SESSION_ID", "01HZZZZZZZZZZZZZZZZZZZZZ00")

    owner = resolve_owner()
    assert owner.actor == "agent:claude"
    assert owner.work_session_id == "01HZZZZZZZZZZZZZZZZZZZZZ00"


def test_resolve_owner_flag_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_ACTOR", "agent:env")
    monkeypatch.setenv("OPSHUB_WORK_SESSION_ID", "env-id")

    owner = resolve_owner(actor="agent:flag", work_session_id="flag-id")
    assert owner.actor == "agent:flag"
    assert owner.work_session_id == "flag-id"


def test_resolve_owner_empty_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_ACTOR", "")
    monkeypatch.setenv("OPSHUB_WORK_SESSION_ID", "")

    owner = resolve_owner()
    assert owner.actor == "cli:default"
    assert owner.work_session_id is None


# ---- state file helpers --------------------------------------------------


def test_state_file_path_uses_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    expected = state_home / "opshub" / "current-session"
    assert state_file_path() == expected


def test_state_file_path_falls_back_to_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``$XDG_STATE_HOME`` is unset the helper picks ``~/.local/state``."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    assert state_file_path() == home / ".local" / "state" / "opshub" / "current-session"


def test_get_current_session_id_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    assert get_current_session_id() is None


def test_set_then_get_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    set_current_session_id("01HZZZZZZZZZZZZZZZZZZZZZ00")
    assert get_current_session_id() == "01HZZZZZZZZZZZZZZZZZZZZZ00"


def test_set_creates_parent_directories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    # Confirm the parent does not yet exist before we call set.
    assert not (state_home / "opshub").exists()
    set_current_session_id("01HZZZZZZZZZZZZZZZZZZZZZ01")
    assert (state_home / "opshub").is_dir()
    assert (state_home / "opshub" / "current-session").read_text(encoding="utf-8") == (
        "01HZZZZZZZZZZZZZZZZZZZZZ01"
    )


def test_clear_removes_state_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    set_current_session_id("01HZZZZZZZZZZZZZZZZZZZZZ02")
    assert get_current_session_id() == "01HZZZZZZZZZZZZZZZZZZZZZ02"
    clear_current_session()
    assert get_current_session_id() is None


def test_clear_is_idempotent_when_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    # No state file yet; clear() must not raise.
    clear_current_session()
    assert get_current_session_id() is None


def test_get_strips_whitespace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    state_dir = state_home / "opshub"
    state_dir.mkdir(parents=True)
    (state_dir / "current-session").write_text("  01HZZZZZZZZZZZZZZZZZZZZZ03\n", encoding="utf-8")
    assert get_current_session_id() == "01HZZZZZZZZZZZZZZZZZZZZZ03"


def test_get_returns_none_when_file_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_home = _isolate_state_home(monkeypatch, tmp_path)
    state_dir = state_home / "opshub"
    state_dir.mkdir(parents=True)
    (state_dir / "current-session").write_text("   \n", encoding="utf-8")
    assert get_current_session_id() is None


# ---- resolve_owner with state file ---------------------------------------


def test_resolve_owner_consults_state_file_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    monkeypatch.delenv("OPSHUB_WORK_SESSION_ID", raising=False)

    set_current_session_id("01HZZZZZZZZZZZZZZZZZZZZZ04")
    owner = resolve_owner()
    assert owner.work_session_id == "01HZZZZZZZZZZZZZZZZZZZZZ04"


def test_resolve_owner_env_takes_precedence_over_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    set_current_session_id("from-state-file")
    monkeypatch.setenv("OPSHUB_WORK_SESSION_ID", "from-env")

    owner = resolve_owner()
    assert owner.work_session_id == "from-env"


def test_resolve_owner_flag_takes_precedence_over_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_state_home(monkeypatch, tmp_path)
    set_current_session_id("from-state-file")
    monkeypatch.setenv("OPSHUB_WORK_SESSION_ID", "from-env")

    owner = resolve_owner(work_session_id="from-flag")
    assert owner.work_session_id == "from-flag"
