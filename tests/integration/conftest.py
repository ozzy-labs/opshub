"""Shared fixtures for integration tests.

Lifts the env-isolation helper that :mod:`test_lifecycle` and the new
:mod:`test_coordination_lifecycle` both need: monkeypatch ``OPSHUB_*``
environment variables to point at ``tmp_path``, then run ``opshub init``
via :class:`typer.testing.CliRunner` so the schema is provisioned and
the workspace directories exist.

Why this lives in ``conftest.py`` rather than each test module:

* Both the Phase 1 lifecycle (``test_lifecycle``) and the per-workstream
  coordination lifecycle (``test_coordination_lifecycle``) reach for the
  same boilerplate: point every ``OPSHUB_*`` env var inside ``tmp_path``,
  invoke ``opshub init``, hand back the paths. Centralising the fixture
  here keeps the two test modules small and prevents the helpers from
  drifting apart.
* The fixture also clears the per-session env vars
  (``OPSHUB_ACTOR`` / ``OPSHUB_WORK_SESSION_ID``) that
  :mod:`opshub.cli._actor` consults — without that, a stray export in
  the developer's shell would bleed into the test and silently flip the
  "no session" branch.
* ``XDG_STATE_HOME`` is also redirected so the ``session start`` →
  ``agent run begin`` → ``session end`` bracket writes its state file
  inside ``tmp_path`` rather than touching
  ``~/.local/state/opshub/current-session``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app


@pytest.fixture
def isolated_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[dict[str, Path]]:
    """Set ``OPSHUB_*`` env to tmp_path subdirs; run ``init``; yield paths dict.

    Every OpsHub path env var is redirected inside ``tmp_path`` so the
    test never touches the developer's real ``~/.config/opshub`` /
    ``~/.local/share/opshub`` / ``~/.local/state/opshub`` directories.
    Returns the resolved paths so individual tests can assert against
    files written under them.
    """
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = data_dir / "db" / "opshub.sqlite"
    state_dir = tmp_path / "state"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))
    # ``opshub.cli._actor`` writes ``current-session`` under
    # ``$XDG_STATE_HOME/opshub/``; redirect it so the session-bracket
    # tests stay hermetic.
    monkeypatch.setenv("XDG_STATE_HOME", str(state_dir))
    # Also clear any session env that could bleed from the host shell.
    monkeypatch.delenv("OPSHUB_ACTOR", raising=False)
    monkeypatch.delenv("OPSHUB_WORK_SESSION_ID", raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout

    yield {
        "config_dir": config_dir,
        "data_dir": data_dir,
        "workspace_root": workspace_root,
        "db_path": db_path,
        "state_dir": state_dir,
    }
