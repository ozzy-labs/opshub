"""Shared infrastructure for per-noun ``opshub <connector> sync`` commands.

Phase 17-B (ADR-0031) reorganises the CLI surface from the old
``opshub connector <verb> <name>`` 3-level layout into a per-noun
2-level layout (``opshub <connector> <verb>``). To avoid duplicating
the sync driver — connector dispatch, progress proxy, sanitised error
trail — across 10 noun modules, the shared work lives here and each
noun's ``sync`` callback boils down to ``run_connector_sync("<name>")``.

Public surface:

* :func:`run_connector_sync` — drives one sync end-to-end (registry
  lookup, cursor bracket, progress proxy, sanitised failure, debug
  trail). Mirrors the byte-for-byte behaviour of the old
  ``connector_sync`` function so scripts grepping ``sync failed:
  <Type>`` / ``synced <name>: N item(s) observed`` keep working.
* :class:`_ProgressSourceProxy` — kept module-public (single
  underscore retained for legibility / historical continuity with the
  pre-Phase-17-B ``opshub.cli.connector`` tests that originally
  anchored on it; the constructor / contract is unchanged).
* :func:`is_debug_enabled` — env-var probe for ``OPSHUB_DEBUG``,
  re-exported so each noun's ``sync`` callback does not duplicate
  the truthy-table.
* :data:`_DEBUG_TRUTHY` — module-public so the legacy
  ``opshub.cli.connector`` → :mod:`opshub.core.logging` drift pin
  test (now re-anchored on this module) can keep its import path.

Module-level imports are restricted to ``__future__``, ``os`` and
``typer`` so ``opshub --help`` cold start stays under the ~300ms
budget set by ADR-0001 (the :file:`tests/integration/test_cli_imports`
guard only inspects public ``opshub/cli/*.py`` modules, so this
private helper is technically exempt, but we keep the same
discipline to make the lazy-import contract self-documenting).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from opshub.cli._progress import ProgressReporter


# Truthy strings accepted by ``OPSHUB_DEBUG`` — mirrors the convention
# used by ``opshub.core.logging`` (``_TRUTHY`` there) so the operator
# sees the same accept-list everywhere. Kept inline rather than
# imported so this module's top-level import set stays light.
_DEBUG_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on", "debug"})


def is_debug_enabled() -> bool:
    """Return True iff ``OPSHUB_DEBUG`` resolves to a truthy value.

    The env var is the **primary** source of truth: the root callback
    in :mod:`opshub.cli.app` sets ``OPSHUB_DEBUG=1`` when the operator
    passed ``--debug`` / ``-vv``, so the same probe works for both
    the in-process CLI path and the MCP subprocess path (where no
    Typer Context is available).
    """
    value = os.environ.get("OPSHUB_DEBUG")
    if value is None:
        return False
    return value.strip().lower() in _DEBUG_TRUTHY


class _ProgressSourceProxy:
    """Wrap a source service so each ``observe`` advances a progress bar.

    The connector sync loop calls ``context.source_service.observe(...)``
    once per item it ingests, but the total item count is unknown until the
    connector's paginated stream drains. Rather than thread a progress
    handle through the :class:`~opshub.connectors.base.Connector` Protocol
    and all ten connectors, the driver swaps the source service for this
    transparent proxy: every successful ``observe`` bumps the indeterminate
    spinner's counter, and all other attributes (``cursor_set`` /
    ``record_sync_failure`` / ...) forward unchanged to the wrapped
    service.

    The counter advances **after** the wrapped ``observe`` returns so a
    failed observe (which raises) does not inflate the count — matching the
    at-most-once-or-no-loss posture the connectors already hold for cursor
    advancement.
    """

    def __init__(self, inner: Any, reporter: ProgressReporter) -> None:
        self._inner = inner
        self._reporter = reporter

    def observe(self, *args: Any, **kwargs: Any) -> Any:
        result = self._inner.observe(*args, **kwargs)
        self._reporter.advance(1)
        return result

    def __getattr__(self, name: str) -> Any:
        # Forward everything else (cursor_set, record_sync_failure, ...) to
        # the wrapped service untouched. ``__getattr__`` only fires for
        # names not found on the proxy itself, so ``observe`` above is not
        # shadowed.
        return getattr(self._inner, name)


def _import_connector_modules() -> None:
    """Best-effort import of every connector subpackage.

    Thin wrapper around
    :func:`opshub.connectors._discovery.import_connector_modules` — the
    same helper the MCP ``connector.sync`` handler and the
    ``opshub connectors`` list command call. Kept under this
    underscore-prefixed name so the historical call site in
    :func:`run_connector_sync` (below) stays stable; the body lives in
    ``opshub.connectors._discovery`` so the import set is not
    duplicated across the CLI / MCP surfaces.
    """
    from opshub.connectors._discovery import import_connector_modules

    import_connector_modules()


def _build_source_service(*, actor: str) -> object:
    """Indirection for the ``build_source_service`` helper.

    Hides the lookup behind this private helper (returning ``object``
    so the caller can cast to ``Any`` for the still-unknown
    :class:`SourceService` interface) so test monkeypatching can
    target this module rather than the per-noun caller.
    """
    from opshub.cli import _wiring

    builder = getattr(_wiring, "build_source_service", None)
    if builder is None:
        raise RuntimeError(
            "opshub.cli._wiring.build_source_service is not available; "
            "Phase 3 step A4 must merge before `opshub <name> sync` "
            "can run."
        )
    return builder(actor=actor)


def run_connector_sync(name: str) -> None:
    """Run sync for the named connector (shared driver for per-noun ``sync`` callbacks).

    Resolves ``name`` from :func:`discover_connectors`, loads the
    cursor from the ``connector_cursors`` projection, opens a sync run
    bracket (``cursor_set(sync_started=True)``), invokes
    :meth:`Connector.sync`, persists the returned cursor
    (``cursor_set(sync_started=False, value=result.new_cursor)``), and
    prints a one-line summary.

    On exception:

    * ``SourceService.record_sync_failure`` records a
      ``ConnectorSyncFailed`` event with the exception **type name only**
      — never the message — so secrets / PII never reach the event log.
    * The CLI exits with code 1.

    Unknown connector → exit code 2 with a list of available names
    (mirrors Typer's convention for usage errors).

    Output byte-for-byte identical to the pre-Phase-17 ``connector
    sync`` surface:

    * stdout: ``synced <name>: N item(s) observed`` on success
    * stderr: ``sync failed: <Type>: <sanitised-msg>`` on failure
      (always — Phase 23-B promotes the previously debug-only body so
      the default trail carries actionable recovery text, e.g. scope
      catalogue URLs / ``run opshub slack auth set``); the sanitised
      traceback stays gated behind ``OPSHUB_DEBUG=1`` (R2 / R3 / R4 of
      #320). The ``sync failed:`` prefix is preserved so log-grep
      scripts keep matching, and the event-log row recorded via
      ``record_sync_failure`` still carries the **type name only** so
      the audit trail never widens its token surface.
    """
    _import_connector_modules()

    from opshub.connectors import discover_connectors
    from opshub.connectors.context import ConnectorContext
    from opshub.core.logging import get_logger

    connectors = {c.name: c for c in discover_connectors()}
    connector = connectors.get(name)
    if connector is None:
        available = ", ".join(connectors) or "(none)"
        typer.echo(
            f"unknown connector {name!r}; available: {available}",
            err=True,
        )
        raise typer.Exit(code=2)

    from opshub.cli import _progress

    source: Any = _build_source_service(actor=f"connector:{name}")
    logger = get_logger().bind(connector=name)
    cursor = source.cursor_get(name)
    # Open the sync run bracket so observers see ConnectorSyncStarted.
    source.cursor_set(name, cursor, sync_started=True)

    # Indeterminate progress: connectors stream items via pagination, so
    # the total is unknown up front. The spinner + per-observe counter +
    # elapsed clock answers "is it moving, and how far has it got" without
    # a costly pre-count pass. Progress renders on stderr and is a no-op
    # on non-TTY (CI / pipes), so the stdout summary below is unchanged.
    with _progress.indeterminate(f"syncing {name}") as reporter:
        context = ConnectorContext(
            source_service=_ProgressSourceProxy(source, reporter),
            cursor_value=cursor,
            secrets=None,
            logger=logger,
        )
        try:
            result = connector.sync(context)
        except Exception as exc:
            # Sanitise: surface only the exception type name, never the
            # message, so tokens / PII never reach the event log
            # (R3 — Phase 14 T3, ADR-0027). The event-log row is the
            # operator's audit trail; growing its token surface there
            # would defeat the redaction stance even if the live
            # terminal is locked down.
            source.record_sync_failure(name, error_message=type(exc).__name__)
            # Phase 23-B (#532): surface the sanitised message body on the
            # **default** failure trail. sync is the path operators hit
            # most (scope / token / rate-limit / legacy-cursor errors) and
            # the connector-side messages carry the actionable recovery
            # text; gating that behind OPSHUB_DEBUG=1 left the default
            # operator with a bare type name. The body is run through
            # ``sanitise_error_message`` (the same scrub the debug path
            # already applied), so no new secret reaches stderr that the
            # debug path did not already emit. The event-log row above
            # stays type-name-only — redaction there is unchanged.
            from opshub.core.sanitise import sanitise_error_message

            sanitised_msg = sanitise_error_message(str(exc))
            typer.echo(
                f"sync failed: {type(exc).__name__}: {sanitised_msg}",
                err=True,
            )
            if is_debug_enabled():
                from opshub.core.logging import format_debug_traceback

                typer.echo(format_debug_traceback(exc), err=True)
            raise typer.Exit(code=1) from exc

    source.cursor_set(name, result.new_cursor, sync_started=False)
    typer.echo(f"synced {name}: {result.observed_count} item(s) observed")
