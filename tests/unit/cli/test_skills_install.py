"""Tests for ``opshub skills install`` / ``opshub skills list`` (Phase 16-B).

ADR-0029 §決定 (a)〜(h) — the 15 assistant skills bundled inside the
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

from opshub._skills_resources import ASSISTANT_SKILL_NAMES, load_skill
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


def test_skills_install_only_writes_15_assistant_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**Critical regression guard** — install never clobbers ecosystem skills.

    ADR-0029 §決定 (h) + §不変条件 2 — the 15 assistant skill names
    must be disjoint from the ecosystem-common skill names
    (drive / lint / commit / ship / pr / review / health / implement /
    phase-issue / topics / commit-conventions / lint-rules / test).
    Pre-populating an ecosystem-common skill at the target before
    running install proves that ``opshub skills install`` leaves it
    alone — the install only writes the 15 names listed in
    :data:`ASSISTANT_SKILL_NAMES`.
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

    # Every assistant skill was written.
    for name in ASSISTANT_SKILL_NAMES:
        assert (skills_root / name / "SKILL.md").is_file()
        # And the assistant skill names truly are disjoint from the
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
    # 15 skills x 1 SKILL.md per skill = 15 lines (current bundle has
    # no reference/ subdirs yet).
    assert len(lines) >= len(ASSISTANT_SKILL_NAMES)
    # Every assistant skill name appears in the printed paths.
    for name in ASSISTANT_SKILL_NAMES:
        assert any(f"/{name}/" in line for line in lines), f"missing {name} path"


def test_skills_list_shows_install_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``opshub skills list`` distinguishes installed / missing / modified."""
    home = _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()

    # 1) Pristine — every skill is ``missing``.
    listed = runner.invoke(app, ["skills", "list", "--host", "claude-code"])
    assert listed.exit_code == 0, listed.output
    for name in ASSISTANT_SKILL_NAMES:
        assert f"{name}  missing" in listed.output

    # 2) After install — every skill is ``installed``.
    runner.invoke(app, ["skills", "install", "--host", "claude-code"])
    listed = runner.invoke(app, ["skills", "list", "--host", "claude-code"])
    assert listed.exit_code == 0, listed.output
    for name in ASSISTANT_SKILL_NAMES:
        assert f"{name}  installed" in listed.output

    # 3) Hand-edit one skill — it flips to ``modified``.
    target = home / ".claude" / "skills" / "personal-brief" / "SKILL.md"
    target.write_bytes(b"# HAND EDITED\n")
    listed = runner.invoke(app, ["skills", "list", "--host", "claude-code"])
    assert listed.exit_code == 0, listed.output
    assert "personal-brief  modified" in listed.output
    # Other skills stay installed.
    assert "next-actions  installed" in listed.output


# ---------------------------------------------------------------------------
# Phase 16 audit followup v2 (#395) — additional regression tests.
#
# The autouse :func:`_mirror_assistant_skill_bundle` fixture in
# ``tests/conftest.py`` rebuilds ``src/opshub/_skills/`` from
# ``docs/skills/`` before every pytest session. That keeps editable
# installs / wheel installs / CI matrix runners aligned, but it also
# made the bundle-missing exit-1 branch in
# :func:`opshub.cli.skills.install_command` untested in practice —
# the fixture would always re-create ``_skills/`` before the test had
# a chance to run. The follow-up tests below patch the resource helper
# directly so the branch is exercised regardless of the autouse mirror,
# alongside additional coverage for ``--host`` / ``--scope`` validation,
# structured logging, and the ``--host all --scope project`` combo.
# ---------------------------------------------------------------------------


