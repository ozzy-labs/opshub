"""Slack connector implementation (Phase 7 step A3).

Composes the A1 auth helper, the A2 fetcher, and the A3 mapper into the
:class:`opshub.connectors.base.Connector` Protocol contract so the
CLI driver (``opshub connector sync slack``) can resolve and run a
Slack sync end-to-end. This module is the Slack analogue of
:class:`opshub.connectors.github.connector.GitHubConnector` and follows
its conventions exactly so a future "common sync orchestrator" refactor
(phase-7-plan §4 Open Q #2) can lift identical structure from both.

Sync semantics
--------------

The per-channel resume cursor is a ``dict[str, str | None]`` mapping
channel id → last-observed Slack ``ts``. We serialise it as JSON so it
fits the single ``cursor_value`` column on the
``connector_cursors`` projection (Phase 3 design — one row per
``connector_name``, no per-key fan-out yet). Round-trip:

1. ``context.cursor_value`` is the JSON string we wrote on the previous
   sync (or ``None`` for first-sync).
2. :func:`_load_cursors` parses it into the ``dict`` shape the fetcher
   takes via ``cursor_per_channel=``.
3. As the fetcher yields ``(channel_id, message, new_cursor)`` triples
   we update the in-memory dict per yield and forward the mapped
   ``SourceObserved`` to :meth:`SourceService.observe`. The dict
   advances in lock-step with the commit so a crash mid-loop loses at
   most one message worth of progress (the one whose ``observe`` call
   was about to commit).
4. After the iterator drains we serialise the dict back to JSON and
   hand it to the CLI driver as :attr:`SyncResult.new_cursor`.

The cursor JSON is opaque to the driver and projection — both treat it
as a single string. A future operator-facing ``opshub connector status``
CLI could pretty-print the parsed dict, but Phase 7 MVP does not
expose it.

Cursor monotonicity (defense in depth)
--------------------------------------

The fetcher (post-issue #339 fix) yields messages in ts-ascending
order across pages so the cursor naturally advances monotonically.
We *additionally* guard the projection-bound cursor with
``cursors[ch] = _max_ts(prior, yielded)`` at the connector level so
a future fetcher regression that loses chronological ordering does
not silently rewind the persisted cursor (and cause the entire
re-ingest cascade documented in issue #339). The pattern mirrors
:class:`~opshub.connectors.github.connector.GitHubConnector`, which
keeps its cursor as ``max(observed.updated_at)`` rather than
trusting iteration order.

Configuration source
--------------------

Channels are read from ``[connectors.slack] channels`` (or the
``OPSHUB_CONNECTORS__SLACK__CHANNELS`` env var) on
:class:`~opshub.core.config.OpsHubSettings`. ``enabled = false`` is the
default per phase-7-plan §1 #2 (opt-in by design) but the CLI driver
treats the connector as runnable as soon as it is registered — the
``enabled`` flag is informational for downstream wiring (Phase 7.x
scheduler / autopilot will respect it). We treat an empty channel list
as a no-op with a structured log warning rather than a hard error so an
operator who misconfigured ``[connectors.slack]`` sees an actionable
event in the log instead of a stack trace.

Fail-fast posture (phase-7-plan §1 #8)
--------------------------------------

* :class:`ConfigError` (missing bot token, empty-channel misconfig)
  propagates verbatim — the CLI driver maps it to an exit code without
  appending a ``ConnectorSyncFailed`` event (cursor projection failure
  records are reserved for genuine connector failures, not config
  mistakes).
* :class:`~opshub.core.errors.ConnectorFailedError` (Slack API errors,
  rate-limit budget exhausted) propagates verbatim too. The CLI driver
  is the single place that appends :class:`ConnectorSyncFailed` with
  the sanitised exception type — keeping the sanitisation in one
  callsite avoids the embedding-service / briefing-service
  duplication that prompted ADR-0005's extraction of
  :func:`opshub.core.sanitise.sanitise_error_message`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from opshub.connectors.base import SyncResult
from opshub.connectors.slack.auth import SlackAuth
from opshub.connectors.slack.fetcher import SlackFetcher
from opshub.connectors.slack.mapper import map_message
from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    from opshub.connectors.context import ConnectorContext


__all__ = ["SlackConnector"]


class SlackConnector:
    """Concrete :class:`Connector` for Slack channel messages.

    The connector holds no Slack-API state at construction time — it
    resolves the bot token and channel list at the start of
    :meth:`sync`, then constructs a fresh :class:`SlackFetcher` per
    invocation. That keeps the cold-start import cheap (the
    ``slack_sdk`` SDK is only loaded by the fetcher, lazily, inside
    its own ``fetch_messages``) and matches the GitHub precedent
    where :class:`GitHubConnector` instantiates no httpx client at
    construction time either.
    """

    name = "slack"

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one Slack sync pass and return the outcome.

        The implementation is intentionally linear: resolve config →
        resolve auth → build fetcher → iterate yields → return
        ``SyncResult``. Each yielded message is forwarded to
        :meth:`SourceService.observe` (which atomically appends a
        :class:`SourceObserved` + :class:`ItemEnqueued` event pair),
        and the per-channel cursor dict is updated **after** the
        observe call so a failure mid-iteration leaves the cursor
        pointing at the last successfully-committed message (the
        same at-most-once-or-no-loss posture pinned by the A2
        fetcher's module docstring).
        """
        channels = self._resolve_channels()
        if not channels:
            # Empty channel list is a degraded-but-not-failing state:
            # the connector is configured (token + extras present) but
            # the operator hasn't picked any channels yet. We log a
            # structured warning and return a no-op SyncResult that
            # preserves the prior cursor — mirrors the GitHub
            # connector's "no observed items → keep prior cursor"
            # contract pinned by ``test_empty_sync_preserves_cursor``.
            context.logger.warning(
                "slack connector: no channels configured; skipping sync. "
                "Populate [connectors.slack] channels in opshub.toml or "
                "set OPSHUB_CONNECTORS__SLACK__CHANNELS to enable."
            )
            return SyncResult(observed_count=0, new_cursor=context.cursor_value)

        auth = SlackAuth()
        fetcher = SlackFetcher(auth, channels=channels)

        # Phase 10 (ADR-0020 §(b)): shared ingest excludes. Slack honours
        # the ``channels`` and ``senders`` selectors — a message in an
        # excluded channel, or from an excluded sender, is never observed
        # (the cursor still advances so the connector does not re-scan it
        # forever). ``load_excludes()`` resolves the file path via
        # ``default_config_dir()`` directly so we avoid threading
        # ``OpsHubSettings`` through this path — tests that patch
        # ``OpsHubSettings`` at the class level would otherwise hand us
        # a MagicMock whose ``config_dir`` attribute is itself a
        # MagicMock that ``yaml.safe_load`` would iterate forever over.
        from opshub.core.excludes import load_excludes

        excludes = load_excludes()

        cursors = _load_cursors(context.cursor_value)
        observed_count = 0
        for channel_id, raw_message, new_cursor in fetcher.fetch_messages(
            cursor_per_channel=cursors,
        ):
            # Defense-in-depth: never let the persisted cursor regress.
            # The fetcher (post-#339 fix) yields ts-ascending across
            # pages so ``new_cursor`` is naturally monotonic, but a
            # future fetcher bug that yields an older ts after a
            # newer one would otherwise rewind the projection cursor
            # and cause every subsequent sync to re-ingest the gap
            # (the regression-cascade documented in issue #339).
            cursors[channel_id] = _max_ts(cursors.get(channel_id), new_cursor)
            if excludes.excludes_channel(raw_message.channel_id) or excludes.excludes_sender(
                raw_message.user_id
            ):
                continue
            kwargs = map_message(raw_message)
            # ``source_service`` is typed as ``Any`` on
            # :class:`ConnectorContext` (the framework predates the
            # Phase 3 ``SourceService`` rename); the keyword-only
            # ``observe`` signature catches argument drift at runtime
            # via TypeError.
            context.source_service.observe(**kwargs)
            observed_count += 1

        new_cursor_value = _dump_cursors(cursors) if cursors else context.cursor_value
        return SyncResult(observed_count=observed_count, new_cursor=new_cursor_value)

    def _resolve_channels(self) -> list[str]:
        """Return the configured Slack channel ids from settings.

        Lazy-imports :mod:`opshub.core.config` so the connectors
        package import path stays free of pydantic-settings — cold
        start (ADR-0001) only pays for this when the operator
        actually runs ``opshub connector sync slack``.
        """
        from opshub.core.config import OpsHubSettings

        settings = OpsHubSettings()
        # ``Field(default_factory=list)`` ensures the list is always
        # present; the explicit copy keeps mutation off the live
        # settings instance if a future refactor caches it.
        return list(settings.connectors.slack.channels)


