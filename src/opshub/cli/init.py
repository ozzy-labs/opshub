"""``opshub init`` — first-time setup.

Creates the XDG directories OpsHub uses, writes a starter TOML config file
if one is not already present, and applies every pending Alembic migration
so the SQLite database is ready to use.

The function is intentionally idempotent: running ``opshub init`` twice on
the same machine is a no-op apart from re-running ``alembic upgrade head``
(which itself is a no-op when already at ``head``). The ``--force`` flag
overwrites an existing ``config.toml`` with the starter template — useful
when a user wants to reset to defaults.

Phase 16-C ([#384](https://github.com/ozzy-labs/opshub/issues/384),
ADR-0029) wires ``opshub init`` to also install the 14 bundled assistant
skills via :func:`opshub.cli.skills.install_command`, so the documented
2-step setup (``uv tool install ozzylabs-opshub[mcp]`` → ``opshub init``)
leaves a fresh host with both the MCP store and the skill loader
populated. The behaviour respects three layers of intent:

* ``--install-skills`` / ``--no-install-skills`` (explicit opt in / out).
* TTY prompt via :class:`rich.prompt.Confirm` when stdin is a terminal
  and no flag was passed (default = yes).
* **Non-interactive default = install** — the motivating user flow is
  ``uv tool install`` from a script / CI / fresh shell where stdin is
  not a TTY, so falling through to install (rather than skip) keeps the
  one-liner experience intact (ADR-0029 §決定 (d)).

Module-level imports are limited to ``__future__`` and stdlib types
(ADR-0001 lazy-import rule); ``OpsHubSettings``, the migration runner,
the skills install command, and :class:`rich.prompt.Confirm` are
imported inside :func:`init_command`.
"""

from __future__ import annotations

__all__ = ["STARTER_CONFIG_TOML", "init_command"]


# Kept as a module-level constant so tests can assert exact bytes after a
# ``--force`` overwrite. The leading comment documents how env-var overrides
# map to TOML sections (``OPSHUB_STORAGE__DB_PATH`` -> ``[storage].db_path``).
STARTER_CONFIG_TOML = """\
# OpsHub configuration. Override per-section via OPSHUB_<SECTION>__<FIELD> env vars.
[storage]
# db_path = "/custom/path/opshub.sqlite"

[workspace]
# root = "/custom/workspace"

[embedding]
backend = "disabled"
"""


def init_command(*, force: bool = False, install_skills: bool | None = None) -> int:
    """Create XDG dirs, write a starter config, run migrations, and install skills.

    Returns ``0`` on success; ``OpsHubError`` subclasses propagate to Typer.

    Parameters
    ----------
    force:
        When ``True``, overwrite an existing ``config.toml`` with the starter
        template. The default ``False`` keeps any user edits intact.
    install_skills:
        Tri-state assistant skill install decision (Phase 16-C, #384):

        * ``True`` — install unconditionally (no prompt, no TTY probe).
        * ``False`` — skip unconditionally (no prompt, no TTY probe).
        * ``None`` (default, flag not supplied) — decide at runtime:
            * TTY (``sys.stdin.isatty() == True``) → prompt via
              :class:`rich.prompt.Confirm` with default = yes.
            * Non-TTY → install (default = yes). The motivating user
              path is ``uv tool install ozzylabs-opshub[mcp] && opshub
              init`` driven from a script or fresh shell where stdin is
              not a terminal, so the non-interactive branch deliberately
              installs rather than skipping (ADR-0029 §決定 (d)).

        Install dispatches to
        :func:`opshub.cli.skills.install_command` with
        ``host='all', scope='user', skip_existing=False`` so the same
        14 assistant skills land in ``~/.claude/skills/`` and
        ``~/.agents/skills/`` as a manual ``opshub skills install``
        invocation. The lazy import inside the branch avoids importing
        :mod:`opshub.cli.skills` (and its
        :mod:`opshub._skills_resources` chain) when the operator
        explicitly passes ``--no-install-skills`` — keeping the
        skip path cheap.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli.db import apply_migrations
    from opshub.core.config import OpsHubSettings

    settings = OpsHubSettings()

    # Directory layout (idempotent — ``exist_ok=True`` on every call):
    #   config_dir/            -> holds config.toml
    #   data_dir/              -> XDG data root for OpsHub
    #   data_dir/db/           -> parent of the SQLite file (storage.db_path)
    #   workspace.root/        -> per-user workspace tree (cloned repos, etc.)
    for directory in (
        settings.config_dir,
        settings.data_dir,
        settings.workspace.root,
        settings.storage.db_path.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = settings.config_dir / "config.toml"
    if force or not config_path.exists():
        config_path.write_text(STARTER_CONFIG_TOML, encoding="utf-8")

    # ``provision_key=True``: when ``[storage] encryption`` is enabled,
    # mint + store a fresh DB key for this brand-new database (ADR-0021
    # §(b)). Safe here because ``init`` runs before the DB carries data.
    apply_migrations(settings, provision_key=True)

    if _should_install_skills(install_skills):
        # Lazy import to avoid pulling ``opshub._skills_resources``
        # (and its ``importlib.resources`` walk) when the operator
        # passed ``--no-install-skills``.
        from opshub.cli.skills import install_command

        install_command(host="all", scope="user", skip_existing=False)

    return 0


def _should_install_skills(install_skills: bool | None) -> bool:
    """Resolve the tri-state ``install_skills`` flag to a boolean.

    * ``True`` / ``False`` — operator explicitly chose; no probe / prompt.
    * ``None`` (default):
        * stdin is a TTY → :class:`rich.prompt.Confirm` with default
          = yes. Operator presses Enter to install, ``n`` to skip.
        * stdin is not a TTY → install (default = yes). The motivating
          flow (``uv tool install ... && opshub init`` in a script /
          fresh shell) is precisely the non-interactive path, so the
          fall-through must install (ADR-0029 §決定 (d)).

    The TTY probe goes through :func:`_stdin_is_tty` (a thin wrapper
    around :data:`sys.stdin`.isatty) rather than :data:`sys.stdout` or
    :data:`sys.stderr` because :class:`rich.prompt.Confirm` reads
    input from stdin — if stdin is closed / redirected, no prompt is
    possible regardless of the output streams' TTY state. The wrapper
    exists so tests can monkeypatch the TTY decision deterministically
    even though :class:`typer.testing.CliRunner` rewires
    :data:`sys.stdin` to a non-TTY buffer during ``invoke()``.
    """
    if install_skills is not None:
        return install_skills

    if _stdin_is_tty():
        # Lazy import — ``opshub init --no-install-skills`` never pays
        # for the rich.prompt module load.
        from rich.prompt import Confirm

        return bool(
            Confirm.ask(
                "アシスタント 14 skill を ~/.claude/skills/ と "
                "~/.agents/skills/ に install しますか?",
                default=True,
            )
        )
    return True


def _stdin_is_tty() -> bool:
    """Return whether :data:`sys.stdin` is connected to a terminal.

    Extracted as a one-line helper so :func:`_should_install_skills`
    has a single, monkeypatch-friendly seam. :class:`typer.testing.CliRunner`
    swaps :data:`sys.stdin` for a :class:`io.BytesIO`-style buffer whose
    ``isatty()`` returns ``False`` regardless of how the host shell was
    invoked, so unit tests that need the TTY-prompt branch patch
    ``opshub.cli.init._stdin_is_tty`` directly rather than fighting
    Click's stdin isolation.
    """
    import sys

    return sys.stdin.isatty()
