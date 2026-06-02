"""Pin the editable-install fallback for the bundled-skill resolver.

When opshub runs from a repo checkout (``uv sync`` / ``pip install -e .``)
the wheel's ``[tool.hatch.build.targets.wheel.force-include]`` mapping
does NOT re-run, so ``src/opshub/_skills/`` becomes stale as soon as an
opshub maintainer edits ``docs/skills/`` (the SSOT, ADR-0004 §決定 (c)).
Without the checkout-mode fallback,
``opshub skills install --scope project`` would silently re-emit the
pre-edit bundle into ``.claude/skills/`` / ``.agents/skills/``, breaking
the Phase 16-D dogfood workflow ([CLAUDE.md](/CLAUDE.md): "編集したら
``opshub skills install --scope project`` で再生成").

:func:`opshub._skills_resources._checkout_docs_skills` short-circuits to
``<repo>/docs/skills/`` when a working-tree layout is detected. These
tests pin the three branches of that detection:

1. **Happy path** — running from the opshub source tree resolves to the
   real ``docs/skills/`` directory; :func:`_skills_root` returns the
   same path so all callers see the freshly edited SSOT.
2. **Wheel fallback** — a synthetic ``site-packages``-shaped layout
   (no sibling ``docs/skills/``) leaves the resolver returning
   ``None``, so :func:`_skills_root` falls through to the bundled
   ``_skills/`` payload as before.
3. **Marker safety net** — a layout that happens to have a sibling
   ``docs/skills/`` directory but lacks the canonical SSOT marker
   (``personal-brief/SKILL.md``) is not treated as a checkout, so an
   unrelated venv that happens to live next to an unrelated
   ``docs/skills/`` directory cannot trick the resolver.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import pytest

from opshub import _skills_resources

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_checkout_resolver_returns_docs_skills_in_repo() -> None:
    """Running from the opshub source tree resolves to ``docs/skills/``."""
    result = _skills_resources._checkout_docs_skills()  # pyright: ignore[reportPrivateUsage]
    assert result is not None, (
        "running tests from the opshub repo should detect the checkout "
        "layout — got None, which means the resolver fell through to "
        "the bundled _skills/ path"
    )
    assert result == _REPO_ROOT / "docs" / "skills"
    assert (result / "personal-brief" / "SKILL.md").is_file()


def test_skills_root_prefers_checkout_over_bundle() -> None:
    """``_skills_root`` returns ``docs/skills/`` when a checkout is detected.

    This is the contract callers (``opshub skills install`` / ``list``)
    depend on: editing ``docs/skills/<name>/SKILL.md`` must be visible
    immediately to the install path, with no ``rsync`` /
    ``uv build`` round-trip.
    """
    root = _skills_resources._skills_root()  # pyright: ignore[reportPrivateUsage]
    assert Path(str(root)) == _REPO_ROOT / "docs" / "skills"


def test_checkout_resolver_rejects_layout_without_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling ``docs/skills/`` without the SSOT marker is not a checkout.

    Guards against the (very unlikely) case where opshub is installed
    into a venv that itself sits next to an unrelated ``docs/skills/``
    directory. The marker check (``personal-brief/SKILL.md``) is the
    minimum signal that the directory is the opshub SSOT.
    """
    fake_pkg = tmp_path / "src" / "opshub"
    fake_pkg.mkdir(parents=True)
    fake_docs_skills = tmp_path / "docs" / "skills"
    fake_docs_skills.mkdir(parents=True)
    # Marker absent — directory exists but is not the opshub SSOT.

    # Patch via the stdlib ``importlib.resources`` module — the
    # ``_skills_resources`` module imports ``resources`` from there,
    # so they share the same module object and the patch is visible
    # at the call site without exposing a private re-export.
    def _fake_files(_name: str) -> Path:
        return fake_pkg

    monkeypatch.setattr(importlib.resources, "files", _fake_files)
    assert _skills_resources._checkout_docs_skills() is None  # pyright: ignore[reportPrivateUsage]


def test_checkout_resolver_returns_none_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wheel-like layout (no sibling ``docs/``) yields ``None``.

    The ``site-packages/opshub/`` directory's grandparent is the
    Python ``lib/`` directory, which never contains a
    ``docs/skills/`` directory in any plausible install layout. The
    resolver must fall through so the bundled ``_skills/`` payload is
    used (preserving the wheel-install code path unchanged).
    """
    fake_pkg = tmp_path / "site-packages" / "opshub"
    fake_pkg.mkdir(parents=True)
    # No sibling docs/skills/ at tmp_path level.

    def _fake_files(_name: str) -> Path:
        return fake_pkg

    monkeypatch.setattr(importlib.resources, "files", _fake_files)
    assert _skills_resources._checkout_docs_skills() is None  # pyright: ignore[reportPrivateUsage]
