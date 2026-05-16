"""Tests for the ``main()`` wrapper in :mod:`opshub.cli.app`.

The wrapper catches :class:`opshub.core.errors.OpsHubError` (and its
:class:`ValidationError` subclass) raised from any command callback and
re-emits them as a single ``Error: <message>`` line on stderr plus a
deterministic exit code (2 for validation, 1 for everything else).

Tests inject a temporary Typer command into the real ``app`` so we
exercise the actual wiring inside :func:`main`; the command is removed
on tear-down so the production CLI surface is not polluted for later
tests in the same session.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

from opshub.cli import app as app_module
from opshub.core.errors import ConfigError, ConflictError, ValidationError

# A unique command name keeps the fixture self-contained even if multiple
# tests run in parallel against the same Typer app.
_RAISER_COMMAND = "__test_raiser__"


@pytest.fixture
def install_raiser() -> Generator[Any]:
    """Yield a callable that registers a temporary command on the CLI.

    The command raises whatever exception is passed in. Tests use this to
    drive ``main()`` end-to-end without needing a real database / config
    on disk.
    """

    holder: dict[str, BaseException] = {}

    @app_module.app.command(_RAISER_COMMAND, hidden=True)
    def _raise() -> None:  # pyright: ignore[reportUnusedFunction]
        raise holder["exc"]

    def _set(exc: BaseException) -> None:
        holder["exc"] = exc

    yield _set

    # Typer (Click) stores commands on the underlying ``typer_instance``;
    # we reach in to pop ours so the fixture leaves no trace.
    registered = app_module.app.registered_commands
    app_module.app.registered_commands = [cmd for cmd in registered if cmd.name != _RAISER_COMMAND]


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> tuple[int, str, str]:
    """Invoke ``main()`` with the given ``argv`` and return (code, stdout, stderr).

    ``main()`` calls ``app()`` which raises ``SystemExit`` on completion
    (Click's convention). We catch it here so each test asserts on the
    code directly.
    """
    monkeypatch.setattr("sys.argv", ["opshub", *argv])
    with pytest.raises(SystemExit) as excinfo:
        app_module.main()
    code = excinfo.value.code
    captured = capsys.readouterr()
    return (int(code) if isinstance(code, int) else 0, captured.out, captured.err)


def test_main_returns_zero_on_clean_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sanity: ``opshub version`` exits 0 through the wrapped ``main``."""
    code, stdout, _ = _run_main(monkeypatch, capsys, ["version"])
    assert code == 0
    assert "opshub" in stdout


def test_main_validation_error_exits_2_with_clean_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    install_raiser: Any,
) -> None:
    install_raiser(ValidationError("bad value 'foo'"))
    code, _, stderr = _run_main(monkeypatch, capsys, [_RAISER_COMMAND])

    assert code == 2
    # Stderr is a single Error: line — no Traceback ever leaks.
    assert stderr.strip() == "Error: bad value 'foo'"
    assert "Traceback" not in stderr


def test_main_config_error_exits_1_with_clean_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    install_raiser: Any,
) -> None:
    install_raiser(ConfigError("run `opshub init` first"))
    code, _, stderr = _run_main(monkeypatch, capsys, [_RAISER_COMMAND])

    assert code == 1
    assert stderr.strip() == "Error: run `opshub init` first"
    assert "Traceback" not in stderr


def test_main_conflict_error_exits_1_with_clean_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    install_raiser: Any,
) -> None:
    """Any non-ValidationError OpsHubError uses exit code 1.

    ``ConflictError`` is the canonical Phase 2 case (lock already held).
    Verifying it goes through the same handler as ``ConfigError`` keeps
    the wrapper honest as new subclasses are added.
    """
    install_raiser(ConflictError("lock already held"))
    code, _, stderr = _run_main(monkeypatch, capsys, [_RAISER_COMMAND])

    assert code == 1
    assert stderr.strip() == "Error: lock already held"
    assert "Traceback" not in stderr


def test_main_unexpected_exception_propagates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    install_raiser: Any,
) -> None:
    """Non-OpsHub exceptions deliberately escape the wrapper.

    Unexpected bugs should surface their tracebacks so they cannot hide
    behind a swallowed-error log line.
    """
    install_raiser(RuntimeError("boom"))
    monkeypatch.setattr("sys.argv", ["opshub", _RAISER_COMMAND])
    with pytest.raises(RuntimeError, match="boom"):
        app_module.main()


def test_main_typer_argument_error_still_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing required argument is handled by Typer itself, not our wrapper.

    Typer's own ``UsageError`` path already prints a clean message and
    sets exit code 2 inside Click's runtime; our wrapper must not
    accidentally swallow that path, which would yield exit code 0.
    """
    code, _, _ = _run_main(monkeypatch, capsys, ["task", "create"])
    assert code == 2
