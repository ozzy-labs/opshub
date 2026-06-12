"""Slack connector implementation (Phase 7 step A3).

Composes the A1 auth helper, the A2 fetcher, and the A3 mapper into the
:class:`opshub.connectors.base.Connector` Protocol contract so the
CLI driver (``opshub slack sync``) can resolve and run a
Slack sync end-to-end. This module is the Slack analogue of
:class:`opshub.connectors.github.connector.GitHubConnector` and follows
its conventions exactly so a future "common sync orchestrator" refactor
(phase-7-plan §4 Open Q #2) can lift identical structure from both.

Sync semantics
--------------

The persisted resume cursor is a **per-workspace nest** of the Phase
20-B compound JSON object (Phase 24-C,
[ADR-0041](docs/adr/0041-slack-multi-workspace.md) §(d); the inner
4-axis shape is unchanged from
[ADR-0030 §(d) revised, epic #465](https://github.com/ozzy-labs/opshub/issues/465)
+ ``backfill`` axis Phase 22-B [ADR-0038](docs/adr/0038-slack-sync-gap-backfill.md)
+ ``team_id`` axis Phase 23-H ADR-0039):

.. code-block:: json

   {
     "workspaces": {
       "<alias>": {
         "channels": {"<channel_id>": "<max_ts>"},
         "backfill": {"<channel_id>": "<low_water_ts>"},
         "threads": {"<channel_id>:<thread_ts>": "<last_reply_ts>"},
         "team_id": "<T...>"
       }
     }
   }

The ``channels`` axis carries the per-channel forward high-water cursor
that drives ``conversations.history`` (Phase 7 semantics, unchanged).
The ``backfill`` axis carries the per-channel low-water mark (the oldest
ts boundary fetched down to, Phase 22-D lifecycle). The ``threads`` axis
carries the per-thread resume cursor for
``conversations.replies(oldest=last_reply_ts)`` increments — the late
thread reply polling path established in Phase 20-C. The scalar
``team_id`` axis binds the alias to its Slack workspace (per-alias bind
guard, ADR-0041 §(a)). Round-trip:

1. ``context.cursor_value`` is the JSON string we wrote on the previous
   sync (or ``None`` for first-sync).
2. :func:`_load_cursors` parses it into the
   ``{"workspaces": {alias: SlackCursorState}}`` envelope. ``None``
   yields the empty envelope (no workspaces). Inside each alias entry a
   pre-Phase-22 state lacking the ``backfill`` axis is tolerated and
   defaulted to empty (ADR-0038 §(g)).
3. :meth:`SlackConnector.sync` loops the configured workspaces
   serially. Each workspace's ``channels`` axis is handed to a fresh
   fetcher via ``cursor_per_channel=``. As the fetcher yields
   ``(channel_id, message, new_cursor)`` triples we update the
   in-memory ``channels`` dict per yield and forward the mapped
   ``SourceObserved`` to :meth:`SourceService.observe`. The dict
   advances in lock-step with the commit so a crash mid-loop loses at
   most one message worth of progress.
4. After **each workspace** completes (success or failure) the envelope
   is checkpointed via ``cursor_set(sync_started=True)`` (per-workspace
   error isolation, ADR-0041 §(b)) — a later workspace's failure can
   never roll back an earlier workspace's progress, because the CLI
   driver only persists the terminal cursor on a normal return.
5. After every workspace drains we serialise the envelope back to JSON
   and hand it to the CLI driver as :attr:`SyncResult.new_cursor`. If
   any workspace failed, a :class:`SlackWorkspaceSyncError` naming the
   failed aliases is raised instead (non-zero exit; the succeeded
   workspaces' cursors are already checkpointed).

The cursor JSON is opaque to the driver and projection — both treat it
as a single string. ``opshub slack status --verbose`` pretty-prints the
parsed envelope.

Schema migration (pre-userbase, no compat)
------------------------------------------

The pre-Phase-24 cursor was a single top-level compound
(``{"channels": ..., "backfill": ..., "threads": ..., "team_id": ...}``,
Phase 20-B/22-B/23-H shape) with no workspace nest; before that, a flat
``dict[channel_id, ts]``. Phase 24-C is a hard schema flip: the
:func:`_load_cursors` parser rejects **both** legacy shapes with a
:class:`ConfigError` that directs the operator to ``opshub slack cursor
reset --all`` (per opshub's pre-userbase stance — see ``AGENTS.md`` §
"設計判断のスタンス"; Phase 23-A [#531](
https://github.com/ozzy-labs/opshub/issues/531) established that
``opshub projections rebuild`` is a dead-end for cursor recovery because
replay restores the same value). We do not silently coerce: the legacy
shapes carry no workspace alias, so any lift would have to invent one —
and the ADR-0041 §(e) upgrade path is a DB re-init anyway (the
``external_id`` re-key of Phase 24-B makes old rows unreachable).

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

Partial-progress checkpoint (issue #339, Bug 2)
------------------------------------------------

``SourceService.observe`` commits ``SourceObserved`` + ``ItemEnqueued``
per message inside its own transaction, but the CLI driver only
writes the resume cursor via ``cursor_set(sync_started=False)``
**after** :meth:`sync` returns normally. If the fetcher raises
mid-iteration (e.g. :class:`ConnectorFailedError` on a Slack-side
``rate_limited`` after the budget is exhausted, or a
:class:`KeyboardInterrupt` from a manually-aborted run), the cursor
stays at the prior run's value while a non-zero number of messages
have already been committed to ``sources`` / ``inbox_items`` — so
the next sync re-fetches them, re-observes them, and (because
``ItemEnqueued`` is not deduplicated per source_ref by the projection)
inflates ``inbox_items`` on every aborted-then-retried run. That is
the second half of the issue #339 cascade.

We close this gap with the **per-workspace checkpoint** (Phase 24-C,
ADR-0041 §(b)): the workspace loop persists the envelope via
``cursor_set(sync_started=True)`` in a per-iteration ``finally`` arm —
after every workspace, success or failure (including
``KeyboardInterrupt``). The
:class:`~opshub.projections.connector_cursors.ConnectorCursorsProjection`
reducer upserts ``cursor_value`` on every ``ConnectorSyncStarted``
event, so the partial-progress write is immediately visible to the
next sync — bounding the re-fetch window on retry to "messages that
threw mid-iteration", not "everything since the last successful
sync". The same mechanism delivers per-workspace error isolation: a
later workspace's failure aborts the driver's terminal cursor write,
but the earlier workspaces' progress is already checkpointed.

The all-clean happy path skips the final checkpoint (the serialised
value would be byte-identical to the terminal
``ConnectorSyncCompleted`` the CLI driver writes — emitting both would
be event-log noise). Idempotency on the failure path: writing the
same cursor twice (once via the partial checkpoint, once via the
CLI's ``record_sync_failure`` arm) is safe because
``record_sync_failure`` itself does not touch the cursor —
:class:`~opshub.domain.events.ConnectorSyncFailed` is a deliberate
no-op in the cursor projection (phase-3-plan §4 Q3).

Configuration source
--------------------

Workspaces + channels are read from
``[connectors.slack.workspaces.<alias>]`` tables (or the
``OPSHUB_CONNECTORS__SLACK__WORKSPACES__<ALIAS>__CHANNELS`` env vars)
on :class:`~opshub.core.config.OpsHubSettings` (Phase 24-C, ADR-0041
§(c)). ``enabled = false`` is the default per phase-7-plan §1 #2
(opt-in by design) but the CLI driver treats the connector as runnable
as soon as it is registered — the ``enabled`` flag is informational
for downstream wiring (Phase 7.x scheduler / autopilot will respect
it). We treat "no workspaces configured" (and a per-workspace empty
channel list) as a no-op with a structured log warning rather than a
hard error so an operator who misconfigured ``[connectors.slack]``
sees an actionable event in the log instead of a stack trace.

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
from typing import TYPE_CHECKING, Any, TypedDict, cast

from opshub.connectors.base import SyncResult
from opshub.connectors.slack.auth import SlackAuth
from opshub.connectors.slack.fetcher import SlackFetcher
from opshub.connectors.slack.mapper import map_message
from opshub.core.errors import ConfigError, ConnectorFailedError
from opshub.core.time import now_utc, parse_since, since_to_ts

if TYPE_CHECKING:
    from datetime import timedelta

    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.slack.fetcher import RawSlackMessage
    from opshub.core.config import (
        SlackChannelSpec,
        SlackConnectorSettings,
        SlackWorkspaceSettings,
    )
    from opshub.core.excludes import ExcludeRules


__all__ = [
    "SlackConnector",
    "SlackCursorEnvelope",
    "SlackCursorState",
    "SlackWorkspaceSyncError",
]

#: Slack ``ts`` sentinel meaning "covered down to the beginning of channel
#: history" (Phase 22-D, [ADR-0038](docs/adr/0038-slack-sync-gap-backfill.md)
#: §(d)). Used as the per-channel ``backfill`` low-water mark when a
#: channel is (or was) synced with no date floor — the cold-start fetched
#: everything, so there is nothing older to backfill. ``since_to_ts`` renders
#: the Unix epoch as exactly this string (``f"{0.0:.6f}"``).
_EPOCH_TS = "0.000000"


# pyright/mypy: ``timedelta`` is imported under ``TYPE_CHECKING`` for the
# helper signatures; the runtime body lazy-imports it inside the helper.


class SlackCursorState(TypedDict):
    """Compound resume-cursor shape persisted in ``connector_cursors.cursor_value``.

    Phase 20-B ([epic #465](https://github.com/ozzy-labs/opshub/issues/465))
    schema. Two axes share one JSON object so the projection's TEXT column
    keeps a single row per connector (no schema migration on
    ``connector_cursors`` itself):

    * ``channels``: ``{channel_id: max_ts}`` — the per-channel forward
      **high-water** mark. Drives ``conversations.history`` resume
      (Phase 7 semantics, unchanged).
    * ``backfill``: ``{channel_id: low_water_ts}`` — the per-channel
      **low-water** mark added in Phase 22 ([ADR-0038](
      https://github.com/ozzy-labs/opshub/issues/516)). Records "the
      oldest ts boundary this channel has been fetched down to" so a
      later floor lowering can fetch only the newly-uncovered gap
      ``(floor_new, low_water]`` (disjoint from the forward set). Phase
      22-B establishes the schema only; the low-water lifecycle +
      gap-backfill pass land in 22-D, so this axis is always written
      empty by the current code.
    * ``threads``: ``{f"{channel_id}:{thread_ts}": last_reply_ts}`` —
      drives the per-thread ``conversations.replies(oldest=...)``
      increment path established in Phase 20-C. Phase 20-B writes this
      axis as an empty dict; 20-C populates it.

    The TypedDict is the source of truth for the on-disk shape; pyright /
    mypy strict catch axis name typos at call sites.
    """

    channels: dict[str, str | None]
    backfill: dict[str, str | None]
    threads: dict[str, str | None]
    # Phase 23-H ([#538](https://github.com/ozzy-labs/opshub/issues/538),
    # ADR-0039; per-alias since Phase 24-C, ADR-0041 §(a)): the Slack
    # workspace ``team_id`` this alias's cursor entry is bound to.
    # ``None`` = not yet bound (first sync for the alias, or after a
    # cursor reset). The sync hot path binds it on the first sync and
    # rejects a later token swap to a different workspace. This is a
    # *scalar* axis (unlike the three dict axes above); callers that
    # iterate the dict axes must enumerate them explicitly, not via
    # ``.values()`` / ``for axis in cursors``.
    team_id: str | None


class SlackCursorEnvelope(TypedDict):
    """Top-level persisted cursor shape (Phase 24-C, ADR-0041 §(d)).

    One :class:`SlackCursorState` per configured workspace alias. The
    ``connector_cursors`` projection row stays a single TEXT value — the
    nest lives inside the JSON. Aliases present in the envelope but no
    longer configured are preserved verbatim (an operator who removes a
    workspace from ``opshub.toml`` and re-adds it later resumes where
    they left off); ``opshub slack cursor reset --workspace <alias>
    --all`` drops a stale entry explicitly.
    """

    workspaces: dict[str, SlackCursorState]


class SlackWorkspaceSyncError(ConnectorFailedError):
    """Aggregate failure for one or more workspaces in a multi-workspace sync.

    Phase 24-C ([ADR-0041](docs/adr/0041-slack-multi-workspace.md) §(b)):
    the sync loop isolates per-workspace failures — every configured
    workspace gets its attempt and a cursor checkpoint — then raises
    this aggregate at the end so the run still exits non-zero. The
    message lists the failed aliases (+ per-alias exception type) for
    the stderr trail; :attr:`failure_event_detail` carries the same
    alias list for the ``ConnectorSyncFailed`` event message (the
    shared CLI driver appends it to the type name — aliases are
    operator-chosen labels, never secrets, so the event log's redaction
    posture is unchanged).
    """

    def __init__(self, message: str, *, failed_aliases: list[str]) -> None:
        super().__init__(message)
        self.failed_aliases = list(failed_aliases)
        self.failure_event_detail = f"failed workspace(s): {', '.join(failed_aliases)}"


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
        """Run one Slack sync pass over every configured workspace.

        Phase 24-C ([ADR-0041](docs/adr/0041-slack-multi-workspace.md)
        §(b)): the configured ``[connectors.slack.workspaces.<alias>]``
        tables are synced **serially** with per-workspace error
        isolation — each workspace gets its own auth resolution, bind
        guard, fetch pass, and a cursor checkpoint
        (``cursor_set(sync_started=True)``) before the loop moves on,
        so one workspace's failure (expired token, missing scope, ...)
        never rolls back another's progress. After the loop, any
        failures aggregate into a :class:`SlackWorkspaceSyncError`
        naming the failed aliases (non-zero exit).

        Each yielded message is forwarded to
        :meth:`SourceService.observe` (which atomically appends a
        :class:`SourceObserved` + :class:`ItemEnqueued` event pair),
        and the per-channel cursor dict is updated **after** the
        observe call so a failure mid-iteration leaves the cursor
        pointing at the last successfully-committed message (the
        same at-most-once-or-no-loss posture pinned by the A2
        fetcher's module docstring).
        """
        slack_settings = self._resolve_slack_settings()
        workspaces = dict(slack_settings.workspaces)
        if not workspaces:
            # No workspaces configured is a degraded-but-not-failing
            # state: the connector may be enabled but the operator has
            # not added any [connectors.slack.workspaces.<alias>] table
            # yet. We log a structured warning and return a no-op
            # SyncResult that preserves the prior cursor — mirrors the
            # GitHub connector's "no observed items → keep prior cursor"
            # contract pinned by ``test_empty_sync_preserves_cursor``.
            context.logger.warning(
                "slack connector: no workspaces configured; skipping sync. "
                "Add a [connectors.slack.workspaces.<alias>] table with a "
                "channels list to opshub.toml (ADR-0041) to enable."
            )
            return SyncResult(observed_count=0, new_cursor=context.cursor_value)

        # Phase 24-C CLI seam: ``opshub slack sync --workspace <alias>``
        # narrows the run to one workspace via the
        # ``sync_workspace_filter`` setting (env-shim, see config.py).
        filter_alias = slack_settings.sync_workspace_filter
        if filter_alias is not None:
            if filter_alias not in workspaces:
                raise ConfigError(
                    f"unknown Slack workspace alias {filter_alias!r}; "
                    f"configured workspaces: {', '.join(sorted(workspaces)) or '(none)'}"
                )
            aliases = [filter_alias]
        else:
            aliases = sorted(workspaces)

        # Phase 10 (ADR-0020 §(b)): shared ingest excludes. Slack honours
        # the ``channels`` and ``senders`` selectors — a message in an
        # excluded channel, or from an excluded sender, is never observed
        # (the cursor still advances so the connector does not re-scan it
        # forever). Phase 24-C (ADR-0041 §(j)): entries may carry an
        # ``<alias>/`` workspace qualifier; ``scoped_to_workspace`` below
        # resolves them per workspace. ``load_excludes()`` resolves the
        # file path via ``default_config_dir()`` directly so we avoid
        # threading ``OpsHubSettings`` through this path — tests that
        # patch ``OpsHubSettings`` at the class level would otherwise
        # hand us a MagicMock whose ``config_dir`` attribute is itself a
        # MagicMock that ``yaml.safe_load`` would iterate forever over.
        from opshub.core.excludes import load_excludes

        excludes = load_excludes()

        envelope = _load_cursors(context.cursor_value)
        last_persisted = context.cursor_value
        failures: dict[str, BaseException] = {}
        bound_teams: dict[str, str] = {}
        observed_total = 0

        for index, alias in enumerate(aliases):
            workspace = workspaces[alias]
            state = envelope["workspaces"].get(alias)
            if state is None:
                state = _empty_state()
                envelope["workspaces"][alias] = state
            workspace_ok = False
            try:
                observed_total += self._sync_workspace(
                    context,
                    alias=alias,
                    workspace=workspace,
                    settings=slack_settings,
                    state=state,
                    excludes=excludes.scoped_to_workspace(alias),
                    bound_teams=bound_teams,
                )
                workspace_ok = True
            except Exception as exc:
                # Per-workspace error isolation (ADR-0041 §(b)): record
                # and continue so the remaining workspaces still get
                # their attempt. The aggregate raise below keeps the run
                # non-zero. The structured log carries the per-alias
                # detail — scrubbed through ``sanitise_error_message``
                # because this is a new emission path that bypasses the
                # CLI driver's sanitise rail (Slack error strings can
                # echo request fragments); the aggregate message carries
                # type names only.
                from opshub.core.sanitise import sanitise_error_message

                failures[alias] = exc
                context.logger.warning(
                    f"slack connector: workspace {alias!r} sync failed: "
                    f"{type(exc).__name__}: {sanitise_error_message(str(exc))}"
                )
            finally:
                # Checkpoint the envelope after every workspace —
                # success or failure — so a later workspace's failure
                # (which aborts the CLI driver's terminal cursor_set)
                # never loses an earlier workspace's progress, and a
                # mid-fetch crash inside this workspace keeps the
                # messages it got through (issue #339 Bug 2 closure,
                # carried into the loop form). The one skip: the final
                # workspace on an all-clean run — the driver's terminal
                # ``ConnectorSyncCompleted`` pins the same value, so the
                # extra ``ConnectorSyncStarted`` would be event-log
                # noise. ``workspace_ok`` is False for non-Exception
                # exits too (KeyboardInterrupt), so an operator abort
                # still checkpoints before propagating.
                is_final_clean = workspace_ok and not failures and index == len(aliases) - 1
                if not is_final_clean:
                    last_persisted = self._checkpoint(context, envelope, last_persisted)

        if failures:
            summary = ", ".join(
                f"{alias} ({type(failures[alias]).__name__})" for alias in sorted(failures)
            )
            raise SlackWorkspaceSyncError(
                f"slack sync failed for {len(failures)} of {len(aliases)} "
                f"workspace(s): {summary}. Succeeded workspaces' cursors were "
                "checkpointed; fix the failing workspace(s) and re-run "
                "`opshub slack sync`.",
                failed_aliases=sorted(failures),
            )

        return SyncResult(observed_count=observed_total, new_cursor=_dump_cursors(envelope))

    def _checkpoint(
        self, context: ConnectorContext, envelope: SlackCursorEnvelope, last_persisted: str | None
    ) -> str:
        """Persist the envelope via ``cursor_set(sync_started=True)`` if it moved.

        The ``ConnectorCursorsProjection`` reducer upserts
        ``cursor_value`` on every ``ConnectorSyncStarted`` event, so the
        checkpoint is immediately visible to the next sync. Writing is
        skipped when the serialised envelope is byte-identical to the
        last persisted value (no progress → no redundant event).
        Returns the serialised value for the caller to thread back in.
        """
        value = _dump_cursors(envelope)
        if value != last_persisted:
            # ``context.source_service`` is the CLI's
            # ``_ProgressSourceProxy`` (or the raw ``SourceService``
            # under unit-test fixtures); both forward ``cursor_set``
            # via ``__getattr__`` so this call is transparent.
            context.source_service.cursor_set(self.name, value, sync_started=True)
        return value

    def _sync_workspace(
        self,
        context: ConnectorContext,
        *,
        alias: str,
        workspace: SlackWorkspaceSettings,
        settings: SlackConnectorSettings,
        state: SlackCursorState,
        excludes: ExcludeRules,
        bound_teams: dict[str, str],
    ) -> int:
        """Sync one workspace's channels; returns the observed count.

        The body is the pre-Phase-24 single-workspace sync verbatim,
        operating on this alias's :class:`SlackCursorState` (mutated in
        place — the caller checkpoints the enclosing envelope). Raises
        on any failure; the caller isolates it per ADR-0041 §(b).
        """
        specs = list(workspace.channels)
        if not specs:
            # Empty per-workspace channel list: degraded-but-not-failing,
            # same posture as the pre-24 connector-wide empty list.
            context.logger.warning(
                f"slack connector: workspace {alias!r} has no channels "
                "configured; skipping. Populate "
                f"[connectors.slack.workspaces.{alias}] channels in "
                "opshub.toml to enable."
            )
            return 0

        channels = [spec.id for spec in specs]
        # Phase 20 (ADR-0036) / Phase 24-C (ADR-0041 §(c)): resolve the
        # per-channel date floor with 3-step precedence — channel
        # ``since`` → workspace ``sync_since`` → connector-wide
        # ``sync_since``. Relative floors (``"90d"``) are evaluated *now*
        # (sync time), not at config load.
        default_since = (
            workspace.sync_since if workspace.sync_since is not None else settings.sync_since
        )
        floors = _resolve_floors(specs, default_since)

        auth = SlackAuth(alias)
        # Per-alias bind guard (ADR-0041 §(a)): resolve the live
        # workspace team_id, reject duplicate registrations (two aliases
        # resolving to the same workspace would corrupt cursor / digest
        # semantics), then bind or verify this alias's cursor entry —
        # all **before any fetch**, so a token swapped to a different
        # workspace can never let foreign messages into the
        # ``external_id`` namespace.
        current_team = _resolve_live_team_id(auth, alias=alias)
        duplicate = next((a for a, t in bound_teams.items() if t == current_team), None)
        if duplicate is not None:
            raise ConfigError(
                f"Slack workspaces {duplicate!r} and {alias!r} both resolve to "
                f"team_id {current_team!r}; each workspace may be registered "
                "under exactly one alias (ADR-0041 §(a)). Remove one of the "
                "two [connectors.slack.workspaces.*] tables (or fix the "
                "stored tokens)."
            )
        team_id = _bind_or_check_workspace(state, current_team=current_team, alias=alias)
        bound_teams[alias] = team_id
        fetcher = SlackFetcher(auth, channels=channels, team_id=team_id)
        # Phase 20-B: the fetcher signature takes ``cursor_per_channel=``
        # (a flat ``dict[str, str | None]``). We hand it the ``channels``
        # axis directly and mutate that dict per yield.
        channel_cursors = state["channels"]
        # Phase 20 (ADR-0036 §(g)): the fetch resume bound is the *later*
        # of the persisted cursor and the date floor, so a floor only ever
        # advances ``oldest`` forward — it never rewinds past a cursor
        # (cursor is authoritative; partial-sync resume stays safe). We
        # build a separate ``resume`` map from the compound channels axis
        # and leave ``channel_cursors`` as the persistence accumulator:
        # the floor bounds what we *fetch*, not the message ts we
        # *persist*. Channels without a floor keep their raw channels-axis
        # entry (including the first-sync "absent key" shape the
        # connector-test contract pins), so a no-floor sync persists the
        # same compound state as before.
        resume: dict[str, str | None] = dict(channel_cursors)
        for channel_id in channels:
            floor = floors.get(channel_id)
            if floor is not None:
                resume[channel_id] = _max_ts(channel_cursors.get(channel_id), floor)
        # Phase 22-D (ADR-0038): per-channel low-water lifecycle. The
        # ``backfill`` axis records the oldest ts each channel has been
        # fetched down to (the invariant: ``(backfill[ch], channels[ch]]``
        # is fully covered).
        backfill_cursors = state["backfill"]
        backfill_enabled = settings.backfill_on_floor_lower
        # ``gap_targets[ch] = target_low`` for channels whose floor was
        # lowered below the recorded low-water mark — the Phase 0 pass
        # (below) fetches the newly-uncovered window for each.
        gap_targets: dict[str, str] = {}
        for channel_id in channels:
            floor = floors.get(channel_id)
            if channel_id not in channel_cursors:
                # Cold-start. A *floored* cold-start resumes from
                # ``oldest = floor`` and so ends up covered down to the
                # floor regardless of how far the forward pass gets this
                # run — record that low-water so a later floor reduction
                # can detect the gap. A *no-floor* cold-start covers the
                # whole history; we leave the ``backfill`` axis absent
                # (an absent entry ≡ epoch low-water = "fully backfilled",
                # nothing older to ever backfill).
                if floor is not None:
                    backfill_cursors[channel_id] = floor
                continue
            if channel_id not in backfill_cursors:
                # An already-synced channel with no recorded low-water:
                # either a pre-Phase-22 channel (historical floor
                # unrecoverable) or one whose floor was added after an
                # earlier no-floor sync (already covers more than any
                # floor). Either way we do NOT auto-backfill (ADR-0038
                # §(e)) and leave the axis absent; a one-off catch-up uses
                # ``opshub slack cursor backfill``.
                continue
            # Post-feature steady state with a recorded low-water: detect a
            # *lowered* (or removed) floor. ``target_low`` is the new floor
            # ts, or the epoch sentinel when the floor was removed (the
            # operator now wants the full history, so the gap spans down to
            # the beginning). Relative floors walk forward so they never
            # trip this; only an explicit reduction / removal does.
            low_water = backfill_cursors[channel_id]
            target_low = floor if floor is not None else _EPOCH_TS
            if backfill_enabled and low_water is not None and _ts_lt(target_low, low_water):
                gap_targets[channel_id] = target_low
        # Phase 20-C: the per-thread late-reply cursors. The
        # activity-window prune runs only on the happy path (after both
        # phases drain) so a mid-iteration crash preserves every thread
        # cursor — the next sync re-evaluates the window from scratch.
        thread_cursors = state["threads"]
        observed_count = 0
        # Phase 0: gap backfill (Phase 22-D, ADR-0038 §(d)). For each
        # channel whose floor was lowered below its recorded low-water
        # mark, fetch the newly-uncovered window ``(target_low,
        # low_water]`` — disjoint from the forward set ``(low_water,
        # now]`` so no already-observed message is re-ingested (no
        # inbox inflation). Run before the forward pass so the threads
        # axis it seeds is visible to the Phase 2 polling path below.
        if gap_targets:
            # The upper bound is the *current* (pre-update) low-water
            # mark; capture it before advancing ``backfill`` after the
            # pass drains.
            gap_oldest: dict[str, str | None] = dict(gap_targets)
            gap_latest: dict[str, str | None] = {
                channel_id: backfill_cursors[channel_id] for channel_id in gap_targets
            }
            context.logger.warning(
                f"slack connector: date floor lowered for {len(gap_targets)} "
                f"channel(s); backfilling the newly-uncovered window "
                f"(one-time catch-up). channels={sorted(gap_targets)}"
            )
            gap_fetcher = SlackFetcher(auth, channels=list(gap_targets), team_id=team_id)
            for channel_id, raw_message, new_cursor in gap_fetcher.fetch_messages(
                cursor_per_channel=gap_oldest,
                latest_per_channel=gap_latest,
                excludes=excludes,
            ):
                if _ingest_yield(
                    channel_id,
                    raw_message,
                    new_cursor,
                    channel_cursors=channel_cursors,
                    thread_cursors=thread_cursors,
                    excludes=excludes,
                    source_service=context.source_service,
                ):
                    observed_count += 1
            # Gap fully drained → advance the low-water mark down to the
            # new floor. Done only after the loop completes so a mid-gap
            # crash leaves ``backfill`` at the prior low-water and the
            # next sync re-attempts the whole window (resume-safe; the
            # re-observed overlap is bounded, idempotent on ``sources``,
            # and healed on inbox by #522).
            for channel_id in gap_targets:
                backfill_cursors[channel_id] = gap_targets[channel_id]

        # Phase 1: ``conversations.history`` (top-level + initial
        # thread snapshot). Phase 20-A wired the initial snapshot
        # into the fetcher's yield stream; the connector's only
        # extra job here is to populate the threads axis so the
        # Phase 2 polling path (below) knows which threads to
        # poll on the *next* sync. The per-yield bookkeeping (cursor
        # advance, threads axis, excludes, observe) is shared with the
        # Phase 0 gap pass via :func:`_ingest_yield`.
        for channel_id, raw_message, new_cursor in fetcher.fetch_messages(
            cursor_per_channel=resume,
            excludes=excludes,
        ):
            if _ingest_yield(
                channel_id,
                raw_message,
                new_cursor,
                channel_cursors=channel_cursors,
                thread_cursors=thread_cursors,
                excludes=excludes,
                source_service=context.source_service,
            ):
                observed_count += 1

        # Phase 2: late-reply polling (Phase 20-C). For every
        # thread the connector knows about that's still inside
        # the activity window, call
        # ``conversations.replies(oldest=last_reply_ts)`` to pick
        # up any replies the workspace landed since the cursor
        # was last advanced. Threads outside the window are
        # pruned after the polling drains so the ``threads`` axis
        # stays bounded by the operator-tunable window (default
        # 30 days, ADR-0030 §(d) revised).
        # Phase 24-C (ADR-0041 §(c)): the window resolves 2-step —
        # workspace-level ``thread_activity_window`` override, falling
        # back to the connector-wide value (symmetric with the floor).
        from opshub.core.config import resolve_slack_thread_activity_window

        window_cutoff_ts = _window_cutoff_ts(
            resolve_slack_thread_activity_window(workspace, settings)
        )
        # Iterate a snapshot of the keys because the polling loop
        # below may add new entries (a thread whose cursor was
        # initialised at ``latest_reply`` and has zero new replies
        # still leaves its key unchanged) — but never modifies the
        # iteration set.
        polling_keys = list(thread_cursors.keys())
        for thread_key in polling_keys:
            last_reply_ts = thread_cursors.get(thread_key)
            if not _within_activity_window(last_reply_ts, window_cutoff_ts):
                # Out-of-window threads are pruned below. Skip the
                # API round-trip; ``conversations.replies`` would
                # still cost a Tier-3 budget slot per cold thread.
                continue
            parsed = _parse_thread_cursor_key(thread_key)
            if parsed is None:
                # Malformed key (operator hand-edit accident). Skip
                # and let the next sync's cursor parse error
                # (``_load_cursors``) surface the problem.
                continue
            channel_id, thread_ts = parsed
            if excludes.excludes_channel(channel_id):
                # Same short-circuit as Phase 1: an excluded
                # channel's replies would be filtered out at the
                # per-yield guard below anyway; skipping the API
                # call saves a Tier-3 budget slot.
                continue
            for reply_message in fetcher.fetch_thread_replies(
                channel_id=channel_id,
                thread_ts=thread_ts,
                oldest_reply_ts=last_reply_ts,
            ):
                # Advance the threads cursor before the observe
                # call so a partial-progress crash mid-thread
                # still records the reply we got through. The
                # parent ``new_cursor`` element on the channels
                # axis (Phase 1) intentionally stays anchored to
                # the parent ts; threads cursor advancement is
                # the *only* signal the polling phase persists,
                # which is why ``fetch_thread_replies`` yields a
                # bare :class:`RawSlackMessage` (no cursor triple).
                thread_cursors[thread_key] = _max_ts(
                    thread_cursors.get(thread_key), reply_message.ts
                )
                if excludes.excludes_channel(reply_message.channel_id) or excludes.excludes_sender(
                    reply_message.user_id
                ):
                    continue
                reply_kwargs = map_message(reply_message)
                context.source_service.observe(**reply_kwargs)
                observed_count += 1

        # Prune out-of-window thread cursors so the ``threads``
        # axis stays bounded. We do this on the happy path only —
        # a mid-iteration crash skips this line (the caller's
        # per-workspace checkpoint persists the partial state) so the
        # next sync's prune pass re-evaluates the window cleanly.
        # Pruning earlier would race with the iteration above; pruning
        # later is safe because :func:`_window_cutoff_ts` is captured
        # once at sync time.
        _prune_inactive_threads(thread_cursors, window_cutoff_ts)
        return observed_count

    def backfill_channel(
        self,
        context: ConnectorContext,
        *,
        alias: str,
        channel_id: str,
        since_ts: str,
        until_ts: str,
    ) -> SyncResult:
        """Explicitly backfill one channel's ``(since_ts, until_ts]`` window.

        Phase 22-E ([ADR-0038](docs/adr/0038-slack-sync-gap-backfill.md)
        §(f)): the manual counterpart to the automatic Phase 0 gap pass,
        driven by ``opshub slack cursor backfill``. Unlike :meth:`sync`
        this does **not** run a forward pass or thread polling — it fetches
        exactly the operator-specified window via the Phase 22-C bounded
        fetch (``oldest=since_ts, latest=until_ts``), observes each message
        through the shared :func:`_ingest_yield` path, and advances the
        channel's ``backfill`` low-water mark down to ``since_ts``.

        The primary use is the pre-feature channel rescue: an operator who
        synced a channel before the gap-backfill feature landed (so its
        low-water is unrecorded) supplies the old floor as ``until_ts`` and
        the desired new floor as ``since_ts`` to pull exactly the missing
        window — disjoint from the already-covered region, so no inbox
        inflation.

        The caller (the CLI) owns the cursor bracket: it passes the current
        cursor as ``context.cursor_value`` and persists
        :attr:`SyncResult.new_cursor` via ``cursor_set`` after this returns.
        ``channels`` / ``threads`` axes advance via ``_ingest_yield`` exactly
        as in :meth:`sync` (the forward high-water never regresses thanks to
        the :func:`_max_ts` guard; gap parents seed the threads axis for the
        next sync's polling).
        """
        from opshub.core.excludes import load_excludes

        auth = SlackAuth(alias)
        excludes = load_excludes().scoped_to_workspace(alias)

        envelope = _load_cursors(context.cursor_value)
        state = envelope["workspaces"].get(alias)
        if state is None:
            state = _empty_state()
            envelope["workspaces"][alias] = state
        # Phase 24-B (ADR-0041 §(a) §(i)): the bind guard now covers the
        # explicit backfill path too — it previously ran only inside
        # :meth:`sync`, so a token swapped to a different workspace could
        # slip a foreign workspace's history in via ``cursor backfill``.
        # The guard runs before any fetch (foreign data never enters the
        # DB) and resolves the ``team_id`` the fetcher stamps onto every
        # message for the 3-token ``external_id``. Phase 24-C: the guard
        # operates on this alias's cursor entry.
        current_team = _resolve_live_team_id(auth, alias=alias)
        team_id = _bind_or_check_workspace(state, current_team=current_team, alias=alias)
        fetcher = SlackFetcher(auth, channels=[channel_id], team_id=team_id)
        channel_cursors = state["channels"]
        thread_cursors = state["threads"]
        backfill_cursors = state["backfill"]

        observed_count = 0
        for ch, raw_message, new_cursor in fetcher.fetch_messages(
            cursor_per_channel={channel_id: since_ts},
            latest_per_channel={channel_id: until_ts},
            excludes=excludes,
        ):
            if _ingest_yield(
                ch,
                raw_message,
                new_cursor,
                channel_cursors=channel_cursors,
                thread_cursors=thread_cursors,
                excludes=excludes,
                source_service=context.source_service,
            ):
                observed_count += 1

        # Advance the low-water mark down to the backfilled floor. As with
        # the auto gap pass, this is done only after the fetch drains so a
        # crash leaves the prior low-water and a re-run re-attempts the
        # whole window.
        backfill_cursors[channel_id] = since_ts
        return SyncResult(observed_count=observed_count, new_cursor=_dump_cursors(envelope))

    def _resolve_slack_settings(self) -> SlackConnectorSettings:
        """Return the resolved ``[connectors.slack]`` settings sub-model.

        Lazy-imports :mod:`opshub.core.config` so the connectors
        package import path stays free of pydantic-settings — cold
        start (ADR-0001) only pays for this when the operator
        actually runs ``opshub slack sync``. The returned model carries
        both ``channels`` (each a :class:`SlackChannelSpec`) and the
        connector-wide ``sync_since`` floor (Phase 20, ADR-0036).
        """
        from opshub.core.config import OpsHubSettings

        return OpsHubSettings().connectors.slack


#: Verbatim legacy-shape reject message (Phase 24-C, ADR-0041 §(d)).
#: ``docs/troubleshooting.md`` / ``docs/upgrading.md`` ship this string
#: for grep-based discovery, so keep the literal stable.
_LEGACY_CURSOR_MESSAGE = (
    "Slack cursor predates the Phase 24 per-workspace schema "
    '({"workspaces": {"<alias>": {...}}}, ADR-0041). Run '
    "`opshub slack cursor reset --all` to drop it and cold-start "
    "(the ADR-0041 §(e) upgrade path is a DB re-init + full re-sync "
    "anyway — the Phase 24-B external_id re-key orphans pre-24 rows). "
    "opshub is pre-userbase and ships no silent migration."
)


def _load_cursors(cursor_value: str | None) -> SlackCursorEnvelope:
    """Parse the persisted JSON cursor into the Phase 24-C envelope shape.

    ``None`` means "first sync — no cursors yet" and yields the empty
    envelope (no workspaces). A malformed JSON string or a
    schema-violating shape raises :class:`ConfigError` so the operator
    sees an actionable error rather than a silently re-fetched history.

    Schema (Phase 24-C, [ADR-0041](docs/adr/0041-slack-multi-workspace.md)
    §(d)):

    .. code-block:: json

       {"workspaces": {"<alias>": {"channels": {...}, "backfill": {...},
                                   "threads": {...}, "team_id": "T..."}}}

    Both legacy shapes — the Phase 20-B/23-H top-level compound
    (``{"channels": ..., "threads": ..., "team_id": ...}``) and the
    pre-20-B flat per-channel dict — are **rejected** with a
    migration-prompt :class:`ConfigError` pointing at ``opshub slack
    cursor reset --all`` (the posture Phase 20-B established and Phase
    23-A [#531](https://github.com/ozzy-labs/opshub/issues/531)
    corrected the prompt target for: ``opshub projections rebuild`` is
    a dead-end because replay restores the same payload). opshub is
    pre-userbase (``AGENTS.md`` §"設計判断のスタンス"), so we do not
    silently coerce — a lift would have to invent a workspace alias,
    and the ADR-0041 §(e) upgrade path is a DB re-init regardless.

    Inside each alias entry, ``channels`` / ``threads`` are required
    axes; ``backfill`` is additive-tolerated (ADR-0038 §(g)) and the
    scalar ``team_id`` defaults to ``None`` (unbound) when absent or
    malformed.
    """
    if cursor_value is None:
        return _empty_envelope()
    try:
        parsed = json.loads(cursor_value)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "Slack cursor is not valid JSON; the connector_cursors "
            "row may have been hand-edited. Reset with "
            "`opshub slack cursor reset --all` to recover."
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            "Slack cursor must be a JSON object with a 'workspaces' "
            f"nest; got {type(parsed).__name__}. "
            "Reset with `opshub slack cursor reset --all` to recover."
        )
    # ``cast`` narrows pyright's ``Unknown`` widen on the ``json.loads``
    # return; mypy treats the inner cast as redundant (the cast is from
    # ``Any`` to ``dict[Any, Any]``) so we suppress only mypy.
    parsed_dict = cast(  # type: ignore[redundant-cast]
        dict[Any, Any], parsed
    )
    if "workspaces" not in parsed_dict:
        # Either the Phase 20-B/23-H top-level compound or the pre-20-B
        # flat dict — both predate the Phase 24 nest and are rejected
        # with the same prompt (the recovery is identical).
        raise ConfigError(_LEGACY_CURSOR_MESSAGE)
    workspaces_raw = parsed_dict["workspaces"]
    if not isinstance(workspaces_raw, dict):
        raise ConfigError(
            "Slack cursor 'workspaces' nest must be a JSON object; got "
            f"{type(workspaces_raw).__name__}. Reset with "
            "`opshub slack cursor reset --all` to recover."
        )
    workspaces_dict = cast(  # type: ignore[redundant-cast]
        dict[Any, Any], workspaces_raw
    )
    workspaces: dict[str, SlackCursorState] = {}
    for alias, raw_state in workspaces_dict.items():
        if not isinstance(alias, str) or not alias:
            raise ConfigError(
                "Slack cursor workspace aliases must be non-empty strings; "
                f"got {alias!r}. Reset with `opshub slack cursor reset --all` "
                "to recover."
            )
        workspaces[alias] = _load_workspace_state(raw_state, alias=alias)
    return SlackCursorEnvelope(workspaces=workspaces)


def _load_workspace_state(raw: Any, *, alias: str) -> SlackCursorState:
    """Parse one alias's nested cursor entry into :class:`SlackCursorState`.

    The inner shape is the pre-Phase-24 compound verbatim (axes +
    lifecycle unchanged, ADR-0041 §(d)): ``channels`` / ``threads``
    required, ``backfill`` additive-tolerated (ADR-0038 §(g)),
    ``team_id`` scalar defaulting to ``None`` (unbound — the next sync
    binds it; a non-string hand-edit is tolerated as ``None`` because
    re-binding is harmless).
    """
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Slack cursor workspace entry {alias!r} must be a JSON object; "
            f"got {type(raw).__name__}. Reset with "
            "`opshub slack cursor reset --all` to recover."
        )
    raw_dict = cast(  # type: ignore[redundant-cast]
        dict[Any, Any], raw
    )
    if "channels" not in raw_dict or "threads" not in raw_dict:
        raise ConfigError(
            f"Slack cursor workspace entry {alias!r} lacks the required "
            "'channels' / 'threads' axes. Reset with "
            "`opshub slack cursor reset --all` to recover."
        )
    channels_axis = _coerce_axis(raw_dict["channels"], axis_name="channels")
    threads_axis = _coerce_axis(raw_dict["threads"], axis_name="threads")
    backfill_axis = _coerce_axis(raw_dict.get("backfill", {}), axis_name="backfill")
    team_id_raw = raw_dict.get("team_id")
    team_id = team_id_raw if isinstance(team_id_raw, str) and team_id_raw else None
    return SlackCursorState(
        channels=channels_axis, backfill=backfill_axis, threads=threads_axis, team_id=team_id
    )


def _empty_state() -> SlackCursorState:
    """Return a fresh, mutation-safe empty per-workspace compound cursor.

    Factored into a helper because :class:`SlackCursorState` is a
    :class:`TypedDict` (so ``{}`` is not type-safe at the call sites)
    and because the empty compound is referenced from both
    :func:`_load_cursors` callers (alias entry creation) and from
    tests that need to construct a baseline first-sync state.
    """
    return SlackCursorState(channels={}, backfill={}, threads={}, team_id=None)


def _empty_envelope() -> SlackCursorEnvelope:
    """Return a fresh, mutation-safe empty Phase 24-C cursor envelope."""
    return SlackCursorEnvelope(workspaces={})


def _coerce_axis(raw: Any, *, axis_name: str) -> dict[str, str | None]:
    """Validate one axis of the compound cursor and coerce to ``dict[str, str | None]``.

    Shared between the ``channels`` and ``threads`` axes because both
    are flat ``str → str | None`` maps — only the key naming convention
    differs (``channel_id`` vs ``"channel_id:thread_ts"``), which is
    not enforced at the type layer.
    """
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Slack cursor {axis_name!r} axis must be a JSON object; got {type(raw).__name__}"
        )
    axis_dict = cast(  # type: ignore[redundant-cast]
        dict[Any, Any], raw
    )
    result: dict[str, str | None] = {}
    for key, value in axis_dict.items():
        if not isinstance(key, str):
            raise ConfigError(
                f"Slack cursor {axis_name!r} axis keys must be strings; got {type(key).__name__}"
            )
        if value is not None and not isinstance(value, str):
            raise ConfigError(
                f"Slack cursor {axis_name!r} axis values must be "
                f"strings or null (ts); got {type(value).__name__} "
                f"for key {key!r}"
            )
        result[key] = value
    return result


def _resolve_floors(specs: list[SlackChannelSpec], sync_since: str | None) -> dict[str, str | None]:
    """Resolve each channel's effective date floor to a Slack ``ts`` string.

    Precedence per ADR-0036 §(e): the channel's own ``since`` wins;
    otherwise the connector-wide ``sync_since`` applies; otherwise there
    is no floor (full history). The :data:`~opshub.core.config.SLACK_FULL_HISTORY_SENTINEL`
    (``"all"``) and ``None`` both resolve to ``None`` (no floor). Relative
    values (``"90d"``) are evaluated *now* via :func:`parse_since`, so the
    floor advances with each sync run rather than freezing at config load.
    """
    floors: dict[str, str | None] = {}
    for spec in specs:
        raw = spec.since if spec.since is not None else sync_since
        floors[spec.id] = _floor_to_ts(raw)
    return floors


def _floor_to_ts(raw: str | None) -> str | None:
    """Convert a floor value (``None`` / ``"all"`` / date) to a ``ts`` or ``None``.

    ``None`` (inherit / unset) and the full-history sentinel short-circuit
    to ``None`` *before* :func:`parse_since` so ``"all"`` is never fed to
    the date parser (which would reject it). Any other value is parsed and
    rendered as a ``"seconds.microseconds"`` string for comparison against
    the per-channel cursor via :func:`_max_ts`.
    """
    # Lazy import keeps the sentinel a single source of truth without
    # pulling pydantic-settings onto the cold-start path (the config
    # import is already paid for inside ``_resolve_slack_settings``).
    from opshub.core.config import SLACK_FULL_HISTORY_SENTINEL

    if raw is None or raw == SLACK_FULL_HISTORY_SENTINEL:
        return None
    return since_to_ts(parse_since(raw))


def _max_ts(prior: str | None, candidate: str | None) -> str | None:
    """Return the chronologically-later of two Slack ``ts`` strings.

    Slack ``ts`` is documented as ``"seconds.microseconds"`` so
    :func:`float` comparison is total. ``None`` represents "no prior
    cursor" (first observation for the channel or thread) and yields
    the other operand. If both sides parse but the candidate is older,
    we keep the prior — that's the load-bearing invariant for the
    issue #339 fix: a yielded ``ts`` that goes *backwards* must never
    overwrite a persisted cursor.

    Axis-agnostic: this helper compares raw ``ts`` strings without
    interpreting the key shape, so Phase 20-B's compound cursor uses
    the same helper for the ``channels`` axis (key = ``channel_id``)
    and the ``threads`` axis (key = ``"channel_id:thread_ts"``). The
    ``ts`` values themselves are Slack-format on both axes.

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


def _ts_lt(candidate: str, bound: str) -> bool:
    """Return ``True`` iff Slack ts ``candidate`` is strictly older than ``bound``.

    Phase 22-D ([ADR-0038](docs/adr/0038-slack-sync-gap-backfill.md) §(d)):
    used to detect a *lowered* floor (``target_low < low_water``) that
    should trigger a gap backfill. A non-numeric operand (Slack contract
    violation) falls through to ``False`` (treat as "not lower") so a
    malformed ts can never trigger an unbounded historical re-fetch — the
    conservative direction, mirroring :func:`_max_ts`'s defensive arm.
    """
    try:
        return float(candidate) < float(bound)
    except (TypeError, ValueError):
        return False


def _ingest_yield(
    channel_id: str,
    raw_message: RawSlackMessage,
    new_cursor: str | None,
    *,
    channel_cursors: dict[str, str | None],
    thread_cursors: dict[str, str | None],
    excludes: ExcludeRules,
    source_service: Any,
) -> bool:
    """Apply one fetcher yield: advance cursors + threads axis, then observe.

    Shared by the Phase 1 forward loop and the Phase 0 gap-backfill loop
    (Phase 22-D) so both paths advance the ``channels`` / ``threads`` axes
    and apply the exclude filter identically. Returns ``True`` iff the
    message was observed (``False`` when dropped by ``excludes``), so the
    caller can maintain ``observed_count``.

    The ``channels`` axis advance is guarded by :func:`_max_ts`, so a gap
    yield (whose ``new_cursor`` is an *older* parent ts than the forward
    high-water) never rewinds the persisted cursor — the gap-backfill pass
    extends the low-water mark via the ``backfill`` axis, never the
    forward ``channels`` axis (ADR-0038 §(b)).
    """
    channel_cursors[channel_id] = _max_ts(channel_cursors.get(channel_id), new_cursor)
    # Maintain the threads axis exactly as the Phase 1 forward loop does:
    # a parent with replies (``thread_ts == ts``) seeds the threads cursor
    # at ``latest_reply``; a reply (``thread_ts != ts``) advances it to
    # ``max(prior, reply.ts)``. For gap parents this registers the thread
    # for the Phase 2 late-reply polling path (their replies were never
    # observed because the parent was below the forward window).
    if raw_message.thread_ts is not None:
        thread_key = _thread_cursor_key(channel_id, raw_message.thread_ts)
        if raw_message.thread_ts == raw_message.ts:
            latest_reply_raw = raw_message.raw.get("latest_reply")
            latest_reply_ts = (
                str(latest_reply_raw) if latest_reply_raw is not None else raw_message.ts
            )
            thread_cursors[thread_key] = _max_ts(thread_cursors.get(thread_key), latest_reply_ts)
        else:
            thread_cursors[thread_key] = _max_ts(thread_cursors.get(thread_key), raw_message.ts)
    if excludes.excludes_channel(raw_message.channel_id) or excludes.excludes_sender(
        raw_message.user_id
    ):
        return False
    # ``source_service`` is typed as ``Any`` on :class:`ConnectorContext`
    # (the framework predates the Phase 3 ``SourceService`` rename); the
    # keyword-only ``observe`` signature catches argument drift at runtime
    # via TypeError.
    source_service.observe(**map_message(raw_message))
    return True


def _thread_cursor_key(channel_id: str, thread_ts: str) -> str:
    """Compose the ``threads`` axis key (Phase 20-C).

    The Phase 20-B compound schema (:class:`SlackCursorState`) keys the
    ``threads`` axis on the composite ``"{channel_id}:{thread_ts}"``
    so a single Slack workspace's threads in two different channels
    with the same ``thread_ts`` (a vanishingly rare but real
    possibility — ``thread_ts`` is a per-channel id, not a
    workspace-global one) don't collide.
    """
    return f"{channel_id}:{thread_ts}"


def _parse_thread_cursor_key(key: str) -> tuple[str, str] | None:
    """Decompose a ``threads`` axis key into ``(channel_id, thread_ts)``.

    Returns ``None`` for a malformed key. The split is on the **first**
    colon so a future ``thread_ts`` shape change that includes a colon
    (Slack documents ``ts`` as ``"seconds.microseconds"`` so this is
    defensive) still rejoins correctly on the right-hand side.
    """
    channel_id, sep, thread_ts = key.partition(":")
    if not sep or not channel_id or not thread_ts:
        return None
    return channel_id, thread_ts


def _window_cutoff_ts(activity_window: timedelta | None) -> str | None:
    """Compute the Slack ``ts`` cutoff for the activity window (Phase 20-C).

    ADR-0030 §(d) revised: threads whose ``last_reply_ts`` falls before
    this cutoff are considered inactive — they're skipped on the polling
    phase and pruned from the ``threads`` axis. The cutoff is evaluated
    at sync time (not at config load), so a relative window like
    ``"30d"`` walks forward with each run.

    Phase 20-E ([#478](https://github.com/ozzy-labs/opshub/issues/478)):
    ``activity_window is None`` (the connector-side coercion of the
    ``thread_activity_window = "all"`` sentinel) returns ``None`` so
    the prune is disabled wholesale. :func:`_within_activity_window`
    and :func:`_prune_inactive_threads` short-circuit on the ``None``
    cutoff.
    """
    if activity_window is None:
        return None
    return since_to_ts(now_utc() - activity_window)


def _within_activity_window(last_reply_ts: str | None, cutoff_ts: str | None) -> bool:
    """Return ``True`` iff the thread is recent enough to poll (Phase 20-C).

    ``None`` means the threads cursor was registered but no reply has
    been observed yet — we treat that as in-window so the next sync
    gets one chance to fetch replies. Malformed ``ts`` strings (Slack
    contract violation) also fall through to in-window so the connector
    doesn't silently drop them; the actual polling round-trip will
    surface the error if Slack rejects it.

    Phase 20-E ([#478](https://github.com/ozzy-labs/opshub/issues/478)):
    ``cutoff_ts is None`` means the operator set
    ``thread_activity_window = "all"`` (or any case-variant of it) to
    disable prune entirely — every thread is in-window.
    """
    if cutoff_ts is None:
        return True
    if last_reply_ts is None:
        return True
    try:
        return float(last_reply_ts) >= float(cutoff_ts)
    except (TypeError, ValueError):
        return True


def _prune_inactive_threads(thread_cursors: dict[str, str | None], cutoff_ts: str | None) -> None:
    """Drop ``threads`` axis entries older than ``cutoff_ts`` (Phase 20-C).

    Mutates ``thread_cursors`` in place. Called on the happy path
    after the polling phase drains — the partial-progress checkpoint
    (in :meth:`SlackConnector.sync`'s ``finally`` arm) deliberately
    skips this so a mid-iteration crash preserves the threads axis
    from entry. ADR-0030 §(d) revised: window default 30d, operator
    overrides via ``[connectors.slack] thread_activity_window`` /
    ``--thread-activity-window`` / ``OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW``.

    Phase 20-E ([#478](https://github.com/ozzy-labs/opshub/issues/478)):
    ``cutoff_ts is None`` (the ``thread_activity_window = "all"`` path)
    is a no-op — every thread stays in the cursor.
    """
    if cutoff_ts is None:
        return
    stale_keys = [
        key
        for key, last_reply_ts in thread_cursors.items()
        if not _within_activity_window(last_reply_ts, cutoff_ts)
    ]
    for key in stale_keys:
        del thread_cursors[key]


def _dump_cursors(envelope: SlackCursorEnvelope) -> str:
    """Serialise the Phase 24-C cursor envelope to JSON for the projection.

    ``sort_keys=True`` recurses into nested dicts, so the top-level
    ``workspaces`` nest, every alias key, the per-alias axes and every
    per-axis entry are emitted in sorted order. That makes a no-op sync
    (no new messages, no new replies) yield a byte-identical cursor
    across runs — the ``connector_cursors`` row's ``updated_at``
    advances on timestamp only and operator dashboards can diff cursor
    values meaningfully (the per-workspace checkpoint also relies on
    this determinism to skip redundant writes).

    ``separators=(",", ":")`` strips default whitespace so the row
    stays compact, matching the GitHub cursor style (the size matters
    less than determinism, but a uniform style keeps the
    ``connector_cursors`` projection rows visually tight across
    connectors).
    """
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"))


def _resolve_live_team_id(auth: SlackAuth, *, alias: str) -> str:
    """Resolve the live workspace ``team_id`` for one alias via ``auth.test``.

    An ``auth.test`` that returns no ``team_id`` (abnormal) is a hard
    :class:`ConfigError` as of Phase 24-B — ``team_id`` is a constituent
    of every ``external_id``, so proceeding without one would mint
    malformed natural keys (the pre-24 fail-soft warn-and-proceed arm is
    gone, ADR-0041 §(i)). Enterprise Grid User Tokens report a stable
    home ``team_id`` so this does not false-positive (ADR-0039
    §Decision 5). This is the single ``auth.test`` round-trip per
    workspace per sync — both the bind guard and the fetcher's
    ``team_id`` stamp consume the same value (zero additional API
    calls).
    """
    current_team = auth.test_token().get("team_id", "")
    if not current_team:
        raise ConfigError(
            f"Slack auth.test returned no team_id for workspace {alias!r}; "
            "cannot compose the external_id namespace (team_id prefixes "
            "every Slack source key, ADR-0041). Verify the stored token with "
            f"`opshub slack auth test --workspace {alias}` and re-run once "
            "auth.test reports a team_id."
        )
    return current_team


def _bind_or_check_workspace(
    state: SlackCursorState,
    *,
    current_team: str,
    alias: str,
) -> str:
    """Bind / verify one alias's ``team_id`` invariant and return it.

    Phase 23-H (ADR-0039) established the guard install-wide; Phase 24-C
    ([ADR-0041](docs/adr/0041-slack-multi-workspace.md) §(a)) generalises
    it **per alias**: each workspace cursor entry records the ``team_id``
    it was first synced against, and the guard reconciles it with the
    live workspace the alias's token resolves to (``current_team``, from
    :func:`_resolve_live_team_id`) **before any fetch**:

    * **unbound** (first sync for the alias / after a cursor reset) →
      bind it (mutates ``state["team_id"]`` in place; the caller
      persists it via the envelope checkpoint).
    * **bound, same team** → no-op (steady state).
    * **bound, different team** → :class:`ConfigError`. A token swapped
      to another workspace under the same alias would otherwise silently
      mix two workspaces' messages into one alias's cursor — and the
      ``external_id`` namespace would still be correct (team-prefixed)
      but the cursor semantics would be corrupted (exit 0 "success" —
      the silent-failure pathology epic #530 targets). Because this runs
      **before any fetch**, the foreign workspace's data never enters
      the DB under this alias (ADR-0039 §Decision 2 semantics, carried
      per-alias into ADR-0041 §(a)).
    """
    bound_team = state["team_id"]
    if bound_team is None:
        state["team_id"] = current_team
        return current_team
    if bound_team != current_team:
        raise ConfigError(
            f"Slack workspace {alias!r} is bound to team_id {bound_team!r} but "
            f"its stored token now resolves to {current_team!r}. If you pasted "
            "the wrong token, restore the original with `opshub slack auth set "
            f"--workspace {alias}`. To intentionally re-point the alias at a "
            f"different workspace, run `opshub slack cursor reset --workspace "
            f"{alias} --all` first (the previous workspace's ingested messages "
            "remain and must be purged manually — an unsupported path; see "
            "ADR-0041 §(e) for the sanctioned DB re-init)."
        )
    return current_team
