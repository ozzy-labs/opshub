"""Tests for ``opshub init``.

Every test isolates the CLI invocation via ``monkeypatch.setenv`` so that
the user's real ``~/.config/opshub`` / ``~/.local/share/opshub`` directories
are never touched. All paths point inside ``tmp_path``.

Phase 16-C ([#384](https://github.com/ozzy-labs/opshub/issues/384),
ADR-0029) adds 5 tests pinning the secretary skill install integration:

* ``--install-skills`` / ``--no-install-skills`` explicit flag wins over
  the TTY probe.
* TTY-detected prompt path honours both ``y`` (install) and ``n`` (skip)
  answers from :class:`rich.prompt.Confirm`.
* Non-TTY fall-through installs by default (ADR-0029 §決定 (d)) —
  motivated by the ``uv tool install ... && opshub init`` one-liner
  flow where stdin is never a terminal.

The tests patch
:func:`opshub.cli.init.install_command` (the lazy-imported reference
inside :func:`init_command`) rather than the real symbol on
:mod:`opshub.cli.skills` so the install side-effect is intercepted
without touching the host's real ``~/.claude/skills/`` directory.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import inspect
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.cli.init import STARTER_CONFIG_TOML
from opshub.db.engine import create_engine_for_sqlite


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point every OpsHub path env var inside ``tmp_path`` and return them."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))

    return {
        "config_dir": config_dir,
        "data_dir": data_dir,
        "workspace_root": workspace_root,
        "db_path": db_path,
    }


def _assert_events_table(db_path: Path) -> None:
    """Open the SQLite file and assert the ``events`` table exists."""
    engine = create_engine_for_sqlite(db_path)
    try:
        inspector = inspect(engine)
        assert "events" in inspector.get_table_names()
    finally:
        engine.dispose()


def test_init_creates_dirs_config_and_runs_migrations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout

    # Every directory was created.
    assert paths["config_dir"].is_dir()
    assert paths["data_dir"].is_dir()
    assert paths["workspace_root"].is_dir()
    assert paths["db_path"].parent.is_dir()

    # Starter config was written.
    config_file = paths["config_dir"] / "config.toml"
    assert config_file.is_file()
    assert config_file.read_text(encoding="utf-8") == STARTER_CONFIG_TOML

    # Alembic ran to head -> SQLite file exists and has the events table.
    assert paths["db_path"].is_file()
    _assert_events_table(paths["db_path"])


def test_init_is_idempotent_and_preserves_user_edits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    # First run installs the starter config.
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.stdout

    # Simulate user customisation of the TOML file.
    config_file = paths["config_dir"] / "config.toml"
    user_edited = STARTER_CONFIG_TOML + '\n# user-added comment\nfoo = "bar"\n'
    config_file.write_text(user_edited, encoding="utf-8")

    # Second run must not overwrite the user's edits.
    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0, second.stdout
    assert config_file.read_text(encoding="utf-8") == user_edited


def test_init_force_overwrites_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.stdout

    config_file = paths["config_dir"] / "config.toml"
    config_file.write_text("# clobbered by user\n", encoding="utf-8")

    forced = runner.invoke(app, ["init", "--force"])
    assert forced.exit_code == 0, forced.stdout
    assert config_file.read_text(encoding="utf-8") == STARTER_CONFIG_TOML


# ---------------------------------------------------------------------------
# Phase 16-C (#384, ADR-0029) — secretary skill install integration.
#
# The 5 tests below patch ``opshub.cli.init.install_command`` to assert the
# tri-state ``--install-skills`` / ``--no-install-skills`` / unset decision
# logic without writing into the operator's real ``~/.claude/skills/`` or
# ``~/.agents/skills/`` directory.
#
# Pre-existing tests above (``test_init_creates_dirs_*`` etc.) run in non-TTY
# (CliRunner.invoke pipes stdin) and therefore go through the non-TTY default
# = install branch. That side effect lands inside the per-test ``tmp_path``
# only because :func:`pytest.MonkeyPatch.setenv` re-points ``HOME``-style env
# vars; for the new tests we go one step further and stub out the install
# command entirely so we can assert *when* it was called.
# ---------------------------------------------------------------------------


def _patch_install_command(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``install_command`` (looked up lazily inside ``init_command``).

    :func:`opshub.cli.init.init_command` does ``from opshub.cli.skills
    import install_command`` *inside* the install branch, so the patched
    symbol must live on the real source module — patching the local name
    in :mod:`opshub.cli.init` would have no effect because the import
    has not happened yet at patch time.
    """
    mock = MagicMock(return_value=None)
    monkeypatch.setattr("opshub.cli.skills.install_command", mock)
    return mock


