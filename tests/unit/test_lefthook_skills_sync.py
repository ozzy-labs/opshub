"""Drift detection contract for the ``skills-sync-check`` lefthook hook.

Phase 16 audit followup v2 (#395) — ``lefthook.yaml`` ships a
``skills-sync-check`` pre-commit hook (lines 44-64) that fires whenever
``docs/skills/**`` / ``.claude/skills/**`` / ``.agents/skills/**`` is
touched and ``diff -rq`` the SSOT (``docs/skills/<name>/``) against
both mirror roots. Phase 16-D landed the hook but left it without an
automated regression test — a future edit that breaks the shell glue
would only surface during a real ``git commit``.

This test reconstructs the hook's shell payload verbatim and runs it
against a *temporary mirror* of the repo so the assertion is hermetic
(no committed sentinel drift, no need for ``lefthook`` to be installed
on the CI runner). The shell script is intentionally copy-pasted from
``lefthook.yaml`` rather than abstracted into a helper because the
hook itself is the SSOT — abstracting would invite the helper and the
hook to drift, defeating the test's purpose.

If this test fails, inspect ``lefthook.yaml:44-64`` and the temporary
fixture below for shape changes. The 15 assistant skill names listed
in :data:`opshub._skills_resources.ASSISTANT_SKILL_NAMES` and the
shell ``$assistant_skills`` variable in the hook must stay in sync —
the test pins that invariant indirectly (a fresh skill name added to
the bundle but not to the hook would surface as a missing drift
report here too).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# The 15 assistant skill names — hard-coded from
# ``opshub._skills_resources.ASSISTANT_SKILL_NAMES`` to keep this test
# free of an import dependency on the source module (the hook itself
# does not import opshub Python, by design).
_ASSISTANT_SKILL_NAMES: tuple[str, ...] = (
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
    "catchup",
)

# Verbatim copy of the ``skills-sync-check`` hook body in
# ``lefthook.yaml:49-64``. Update both at the same time. The long
# single-quoted ``assistant_skills`` line and the two trailing ``echo``
# lines exceed Ruff's E501 budget but are intentionally verbatim against
# the YAML shell payload — splitting them would change the byte-level
# equality the hook contract relies on. The lines are tagged ``noqa:
# E501`` so the test stays a true SSOT mirror of ``lefthook.yaml``.
_HOOK_SHELL = """\
assistant_skills='personal-brief next-actions pr-review find-document meeting-prep research external-brief decision-rationale handoff-draft announcement-draft reply-draft inbox-triage source-extract meeting-followup catchup'
drift=0
for name in $assistant_skills; do
  for mirror in .claude/skills .agents/skills; do
    if ! diff -rq "docs/skills/$name" "$mirror/$name" >/dev/null 2>&1; then
      echo "  drift: $mirror/$name vs docs/skills/$name"
      drift=1
    fi
  done
done
if [ $drift -ne 0 ]; then
  echo 'lefthook[skills-sync-check] drift detected between docs/skills/ (SSOT) and .claude/skills/ + .agents/skills/ mirrors.'
  echo 'Run `uv run opshub skills install --scope project` and stage the result before committing (Phase 16-D, ADR-0029).'
  exit 1
