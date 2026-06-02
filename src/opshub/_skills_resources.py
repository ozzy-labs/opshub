"""Read-only access to the bundled secretary skill payload.

Phase 16-B (ADR-0029 §決定 (a)) — the 14 secretary skills (`personal-brief`,
`next-actions`, `pr-review`, `find-document`, `meeting-prep`, `research`,
`external-brief`, `decision-rationale`, `handoff-draft`,
`announcement-draft`, `reply-draft`, `inbox-triage`, `source-extract`,
`meeting-followup`) are bundled inside the opshub wheel under
``opshub/_skills/<name>/...`` via ``[tool.hatch.build.force-include]``
(copy of the ``docs/skills/`` SSOT, Phase 12 H1 / ADR-0004 §決定 (c)).
This module exposes a small helper API on top of :mod:`importlib.resources`
so the ``opshub skills install`` / ``opshub skills list`` CLI subcommands
in :mod:`opshub.cli.skills` can iterate the payload without touching the
filesystem layout directly.

Resolution strategy:

* ``importlib.resources.files('opshub')`` is the **only** lookup path
  used here. The wheel layout (``site-packages/opshub/_skills/...``) is
  identical to the source-tree layout once Phase 16-B's
  ``force-include`` mapping kicks in at build time, so the same code
  path works for ``uv tool install ozzylabs-opshub`` (wheel-installed
  resources) and for editable installs / repo checkouts. We deliberately
  avoid ``__file__`` walking because editable installs that use
  importlib-style resource finders (PEP 660) would break that pattern.

* Every helper returns a snapshot of the bundle (sorted skill names,
  byte payloads keyed by relative path). The bundle is **read-only**;
  callers that need to mutate the host filesystem layout do so by
  writing the returned bytes through ``Path.write_bytes`` (see
  :func:`opshub.cli.skills.install`).

The 14 secretary skill names are the authoritative catalogue. Tests
(`tests/unit/test_package_resources.py`) pin that every name resolves
to an `SKILL.md` file inside the bundle. The ecosystem-common skill
names (drive / lint / commit / ...) are intentionally NOT in this list
— they ship via the ``@ozzylabs/skills`` Renovate preset path
(ADR-0029 §決定 (h) scope carve-out) and `opshub skills install` MUST
NOT touch them. The disjoint-namespace invariant is pinned by
`tests/unit/cli/test_skills_install.py::test_skills_install_only_writes_14_secretary_skills`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable

__all__ = [
    "SECRETARY_SKILL_NAMES",
    "SkillBundleEntry",
    "SkillResourceError",
    "iter_skill_files",
    "iter_skills",
    "load_skill",
]


# Canonical list of the 14 secretary skill identifiers. The order is
# stable (matches `docs/secretary-agent.md` §1's mapping table) so that
# ``opshub skills list`` output is deterministic across runs and across
# operating systems (`os.scandir` order is filesystem-dependent and
# would otherwise leak through).
#
# Adding a new secretary skill requires updating:
#   1. This tuple (callers iterate it directly).
#   2. ``docs/skills/<name>/SKILL.md`` (SSOT — ADR-0004 §決定 (c)).
#   3. ``tests/unit/test_package_resources.py``
#      (`test_package_ships_skill_files` iterates this tuple).
#   4. ``docs/secretary-agent.md`` §1 (catalog) and CLAUDE.md / AGENTS.md.
#
# Removal is symmetric. The ecosystem-common skill namespace
# (drive / lint / commit / ship / pr / review / health / implement /
# phase-issue / topics / commit-conventions / lint-rules / test) MUST
# stay disjoint from this set (ADR-0029 §決定 (h) + §不変条件 2).
SECRETARY_SKILL_NAMES: tuple[str, ...] = (
    "personal-brief",
    "next-actions",
    "pr-review",
    "find-document",
    "meeting-prep",
    "research",
    "external-brief",
    "decision-rationale",
    "handoff-draft",
    "announcement-draft",
    "reply-draft",
    "inbox-triage",
    "source-extract",
    "meeting-followup",
)


class SkillResourceError(RuntimeError):
    """Raised when the bundled skill payload cannot be resolved.

    Two failure modes surface this:

    1. The ``opshub`` package was installed without the
       ``[tool.hatch.build.force-include]`` mapping ever running
       (e.g. an extremely old wheel from before Phase 16-B). The
       ``_skills/`` directory is then absent from the wheel; we raise
       so the CLI can render an actionable upgrade hint rather than
       silently install zero skills.
    2. The bundled payload exists but is missing one of the 14
       expected skill names (caller asked for a skill that was added
       to :data:`SECRETARY_SKILL_NAMES` but not yet authored under
       ``docs/skills/``). This is a packaging bug, not an operator
       error, but we surface it the same way for a uniform UX.
    """


@dataclass(frozen=True, slots=True)
class SkillBundleEntry:
    """One file inside the bundled skill payload.

    Attributes
    ----------
    skill_name:
        The directory name under ``_skills/`` (e.g. ``"personal-brief"``).
    relative_path:
        Path relative to the skill directory using POSIX separators
        (e.g. ``"SKILL.md"`` or ``"reference/example.md"``). Always
        forward-slash so the install path on Windows / WSL is the same
        as on macOS / Linux. ``opshub skills install`` joins this onto
        the host loader directory with :class:`pathlib.PurePosixPath`
        for stability.
    data:
        Raw file bytes. The bundle ships text-only payload
        (`.md` / `.toml`) today; bytes is the safest shared type if a
        future skill adds a small binary asset under ``reference/``.
    """

    skill_name: str
    relative_path: str
    data: bytes


def _skills_root() -> Traversable:
    """Return the ``importlib.resources`` handle for the bundled payload.

    Raises :class:`SkillResourceError` when the bundle is missing —
    e.g. when running against a wheel built before Phase 16-B added the
    ``force-include`` mapping. The CLI catches this and renders an
    upgrade hint instead of crashing with an opaque ``FileNotFoundError``.
    """
    package_root = resources.files("opshub")
    bundle = package_root.joinpath("_skills")
    if not bundle.is_dir():
        raise SkillResourceError(
            "opshub package is missing the bundled skill payload "
            "(_skills/ directory). Reinstall via "
            "`uv tool install --reinstall ozzylabs-opshub` to pick up "
            "the Phase 16-B build (ADR-0029)."
        )
    return bundle


def _iter_files_recursive(
    node: Traversable,
    *,
    relative_to: str,
) -> Iterator[tuple[str, Traversable]]:
    """Depth-first walk yielding ``(rel_path, leaf)`` for every file.

    ``relative_to`` is the POSIX-style directory prefix to strip from
    each leaf name. ``importlib.Traversable`` does not
    expose a built-in recursive walker (only ``iterdir`` + ``is_dir``),
    so we recurse manually. The yield order is sorted by name at every
    level for deterministic install output.
    """
    children = sorted(node.iterdir(), key=lambda child: child.name)
    for child in children:
        child_rel = f"{relative_to}/{child.name}" if relative_to else child.name
        if child.is_dir():
            yield from _iter_files_recursive(child, relative_to=child_rel)
        elif child.is_file():
            yield (child_rel, child)


def iter_skill_files(skill_name: str) -> Iterator[SkillBundleEntry]:
    """Yield every bundled file for ``skill_name``.

    Validates that ``skill_name`` is in :data:`SECRETARY_SKILL_NAMES`
    before touching resources so a typo surfaces as a clear error rather
    than an empty iteration result. Each yielded entry carries the file
    bytes preloaded — small SKILL.md / reference Markdown payloads make
    this cheap and avoids juggling Traversable lifetimes at the caller.
    """
    if skill_name not in SECRETARY_SKILL_NAMES:
        raise SkillResourceError(
            f"{skill_name!r} is not one of the 14 secretary skills "
            f"({', '.join(SECRETARY_SKILL_NAMES)})"
        )

    bundle = _skills_root()
    skill_root = bundle.joinpath(skill_name)
    if not skill_root.is_dir():
        raise SkillResourceError(
            f"bundled skill {skill_name!r} is not present in the "
            "opshub wheel. This indicates a packaging bug — check the "
            "force-include mapping in pyproject.toml."
        )

    for rel, leaf in _iter_files_recursive(skill_root, relative_to=""):
        yield SkillBundleEntry(
            skill_name=skill_name,
            relative_path=rel,
            data=leaf.read_bytes(),
        )


def iter_skills() -> Iterator[tuple[str, list[SkillBundleEntry]]]:
    """Yield ``(skill_name, entries)`` for the 14 secretary skills.

    Convenience wrapper: install / list both want to iterate every
    skill, and this materialises the per-skill file list eagerly so
    the caller can compute counts (`len(entries)`) without a second
    pass. Skill names are yielded in :data:`SECRETARY_SKILL_NAMES`
    order for deterministic UX.
    """
    for name in SECRETARY_SKILL_NAMES:
        yield name, list(iter_skill_files(name))


def load_skill(skill_name: str) -> dict[str, bytes]:
    """Return ``{relative_path: data}`` for every bundled file of ``skill_name``.

    Used by ``opshub skills list`` to compute the byte-equality check
    against an already-installed host copy (a hand edit shows up as a
    ``modified`` status). The dict form is convenient for ``in`` /
    ``__getitem__`` lookups by relative path.
    """
    return {entry.relative_path: entry.data for entry in iter_skill_files(skill_name)}
