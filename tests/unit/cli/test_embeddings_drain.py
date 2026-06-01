"""Tests for ``opshub embeddings drain`` (Phase 5 step C2).

The drain command is a thin wrapper around
:meth:`EmbeddingService.embed_pending`. The tests below mock the
service via :func:`opshub.cli._wiring.build_embedding_service` so the
unit suite stays focused on CLI plumbing (Typer option parsing, exit
code mapping, output rendering) and does not exercise the embedding
pipeline itself — that path is covered by
``tests/unit/cli/test_embeddings.py`` (rebuild) and
``tests/unit/services/test_embedding_service.py``.

The disabled-backend case is exercised end-to-end through the same
wiring rebuild uses, so the "same exit semantic as rebuild" promise
in the Phase 5 plan is verified by going through the real factory
(NoOpEmbedder + the standard ``build_embedding_service`` path).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.services.embedding_service import EmbedResult


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point OpsHub env vars at ``tmp_path`` and return the SQLite path.

    Mirrors the helper in ``tests/unit/cli/test_embeddings.py`` so the
    drain test suite stays in lock-step with the rebuild suite on env
    isolation. We intentionally duplicate the helper instead of
    importing it: the test files are siblings, and pytest discourages
    cross-test imports for fixture / helper reuse.
    """
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))

    return db_path


def _install_mock_service(monkeypatch: pytest.MonkeyPatch, result: EmbedResult) -> MagicMock:
    """Replace ``build_embedding_service`` with a mock returning ``result``.

    Returns the inner service mock so individual tests can assert on the
    exact call shape (``entity_type`` / ``limit`` kwargs).
    """
    service = MagicMock()
    service.embed_pending.return_value = result

    def _stub_build(actor: str = "cli:embeddings_drain") -> MagicMock:
        del actor  # the wiring helper accepts but does not need to vary actor
        return service

    # Patch the symbol where the CLI imports it (inside the callback).
    # The drain callback uses ``from opshub.cli._wiring import
    # build_embedding_service`` so the lookup happens in
    # :mod:`opshub.cli._wiring`.
    from opshub.cli import _wiring

    monkeypatch.setattr(_wiring, "build_embedding_service", _stub_build)
    return service


# ---- drain ----------------------------------------------------------------


def test_drain_calls_embed_pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``opshub embeddings drain`` invokes ``embed_pending`` once with no filter."""
    _isolate_env(monkeypatch, tmp_path)
    service = _install_mock_service(
        monkeypatch,
        EmbedResult(
            embedded_count=3,
            skipped_count=1,
            failed_count=0,
            rebuild_run_id="01HZRAIN0000000000000DRAIN",
        ),
    )
    runner = CliRunner()

    result = runner.invoke(app, ["embeddings", "drain"])

    assert result.exit_code == 0, result.stdout
    # progress_callback is the determinate reporter's advance (a no-op on the
    # non-TTY test runner); assert the scope/limit kwargs and accept any callback.
    service.embed_pending.assert_called_once_with(
        entity_type=None, limit=None, progress_callback=ANY
    )
    assert "embedded 3" in result.stdout
    assert "skipped 1" in result.stdout
    assert "failed 0" in result.stdout
    assert "rebuild_run_id=01HZRAIN0000000000000DRAIN" in result.stdout


def test_drain_passes_entity_type_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--entity-type task`` reaches ``embed_pending(entity_type="task", ...)``."""
    _isolate_env(monkeypatch, tmp_path)
    service = _install_mock_service(
        monkeypatch,
        EmbedResult(
            embedded_count=2,
            skipped_count=0,
            failed_count=0,
            rebuild_run_id="01HZRAIN0000000000000FILTR",
        ),
    )
    runner = CliRunner()

    result = runner.invoke(app, ["embeddings", "drain", "--entity-type", "task"])

    assert result.exit_code == 0, result.stdout
    service.embed_pending.assert_called_once_with(
        entity_type="task", limit=None, progress_callback=ANY
    )


def test_drain_passes_limit_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--limit 5`` reaches ``embed_pending(..., limit=5)``."""
    _isolate_env(monkeypatch, tmp_path)
    service = _install_mock_service(
        monkeypatch,
        EmbedResult(
            embedded_count=5,
            skipped_count=0,
            failed_count=0,
            rebuild_run_id="01HZRAIN0000000000000LIMIT",
        ),
    )
    runner = CliRunner()

    result = runner.invoke(app, ["embeddings", "drain", "--limit", "5"])

    assert result.exit_code == 0, result.stdout
    service.embed_pending.assert_called_once_with(entity_type=None, limit=5, progress_callback=ANY)


def test_drain_exit_code_zero_on_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A populated ``EmbedResult`` round-trips with exit code 0.

    Even when ``failed_count`` is non-zero the command exits 0: per-row
    failures are accounted for in the EmbeddingFailed event log and do
    not change the CLI exit code (matches ``embeddings rebuild`` from
    Phase 4 step B3).
    """
    _isolate_env(monkeypatch, tmp_path)
    _install_mock_service(
        monkeypatch,
        EmbedResult(
            embedded_count=10,
            skipped_count=2,
            failed_count=1,  # per-row failure does not raise the exit code
            rebuild_run_id="01HZRAIN00000000000000FAIL",
        ),
    )
    runner = CliRunner()

    result = runner.invoke(app, ["embeddings", "drain"])

    assert result.exit_code == 0, result.stdout
    assert "embedded 10" in result.stdout
    assert "failed 1" in result.stdout


def test_drain_disabled_backend_exits_with_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``backend = disabled`` produces the same exit code as ``rebuild``.

    Rationale: ``drain`` is a thin wrapper around the same
    :meth:`EmbeddingService.embed_pending` flow ``rebuild`` uses.
    Whatever exit semantic ``rebuild`` produces on a disabled backend,
    ``drain`` must match — operators chaining ``drain`` from a shell
    script should not have to special-case the wrapper's exit code.

    We go through the real wiring (no service mock) so the test pins
    the actual NoOpEmbedder path. The rebuild + drain CLI runs are
    paired side-by-side and asserted equal.
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "disabled")
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    rebuild = runner.invoke(app, ["embeddings", "rebuild"])
    drain = runner.invoke(app, ["embeddings", "drain"])

    # The CLI exit codes must agree — that is the contract under test.
    assert drain.exit_code == rebuild.exit_code, (
        f"drain exit {drain.exit_code} != rebuild exit {rebuild.exit_code}\n"
        f"drain stdout:\n{drain.stdout}\n"
        f"rebuild stdout:\n{rebuild.stdout}"
    )