fi
"""  # noqa: E501


def _seed_pristine_mirror(workdir: Path, repo_root: Path) -> None:
    """Copy ``docs/skills/<15 names>/`` into both ``.claude/skills/`` and ``.agents/skills/``.

    Mirrors the post-`opshub skills install --scope project` state so
    the hook's ``diff -rq`` returns zero for every (skill, mirror)
    pair. We deliberately copy from the source-tree SSOT (not from the
    autouse ``src/opshub/_skills/`` bundle) because the hook itself
    compares against ``docs/skills/`` — staying byte-equal to that SSOT
    is the contract under test.
    """
    docs_skills = repo_root / "docs" / "skills"
    for mirror in (".claude/skills", ".agents/skills"):
        mirror_root = workdir / mirror
        mirror_root.mkdir(parents=True, exist_ok=True)
        for name in _ASSISTANT_SKILL_NAMES:
            src = docs_skills / name
            dst = mirror_root / name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
    # Mirror the docs/skills/ SSOT into the workdir too so ``diff -rq``
    # has both sides to compare against.
    workdir_docs = workdir / "docs" / "skills"
    if workdir_docs.exists():
        shutil.rmtree(workdir_docs)
    shutil.copytree(docs_skills, workdir_docs)


def _run_hook(workdir: Path) -> subprocess.CompletedProcess[str]:
    """Run the verbatim hook shell payload with ``workdir`` as cwd."""
    return subprocess.run(
        ["bash", "-c", _HOOK_SHELL],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )


def _repo_root() -> Path:
    """Return the repo root (parent of ``tests/``)."""
    return Path(__file__).resolve().parents[2]


def test_lefthook_skills_sync_check_passes_when_mirrors_match(tmp_path: Path) -> None:
    """When ``.claude/skills/`` and ``.agents/skills/`` byte-equal the SSOT, the hook exits 0."""
    _seed_pristine_mirror(tmp_path, _repo_root())
    result = _run_hook(tmp_path)
    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "drift" not in result.stdout.lower()


def test_lefthook_skills_sync_check_detects_drift(tmp_path: Path) -> None:
    """A hand edit in ``.claude/skills/personal-brief/SKILL.md`` triggers the drift exit-1 path.

    Reproduces what would happen if an operator edits the dogfood
    mirror without re-running ``opshub skills install --scope project``
    (or edits the SSOT and forgets to commit the mirror update). The
    hook surfaces the offending path on stdout and the actionable
    fix-up command in the trailing message.
    """
    _seed_pristine_mirror(tmp_path, _repo_root())

    # Sentinel edit — flip personal-brief's SKILL.md in the Claude
    # Code mirror only. The hook should detect drift on both mirrors
    # iterations for this skill (the .agents/skills/personal-brief one
    # would still match SSOT, so only the .claude/skills line drifts).
    target = tmp_path / ".claude" / "skills" / "personal-brief" / "SKILL.md"
    target.write_bytes(b"# HAND EDITED\n")

    result = _run_hook(tmp_path)
    assert result.returncode != 0, (
        f"hook should have exited non-zero on drift; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # The hook lists the offending mirror path and surfaces the
    # actionable repair command.
    assert ".claude/skills/personal-brief" in result.stdout, result.stdout
    assert "opshub skills install" in result.stdout, result.stdout


def test_lefthook_skills_sync_check_detects_missing_mirror_dir(
    tmp_path: Path,
) -> None:
    """A wholesale-missing skill dir in a mirror also surfaces as drift.

    ``diff -rq dirA dirB`` exits non-zero when ``dirB`` does not exist,
    so the hook treats "operator deleted .agents/skills/research/"
    identically to a byte-level mismatch. Pinning this branch keeps
    the drift detector honest if ``opshub skills install`` ever races
    against a stale checkout.
    """
    _seed_pristine_mirror(tmp_path, _repo_root())
    shutil.rmtree(tmp_path / ".agents" / "skills" / "research")

    result = _run_hook(tmp_path)
    assert result.returncode != 0, result.stdout
    assert ".agents/skills/research" in result.stdout, result.stdout


@pytest.mark.parametrize("skill_name", _ASSISTANT_SKILL_NAMES)
def test_lefthook_skills_sync_check_covers_every_assistant_skill(
    tmp_path: Path, skill_name: str
) -> None:
    """The hook's ``$assistant_skills`` list covers all 15 names.

    Iterates every skill name in :data:`_ASSISTANT_SKILL_NAMES`, drifts
    one mirror copy at a time, and confirms the hook flags it. If a
    future PR adds a 15th assistant skill but forgets to update
    ``lefthook.yaml:50``, this parametrised test would flag the missing
    entry (the new skill's drift would not be detected).
    """
    _seed_pristine_mirror(tmp_path, _repo_root())

    target = tmp_path / ".claude" / "skills" / skill_name / "SKILL.md"
    target.write_bytes(b"# DRIFT SENTINEL\n")

    result = _run_hook(tmp_path)
    assert result.returncode != 0, (
        f"hook missed drift for {skill_name!r}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert f".claude/skills/{skill_name}" in result.stdout, result.stdout