def test_skills_install_payload_missing_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**Critical regression guard** — wheel without ``_skills/`` exits 1 with reinstall hint.

    The :class:`opshub._skills_resources.SkillResourceError` path in
    :func:`opshub.cli.skills.install_command` (``src/opshub/cli/skills.py``
    lines 303-306) is normally false-green because
    :func:`tests.conftest._mirror_assistant_skill_bundle` re-materialises
    ``src/opshub/_skills/`` before every session. An sdist build (or any
    wheel that loses the ``[tool.hatch.build.force-include]`` mapping
    before Phase 16-B) would expose this branch on operator machines —
    pin it here by monkeypatching :func:`iter_skill_files` so the
    install path hits the error handler regardless of the autouse
    mirror.
    """
    _isolate_home(monkeypatch, tmp_path)

    from opshub import _skills_resources
    from opshub.cli import skills as cli_skills

    sentinel_message = (
        "opshub package is missing the bundled skill payload "
        "(_skills/ directory). Reinstall via "
        "`uv tool install --reinstall ozzylabs-opshub` to pick up "
        "the Phase 16-B build (ADR-0029)."
    )

    def _raise_missing(_skill_name: str) -> object:
        raise _skills_resources.SkillResourceError(sentinel_message)

    # Patch the symbol resolved by the lazy import inside
    # ``install_command`` (``from opshub._skills_resources import ...
    # iter_skill_files``) — patching the source module is enough because
    # the import resolves the name from ``_skills_resources.__dict__``
    # at call time.
    monkeypatch.setattr(cli_skills, "iter_skill_files", _raise_missing, raising=False)
    monkeypatch.setattr(_skills_resources, "iter_skill_files", _raise_missing, raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--host", "claude-code"])

    # Exit code 1 (packaging failure) — distinct from the BadParameter
    # ``exit 2`` path so operators can branch on the cause.
    assert result.exit_code == 1, result.output
    # The actionable reinstall hint surfaces on stderr / stdout (Typer
    # routes ``typer.echo(..., err=True)`` through the CliRunner output
    # buffer). The exact wording is part of the contract because
    # docs/troubleshooting.md §3.9 instructs operators to look for it.
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "reinstall" in combined.lower(), combined
    assert "_skills" in combined or "ozzylabs-opshub" in combined, combined


def test_skills_install_invalid_host_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--host garbage`` exits with a BadParameter (exit code 2)."""
    _isolate_home(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--host", "garbage"])

    # Typer maps ``typer.BadParameter`` to exit code 2 with the usage
    # error message printed on stderr.
    assert result.exit_code == 2, result.output
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "garbage" in combined or "Invalid" in combined or "invalid" in combined, combined


def test_skills_install_invalid_scope_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--scope garbage`` exits with a BadParameter (exit code 2)."""
    _isolate_home(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--scope", "garbage"])

    assert result.exit_code == 2, result.output
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "garbage" in combined or "Invalid" in combined or "invalid" in combined, combined


def test_skills_install_emits_structured_log_category_skill_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``install_command`` emits ``category=skill_install`` with byte counts.

    Pin the ADR-0027 structured-logging contract: every install must
    emit a single ``event=skill_install_complete`` row with
    ``category=skill_install`` and the ``written`` / ``skipped`` /
    ``overwritten`` integer columns so downstream observability
    (`opshub --log-format json`) can be aggregated without re-parsing
    the human summary lines.
    """
    _isolate_home(monkeypatch, tmp_path)

    from opshub.cli import skills as cli_skills

    # Capture every bound logger call by replacing ``get_logger`` with
    # a stub that records the ``bind`` kwargs (category / host / scope /
    # dry_run / skip_existing) and the subsequent ``info(...)`` event
    # name + kwargs. This sidesteps structlog's stderr cache (capsys
    # would be flaky) while still pinning the public contract.
    bound_state: dict[str, object] = {}
    info_calls: list[tuple[str, dict[str, object]]] = []

    class _StubBound:
        def info(self, event: str, **kwargs: object) -> None:
            info_calls.append((event, kwargs))

        def warning(self, event: str, **kwargs: object) -> None:  # pragma: no cover
            info_calls.append((event, kwargs))

        def error(self, event: str, **kwargs: object) -> None:  # pragma: no cover
            info_calls.append((event, kwargs))

    class _StubLogger:
        def bind(self, **kwargs: object) -> _StubBound:
            bound_state.update(kwargs)
            return _StubBound()

    def _stub_get_logger(*_args: object, **_kwargs: object) -> _StubLogger:
        return _StubLogger()

    monkeypatch.setattr("opshub.core.logging.get_logger", _stub_get_logger)
    # ``install_command`` does ``from opshub.core.logging import
    # get_logger`` lazily; patch the symbol on the source module so the
    # lazy resolution picks up the stub.

    cli_skills.install_command(host="claude-code", scope="user", skip_existing=False)

    # bind(...) carried the bookkeeping columns.
    assert bound_state["category"] == "skill_install", bound_state
    assert bound_state["host"] == "claude-code", bound_state
    assert bound_state["scope"] == "user", bound_state
    assert bound_state["dry_run"] is False, bound_state
    assert bound_state["skip_existing"] is False, bound_state

    # Exactly one ``skill_install_complete`` event with numeric counts.
    completes = [(name, kw) for name, kw in info_calls if name == "skill_install_complete"]
    assert len(completes) == 1, info_calls
    _name, kwargs = completes[0]
    assert isinstance(kwargs["written"], int) and kwargs["written"] > 0, kwargs
    assert isinstance(kwargs["skipped"], int), kwargs
    assert isinstance(kwargs["overwritten"], int), kwargs
    # ``distinct_skills`` (15 assistant skills) is emitted alongside
    # for dashboards that want a per-bundle count.
    assert kwargs["distinct_skills"] == len(ASSISTANT_SKILL_NAMES), kwargs


def test_skills_install_host_all_scope_project_writes_both_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--host all --scope project`` writes 15 skills to both CWD roots.

    ADR-0029 §決定 (f) — ``project`` scope rewrites both ``claude-code``
    and ``codex``/``copilot`` host roots into CWD-relative paths. The
    ``--host all`` default expands to two distinct directories
    (``./.claude/skills/`` and ``./.agents/skills/``); pinning the
    combination here guards the in-repo dogfood pattern
    (Phase 16-D, ``uv run opshub skills install --scope project``).
    """
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "myrepo"
    project.mkdir()
    monkeypatch.chdir(project)

    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--host", "all", "--scope", "project"])
    assert result.exit_code == 0, result.output

    claude_root = project / ".claude" / "skills"
    agents_root = project / ".agents" / "skills"
    assert claude_root.is_dir()
    assert agents_root.is_dir()
    for name in ASSISTANT_SKILL_NAMES:
        assert (claude_root / name / "SKILL.md").is_file(), name
        assert (agents_root / name / "SKILL.md").is_file(), name
    # The user-scope roots stay untouched.
    assert not (tmp_path / "home" / ".claude" / "skills").exists()
    assert not (tmp_path / "home" / ".agents" / "skills").exists()


def test_skills_install_host_copilot_uses_agents_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--host copilot`` writes to ``~/.agents/skills/`` only.

    Codex CLI and Copilot CLI both load skills from
    ``~/.agents/skills/`` per handbook ADR-0016, so ``--host copilot``
    must resolve to the same directory as ``--host codex`` (and
    ``--host all`` de-duplicates the two into one root).
    """
    home = _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["skills", "install", "--host", "copilot"])
    assert result.exit_code == 0, result.output

    assert not (home / ".claude" / "skills").exists()
    assert (home / ".agents" / "skills" / "personal-brief" / "SKILL.md").is_file()


