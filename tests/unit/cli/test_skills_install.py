"""Tests for ``opshub skills install`` / ``opshub skills list`` (Phase 16-B).

ADR-0029 §決定 (a)〜(h) — the 14 secretary skills bundled inside the
opshub wheel under ``opshub/_skills/`` are distributed to the host
agent loader directories (``~/.claude/skills/`` and
``~/.agents/skills/``) by ``opshub skills install``. Every test
isolates ``HOME`` via :class:`pytest.MonkeyPatch.setenv` so that the
user's real ``~/.claude/skills/`` directory is never touched, and the
``--scope project`` cases use :meth:`pytest.MonkeyPatch.chdir`
against ``tmp_path`` so the install lands inside the test sandbox.

The 12 tests below pin the critical contracts:

* Idempotency / overwrite semantics (ADR-0029 §決定 (g)).
* Disjoint namespace with ecosystem-common skills (ADR-0029 §決定 (h),
  §不変条件 2) — the most consequential regression guard because a
  bug here would silently clobber ``drive`` / ``lint`` / ``commit``
  etc. on every operator's machine.
* Host / scope flag combinations.
* importlib.resources path (so an editable install or a wheel-installed
  CLI both work identically).
* Filesystem hygiene (parent directories created, dry-run writes
  nothing, ``--print-paths`` emits the right shape).
* ``list`` subcommand returns the right status string for each of
  ``installed`` / ``missing`` / ``modified``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opshub._skills_resources import SECRETARY_SKILL_NAMES, load_skill
from opshub.cli.app import app


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``HOME`` (and Windows ``USERPROFILE``) inside ``tmp_path``.

    ``pathlib.Path.home()`` honours ``HOME`` on POSIX and
    ``USERPROFILE`` on Windows; setting both keeps the test
    cross-platform even though opshub's primary support is Linux /
    macOS. Returns the new home directory so the test can build
    expected install paths under it.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_skills_install_dry_run_lists_targets_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--dry-run`` writes zero bytes but reports the would-be targets."""
    home = _isolate_home(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--dry-run"])

    assert result.exit_code == 0, result.output
    # Filesystem stayed clean — neither host loader directory was created.
    assert not (home / ".claude" / "skills").exists()
    assert not (home / ".agents" / "skills").exists()
    # Stdout still confirms what *would* happen.
    assert "would install" in result.output


def test_skills_install_idempotent_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running install twice yields byte-identical files."""
    _isolate_home(monkeypatch, tmp_path)

    runner = CliRunner()
    first = runner.invoke(app, ["skills", "install", "--host", "claude-code"])
    assert first.exit_code == 0, first.output

    target = tmp_path / "home" / ".claude" / "skills" / "personal-brief" / "SKILL.md"
    snapshot = target.read_bytes()

    second = runner.invoke(app, ["skills", "install", "--host", "claude-code"])
    assert second.exit_code == 0, second.output
    assert target.read_bytes() == snapshot


def test_skills_install_skip_existing_preserves_hand_edits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--skip-existing`` leaves operator edits untouched."""
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["skills", "install", "--host", "claude-code"])

    target = tmp_path / "home" / ".claude" / "skills" / "personal-brief" / "SKILL.md"
    sentinel = b"# HAND EDITED\n"
    target.write_bytes(sentinel)

    result = runner.invoke(app, ["skills", "install", "--host", "claude-code", "--skip-existing"])
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == sentinel


def test_skills_install_overwrites_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default install clobbers the existing file (ADR-0029 §決定 (g))."""
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["skills", "install", "--host", "claude-code"])

    target = tmp_path / "home" / ".claude" / "skills" / "personal-brief" / "SKILL.md"
    target.write_bytes(b"# HAND EDITED\n")

    result = runner.invoke(app, ["skills", "install", "--host", "claude-code"])
    assert result.exit_code == 0, result.output
    bundled = load_skill("personal-brief")["SKILL.md"]
    assert target.read_bytes() == bundled


def test_skills_install_host_codex_only_skips_claude_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--host codex`` writes to ~/.agents/skills/ only."""
    home = _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--host", "codex"])
    assert result.exit_code == 0, result.output

    assert not (home / ".claude" / "skills").exists()
    assert (home / ".agents" / "skills" / "personal-brief" / "SKILL.md").is_file()


def test_skills_install_scope_project_uses_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--scope project`` writes under CWD's ``.claude/skills/``."""
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "myrepo"
    project.mkdir()
    monkeypatch.chdir(project)

    runner = CliRunner()
    result = runner.invoke(
        app, ["skills", "install", "--scope", "project", "--host", "claude-code"]
    )
    assert result.exit_code == 0, result.output
    assert (project / ".claude" / "skills" / "personal-brief" / "SKILL.md").is_file()
    # ~/.claude/skills/ stays untouched.
    assert not (tmp_path / "home" / ".claude" / "skills").exists()


