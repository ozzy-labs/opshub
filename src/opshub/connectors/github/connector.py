"""GitHub connector implementation (Phase 3 step B3).

Composes the B1 auth helper + B2 fetch primitives into the
:class:`opshub.connectors.base.Connector` Protocol contract. Driven
by the ``opshub github sync`` CLI in :mod:`opshub.cli.github`
(shared driver: :mod:`opshub.cli._connector_common`; the
pre-Phase-17-B single dispatch module ``opshub.cli.connector`` was
split into per-noun groups + a typer-free driver per ADR-0031).

Sync semantics:

* Cursor = ISO 8601 UTC timestamp ("last successful sync end time").
  The first sync (cursor=None) fetches *everything* the PAT can see
  for the configured repo + the authenticated user's notifications.
* Each fetched item is forwarded to
  :func:`opshub.services.source_service.SourceService.observe`. That
  one call emits both a ``SourceObserved`` and an ``ItemEnqueued``
  in a single UoW per item (PR #47 contract).
* The new cursor returned in :class:`SyncResult.new_cursor` is the
  **maximum updated_at** observed across all items in this sync.
  If zero items were observed, the cursor stays at the prior value
  (``None`` for first-sync-no-items).

Configuration:

* ``OPSHUB_CONNECTOR_GITHUB_REPO`` (env var) — ``"owner/repo"``
  format. Required: the connector cannot guess which repo to sync.
  Future: support multiple repos via a config table (Phase 3.x).
* PAT — via :func:`opshub.connectors.github.auth.get_github_token`
  (env override + keyring per ADR-0014, PR #51).

Fail-fast posture (phase-3-plan §4 Q3):

* Any GitHubAPIError / network exception propagates up so the CLI
  driver in A5 records ``ConnectorSyncFailed`` and returns non-zero.
* No retry / backoff in Phase 3 — operator re-runs ``opshub
  connector sync github`` manually after addressing the underlying
  issue (rate limit reset, token rotation, network restoration).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING

from opshub.connectors.base import SyncResult
from opshub.connectors.github.auth import get_github_token
from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    # ``api`` pulls ``httpx`` (a ``connectors-github`` extra) at module
    # level, so it is imported lazily inside :meth:`GitHubConnector.sync`
    # to keep this package import-clean — an operator who installed only
    # another connector's extra (e.g. ``connectors-slack``) must be able
    # to ``import opshub.connectors.github`` (the registration side effect)
    # without the ``httpx`` dependency. The ``_observe`` annotation below
    # is the only compile-time use, resolved here under ``TYPE_CHECKING``
    # (never executed at runtime thanks to ``from __future__ import
    # annotations``).
    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.github import api as github_api

__all__ = ["GitHubConnector"]

_REPO_ENV_VAR = "OPSHUB_CONNECTOR_GITHUB_REPO"


class GitHubConnector:
    """Concrete :class:`Connector` for GitHub Issues / PRs / Notifications."""

    name = "github"

    def sync(self, context: ConnectorContext) -> SyncResult:
        # Lazy import: ``api`` imports ``httpx`` at module level, so we
        # defer it to the one path that actually hits the GitHub API.
        # This keeps ``import opshub.connectors.github`` working without
        # the ``connectors-github`` extra installed (see the package
        # docstring + the ``TYPE_CHECKING`` note above).
        from opshub.connectors.github import api as github_api

        repo = os.environ.get(_REPO_ENV_VAR)
        if not repo or "/" not in repo:
            raise ConfigError(
                f"GitHub connector requires {_REPO_ENV_VAR}=owner/repo "
                f"in the environment (got {repo!r})"
            )
        # Phase 10 (ADR-0020 §(b)): shared ingest excludes. When the
        # configured ``owner/repo`` is in the ``repos`` selector, the
        # connector observes nothing (no-op sync, prior cursor kept).
        # ``load_excludes()`` resolves the file path via
        # ``default_config_dir()`` directly — avoids threading a
        # potentially-mocked ``OpsHubSettings`` (see slack connector).
        from opshub.core.excludes import load_excludes

        if load_excludes().excludes_repo(repo):
            context.logger.warning(
                "github connector: repo %s is excluded by excludes.yaml; skipping sync",
                repo,
            )
            return SyncResult(observed_count=0, new_cursor=context.cursor_value)
        token = get_github_token()
        since = _parse_cursor(context.cursor_value)

        observed: list[datetime] = []
        for item in github_api.list_issues_since(repo, since, token=token):
            self._observe(context, item)
            observed.append(item.updated_at)
        for item in github_api.list_pulls_since(repo, since, token=token):
            self._observe(context, item)
            observed.append(item.updated_at)
        for item in github_api.list_notifications(token=token, since=since):
            self._observe(context, item)
            observed.append(item.updated_at)

        if observed:
            new_cursor: str | None = max(observed).isoformat().replace("+00:00", "Z")
        else:
            new_cursor = context.cursor_value
        return SyncResult(observed_count=len(observed), new_cursor=new_cursor)

    def _observe(self, context: ConnectorContext, item: github_api.GitHubItem) -> None:
        # ``source_service`` is typed as ``Any`` on :class:`ConnectorContext`
        # (A5 placeholder) — runtime type is :class:`SourceService` from
        # PR #47. The untyped attribute access keeps the connector layer
        # compiling without circular imports.
        context.source_service.observe(
            connector_name=self.name,
            external_id=item.external_id,
            source_type=item.source_type,
            title=item.title,
            url=item.url,
            summary=item.summary,
            # Phase 10 (ADR-0020): retain the full body and tag it as
            # external + untrusted so downstream agent / LLM context
            # treats it as reference material, never instructions
            # (content poisoning / indirect prompt injection mitigation,
            # ADR-0020 §(e)).
            body=item.body,
            provenance_origin="external",
            provenance_trust="untrusted",
        )


def _parse_cursor(value: str | None) -> datetime | None:
    """Parse an ISO 8601 (``...Z``) cursor into a tz-aware UTC datetime.

    Mirrors :func:`opshub.connectors.github.api._parse_iso_utc` so the
    round-trip ``max(updated_at).isoformat() → context.cursor_value →
    datetime`` is symmetric.
    """
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
