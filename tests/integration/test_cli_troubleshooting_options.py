"""Integration tests for the global troubleshooting CLI options (T2 of #317).

Pins the wiring added to :func:`opshub.cli.app._root`:

* ``-v`` / ``-vv`` lift the log level to INFO / DEBUG
* ``-q`` / ``-qq`` drop it to WARNING / ERROR
* ``--debug`` flips the debug flag (full sanitised traceback in
  :func:`opshub.cli.app.main`'s error wrapper) and forces DEBUG level
* ``--log-format`` overrides the TTY-based renderer detection
* ``--log-file`` tees logs to a 0600-mode file
* The five flags resolve through
  :func:`opshub.core.logging.resolve_log_settings` so CLI > env > default
  priority holds end-to-end
* The resolved :class:`LogSettings` lands on ``ctx.obj`` (T3 reads
  ``ctx.obj["debug"]`` from connector sync / MCP rendering paths)
* ``OPSHUB_DEBUG=1`` is exported into the environment when DEBUG is
  effective, so subprocess paths (MCP serve) that do not inherit the
  parent ``ctx`` still receive the signal
* ``opshub --help`` / ``opshub --version`` short-circuit before the
  callback body runs, so ``configure_logging`` is never invoked on
  the help / version path (R6 cold-start guard)

All tests run in-process via :class:`typer.testing.CliRunner`. The
cold-start wall-clock budget itself is policed by
:file:`tests/integration/test_cold_start.py`; this file pins the
*cause* (no ``configure_logging`` call on the help path) so a
regression flags here loudly before the wall-clock test catches it.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import structlog
import typer
from typer.testing import CliRunner

from opshub.cli.app import _debug_active, app, main  # pyright: ignore[reportPrivateUsage]
from opshub.core import logging as opshub_logging
from opshub.core.errors import ConflictError, OpsHubError, ValidationError
from tests._secrets import FAKE_GITHUB_PAT

# A canonical ``sk-`` token kept locally so tests do not have to import
# from ``tests._secrets`` for the most common shape (matches the
# pattern used by ``tests/unit/core/test_logging.py``).
FAKE_SK_KEY = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ12345"


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Drop the structlog config / handlers between tests.

    :func:`opshub.core.logging.configure_logging` is idempotent — first
    call wins for the lifetime of the interpreter — so every test that
    invokes the CLI must start from a clean slate or the *second*
    test would silently observe the *first* test's level setting.

    Also restores ``OPSHUB_*`` env vars so a test that mutates them
    cannot leak into the next.
    """
    previous = opshub_logging._configured  # pyright: ignore[reportPrivateUsage]
    saved_env = {
        key: os.environ.get(key)
        for key in (
            "OPSHUB_LOG_LEVEL",
            "OPSHUB_LOG_FORMAT",
            "OPSHUB_LOG_FILE",
            "OPSHUB_DEBUG",
        )
    }

    opshub_logging._configured = False  # pyright: ignore[reportPrivateUsage]
    structlog.reset_defaults()
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)

    try:
        yield
    finally:
        opshub_logging._configured = previous  # pyright: ignore[reportPrivateUsage]
        structlog.reset_defaults()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _spy_configure(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``configure_logging`` with a spy that records every call.

    The returned list captures the kwargs of each invocation so tests
    can assert on the resolved ``level`` / ``json`` / ``log_file``
    values that the root callback handed downstream. We delegate to
    the real implementation so any downstream side effects (file
    creation, structlog config) still run — which lets the same spy
    cover both the "what was requested" and "what actually happened"
    axes.

    Tests must not assert on ``logging.getLogger().level`` directly:
    pytest's logging plugin re-installs its capture handler between
    fixture teardown and test body, which makes
    :func:`logging.basicConfig` a no-op (it does nothing when the
    root logger already has handlers) and leaves the level frozen at
    whatever pytest configured. Spying on ``configure_logging`` is
    the contract-level check.
    """
    calls: list[dict[str, Any]] = []
    real = opshub_logging.configure_logging

    def _capture(**kwargs: Any) -> None:
        calls.append(kwargs)
        real(**kwargs)

    monkeypatch.setattr(opshub_logging, "configure_logging", _capture)
    return calls


# ============================================================================
# Verbosity flags (assert what was resolved & passed to configure_logging)
# ============================================================================


class TestVerbosityFlags:
    def test_no_flags_keeps_default_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, result.stdout
        assert len(calls) == 1
        assert calls[0]["level"] == "INFO"

    def test_single_v_keeps_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["-v", "version"])
        assert result.exit_code == 0, result.stdout
        # -v alone stays at INFO (the default); -vv lifts to DEBUG.
        assert calls[0]["level"] == "INFO"

    def test_double_v_lifts_to_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["-vv", "version"])
        assert result.exit_code == 0, result.stdout
        assert calls[0]["level"] == "DEBUG"

    def test_single_q_drops_to_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["-q", "version"])
        assert result.exit_code == 0, result.stdout
        assert calls[0]["level"] == "WARNING"

    def test_double_q_drops_to_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["-qq", "version"])
        assert result.exit_code == 0, result.stdout
        assert calls[0]["level"] == "ERROR"

    def test_debug_flag_forces_debug_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["--debug", "version"])
        assert result.exit_code == 0, result.stdout
        assert calls[0]["level"] == "DEBUG"


# ============================================================================
# Help / version short-circuit (R6 cold-start guard)
# ============================================================================


class TestHelpVersionShortCircuit:
    """``--help`` and ``--version`` must not invoke ``configure_logging``.

    The wiring relies on this: ``configure_logging`` imports structlog,
    builds the processor chain, and (with ``--log-file``) opens a file
    handler. Paying for any of that on ``opshub --help`` would blow
    the ADR-0001 cold-start budget. The cold-start wall-clock test
    catches the *symptom*; this test pins the *cause*.
    """

    def test_help_does_not_configure_logging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []

        def _spy(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(opshub_logging, "configure_logging", _spy)

        runner = CliRunner()
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0, result.stdout
        assert not calls, (
            "configure_logging must not run on the --help path "
            "(ADR-0001 cold-start budget). "
            f"Got {len(calls)} call(s): {calls!r}"
        )

    def test_version_flag_does_not_configure_logging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []

        def _spy(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(opshub_logging, "configure_logging", _spy)

        runner = CliRunner()
        result = runner.invoke(app, ["--version"])

        assert result.exit_code == 0, result.stdout
        assert not calls, (
            "configure_logging must not run on the eager --version path. "
            f"Got {len(calls)} call(s): {calls!r}"
        )


# ============================================================================
# Context object (ctx.obj) — handover to T3 (#320)
# ============================================================================


class TestContextHandover:
    """The root callback exposes the resolved settings on ``ctx.obj``.

    T3 (connector sync / MCP error rendering, #320) reads
    ``ctx.obj["debug"]`` to decide whether to print a sanitised
    traceback alongside the connector summary. The contract is pinned
    here so a T3 patch can rely on the shape staying stable.
    """

    def test_ctx_obj_carries_log_settings(self) -> None:
        """A subcommand can read ``ctx.obj["debug"]`` (T3 contract)."""
        from opshub.core.logging import LogSettings

        captured: dict[str, Any] = {}

        @app.command(name="_probe_ctx", hidden=True)
        def _probe_ctx(ctx: typer.Context) -> None:  # pyright: ignore[reportUnusedFunction]
            captured["obj"] = ctx.obj
            typer.echo("ok")

        runner = CliRunner()
        result = runner.invoke(app, ["--debug", "_probe_ctx"])
        assert result.exit_code == 0, result.stdout
        obj = captured["obj"]
        assert isinstance(obj, dict)
        assert obj["debug"] is True
        settings = cast("LogSettings", obj["log_settings"])
        assert isinstance(settings, LogSettings)
        assert settings.level == "DEBUG"
        assert settings.debug is True

    def test_ctx_obj_carries_default_settings_without_debug(self) -> None:
        """Without ``--debug``, ``ctx.obj["debug"]`` is False but settings are still present."""
        from opshub.core.logging import LogSettings

        captured: dict[str, Any] = {}

        @app.command(name="_probe_ctx_default", hidden=True)
        def _probe_ctx_default(ctx: typer.Context) -> None:  # pyright: ignore[reportUnusedFunction]
            captured["obj"] = ctx.obj
            typer.echo("ok")

        runner = CliRunner()
        result = runner.invoke(app, ["_probe_ctx_default"])
        assert result.exit_code == 0, result.stdout
        obj = captured["obj"]
        assert obj["debug"] is False
        settings = cast("LogSettings", obj["log_settings"])
        assert isinstance(settings, LogSettings)
        assert settings.level == "INFO"

    def test_debug_flag_exports_opshub_debug_env(self) -> None:
        # Pre-condition: the autouse fixture restored a clean env.
        os.environ.pop("OPSHUB_DEBUG", None)
        runner = CliRunner()
        result = runner.invoke(app, ["--debug", "version"])
        assert result.exit_code == 0, result.stdout
        assert os.environ.get("OPSHUB_DEBUG") == "1"

    def test_double_v_exports_opshub_debug_env(self) -> None:
        """``-vv`` exports ``OPSHUB_DEBUG=1`` (matches docs §1, §33 contract).

        Post-merge audit followup for epic #317: docs already document
        that ``-vv`` implies ``--debug`` (incl. the ``OPSHUB_DEBUG``
        subprocess hand-off), so the env export must fire on the ``-vv``
        path too. Pins the H1 fix.
        """
        os.environ.pop("OPSHUB_DEBUG", None)
        runner = CliRunner()
        result = runner.invoke(app, ["-vv", "version"])
        assert result.exit_code == 0, result.stdout
        assert os.environ.get("OPSHUB_DEBUG") == "1"

    def test_default_does_not_export_opshub_debug_env(self) -> None:
        os.environ.pop("OPSHUB_DEBUG", None)
        runner = CliRunner()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, result.stdout
        assert os.environ.get("OPSHUB_DEBUG") is None


# ============================================================================
# Log format override
# ============================================================================


class TestLogFormatOverride:
    """``--log-format`` overrides the TTY-based renderer auto-detect."""

    def test_json_format_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force stderr to look like a TTY so the default would pick
        # console; the explicit flag must override that.
        monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
        calls = _spy_configure(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(app, ["--log-format", "json", "version"])
        assert result.exit_code == 0, result.stdout
        assert calls[0]["json"] is True

    def test_console_format_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force stderr to look like a non-TTY (CI / pipe) so the default
        # would pick JSON; the explicit flag must override that.
        monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
        calls = _spy_configure(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(app, ["--log-format", "console", "version"])
        assert result.exit_code == 0, result.stdout
        assert calls[0]["json"] is False

    def test_auto_format_leaves_decision_to_configure_logging(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_configure(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, result.stdout
        # ``auto`` translates to ``json=None`` so ``configure_logging``
        # probes ``sys.stderr.isatty()`` itself.
        assert calls[0]["json"] is None


# ============================================================================
# Log file tee
# ============================================================================


class TestLogFileTee:
    """``--log-file`` plumbs the path into ``configure_logging``.

    The detailed 0600-mode and redaction guarantees are pinned by the
    T1 unit tests under ``tests/unit/core/test_logging.py``. This
    integration test only confirms that the CLI flag actually reaches
    the configuration call and that the file does get created (so the
    end-to-end path is wired).
    """

    def test_log_file_path_reaches_configure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "opshub.log"
        calls = _spy_configure(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(app, ["--log-file", str(target), "version"])
        assert result.exit_code == 0, result.stdout
        assert calls[0]["log_file"] == target
        assert target.exists(), "configure_logging should have created the file"
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"expected 0o600 (R5), got {oct(mode)}"


# ============================================================================
# Env-var priority (CLI > env > default)
# ============================================================================


class TestEnvPriority:
    """CLI flag wins over ``OPSHUB_LOG_LEVEL`` env when both are set."""

    def test_cli_flag_overrides_env_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPSHUB_LOG_LEVEL", "WARNING")
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["-vv", "version"])
        assert result.exit_code == 0, result.stdout
        # CLI ``-vv`` (DEBUG) wins over env ``WARNING``.
        assert calls[0]["level"] == "DEBUG"

    def test_env_log_level_used_without_cli_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPSHUB_LOG_LEVEL", "ERROR")
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, result.stdout
        assert calls[0]["level"] == "ERROR"

    def test_env_debug_implies_debug_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, result.stdout
        assert calls[0]["level"] == "DEBUG"


# ============================================================================
# Error wrapper (--debug semantics + R2 redaction)
# ============================================================================


def _install_failing_command(*, exc: BaseException, name: str = "_boom") -> None:
    """Register a hidden ``opshub _boom`` command that raises ``exc``.

    The command is registered on the real ``app`` (not a fresh Typer)
    so the wired root callback actually runs first — that is precisely
    what we need to test: the callback's ``--debug`` plumbing flowing
    into ``main()``'s error wrapper.

    Idempotent: registering the same name twice is safe; Click silently
    overrides the existing command.
    """

    @app.command(name=name, hidden=True)
    def _failing() -> None:  # pyright: ignore[reportUnusedFunction]
        raise exc


class TestErrorWrapperDebug:
    """``main()`` wrapper: default one-liner vs ``--debug`` full traceback."""

    def test_default_emits_one_line_error_for_validation(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_failing_command(exc=ValidationError("bad input"), name="_boom_validation_default")
        monkeypatch.setattr(sys, "argv", ["opshub", "_boom_validation_default"])

        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 2

        captured = capsys.readouterr()
        assert "Error: bad input" in captured.err
        # No full traceback in default mode.
        assert "Traceback" not in captured.err

    def test_default_emits_one_line_error_for_opshub_error(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_failing_command(exc=ConflictError("locked already"), name="_boom_opshub_default")
        monkeypatch.setattr(sys, "argv", ["opshub", "_boom_opshub_default"])

        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1

        captured = capsys.readouterr()
        assert "Error: locked already" in captured.err
        assert "Traceback" not in captured.err

    def test_debug_emits_sanitised_traceback_for_opshub_error(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_failing_command(exc=ConflictError("locked already"), name="_boom_opshub_debug")
        monkeypatch.setattr(sys, "argv", ["opshub", "--debug", "_boom_opshub_debug"])

        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1

        captured = capsys.readouterr()
        # One-line error still present.
        assert "Error: locked already" in captured.err
        # Plus full traceback.
        assert "Traceback" in captured.err
        assert "ConflictError" in captured.err

    def test_debug_sanitises_token_in_opshub_error_message(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R2: ``--debug`` traceback for an ``OpsHubError`` carrying a token must redact it."""
        _install_failing_command(
            exc=OpsHubError(f"upstream 401 sk={FAKE_SK_KEY}"),
            name="_boom_sk_debug",
        )
        monkeypatch.setattr(sys, "argv", ["opshub", "--debug", "_boom_sk_debug"])

        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1

        captured = capsys.readouterr()
        # The raw token must NOT appear, neither in the one-liner nor
        # in the full traceback.
        assert FAKE_SK_KEY not in captured.err
        # The marker must be present in both layers.
        assert "sk-***" in captured.err
        assert "Traceback" in captured.err

    def test_default_sanitises_token_in_one_line_error(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R2 (default path): even without ``--debug``, a token in the message must redact."""
        _install_failing_command(
            exc=OpsHubError(f"github 401 pat={FAKE_GITHUB_PAT}"),
            name="_boom_pat_default",
        )
        monkeypatch.setattr(sys, "argv", ["opshub", "_boom_pat_default"])

        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1

        captured = capsys.readouterr()
        assert FAKE_GITHUB_PAT not in captured.err
        assert "github_pat_***" in captured.err
        # Default mode: no traceback.
        assert "Traceback" not in captured.err

    def test_debug_re_raises_non_opshub_error_after_sanitised_render(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-OpsHubError (real bug) propagates; --debug pre-renders sanitised traceback."""
        _install_failing_command(
            exc=RuntimeError(f"unexpected sk={FAKE_SK_KEY}"),
            name="_boom_runtime_debug",
        )
        monkeypatch.setattr(sys, "argv", ["opshub", "--debug", "_boom_runtime_debug"])

        with pytest.raises(RuntimeError):
            main()

        captured = capsys.readouterr()
        assert FAKE_SK_KEY not in captured.err
        assert "sk-***" in captured.err

    def test_default_lets_non_opshub_error_propagate_silently(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --debug, non-OpsHubError still propagates raw (native traceback for CI)."""
        _install_failing_command(
            exc=RuntimeError("plain bug"),
            name="_boom_runtime_default",
        )
        monkeypatch.setattr(sys, "argv", ["opshub", "_boom_runtime_default"])

        with pytest.raises(RuntimeError, match="plain bug"):
            main()

        captured = capsys.readouterr()
        # Default mode: we did NOT pre-render a sanitised traceback (the
        # interpreter's default handler will print it after we re-raise).
        assert "sk-***" not in captured.err

    def test_env_opshub_debug_activates_full_traceback(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``OPSHUB_DEBUG=1`` (without CLI ``--debug``) also activates full traceback."""
        _install_failing_command(exc=OpsHubError("env debug works"), name="_boom_env_debug")
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        monkeypatch.setattr(sys, "argv", ["opshub", "_boom_env_debug"])

        with pytest.raises(SystemExit):
            main()

        captured = capsys.readouterr()
        assert "Error: env debug works" in captured.err
        assert "Traceback" in captured.err


# ============================================================================
# _debug_active helper
# ============================================================================


class TestDebugActiveHelper:
    """The env-var probe used by ``main()``'s error wrapper."""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " on ", "debug"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("OPSHUB_DEBUG", value)
        assert _debug_active() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("OPSHUB_DEBUG", value)
        assert _debug_active() is False

    def test_unset_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        assert _debug_active() is False


# ============================================================================
# Flag co-occurrence (epic #316 ``--no-progress`` x epic #317 verbosity)
# ============================================================================


class TestFlagCombinations:
    """``--no-progress`` (epic #316) combines cleanly with verbosity flags (epic #317).

    Post-merge audit followup: epic #316 and epic #317 each added
    root-callback flags but the integration combinations were never
    pinned. A future refactor of the root callback (e.g. parameter
    reordering, eager-option promotion, callback signature change)
    could silently break one combination while keeping the others
    green. These tests pin every pairwise combination at the
    happy-path level so regressions surface here.
    """

    def test_no_progress_and_debug_combine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--no-progress --debug`` resolves both flags together."""
        os.environ.pop("OPSHUB_DEBUG", None)
        calls = _spy_configure(monkeypatch)

        from opshub.cli import _progress

        runner = CliRunner()
        result = runner.invoke(app, ["--no-progress", "--debug", "version"])

        assert result.exit_code == 0, result.stdout
        # ``--debug`` lifted to DEBUG level.
        assert calls[0]["level"] == "DEBUG"
        # ``--no-progress`` flowed into the progress preference.
        assert _progress._preference is False  # pyright: ignore[reportPrivateUsage]
        # ``OPSHUB_DEBUG=1`` env export still fires alongside ``--no-progress``.
        assert os.environ.get("OPSHUB_DEBUG") == "1"

    def test_no_progress_and_verbose_combine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--no-progress -vv`` lifts level to DEBUG and disables progress."""
        os.environ.pop("OPSHUB_DEBUG", None)
        calls = _spy_configure(monkeypatch)

        from opshub.cli import _progress

        runner = CliRunner()
        result = runner.invoke(app, ["--no-progress", "-vv", "version"])

        assert result.exit_code == 0, result.stdout
        assert calls[0]["level"] == "DEBUG"
        assert _progress._preference is False  # pyright: ignore[reportPrivateUsage]
        # ``-vv`` implies ``--debug`` (H1) → OPSHUB_DEBUG exported.
        assert os.environ.get("OPSHUB_DEBUG") == "1"

    def test_no_progress_and_quiet_combine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--no-progress -qq`` drops level to ERROR and disables progress."""
        calls = _spy_configure(monkeypatch)

        from opshub.cli import _progress

        runner = CliRunner()
        result = runner.invoke(app, ["--no-progress", "-qq", "version"])

        assert result.exit_code == 0, result.stdout
        assert calls[0]["level"] == "ERROR"
        assert _progress._preference is False  # pyright: ignore[reportPrivateUsage]

    def test_progress_and_log_format_combine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--progress --log-format json`` honours both flags."""
        calls = _spy_configure(monkeypatch)

        from opshub.cli import _progress

        runner = CliRunner()
        result = runner.invoke(app, ["--progress", "--log-format", "json", "version"])

        assert result.exit_code == 0, result.stdout
        assert calls[0]["json"] is True
        assert _progress._preference is True  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# -v / -q precedence pin (epic #317 audit L4)
# ============================================================================


class TestVerboseQuietPrecedence:
    """``-q`` wins over ``-v`` when both are supplied.

    The implementation in :mod:`opshub.core.logging` has had this
    "quiet beats verbose" behaviour from T1, but it was undocumented at
    the operator-facing layer. Pin it here so the docs sentence in
    [`docs/troubleshooting.md`](../../docs/troubleshooting.md) §1 (L4
    of the post-merge audit) cannot drift away from the implementation.
    """

    def test_verbose_then_quiet_yields_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["-v", "-q", "version"])
        assert result.exit_code == 0, result.stdout
        # ``-q`` (WARNING) wins over ``-v`` (INFO).
        assert calls[0]["level"] == "WARNING"

    def test_double_verbose_then_quiet_yields_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_configure(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(app, ["-vv", "-q", "version"])
        assert result.exit_code == 0, result.stdout
        # Even ``-vv`` (which would imply DEBUG + debug=True) is
        # overruled by a subsequent ``-q``. The conservative posture
        # protects operators who add ``-q`` to a noisy command without
        # noticing an earlier ``-v``.
        assert calls[0]["level"] == "WARNING"