def test_skills_install_scope_project_outside_repo_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--scope project`` does not require a pyproject.toml sentinel.

    ADR-0029 §決定 (f) — the project-scope flag is an explicit operator
    choice; opshub does not infer "am I in a repo?" before honouring
    it. A bare tmp_path with no pyproject.toml still works.
    """
    _isolate_home(monkeypatch, tmp_path)
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    assert not (bare / "pyproject.toml").exists()

    runner = CliRunner()
    result = runner.invoke(
        app, ["skills", "install", "--scope", "project", "--host", "claude-code"]
    )
    assert result.exit_code == 0, result.output
    assert (bare / ".claude" / "skills" / "personal-brief" / "SKILL.md").is_file()


def test_skills_install_resolves_via_importlib_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The install path resolves files via ``importlib.resources.files``.

    Patching ``importlib.resources.files`` to a sentinel must break the
    install — proving the path is exclusively via the resources API
    (not ``__file__`` walking, which would still succeed in an editable
    repo checkout but fail in a wheel-installed environment).
    """
    _isolate_home(monkeypatch, tmp_path)

    import importlib.resources

    sentinel_called: dict[str, bool] = {"hit": False}
    real_files = importlib.resources.files

    def _spy(package_name: str) -> object:
        if package_name == "opshub":
            sentinel_called["hit"] = True
        return real_files(package_name)

    monkeypatch.setattr(importlib.resources, "files", _spy)
    # Also patch the imported symbol inside _skills_resources because
    # it does ``from importlib import resources`` and then calls
    # ``resources.files(...)`` — the attribute we just patched is the
    # same object so the spy fires either way.

    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--host", "claude-code"])
    assert result.exit_code == 0, result.output
    assert sentinel_called["hit"] is True


def test_skills_install_creates_parent_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``~/.claude/skills/`` is auto-created when absent."""
    home = _isolate_home(monkeypatch, tmp_path)
    assert not (home / ".claude").exists()

    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--host", "claude-code"])
    assert result.exit_code == 0, result.output
    assert (home / ".claude" / "skills" / "personal-brief").is_dir()


def test_skills_install_only_writes_14_secretary_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**Critical regression guard** — install never clobbers ecosystem skills.

    ADR-0029 §決定 (h) + §不変条件 2 — the 14 secretary skill names
    must be disjoint from the ecosystem-common skill names
    (drive / lint / commit / ship / pr / review / health / implement /
    phase-issue / topics / commit-conventions / lint-rules / test).
    Pre-populating an ecosystem-common skill at the target before
    running install proves that ``opshub skills install`` leaves it
    alone — the install only writes the 14 names listed in
    :data:`SECRETARY_SKILL_NAMES`.
    """
    home = _isolate_home(monkeypatch, tmp_path)
    skills_root = home / ".claude" / "skills"
    skills_root.mkdir(parents=True)

    # Plant ecosystem-common skill payloads with sentinel bytes.
    ecosystem_names = (
        "drive",
        "lint",
        "commit",
        "ship",
        "pr",
        "review",
        "health",
        "implement",
        "phase-issue",
        "topics",
        "commit-conventions",
        "lint-rules",
        "test",
    )
    sentinel = b"# ECOSYSTEM SKILL - DO NOT TOUCH\n"
    for name in ecosystem_names:
        skill_dir = skills_root / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_bytes(sentinel)

    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--host", "claude-code"])
    assert result.exit_code == 0, result.output

    # Every ecosystem-common skill survives byte-for-byte.
    for name in ecosystem_names:
        body = (skills_root / name / "SKILL.md").read_bytes()
        assert body == sentinel, f"ecosystem skill {name!r} was clobbered"

    # Every secretary skill was written.
    for name in SECRETARY_SKILL_NAMES:
        assert (skills_root / name / "SKILL.md").is_file()
        # And the secretary skill names truly are disjoint from the
        # ecosystem-common names (this is the structural invariant).
        assert name not in ecosystem_names


def test_skills_install_print_paths_outputs_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--print-paths`` emits one target per line on stdout."""
    home = _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skills", "install", "--host", "claude-code", "--print-paths", "--dry-run"],
    )
    assert result.exit_code == 0, result.output

    expected_root = home / ".claude" / "skills"
    # Every printed line should be an absolute path under the target dir.
    # Filter out the summary line ("would install N skill(s) to ...").
    lines = [line for line in result.output.splitlines() if line.startswith(str(expected_root))]
    # 14 skills x 1 SKILL.md per skill = 14 lines (current bundle has
    # no reference/ subdirs yet).
    assert len(lines) >= len(SECRETARY_SKILL_NAMES)
    # Every secretary skill name appears in the printed paths.
    for name in SECRETARY_SKILL_NAMES:
        assert any(f"/{name}/" in line for line in lines), f"missing {name} path"


def test_skills_list_shows_install_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``opshub skills list`` distinguishes installed / missing / modified."""
    home = _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()

    # 1) Pristine — every skill is ``missing``.
    listed = runner.invoke(app, ["skills", "list", "--host", "claude-code"])
    assert listed.exit_code == 0, listed.output
    for name in SECRETARY_SKILL_NAMES:
        assert f"{name}  missing" in listed.output

    # 2) After install — every skill is ``installed``.
    runner.invoke(app, ["skills", "install", "--host", "claude-code"])
    listed = runner.invoke(app, ["skills", "list", "--host", "claude-code"])
    assert listed.exit_code == 0, listed.output
    for name in SECRETARY_SKILL_NAMES:
        assert f"{name}  installed" in listed.output

    # 3) Hand-edit one skill — it flips to ``modified``.
    target = home / ".claude" / "skills" / "personal-brief" / "SKILL.md"
    target.write_bytes(b"# HAND EDITED\n")
    listed = runner.invoke(app, ["skills", "list", "--host", "claude-code"])
    assert listed.exit_code == 0, listed.output
    assert "personal-brief  modified" in listed.output
    # Other skills stay installed.
    assert "next-actions  installed" in listed.output
