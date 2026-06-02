"""Pin that the opshub wheel ships the 14 bundled secretary skills.

ADR-0029 §不変条件 — the ``[tool.hatch.build.force-include]`` mapping
in ``pyproject.toml`` copies ``docs/skills/`` to
``src/opshub/_skills/`` at build time so ``importlib.resources``
lookups succeed for wheel-installed users. This test asserts that
every name in :data:`SECRETARY_SKILL_NAMES` resolves to a valid
``SKILL.md`` file inside the bundle — both in the source tree (Phase
16-B onwards, ``docs/skills/`` and ``src/opshub/_skills/`` will look
the same once ``uv build`` has been run, but the resources API works
against either layout) and inside a wheel-installed environment.

The test runs in two passes:

1. ``test_package_ships_skill_files`` — iterate every name and
   confirm ``opshub/_skills/<name>/SKILL.md`` is reachable via
   :mod:`importlib.resources` and contains the SSOT-shaped frontmatter
   (``---`` opener + ``name:`` + ``description:``). The check uses
   ``importlib.resources.files('opshub')`` so it implicitly verifies
   the wheel build glued the payload at the right import path.

A failure here means either the build did not run yet (developers
need to ``uv build`` once before running the test in a clean
checkout, OR the source tree already mirrors the bundle path because
Phase 16-B's force-include happens during ``uv build``). In a CI
green path the test always runs against the freshly-built wheel via
``uv sync`` so the bundled payload is in place.
"""

from __future__ import annotations

from importlib import resources
from typing import cast

import pytest
import yaml

from opshub._skills_resources import SECRETARY_SKILL_NAMES


@pytest.mark.parametrize("skill_name", SECRETARY_SKILL_NAMES)
def test_package_ships_skill_files(skill_name: str) -> None:
    """Every secretary skill resolves to a valid ``SKILL.md`` in the wheel."""
    package_root = resources.files("opshub")
    skill_md = package_root.joinpath("_skills", skill_name, "SKILL.md")
    assert skill_md.is_file(), (
        f"opshub/_skills/{skill_name}/SKILL.md not found in the package "
        "— check [tool.hatch.build.force-include] in pyproject.toml "
        "(ADR-0029 §決定 (a)) and run `uv build` if the source tree "
        "is missing src/opshub/_skills/ (developers in editable mode "
        "need a one-time build to materialise the bundle)."
    )
    text = skill_md.read_text(encoding="utf-8")
    # SSOT shape: SKILL.md starts with a YAML frontmatter block carrying
    # name + description (these are what the host loader reads to
    # surface the skill in the menu / trigger description).
    assert text.startswith("---\n"), f"{skill_name}/SKILL.md missing frontmatter opener"
    frontmatter = text.split("---\n", 2)[1]
    assert "name:" in frontmatter, f"{skill_name}/SKILL.md missing name: field"
    assert "description:" in frontmatter, f"{skill_name}/SKILL.md missing description: field"

    # The host loaders (Claude Code / codex CLI / Copilot CLI) parse the
    # frontmatter with a strict YAML 1.2 parser. A plain scalar that
    # contains the forbidden ``": "`` (colon-space) sequence — easy to
    # introduce when the description mentions ``pair: X`` or other
    # ``label: value`` phrases — passes the substring checks above but
    # makes codex CLI bail with ``invalid YAML``. Round-tripping through
    # :func:`yaml.safe_load` here pins the same constraint the strict
    # hosts apply, so a regression surfaces in CI instead of on a
    # downstream user's machine.
    try:
        parsed = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:  # pragma: no cover - regression guard
        pytest.fail(
            f"{skill_name}/SKILL.md frontmatter is not valid YAML: {exc}. "
            "If the description contains ``label: value`` phrases (e.g. "
            "``pair: external-brief``), wrap the whole value in single "
            "quotes to satisfy YAML 1.2 plain-scalar rules."
        )
    assert isinstance(parsed, dict), (
        f"{skill_name}/SKILL.md frontmatter must parse to a YAML mapping, "
        f"got {type(parsed).__name__}"
    )
    fields = cast(dict[str, object], parsed)
    assert isinstance(fields.get("name"), str), f"{skill_name}/SKILL.md ``name`` must be a string"
    assert isinstance(fields.get("description"), str), (
        f"{skill_name}/SKILL.md ``description`` must be a string"
    )
