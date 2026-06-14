"""Root-level pytest configuration.

Phase 16-B (ADR-0029 §決定 (a)) — the assistant-skill bundle lives at
``opshub/_skills/<name>/`` and is materialised at *build* time by the
``[tool.hatch.build.force-include]`` mapping in ``pyproject.toml``
(``docs/skills/`` → ``src/opshub/_skills/``). Editable installs and
plain source-tree checkouts never run that build step, so
``importlib.resources.files('opshub').joinpath('_skills')`` would
return a non-existent path during ``pytest``.

This fixture mirrors ``docs/skills/`` into ``src/opshub/_skills/``
once per pytest session so the resources lookup works identically in:

* editable installs (``uv sync`` against this repo);
* wheel installs (``uv tool install ozzylabs-opshub`` — the mapping
  already populated the bundle, the mirror here is a no-op overwrite
  with identical bytes);
* CI matrix runners that never call ``uv build`` explicitly.

The SSOT location is unchanged — only the build-time mirror is
reproduced. ``src/opshub/_skills/`` is in ``.gitignore`` so the
mirror never leaks into commits, and the directory is materially
distinct from the (forbidden) hand-edited ``src/opshub/skills/``
that :mod:`tests.unit.skills.test_core_boundary` already pins.

Phase 16-C (#384) — ``opshub init`` now writes the 15 assistant skills
to ``~/.claude/skills/`` and ``~/.agents/skills/`` by default
(non-interactive default = install per ADR-0029 §決定 (d), as
explained in :func:`opshub.cli.init._should_install_skills`).
Without intervention every test that invokes ``opshub init`` via
:class:`typer.testing.CliRunner` (`test_decision`, `test_task`,
``tests/integration/test_lifecycle``, et al.) would clobber the
developer's real ``~/.claude/skills/`` / ``~/.agents/skills/`` payload.
The autouse :func:`_isolate_home_for_skill_install` fixture redirects
``HOME`` (and ``USERPROFILE`` on Windows) into a per-test temporary
directory so the install lands inside the test sandbox. Tests that
need a specific HOME (e.g. ``test_skills_install``) still override
the env explicitly because :class:`pytest.MonkeyPatch` honours the
most recent ``setenv`` call.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS_SKILLS = _REPO_ROOT / "docs" / "skills"
_BUNDLE_DIR = _REPO_ROOT / "src" / "opshub" / "_skills"


@pytest.fixture(autouse=True, scope="session")
def _mirror_assistant_skill_bundle() -> None:  # pyright: ignore[reportUnusedFunction]
    """Mirror ``docs/skills/`` into ``src/opshub/_skills/`` for the test run.

    Idempotent — every pytest session re-syncs from the SSOT so a
    drift between the bundle and the source-of-truth (e.g. a stale
    file left over from a previous ``uv build``) cannot make the test
    suite false-green. The mirror is destructive on purpose:
    :func:`shutil.rmtree` followed by :func:`shutil.copytree` is
    cheap for the ~110 KB / 15-file payload and keeps the semantics
    crystal clear ("after this fixture runs, ``_skills/`` is byte-equal
    to ``docs/skills/``").
    """
    if not _DOCS_SKILLS.is_dir():
        # The SSOT is missing — surfacing this loudly via the assertion
        # message beats letting a single test fail with an opaque
        # FileNotFoundError later.
        msg = (
            f"docs/skills/ is missing at {_DOCS_SKILLS}; the assistant "
            "skill bundle cannot be mirrored. Restore the SSOT before "
            "running pytest."
        )
        raise AssertionError(msg)

    if _BUNDLE_DIR.exists():
        shutil.rmtree(_BUNDLE_DIR)
    shutil.copytree(_DOCS_SKILLS, _BUNDLE_DIR)


@pytest.fixture(autouse=True)
def _isolate_home_for_skill_install(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Redirect ``HOME`` / ``USERPROFILE`` to a per-test sandbox.

    Phase 16-C (#384) wired ``opshub init`` to also install the 15
    assistant skills via
    :func:`opshub.cli.init._should_install_skills` (non-interactive
    default = install, per ADR-0029 §決定 (d)). The wide swath of
    existing tests that drive ``opshub init`` through
    :class:`typer.testing.CliRunner` did not previously care about
    ``HOME``; without this fixture they would each overwrite the
    developer's real ``~/.claude/skills/`` / ``~/.agents/skills/``
    payload as a side-effect of the test run.

    The fixture creates a fresh temp directory per test (so leakage
    between tests is impossible) and sets both ``HOME`` (POSIX) and
    ``USERPROFILE`` (Windows) — :meth:`pathlib.Path.home` consults
    the former on POSIX and the latter on Windows, and we mirror the
    pattern already used by :func:`tests.unit.cli.test_skills_install._isolate_home`.

    Tests that need a specific HOME still call ``monkeypatch.setenv``
    on the same variable; pytest applies the most recent setenv, so
    the explicit per-test value wins over this autouse default.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
