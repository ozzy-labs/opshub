"""Root-level pytest configuration.

Phase 16-B (ADR-0029 §決定 (a)) — the secretary-skill bundle lives at
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
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS_SKILLS = _REPO_ROOT / "docs" / "skills"
_BUNDLE_DIR = _REPO_ROOT / "src" / "opshub" / "_skills"


@pytest.fixture(autouse=True, scope="session")
def _mirror_secretary_skill_bundle() -> None:
    """Mirror ``docs/skills/`` into ``src/opshub/_skills/`` for the test run.

    Idempotent — every pytest session re-syncs from the SSOT so a
    drift between the bundle and the source-of-truth (e.g. a stale
    file left over from a previous ``uv build``) cannot make the test
    suite false-green. The mirror is destructive on purpose:
    :func:`shutil.rmtree` followed by :func:`shutil.copytree` is
    cheap for the ~110 KB / 14-file payload and keeps the semantics
    crystal clear ("after this fixture runs, ``_skills/`` is byte-equal
    to ``docs/skills/``").
    """
    if not _DOCS_SKILLS.is_dir():
        # The SSOT is missing — surfacing this loudly via the assertion
        # message beats letting a single test fail with an opaque
        # FileNotFoundError later.
        msg = (
            f"docs/skills/ is missing at {_DOCS_SKILLS}; the secretary "
            "skill bundle cannot be mirrored. Restore the SSOT before "
            "running pytest."
        )
        raise AssertionError(msg)

    if _BUNDLE_DIR.exists():
        shutil.rmtree(_BUNDLE_DIR)
    shutil.copytree(_DOCS_SKILLS, _BUNDLE_DIR)