def _load_cursors(cursor_value: str | None) -> dict[str, str | None]:
    """Parse the persisted JSON cursor into the fetcher's dict shape.

    ``None`` means "first sync — no cursors yet" and yields an empty
    dict. A malformed JSON string raises :class:`ConfigError` so the
    operator sees an actionable error rather than a silently re-fetched
    history; this can only happen if a future schema change rolled
    forward without a migration (very unlikely) or a manual
    ``connector_cursors`` row edit went wrong.
    """
    if cursor_value is None:
        return {}
    try:
        parsed = json.loads(cursor_value)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "Slack cursor is not valid JSON; the connector_cursors "
            "row may have been hand-edited. Reset with "
            "`opshub projections rebuild` to recover."
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"Slack cursor must be a JSON object mapping channel id "
            f"to ts; got {type(parsed).__name__}"
        )
    # Coerce to the documented ``dict[str, str | None]`` shape. Bool
    # / int values would be a hand-edit accident; we reject rather
    # than silently coerce so the operator sees the bad data. The
    # ``cast`` narrows pyright's ``Unknown`` widen on the
    # ``json.loads`` return; mypy treats it as redundant (the cast is
    # from ``Any`` to ``dict[Any, Any]``) so we suppress only mypy.
    parsed_dict = cast(  # type: ignore[redundant-cast]
        dict[Any, Any], parsed
    )
    result: dict[str, str | None] = {}
    for key, value in parsed_dict.items():
        if not isinstance(key, str):
            raise ConfigError(
                f"Slack cursor keys must be strings (channel ids); got {type(key).__name__}"
            )
        if value is not None and not isinstance(value, str):
            raise ConfigError(
                f"Slack cursor values must be strings or null (ts); "
                f"got {type(value).__name__} for channel {key!r}"
            )
        result[key] = value
    return result


