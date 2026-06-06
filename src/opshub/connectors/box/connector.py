"""Box connector implementation (Phase 7 step C3).

Composes the C1 :class:`opshub.connectors.box.auth.BoxAuth`, the C2
:class:`opshub.connectors.box.fetcher.BoxFetcher`, and the C3
:func:`opshub.connectors.box.mapper.map_event` projection into the
:class:`opshub.connectors.base.Connector` Protocol contract. Driven by
the ``opshub box sync`` CLI in :mod:`opshub.cli.box`
(shared driver: :mod:`opshub.cli._connector_common`).

Sync semantics (Phase 7 plan §2.3 C3, mirroring the Phase 3 GitHub
precedent):

* Cursor = Box's opaque ``stream_position`` string. The first sync
  resumes from ``None`` and the fetcher then asks Box for ``"now"`` so
  no historical events are backfilled — see
  :class:`BoxFetcher.fetch_events` for the rationale.
* Each ``(RawBoxEvent, new_position)`` pair the fetcher yields is
  mapped through :func:`map_event` and observed via
  :meth:`SourceService.observe`, which produces a
  :class:`SourceObserved` + :class:`ItemEnqueued` pair atomically in
  one UoW (PR #47 contract).
* :class:`SyncResult.new_cursor` is the **last** ``stream_position``
  the fetcher reported. Box advances the position once per *page*, not
  once per event, so threading the last-seen value through the loop is
  enough — there is no max-aggregation needed (unlike GitHub which
  aggregates updated_at across heterogeneous endpoints).
* On no observations the cursor falls back to ``context.cursor_value``
  — leaving the cursor unchanged is the correct rollback when Box
  returns an empty page on a session that started from a non-``None``
  resume token.

Failure posture (Phase 7 plan §1 #8 / phase-3-plan §4 Q3):

* :class:`ConnectorFailedError` from the fetcher (e.g. exhausted 429
  retries, refresh-token revoked) propagates so the CLI driver records
  a :class:`ConnectorSyncFailed` event with a sanitised message
  (``type(exc).__name__`` only — never the raw text, see
  :func:`opshub.cli._connector_common.run_connector_sync`).
* :class:`ConfigError` from the auth helper (missing ``client_id``,
  no refresh token) likewise propagates — operator action required.
* Any other exception propagates verbatim; the CLI driver applies the
  same sanitisation rule before recording the failure event.

Cold-start budget (ADR-0001): this module imports only the framework
primitives and stdlib at top level. The :class:`boxsdk` SDK and the
:class:`opshub.core.config.OpsHubSettings` loader are deferred into
:meth:`BoxConnector.sync` so ``opshub --help`` keeps its ~300 ms
cold-start budget on installations that never use Box.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opshub.connectors.base import SyncResult
from opshub.connectors.box.mapper import map_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from opshub.connectors.box.auth import BoxAuth
    from opshub.connectors.box.fetcher import BoxFetcher, RawBoxEvent
    from opshub.connectors.context import ConnectorContext

__all__ = ["BoxConnector"]


class BoxConnector:
    """Concrete :class:`~opshub.connectors.base.Connector` for Box Events.

    Parameters
    ----------
    fetcher_factory:
        Test seam. Defaults to building a
        :class:`opshub.connectors.box.fetcher.BoxFetcher` from a fresh
        :class:`opshub.connectors.box.auth.BoxAuth` constructed against
        :class:`opshub.core.config.OpsHubSettings`. Integration tests
        substitute a factory returning a stubbed fetcher so the suite
        never touches the real Box SDK or the keyring.

    The factory is a constructor argument (not a class attribute) so
    each :class:`BoxConnector` instance carries its own override —
    tests can register a fresh connector under the global registry
    without leaking state across cases.
    """

    name = "box"

    def __init__(self, fetcher_factory: Callable[[], BoxFetcher] | None = None) -> None:
        self._fetcher_factory = fetcher_factory

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one sync pass.

        See module docstring for the cursor / failure contract.
        """
        # Phase 10 (ADR-0020 §(b)): shared ingest excludes. Box honours
        # the ``senders`` selector against the actor (Box user id) and
        # the ``paths`` selector against the item path. An excluded
        # event is still advanced past on the cursor — Box's
        # ``next_stream_position`` advances once per page, so skipping
        # the observe call alone is enough; the cursor moves regardless.
        # ``load_excludes()`` resolves the file path via
        # ``default_config_dir()`` directly to avoid threading
        # potentially-mocked ``OpsHubSettings.config_dir`` — see the
        # slack connector for the MagicMock-yaml.safe_load infinite-loop
        # rationale (Phase 10 audit Cluster 3).
        from opshub.core.excludes import load_excludes

        excludes = load_excludes()
        fetcher = self._build_fetcher()
        observed_count = 0
        new_cursor: str | None = context.cursor_value
        for raw_event, position in self._iter_events(fetcher, stream_position=context.cursor_value):
            # Box's contract: every event on the same page carries the
            # *same* ``next_stream_position``. We assign on each loop
            # iteration anyway so a future paginated fetcher does not
            # need a separate "did we ever observe?" tracker — the loop
            # post-condition is simply "the last position we saw".
            new_cursor = position
            if excludes.excludes_sender(raw_event.actor_id) or excludes.excludes_path(
                raw_event.item_path
            ):
                continue
            self._observe(context, raw_event)
            observed_count += 1
        return SyncResult(observed_count=observed_count, new_cursor=new_cursor)

    def _build_fetcher(self) -> BoxFetcher:
        """Resolve the fetcher to use this sync.

        Production path: load settings, construct :class:`BoxAuth` with
        the operator's ``client_id`` (the matching ``client_secret``
        comes from the keyring via :func:`opshub.core.secrets.get_secret`
        inside :class:`BoxAuth.__init__`), and wrap it in
        :class:`BoxFetcher`.

        Test path: a constructor-provided factory short-circuits the
        SDK loading entirely.
        """
        if self._fetcher_factory is not None:
            return self._fetcher_factory()
        # Lazy imports keep the boxsdk dependency off the cold-start
        # path for installs that never run a Box sync.
        from opshub.connectors.box.auth import BoxAuth
        from opshub.connectors.box.fetcher import BoxFetcher
        from opshub.core.config import OpsHubSettings

        settings = OpsHubSettings()
        auth = self._build_auth(BoxAuth, client_id=settings.connectors.box.client_id)
        return BoxFetcher(auth)

    @staticmethod
    def _build_auth(auth_cls: type[BoxAuth], *, client_id: str) -> BoxAuth:
        """Construct :class:`BoxAuth` with the configured client id.

        Indirected through a static helper so tests can monkeypatch the
        resolution path independently of the fetcher factory. In
        production this just forwards to the standard constructor —
        :class:`BoxAuth` resolves ``client_secret`` from the keyring
        when omitted.
        """
        return auth_cls(client_id=client_id)

    @staticmethod
    def _iter_events(
        fetcher: BoxFetcher, *, stream_position: str | None
    ) -> Iterator[tuple[RawBoxEvent, str]]:
        """Indirection over :meth:`BoxFetcher.fetch_events` for ergonomics."""
        yield from fetcher.fetch_events(stream_position=stream_position)

    @staticmethod
    def _observe(context: ConnectorContext, raw_event: RawBoxEvent) -> None:
        """Project ``raw_event`` and append through the source service.

        :func:`map_event` builds a :class:`SourceObserved` value object
        carrying the canonical field set (truncated summary, normalised
        timestamps, etc.). We extract the keyword arguments back out
        and call :meth:`SourceService.observe`, which constructs the
        actual persisted event with the service's configured actor +
        :class:`ItemEnqueued` companion in a single UoW.

        Why route through ``observe`` rather than appending the mapped
        event directly: the service guarantees ``SourceObserved`` and
        ``ItemEnqueued`` land in one transaction (PR #47 atomicity
        contract). Sidestepping it would either lose the inbox event
        or break the rollback guarantee.
        """
        projected = map_event(raw_event)
        # ``source_service`` is typed as ``Any`` on
        # :class:`ConnectorContext` (the placeholder retained from
        # Phase 3 step A5); the runtime type is
        # :class:`opshub.services.source_service.SourceService`.
        context.source_service.observe(
            connector_name=projected.connector_name,
            external_id=projected.external_id,
            source_type=projected.source_type,
            title=projected.title,
            url=projected.url,
            summary=projected.summary,
            # Phase 10 (ADR-0020): thread the body + provenance the
            # mapper stamped. Box events are metadata-only, so the
            # mapper substitutes ``body = summary`` to satisfy the
            # ``SourceObserved.body`` ``min_length=1`` invariant
            # (epic #470 / #481, ADR-0010 §不変条件).
            body=projected.body,
            provenance_origin=projected.provenance_origin,
            provenance_trust=projected.provenance_trust,
        )