def test_skills_install_skip_existing_dry_run_reports_skipped_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--skip-existing --dry-run`` accounts for pre-existing files in skipped.

    Pin the combo to confirm the skipped-counter path runs even when no
    bytes are written: dry-run still walks the install plan, and
    ``--skip-existing`` still routes pre-existing destination paths
    into the ``skipped_paths`` list (visible via the structured log
    ``skipped`` count).
    """
    home = _isolate_home(monkeypatch, tmp_path)
    target_dir = home / ".claude" / "skills" / "personal-brief"
    target_dir.mkdir(parents=True)
    (target_dir / "SKILL.md").write_bytes(b"# pre-existing\n")

    from opshub.cli import skills as cli_skills

    info_calls: list[tuple[str, dict[str, object]]] = []

    class _StubBound:
        def info(self, event: str, **kwargs: object) -> None:
            info_calls.append((event, kwargs))

        def warning(self, event: str, **kwargs: object) -> None:  # pragma: no cover
            info_calls.append((event, kwargs))

        def error(self, event: str, **kwargs: object) -> None:  # pragma: no cover
            info_calls.append((event, kwargs))

    class _StubLogger:
        def bind(self, **_kwargs: object) -> _StubBound:
            return _StubBound()

    def _stub_get_logger(*_args: object, **_kwargs: object) -> _StubLogger:
        return _StubLogger()

    monkeypatch.setattr("opshub.core.logging.get_logger", _stub_get_logger)

    cli_skills.install_command(host="claude-code", scope="user", skip_existing=True, dry_run=True)

    completes = [(n, kw) for n, kw in info_calls if n == "skill_install_complete"]
    assert len(completes) == 1, info_calls
    _name, kwargs = completes[0]
    # The pre-existing personal-brief/SKILL.md was skipped, so ``skipped >= 1``.
    assert isinstance(kwargs["skipped"], int) and kwargs["skipped"] >= 1, kwargs
    # dry-run never overwrites — the overwritten counter stays zero.
    assert kwargs["overwritten"] == 0, kwargs
    # Pre-existing bytes survive even though dry-run was the intent.
    assert (target_dir / "SKILL.md").read_bytes() == b"# pre-existing\n"
