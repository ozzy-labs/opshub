"""Workspace-alias resolution for ``opshub slack ...`` commands (Phase 24-C).

[ADR-0041](../../docs/adr/0041-slack-multi-workspace.md) §(f) pins the
``--workspace <alias>`` default resolution rule shared by ``auth set`` /
``auth test`` / ``conversations`` / ``cursor backfill`` / ``cursor reset
--channel``:

* explicit ``--workspace`` → validate the alias grammar and use it
  (membership in ``[connectors.slack.workspaces]`` is **not** required
  for the token-oriented commands — ``auth set`` legitimately runs
  before the config table is written),
* no flag + exactly one configured workspace → that workspace,
* no flag + zero or multiple configured workspaces → :class:`ConfigError`
  listing the configured aliases (no silent guess — channel ids collide
  across workspaces, so ambiguity must be loud).

``slack sync`` (all-workspace default with ``--workspace`` narrowing)
and ``slack status`` (all-workspace display with ``--workspace``
filtering) do **not** use this helper's default rule — they iterate the
configured set instead.

Module-level imports are restricted to ``__future__`` so ``opshub
--help`` cold start stays under the ADR-0001 budget; heavy imports
(config) happen inside the functions.
"""

from __future__ import annotations

__all__ = ["configured_workspace_aliases", "resolve_workspace_alias", "validate_alias_format"]


def configured_workspace_aliases() -> list[str]:
    """Return the sorted aliases under ``[connectors.slack.workspaces]``."""
    from opshub.core.config import OpsHubSettings

    return sorted(OpsHubSettings().connectors.slack.workspaces)


def validate_alias_format(alias: str) -> str:
    """Validate ``alias`` against the ADR-0041 §(a) grammar; return it.

    Raises :class:`~opshub.core.errors.ConfigError` on a miss so every
    CLI entry point rejects e.g. ``my-ws`` (the ``-`` would collide with
    ``_`` in the keyring env override name) before any keyring write.
    """
    from opshub.core.config import SLACK_WORKSPACE_ALIAS_RE
    from opshub.core.errors import ConfigError

    if not SLACK_WORKSPACE_ALIAS_RE.match(alias):
        raise ConfigError(
            f"invalid Slack workspace alias {alias!r}; aliases must match "
            "^[a-z0-9][a-z0-9_]*$ (lowercase, '-' is not allowed — it would "
            "collide with '_' in the keyring env override name, ADR-0041 §(a))."
        )
    return alias


def resolve_workspace_alias(explicit: str | None) -> str:
    """Resolve the effective workspace alias per the ADR-0041 §(f) default rule.

    ``explicit`` (the ``--workspace`` flag value) wins after a grammar
    check. Otherwise: exactly one configured workspace → use it; zero or
    multiple → :class:`~opshub.core.errors.ConfigError` naming the
    configured aliases so the operator can re-run with ``--workspace``.
    """
    from opshub.core.errors import ConfigError

    if explicit is not None:
        return validate_alias_format(explicit)

    aliases = configured_workspace_aliases()
    if len(aliases) == 1:
        return aliases[0]
    if not aliases:
        raise ConfigError(
            "no Slack workspaces configured; add a "
            "[connectors.slack.workspaces.<alias>] table to opshub.toml "
            "(ADR-0041) or pass --workspace <alias> explicitly."
        )
    raise ConfigError(
        f"multiple Slack workspaces configured ({', '.join(aliases)}); "
        "pass --workspace <alias> to pick one."
    )
