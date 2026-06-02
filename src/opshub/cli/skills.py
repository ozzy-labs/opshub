"""``opshub skills ...`` subcommands.

Phase 16-B (ADR-0029) ships ``opshub skills install`` and
``opshub skills list`` to distribute the 14 secretary skills (SSOT
``docs/skills/<name>/SKILL.md``, Phase 12 H1 / ADR-0004 §決定 (c))
bundled inside the opshub wheel under ``opshub/_skills/`` to the host
agent's skill loader directories (``~/.claude/skills/`` for Claude
Code, ``~/.agents/skills/`` for Codex CLI / Copilot CLI, with
``--scope project`` rewriting both to ``./.claude/skills/`` /
``./.agents/skills/`` under the current working directory).

Module-level imports are restricted to ``__future__`` / stdlib /
``typer`` so this file passes
``tests/integration/test_cli_imports.py`` (ADR-0001 lazy-import rule);
the structlog logger and the resource-loading helpers are imported
inside each command callback. The ecosystem-common skill namespace
(drive / lint / commit / ...) is disjoint from this command's payload
(ADR-0029 §決定 (h)) — pinned by
``tests/unit/cli/test_skills_install.py::test_skills_install_only_writes_14_secretary_skills``.

CLI surface:

* ``opshub skills install`` — copy the 14 bundled secretary skills to
  the host skill loader directory. Flags:

  * ``--host {claude-code,codex,copilot,all}`` (default ``all``) —
    install target host(s).
  * ``--scope {user,project}`` (default ``user``) — ``user`` writes to
    ``~/.claude/skills/`` / ``~/.agents/skills/``; ``project`` writes
    to ``./.claude/skills/`` / ``./.agents/skills/`` under the CWD.
  * ``--skip-existing`` (default off) — preserve any pre-existing
    ``SKILL.md`` on the target. Without this flag the install
    overwrites the existing file with the bundled payload
    (ADR-0029 §決定 (g): SSOT sync wins by default; opt out with this
    flag to protect hand edits).
  * ``--dry-run`` — list the targets that would be touched without
    writing any byte. Useful for the operator to preview before
    overwriting hand-edited files.
  * ``--print-paths`` — emit one target path per line on stdout so
    pipelines can post-process (``opshub skills install --dry-run
    --print-paths | xargs ls``).

* ``opshub skills list`` — read-only catalogue with install status
  (``installed`` / ``missing`` / ``modified``) for the 14 skills under
  every requested host / scope combination. Compares byte payloads
  against the bundled SSOT.

Structured logging (ADR-0027) — every install emits a single
``category=skill_install`` log line carrying ``host``, ``scope``,
``count`` (number of skills written / would-be-written), ``skipped``
(existing-file count when ``--skip-existing`` is in effect),
``overwritten`` (count of files clobbered by default behaviour), and
``dry_run`` flag. The MCP ``mcp serve`` subprocess path inherits the
same logger configuration via the root callback's stored
``LogSettings``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["install_command", "skills_app"]


# Top-level Typer group registered by ``opshub.cli.app``. The sub-app is
# constructed at module load (a Typer instance is cheap, no heavy
# imports) so the ADR-0001 cold-start budget is preserved — every heavy
# helper (resources / structlog / pathlib operations) is imported
# inside the command callbacks below.
skills_app = typer.Typer(
    name="skills",
    help="Install or inspect the 14 bundled secretary skills (ADR-0029).",
    no_args_is_help=True,
)


# Sentinel enum-like accept lists. Typer would accept ``Enum`` here, but
# the matching CLI surface in ``opshub.cli.connector`` (``--format``,
# ``--types``) uses plain strings for the same readability trade-off so
# we follow that precedent. ``--host`` and ``--scope`` validation lives
# inside the command body (raising ``typer.BadParameter`` for unknown
# values).
_HOST_CHOICES: tuple[str, ...] = ("claude-code", "codex", "copilot", "all")
_SCOPE_CHOICES: tuple[str, ...] = ("user", "project")


def _resolve_install_dirs(*, host: str, scope: str) -> list[Path]:
    """Return the host loader directories implied by ``--host`` / ``--scope``.

    ``host = all`` expands to both ``claude-code`` and the shared
    ``codex``/``copilot`` directory (Codex CLI and Copilot CLI share
    ``~/.agents/skills/`` per handbook ADR-0016, so we de-duplicate).
    ``scope = project`` rewrites the user-home roots into CWD-relative
    ones (``./.claude/skills/`` / ``./.agents/skills/``); the operator
    is responsible for being in the right repo, the CLI does not
    require a ``pyproject.toml`` sentinel (ADR-0029 §決定 (f) trade-off
    against project scope friction).

    Resolution rules:

    * ``user`` scope uses :func:`pathlib.Path.home` so tests can patch
      ``HOME`` via :class:`pytest.MonkeyPatch.setenv` and the result is
      ``$HOME/.claude/skills/`` on POSIX (no XDG override — host
      loaders themselves do not honour XDG paths today).
    * ``project`` scope uses :func:`pathlib.Path.cwd` so tests can use
      :meth:`pytest.MonkeyPatch.chdir` against ``tmp_path``.
    """
    # Lazy import keeps ``opshub --help`` under the ADR-0001 budget
    # (stdlib only here, but consistent with the policy).
    from pathlib import Path

    if host not in _HOST_CHOICES:
        raise typer.BadParameter(
            f"unknown --host value {host!r}; choose one of {', '.join(_HOST_CHOICES)}"
        )
    if scope not in _SCOPE_CHOICES:
        raise typer.BadParameter(
            f"unknown --scope value {scope!r}; choose one of {', '.join(_SCOPE_CHOICES)}"
        )

    if scope == "user":
        claude_root = Path.home() / ".claude" / "skills"
        agents_root = Path.home() / ".agents" / "skills"
    else:  # project
        cwd = Path.cwd()
        claude_root = cwd / ".claude" / "skills"
        agents_root = cwd / ".agents" / "skills"

    # ``codex`` and ``copilot`` share the same loader directory
    # (`~/.agents/skills/` per handbook ADR-0016), so ``all`` collapses
    # to two install roots, not three. ``dict.fromkeys`` preserves
    # insertion order while de-duplicating — important because the
    # structured log emits the directory list verbatim.
    if host == "claude-code":
        targets = [claude_root]
    elif host in ("codex", "copilot"):
        targets = [agents_root]
    else:  # all
        targets = list(dict.fromkeys([claude_root, agents_root]))

    return targets


def _format_target_count_line(*, action: str, target_dir: Path, count: int) -> str:
    """Render a one-line summary for stdout, e.g. ``would install 14 skill(s) to ...``.

    Pulled out so install / list / dry-run share identical phrasing.
    The plural ``skill(s)`` form avoids a per-call branch and reads
    fine in both the ``count == 1`` and ``count == 14`` cases (no
    English plural specifically needed for a Japanese-leaning audience
    either — the parenthesised ``(s)`` is the standard CLI idiom).
    """
    return f"{action} {count} skill(s) to {target_dir}"


@skills_app.command("install")
def install(
    host: str = typer.Option(
        "all",
        "--host",
        help=(
            "Target host: claude-code, codex, copilot, or all "
            "(default: all). Codex / Copilot share ~/.agents/skills/ "
            "so `all` resolves to 2 directories."
        ),
    ),
    scope: str = typer.Option(
        "user",
        "--scope",
        help=(
            "Install scope: user (default; ~/.claude/skills/ + "
            "~/.agents/skills/) or project (./.claude/skills/ + "
            "./.agents/skills/ under CWD)."
        ),
    ),
    skip_existing: bool = typer.Option(
        False,
        "--skip-existing",
        help=(
            "Preserve any pre-existing SKILL.md on the target. "
            "Default behaviour overwrites (SSOT sync wins, ADR-0029 §決定 (g))."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List targets without writing any byte.",
    ),
    print_paths: bool = typer.Option(
        False,
        "--print-paths",
        help="Emit one target path per line on stdout (pipeline-friendly).",
    ),
) -> None:
    """Install the 14 bundled secretary skills to the host loader directory.

    Thin Typer wrapper around :func:`install_command` — see that
    function's docstring for the full semantics. The wrapper exists so
    that internal callers (notably :func:`opshub.cli.init.init_command`
    for the Phase 16-C ``opshub init`` integration, #384) can invoke
    the install logic with plain Python kwargs without going through
    Typer / Click parsing.
    """
    install_command(
        host=host,
        scope=scope,
        skip_existing=skip_existing,
        dry_run=dry_run,
        print_paths=print_paths,
    )


def install_command(
    *,
    host: str = "all",
    scope: str = "user",
    skip_existing: bool = False,
    dry_run: bool = False,
    print_paths: bool = False,
) -> None:
    """Install the 14 bundled secretary skills to the host loader directory.

    Reads bundled bytes from ``importlib.resources.files('opshub') /
    _skills/<name>/...`` (populated at build time by
    ``[tool.hatch.build.force-include]`` from ``docs/skills/``,
    ADR-0029 §決定 (a)) and writes them under
    ``~/.claude/skills/`` / ``~/.agents/skills/`` (user scope) or
    ``./.claude/skills/`` / ``./.agents/skills/`` (project scope).

    The ecosystem-common skill names (drive / lint / commit / ...) are
    intentionally NOT touched — only the 14 secretary names listed in
    :data:`opshub._skills_resources.SECRETARY_SKILL_NAMES` are written
    (ADR-0029 §決定 (h) scope carve-out). A regression that adds any
    other name to that tuple would silently start clobbering
    ecosystem-common skills, so the disjoint invariant is pinned by
    ``test_skills_install_only_writes_14_secretary_skills``.

    Phase 16-C (#384) added :func:`opshub.cli.init.init_command` as an
    internal caller so the documented ``uv tool install ozzylabs-opshub[mcp]
    && opshub init`` 2-step setup also installs the 14 secretary
    skills. The extraction from the Typer wrapper (:func:`install`)
    follows the same pattern as
    :func:`opshub.cli.init.init_command` / :func:`opshub.cli.db.migrate_command`:
    the Typer-decorated callable owns the flag surface, the
    plain ``*_command`` function owns the business logic and is safe to
    call programmatically.

    Exit codes (when invoked from the Typer wrapper):

    * ``0`` — every applicable skill was written (or, for ``--dry-run``,
      every target path was printed).
    * ``1`` — packaging failure (bundled ``_skills/`` directory missing
      from the wheel; reinstall hint surfaced). Logged with
      ``category=skill_install_failed`` for downstream tooling. A
      :class:`typer.Exit` is raised; in-process callers should catch
      this if they want to handle the failure differently from a hard
      process exit.
    """
    # Lazy imports — keep cold start fast (ADR-0001) and satisfy
    # ``test_cli_imports`` (no ``opshub.core`` at module level).
    from opshub._skills_resources import (
        SECRETARY_SKILL_NAMES,
        SkillResourceError,
        iter_skill_files,
    )
    from opshub.core.logging import get_logger

    logger = get_logger().bind(
        category="skill_install",
        host=host,
        scope=scope,
        dry_run=dry_run,
        skip_existing=skip_existing,
    )

    try:
        install_dirs = _resolve_install_dirs(host=host, scope=scope)
    except typer.BadParameter as exc:
        # Typer maps BadParameter to exit 2 with the usage error
        # message; re-raise so the contract matches ``opshub connector
        # *`` commands. Log the rejection so structured-log consumers
        # see the failed invocation.
        logger.warning("skill_install_bad_parameter", error=str(exc))
        raise

    # Collect (skill_name, relative_path, bytes) lazily but materialise
    # per-skill so a SkillResourceError surfaces before we touch the
    # filesystem (a wheel missing _skills/ should not leave half-written
    # state behind).
    try:
        skills_payload: list[tuple[str, list[tuple[str, bytes]]]] = [
            (
                name,
                [(entry.relative_path, entry.data) for entry in iter_skill_files(name)],
            )
            for name in SECRETARY_SKILL_NAMES
        ]
    except SkillResourceError as exc:
        logger.error("skill_install_payload_missing", error=str(exc))
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    written_paths: list[Path] = []
    skipped_paths: list[Path] = []
    overwritten_paths: list[Path] = []

    for target_dir in install_dirs:
        for skill_name, file_entries in skills_payload:
            skill_dir = target_dir / skill_name
            for relative_path, data in file_entries:
                # ``relative_path`` always uses POSIX separators (see
                # ``_skills_resources``); split on ``/`` and join via
                # the platform-native ``Path`` so Windows lands in the
                # right place even though our primary support is POSIX.
                dest = skill_dir
                for segment in relative_path.split("/"):
                    dest = dest / segment

                if dest.exists() and skip_existing:
                    skipped_paths.append(dest)
                    continue

                if dest.exists():
                    overwritten_paths.append(dest)

                if not dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)

                written_paths.append(dest)

    # ``--print-paths`` emits one path per line on stdout regardless of
    # dry-run / normal mode so pipelines can branch off the same column.
    if print_paths:
        for path in written_paths:
            typer.echo(str(path))

    # One-line human summary per target dir, then a structured log row.
    action = "would install" if dry_run else "installed"
    distinct_skills = len({path.parent for path in written_paths})
    for target_dir in install_dirs:
        # Filter for paths under this target (so the "14 skill(s) to ..."
        # phrasing stays accurate per directory in the ``--host all``
        # case where the same payload is written to two roots).
        count = len({path.parent for path in written_paths if _is_under(path, target_dir)})
        typer.echo(_format_target_count_line(action=action, target_dir=target_dir, count=count))

    logger.info(
        "skill_install_complete",
        written=len(written_paths),
        skipped=len(skipped_paths),
        overwritten=len(overwritten_paths),
        distinct_skills=distinct_skills,
        install_dirs=[str(p) for p in install_dirs],
    )


@skills_app.command("list")
def list_skills(
    host: str = typer.Option(
        "all",
        "--host",
        help="Target host: claude-code, codex, copilot, or all (default: all).",
    ),
    scope: str = typer.Option(
        "user",
        "--scope",
        help="Scope: user (default) or project.",
    ),
) -> None:
    """Show the 14 secretary skills with install status per host directory.

    Each row prints the host directory + skill name + status:

    * ``installed`` — file exists and bytes match the bundled SSOT.
    * ``missing`` — file does not exist (operator never ran
      ``opshub skills install`` for this host / scope, or a previous
      install was rolled back).
    * ``modified`` — file exists but bytes differ from the bundle
      (operator hand-edited the host copy; ``opshub skills install``
      without ``--skip-existing`` would overwrite it).

    The comparison is byte-exact rather than semver-aware — SSOT sync
    is the contract (ADR-0029 §決定 (g)) and any mismatch is
    actionable as either ``install`` (clobber the hand edit) or
    ``--skip-existing`` (preserve it).
    """
    # Lazy imports preserve the cold-start budget.
    from opshub._skills_resources import SECRETARY_SKILL_NAMES, load_skill

    install_dirs = _resolve_install_dirs(host=host, scope=scope)

    # Header row keeps the output greppable: ``<dir> <skill> <status>``.
    for target_dir in install_dirs:
        for skill_name in SECRETARY_SKILL_NAMES:
            expected = load_skill(skill_name)
            skill_dir = target_dir / skill_name
            status = _compute_skill_status(skill_dir=skill_dir, expected=expected)
            typer.echo(f"{target_dir}  {skill_name}  {status}")


def _compute_skill_status(*, skill_dir: Path, expected: dict[str, bytes]) -> str:
    """Return ``"installed"`` / ``"missing"`` / ``"modified"`` for one skill.

    A skill counts as ``installed`` iff every bundled file exists on
    disk AND every byte matches. Any missing file makes it ``missing``
    (even if some files exist, because the host loader expects the full
    skill bundle); any byte mismatch in any file makes it ``modified``.
    The ``missing > modified > installed`` precedence keeps the status
    decision deterministic when both conditions apply (a partially
    written bundle counts as missing).
    """
    any_modified = False
    for relative_path, data in expected.items():
        dest = skill_dir
        for segment in relative_path.split("/"):
            dest = dest / segment
        if not dest.exists():
            return "missing"
        if dest.read_bytes() != data:
            any_modified = True
    return "modified" if any_modified else "installed"


def _is_under(path: Path, parent: Path) -> bool:
    """Return True iff ``path`` is inside ``parent`` (or equal to it).

    Used only by the install summary to attribute each written path
    back to its install-dir root for the per-target count line. We
    avoid :meth:`Path.is_relative_to` (Python 3.9+) for symmetry with
    older code paths that walked manually; ``Path.resolve()`` would be
    overkill here because we built the paths ourselves from
    ``install_dirs`` and never crossed a symlink.
    """
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
