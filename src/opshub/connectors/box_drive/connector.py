"""Box Drive connector implementation (Phase 9, ADR-0019).

Composes the B1 :class:`opshub.connectors.box_drive.scanner.BoxDriveScanner`
and the B2 :func:`opshub.connectors.box_drive.mapper.map_scanned_file`
into the :class:`opshub.connectors.base.Connector` Protocol contract.
Driven by the ``opshub connector sync box_drive`` CLI in
:mod:`opshub.cli.connector` (the CLI surface itself ships in Phase 9
step C1; this module already satisfies the runtime contract).

Sync flow (Phase 9 plan §1 #5 / ADR-0019 §決定 (a)(c)(d)(g))
-----------------------------------------------------------

1. **Resolve ``root_path``** from
   :class:`opshub.core.config.OpsHubSettings`'
   :class:`BoxDriveConnectorSettings`. A ``None`` value falls back
   to :func:`opshub.core.platform.box_drive_default_root_path`
   (WSL2 → ``/mnt/b``; macOS → ``~/Box``). Linux native /
   unsupported platforms surface as ``None`` here, which we turn
   into a :class:`ConfigError` with a pointer to
   ``docs/box-drive-setup.md`` — fail-fast at the boundary, never
   silently no-op.
2. **Hydrate ``prior_fingerprints``** by selecting
   ``(external_id, fingerprint)`` rows from the ``sources``
   projection where ``connector_name = 'box_drive'``. The result
   becomes the dict the scanner short-circuits against
   (ADR-0019 §決定 (d)). We read directly from the projection via
   the ``SourceService.engine`` handle rather than minting a new
   :class:`SourceService` method — the read is read-only and Box
   Drive-specific, so a dedicated helper would not be reusable.
3. **Run the scan**. :meth:`BoxDriveScanner.scan` yields one
   :class:`ScannedFile` per file whose fingerprint differs from the
   prior map. Each yield is forwarded through
   :func:`map_scanned_file` → :meth:`SourceService.observe` with
   ``fingerprint=`` so the projection's
   ``sources.fingerprint`` (Phase 9 step A2 / migration ``0017``)
   advances for the next run.
4. **Return a ``SyncResult``** carrying the count and a
   ``new_cursor`` set to the current UTC ISO timestamp. Phase 9 MVP
   treats this as **informational only** — resume / restart is
   driven by the per-file fingerprint diff, not by the cursor. The
   string is still persisted so the
   :class:`~opshub.projections.connector_cursors.ConnectorCursorsProjection`
   row's ``updated_at`` reflects the last successful sync (useful
   in ``opshub connector status``).

Cold-start budget (ADR-0001)
----------------------------

This module imports only the framework primitives and stdlib
``TYPE_CHECKING`` shims at top level. :class:`OpsHubSettings`,
:func:`box_drive_default_root_path`, :class:`BoxDriveScanner`, and
the ``sources`` projection table are loaded lazily inside
:meth:`sync` so ``opshub --help`` cold start stays within the
~300 ms budget on installations that never run a Box Drive sync.

Atomicity (matches Phase 3 GitHub + Phase 7 Slack / MS365 / Box)
----------------------------------------------------------------

Each yielded file is observed through a single
:meth:`SourceService.observe` call, which atomically appends one
:class:`SourceObserved` + one :class:`ItemEnqueued` event in one UoW
(PR #26 / PR #47 contract). A projector failure on event N rolls
the same event's :class:`SourceObserved` back, so the sources
projection and the inbox projection cannot diverge. Files 1..N-1
already committed in their own UoWs persist — the connector loop
is intentionally one-UoW-per-file so partial-success scenarios
remain auditable (the Phase 7 atomicity tests pin this same
posture).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from opshub.connectors.base import SyncResult
from opshub.connectors.box_drive.mapper import map_scanned_file
from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from opshub.connectors.box_drive.scanner import BoxDriveScanner
    from opshub.connectors.context import ConnectorContext

__all__ = ["BoxDriveConnector"]


class BoxDriveConnector:
    """Concrete :class:`~opshub.connectors.base.Connector` for local Box Drive scans.

    Parameters
    ----------
    scanner_factory:
        Test seam. Defaults to ``None``, which causes :meth:`sync` to
        construct a :class:`BoxDriveScanner` from the resolved
        :class:`BoxDriveConnectorSettings`. Unit tests supply a
        zero-arg callable returning a stub scanner so the connector
        path is exercised without touching the real filesystem; the
        factory is a constructor argument (not a class attribute) so
        each :class:`BoxDriveConnector` instance carries its own
        override — tests can register a fresh connector under the
        global registry without leaking state across cases.

    The connector holds no FS state at construction time — every
    resolve happens at the start of :meth:`sync`. That keeps the
    cold-start import cheap (pydantic-settings + the scanner only
    load when an operator actually runs ``opshub connector sync
    box_drive``).
    """

    name = "box_drive"

    def __init__(
        self,
        scanner_factory: Callable[[], BoxDriveScanner] | None = None,
    ) -> None:
        self._scanner_factory = scanner_factory

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one Box Drive sync pass.

        See module docstring for the full flow. Raises
        :class:`ConfigError` when ``root_path`` cannot be resolved
        (Linux native / unsupported platforms / explicit path
        missing on disk). The CLI driver in
        :mod:`opshub.cli.connector` maps :class:`ConfigError` to a
        non-zero exit code without appending a
        ``ConnectorSyncFailed`` event — config mistakes are not
        connector failures.
        """
        scanner = self._resolve_scanner()
        prior_fingerprints = self._load_prior_fingerprints(context)

        observed_count = 0
        for scanned in scanner.scan(prior_fingerprints=prior_fingerprints):
            event = map_scanned_file(scanned, root_path=scanner.root_path)
            # ``source_service`` is typed as ``Any`` on
            # :class:`ConnectorContext` (the framework predates the
            # Phase 3 ``SourceService`` rename); the keyword-only
            # ``observe`` signature catches argument drift at
            # runtime via :class:`TypeError`. The ``fingerprint``
            # keyword is the Phase 9 step A2 addition that the
            # :class:`SourcesProjection` upserts onto
            # ``sources.fingerprint``.
            context.source_service.observe(
                connector_name=event.connector_name,
                external_id=event.external_id,
                source_type=event.source_type,
                title=event.title,
                url=event.url,
                summary=event.summary,
                fingerprint=event.fingerprint,
                # Phase 10 (ADR-0020): thread the provenance tags the
                # mapper stamped on (body stays ``None`` — no file read).
                body=event.body,
                provenance_origin=event.provenance_origin,
                provenance_trust=event.provenance_trust,
            )
            observed_count += 1

        # ``new_cursor`` is informational-only for Phase 9 MVP — the
        # scanner's fingerprint diff is the actual resume mechanism.
        # We still write *something* so the ``connector_cursors`` row
        # touches its ``updated_at`` column (operators looking at
        # ``opshub connector status`` then see when each run
        # completed).
        new_cursor = datetime.now(tz=UTC).isoformat()
        return SyncResult(observed_count=observed_count, new_cursor=new_cursor)

    # ------------------------------------------------------------------ helpers

    def _resolve_scanner(self) -> BoxDriveScanner:
        """Build the :class:`BoxDriveScanner` from settings (or the test seam).

        Production path: load :class:`OpsHubSettings`, resolve
        ``root_path`` (falling back to the platform default), and
        construct a fresh scanner with the operator's caps
        (``max_depth`` / ``max_files`` / ``exclude_globs`` /
        ``follow_symlinks``). Test path: the constructor-provided
        factory short-circuits all of that.

        Raises :class:`ConfigError` when:

        * The platform has no default root and the operator did not
          override (Linux native — Box Drive provides no Linux
          client).
        * The resolved root does not exist on disk
          (re-raised verbatim from the :class:`BoxDriveScanner`
          constructor; the scanner's error message already points
          at ``docs/box-drive-setup.md``).
        """
        if self._scanner_factory is not None:
            return self._scanner_factory()
        # Lazy imports keep the cold-start budget tight.
        from opshub.connectors.box_drive.scanner import BoxDriveScanner
        from opshub.core.config import OpsHubSettings
        from opshub.core.excludes import load_excludes
        from opshub.core.platform import box_drive_default_root_path

        settings = OpsHubSettings()
        cfg = settings.connectors.box_drive
        # Phase 10 (ADR-0020 §(b)): the shared ``excludes.yaml`` ``paths``
        # selector augments the connector's inline ``exclude_globs`` so an
        # operator can migrate inline globs to the shared file at their
        # own pace. Both lists feed the scanner's path matcher.
        shared_paths = list(load_excludes(config_dir=settings.config_dir).paths)
        exclude_globs = list(cfg.exclude_globs) + shared_paths
        root_path: Path | None = cfg.root_path
        if root_path is None:
            root_path = box_drive_default_root_path()
        if root_path is None:
            # Linux native / unsupported — no Box Drive client
            # exists. Fail-fast with an actionable message rather
            # than silently no-op or attempt to scan a sentinel
            # path.
            raise ConfigError(
                "Box Drive root_path is not configured and this platform has no "
                "default (Box Drive provides no Linux client). Set "
                "`[connectors.box_drive] root_path` in opshub.toml or run on "
                "WSL2 (/mnt/b) / macOS (~/Box). "
                "See docs/box-drive-setup.md for setup instructions."
            )
        # The scanner itself raises :class:`ConfigError` if
        # ``root_path`` does not exist or is not a directory; we
        # let that surface verbatim — the scanner's message already
        # mentions ``docs/box-drive-setup.md``.
        return BoxDriveScanner(
            root_path=root_path,
            exclude_globs=exclude_globs,
            max_depth=cfg.max_depth,
            follow_symlinks=cfg.follow_symlinks,
            max_files=cfg.max_files,
        )

    @staticmethod
    def _load_prior_fingerprints(context: ConnectorContext) -> dict[str, str]:
        """Build the ``{rel_path: fingerprint}`` map from the ``sources`` projection.

        The scanner short-circuits files whose live fingerprint
        matches the prior value (ADR-0019 §決定 (d)). We hydrate the
        prior map by selecting
        ``(external_id, fingerprint) FROM sources WHERE
        connector_name = 'box_drive'`` through the
        :class:`SourceService` engine handle. Rows with a ``NULL``
        fingerprint (legacy rows from before migration ``0017``, or
        any other connector that never populates the field) are
        skipped — a ``None`` value cannot match the live
        ``f"{size}:{mtime_ns}"`` string, so re-yielding such a file
        is the right behaviour (the next observe call will populate
        ``fingerprint`` and the projection upsert will refresh the
        row).

        The lookup runs on a freshly-opened connection (not the
        service's UoW) because it is a *pre-sync* read — the per-file
        ``observe`` calls open their own UoWs once the scan starts.
        """
        # Lazy imports preserve the ADR-0001 cold-start budget:
        # ``sources_table`` pulls in SQLAlchemy metadata and the
        # ``opshub.projections`` re-exports.
        from sqlalchemy import select

        from opshub.projections.sources import sources_table

        engine = getattr(context.source_service, "engine", None)
        if engine is None:
            # ``SourceService`` exposes the engine via the
            # ``self._engine`` attribute set in
            # :meth:`SourceService.__init__`. The attribute is
            # private but the integration wiring
            # (:func:`opshub.cli._wiring.build_source_service`)
            # always passes one. If a test stub omits it we fall
            # back to an empty dict so first-sync semantics still
            # hold (every file gets yielded).
            engine = getattr(context.source_service, "_engine", None)
        if engine is None:
            return {}

        statement = select(
            sources_table.c.external_id,
            sources_table.c.fingerprint,
        ).where(sources_table.c.connector_name == "box_drive")

        prior: dict[str, str] = {}
        with engine.connect() as conn:
            for external_id, fingerprint in conn.execute(statement):
                if fingerprint is None:
                    # NULL fingerprint cannot match anything; leave
                    # the file out so the scanner re-yields it and
                    # the next ``observe`` populates the column.
                    continue
                prior[external_id] = fingerprint
        return prior
