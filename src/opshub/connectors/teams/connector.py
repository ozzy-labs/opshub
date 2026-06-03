"""Teams connector implementation (Phase 11 F5).

Composes the F5 auth helper, the F5 fetcher, and the F5 mapper into
the :class:`opshub.connectors.base.Connector` Protocol contract so the
CLI driver (``opshub teams sync``) can resolve and run a
Teams sync end-to-end. The shape mirrors :class:`SlackConnector` /
:class:`MS365Connector` so a future common sync orchestrator can lift
identical structure from all three.

Sync semantics
--------------

The cursor for Teams is the opaque Microsoft Graph ``@odata.deltaLink``
URL returned on the final page of the previous sync. We serialise it
verbatim into the single ``cursor_value`` column on the
``connector_cursors`` projection — no JSON wrapping needed because
Teams has one cursor (per-chat fan-out happens inside the
``getAllMessages`` endpoint, not at our layer).

ADR-0010 §改訂 (c) full-pass fallback
-------------------------------------

When Graph rejects the stored delta link with ``410 Gone`` the fetcher
transparently falls back to a ``$filter``-based window scan and
re-acquires a fresh delta link. The connector therefore needs to read
the fetcher's :attr:`TeamsFetcher.pending_delta_link` after the
iterator drains and persist that value instead of the in-flight cursor
when the fallback ran. The cursor advance still happens on every
yield (so a crash mid-fallback only loses the last in-flight commit),
but the final value we persist is the freshly-acquired delta link when
available.

Configuration source
--------------------

The Teams connector reads ``[connectors.teams]`` from
:class:`~opshub.core.config.OpsHubSettings`. ``enabled = False`` is
the default (every SaaS connector is opt-in) but the CLI driver
treats the connector as runnable as soon as it is registered — the
``enabled`` flag is informational for downstream scheduler wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opshub.connectors.base import SyncResult
from opshub.connectors.teams.auth import TeamsAuth
from opshub.connectors.teams.fetcher import TeamsFetcher
from opshub.connectors.teams.mapper import map_chat_message

if TYPE_CHECKING:
    from opshub.connectors.context import ConnectorContext


__all__ = ["TeamsConnector"]


class TeamsConnector:
    """Concrete :class:`Connector` for Microsoft Teams chat messages.

    The connector holds no Graph-API state at construction time — it
    resolves the User Token and fallback window at the start of
    :meth:`sync`, then constructs a fresh :class:`TeamsFetcher` per
    invocation. That keeps the cold-start import cheap (the ``httpx``
    SDK is only loaded by the fetcher, lazily, inside its constructor)
    and matches the Slack / MS365 precedents.
    """

    name = "teams"

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one Teams sync pass and return the outcome.

        Linear implementation:

        1. Resolve config → resolve auth → build fetcher.
        2. Load shared ingest excludes (ADR-0020 §(b)). Teams honours
           the ``channels`` selector (matched against ``chat_id`` /
           ``chat_topic`` — same selector reused from Slack per the
           Phase 10 Sub-issue F instruction) and the ``senders``
           selector (matched against ``sender_id``).
        3. Iterate the fetcher's ``(message, new_cursor)`` yields.
           Each yielded message is forwarded to
           :meth:`SourceService.observe` (which atomically appends a
           :class:`SourceObserved` + :class:`ItemEnqueued` event
           pair). Excluded messages still advance the cursor —
           otherwise the connector would re-scan them on every sync —
           but never reach the projection.
        4. After the iterator drains, if the fetcher's fallback ran
           and produced a fresh delta link, prefer that value over
           the in-flight cursor so the next sync resumes on the
           delta path.
        """
        # Phase 10 (ADR-0020 §(b)): shared ingest excludes. We import
        # here (not at module level) for the same reason the Slack
        # connector does: ``load_excludes()`` resolves the config file
        # path via ``default_config_dir()`` directly to avoid threading
        # ``OpsHubSettings`` through this path — tests that patch
        # ``OpsHubSettings`` at the class level would otherwise hand us
        # a MagicMock whose ``config_dir`` attribute is itself a
        # MagicMock that ``yaml.safe_load`` would iterate forever over.
        from opshub.core.config import OpsHubSettings
        from opshub.core.excludes import load_excludes

        settings = OpsHubSettings()
        teams_settings = settings.connectors.teams

        auth = TeamsAuth()
        fetcher = TeamsFetcher(
            auth,
            fallback_window_days=teams_settings.fallback_window_days,
        )
        excludes = load_excludes()

        cursor = context.cursor_value
        observed_count = 0
        last_cursor: str | None = cursor

        try:
            for raw_message, new_cursor in fetcher.fetch_chat_messages(delta_link=cursor):
                last_cursor = new_cursor
                # ADR-0020 §(b): excluded items still advance the cursor
                # (otherwise the connector would re-fetch them every
                # run) but are never observed.
                if excludes.excludes_channel(raw_message.chat_id) or excludes.excludes_sender(
                    raw_message.sender_id
                ):
                    continue
                kwargs = map_chat_message(raw_message)
                # ``source_service`` is typed as ``Any`` on
                # :class:`ConnectorContext`; the keyword-only ``observe``
                # signature catches argument drift at runtime via
                # TypeError.
                context.source_service.observe(**kwargs)
                observed_count += 1
        finally:
            fetcher.close()

        # ADR-0010 §改訂 (c): when the fallback ran the fetcher acquired
        # a fresh delta link. Prefer it over the in-flight cursor so
        # the next sync resumes on the delta path.
        pending = fetcher.pending_delta_link
        new_cursor_value = pending if pending is not None else last_cursor

        return SyncResult(observed_count=observed_count, new_cursor=new_cursor_value)