def _max_ts(prior: str | None, candidate: str | None) -> str | None:
    """Return the chronologically-later of two Slack ``ts`` strings.

    Slack ``ts`` is documented as ``"seconds.microseconds"`` so
    :func:`float` comparison is total. ``None`` represents "no prior
    cursor" (first observation for the channel) and yields the other
    operand. If both sides parse but the candidate is older, we keep
    the prior — that's the load-bearing invariant for the issue #339
    fix: a yielded ``ts`` that goes *backwards* must never overwrite
    a persisted cursor.

    Defensive fallback: a non-numeric ``ts`` (Slack contract
    violation, would have to be a malformed test fixture or a future
    API shape change) falls through to the candidate so the connector
    still records some progress rather than silently dropping the
    new value. The fetcher's malformed-ts skip-arm normally prevents
    this branch from being reached.
    """
    if prior is None:
        return candidate
    if candidate is None:
        return prior
    try:
        return candidate if float(candidate) >= float(prior) else prior
    except (TypeError, ValueError):
        return candidate


def _dump_cursors(cursors: dict[str, str | None]) -> str:
    """Serialise the per-channel cursor dict to JSON for the projection.

    ``sort_keys=True`` makes the serialised value deterministic so a
    no-op sync (no new messages) yields a byte-identical cursor and
    the ``connector_cursors`` row's ``updated_at`` advances on
    timestamp only — the cursor itself is stable enough to compare
    across runs in operator dashboards. ``separators`` strips the
    default whitespace so the row stays compact (one row per
    connector, the size matters less than the determinism, but
    matching :func:`json.dumps(..., separators=(",", ":"))` to the
    GitHub cursor style keeps the projection rows uniformly tight).
    """
    return json.dumps(cursors, sort_keys=True, separators=(",", ":"))
