"""Operator self-identity resolution (Phase 25-A, ADR-0010 §改訂).

The Phase 25 秘書化 v1 commitment ledger (25-C) decides a source's
commitment *direction* from whether the operator authored it: a message
the operator wrote is an ``i_owe`` candidate (a promise the operator
made), a message someone else wrote is an ``owed_to_me`` candidate (a
request the operator received). That decision needs a per-connector
notion of "who am I" so an ``author_handle`` can be compared against the
operator's own identity in the same connector's namespace.

This module is the single place that resolves the operator's identity
per connector and exposes :func:`is_authored_by_operator`, the helper
25-B / 25-C call. The identity sources are:

* **Slack** — reuses the per-workspace self user id the
  ``slack_demand_digest`` projection already resolves (env override
  ``OPSHUB_SLACK_SELF_USER_ID__<ALIAS>`` → keyring ``auth.test``,
  Phase 24-C / ADR-0041 §(g)). The source's ``team_id`` (the leading
  token of the Slack ``external_id`` ``"{team_id}:{channel_id}:{ts}"``)
  selects which workspace's self id to compare against.
* **Gmail / MS365 (Outlook + Calendar) / Google Workspace** — the
  operator's own email address from the connector's
  ``operator_email`` config field (env override
  ``OPSHUB_CONNECTORS__<CONN>__OPERATOR_EMAIL``). Compared
  case-insensitively against the lower-cased ``author_handle`` the
  mapper stamps.
* **GitHub** — the operator's GitHub ``login`` from
  ``[connectors.github] operator_login`` (env override
  ``OPSHUB_CONNECTORS__GITHUB__OPERATOR_LOGIN``). GitHub logins are
  case-insensitive, so the comparison case-folds.
* **Teams** — the operator's Graph user id from
  ``[connectors.teams] operator_id``. Graph ids are opaque GUIDs, so
  the comparison is exact (no case folding).

Connectors that surface no author identity (``box`` file-activity
events, the local-FS ``box_drive`` / ``onedrive_drive`` scanners, the
operator-listed ``web`` connector) always resolve to ``False`` — there
is no "self" to match. Unconfigured operator identity (the empty-string
defaults) likewise yields ``False``: the helper never *guesses* that a
source is self-authored, so a missing config degrades to "treat
everything as inbound" rather than mislabelling commitments.

Design note — why a value object rather than a DB row
-----------------------------------------------------
There is no ``Source`` read-model dataclass in opshub (sources are
queried as raw projection rows), so the helper takes a small
:class:`SourceAuthor` value object carrying only the fields the
resolution needs (``connector_name`` / ``author_handle`` /
``external_id``). 25-B / 25-C build one from the ``sources`` row they
already hold.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SourceAuthor", "is_authored_by_operator"]


#: Email-keyed connectors whose operator identity is an ``operator_email``
#: config field. The mapper stamps a lower-cased email on ``author_handle``
#: for each of these, so the comparison case-folds the configured value
#: too. ``connector_name`` → settings attribute on
#: :class:`opshub.core.config.ConnectorSettings`.
_EMAIL_CONNECTORS: dict[str, str] = {
    "google_mail": "google_mail",
    "ms365": "ms365",
    "google_calendar": "google_calendar",
    "google_workspace": "google_workspace",
}


@dataclass(frozen=True)
class SourceAuthor:
    """Minimal author identity of an observed source (Phase 25-A).

    ``connector_name`` selects the resolution path; ``author_handle`` is
    the connector-native author join key the ``sources`` projection
    stamped (``None`` when the connector surfaces no author).
    ``external_id`` is only consulted for Slack, where the leading token
    carries the ``team_id`` that selects which workspace's self user id
    to compare against.
    """

    connector_name: str
    author_handle: str | None
    external_id: str | None = None


def is_authored_by_operator(author: SourceAuthor) -> bool:
    """Return whether ``author`` identifies the operator themselves.

    The comparison is per-connector (see the module docstring).
    ``False`` is the safe default for every "cannot tell" case:

    * the connector surfaces no author (``author_handle is None``);
    * the connector has no operator-identity concept (``box`` /
      ``box_drive`` / ``onedrive_drive`` / ``web``);
    * the operator identity is unconfigured (empty-string defaults);
    * any resolution failure (Slack self id unavailable, config parse
      error).

    The helper never *guesses* self-authorship — a missing config
    degrades to "treat as inbound" so the commitment ledger (25-C) over-
    counts ``owed_to_me`` rather than mislabelling a request as a
    promise.
    """
    handle = author.author_handle
    if handle is None or not handle.strip():
        return False

    connector = author.connector_name
    if connector == "slack":
        return _slack_is_operator(handle, author.external_id)
    if connector in _EMAIL_CONNECTORS:
        return _email_is_operator(_EMAIL_CONNECTORS[connector], handle)
    if connector == "github":
        return _github_is_operator(handle)
    if connector == "teams":
        return _teams_is_operator(handle)
    # box / box_drive / onedrive_drive / web / unknown — no self concept.
    return False


# ----------------------------------------------------------------- resolvers


def _email_is_operator(settings_attr: str, handle: str) -> bool:
    """Compare ``handle`` against the connector's ``operator_email`` (case-insensitive)."""
    operator = _connector_str(settings_attr, "operator_email")
    if not operator:
        return False
    return handle.strip().casefold() == operator.casefold()