def test_init_install_skills_flag_runs_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--install-skills`` overrides the TTY probe and calls install."""
    _isolate_env(monkeypatch, tmp_path)
    install_mock = _patch_install_command(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--install-skills"])

    assert result.exit_code == 0, result.stdout
    install_mock.assert_called_once_with(host="all", scope="user", skip_existing=False)


def test_init_no_install_skills_flag_skips_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--no-install-skills`` overrides the TTY probe and skips install."""
    _isolate_env(monkeypatch, tmp_path)
    install_mock = _patch_install_command(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--no-install-skills"])

    assert result.exit_code == 0, result.stdout
    install_mock.assert_not_called()


def test_init_tty_prompt_yes_runs_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TTY + Confirm.ask returns True → install runs.

    ``opshub.cli.init._stdin_is_tty`` is the one-line wrapper around
    ``sys.stdin.isatty()`` documented at the symbol's source — patching
    it (rather than ``sys.stdin.isatty`` directly) sidesteps
    :class:`typer.testing.CliRunner`'s stdin isolation, which swaps in
    a buffer whose ``isatty()`` always returns ``False`` regardless of
    upstream monkeypatching.
    """
    _isolate_env(monkeypatch, tmp_path)
    install_mock = _patch_install_command(monkeypatch)

    # Force the TTY branch even though CliRunner pipes stdin.
    monkeypatch.setattr("opshub.cli.init._stdin_is_tty", lambda: True)
    # ``rich.prompt.Confirm`` is imported lazily inside ``init_command``;
    # patch the source class so the lazy import returns the stub.
    confirm_mock = MagicMock(return_value=True)
    monkeypatch.setattr("rich.prompt.Confirm.ask", confirm_mock)

    runner = CliRunner()
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.stdout
    confirm_mock.assert_called_once()
    install_mock.assert_called_once_with(host="all", scope="user", skip_existing=False)


def test_init_tty_prompt_no_skips_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TTY + Confirm.ask returns False → install is skipped."""
    _isolate_env(monkeypatch, tmp_path)
    install_mock = _patch_install_command(monkeypatch)

    monkeypatch.setattr("opshub.cli.init._stdin_is_tty", lambda: True)
    confirm_mock = MagicMock(return_value=False)
    monkeypatch.setattr("rich.prompt.Confirm.ask", confirm_mock)

    runner = CliRunner()
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.stdout
    confirm_mock.assert_called_once()
    install_mock.assert_not_called()


def test_init_non_tty_defaults_to_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Non-TTY + flag unset → install runs (ADR-0029 §決定 (d) motivating flow).

    Explicitly forces the non-TTY branch via the wrapper so this test
    documents the contract even though :class:`typer.testing.CliRunner`
    already produces a non-TTY stdin by default.
    """
    _isolate_env(monkeypatch, tmp_path)
    install_mock = _patch_install_command(monkeypatch)

    monkeypatch.setattr("opshub.cli.init._stdin_is_tty", lambda: False)
    # ``Confirm.ask`` must not be reached on the non-TTY branch.
    confirm_mock = MagicMock(side_effect=AssertionError("Confirm.ask reached on non-TTY"))
    monkeypatch.setattr("rich.prompt.Confirm.ask", confirm_mock)

    runner = CliRunner()
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.stdout
    confirm_mock.assert_not_called()
    install_mock.assert_called_once_with(host="all", scope="user", skip_existing=False)