def _github_is_operator(handle: str) -> bool:
    """Compare ``handle`` against ``[connectors.github] operator_login`` (case-insensitive)."""
    operator = _connector_str("github", "operator_login")
    if not operator:
        return False
    return handle.strip().casefold() == operator.casefold()


def _teams_is_operator(handle: str) -> bool:
    """Compare ``handle`` against ``[connectors.teams] operator_id`` (exact, opaque GUID)."""
    operator = _connector_str("teams", "operator_id")
    if not operator:
        return False
    return handle.strip() == operator.strip()


def _slack_is_operator(handle: str, external_id: str | None) -> bool:
    """Compare a Slack ``U...`` handle against the operator's self id in its workspace.

    The source's ``external_id`` is ``"{team_id}:{channel_id}:{ts}"``
    (Phase 24-B); its leading token selects which workspace's self user
    id to compare against. A missing / malformed ``external_id`` (no
    team_id to disambiguate) degrades to ``False``.
    """
    if not external_id:
        return False
    team_id, sep, _rest = external_id.partition(":")
    if not sep or not team_id:
        return False
    self_id = _slack_self_user_id(team_id)
    if not self_id:
        return False
    return handle.strip() == self_id


def _slack_self_user_id(team_id: str) -> str | None:
    """Resolve the operator's Slack self user id in workspace ``team_id``.

    Reuses the ``slack_demand_digest`` resolution cascade (per-alias env
    override → keyring ``auth.test``) so Slack self-identity has a single
    SSOT. The import is deferred to keep :mod:`opshub.connectors.slack`
    off the cold-start path. Any failure degrades to ``None`` (fail-soft,
    matching the projection's posture).
    """
    try:
        from opshub.projections.slack_demand_digest import (
            resolve_self_user_ids_from_config,
        )
    except Exception:  # pragma: no cover — defensive against extras pruning
        return None

    try:
        return resolve_self_user_ids_from_config().get(team_id)
    except Exception:  # last-resort fail-soft
        return None


def _connector_str(settings_attr: str, field: str) -> str:
    """Read a string field off ``connectors.<settings_attr>`` (fail-soft).

    Any failure (config parse error, settings unavailable in an embedded
    context, missing attribute) degrades to ``""`` so the caller treats
    the operator identity as unconfigured rather than crashing the
    commitment scan. Env overrides flow through pydantic-settings'
    nested-env path (``OPSHUB_CONNECTORS__<CONN>__<FIELD>``) so they are
    already reflected on the constructed settings object — but the helper
    also honours a freshly-set process env var by constructing settings
    on each call (the call site is the on-demand ``commitment scan``, not
    a hot loop).
    """
    try:
        from opshub.core.config import OpsHubSettings

        connectors = OpsHubSettings().connectors
        section = getattr(connectors, settings_attr, None)
        value = getattr(section, field, "")
        return value if isinstance(value, str) else ""
    except Exception:
        return ""
