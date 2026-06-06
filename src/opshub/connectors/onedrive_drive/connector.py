"""OneDrive Drive connector implementation (Phase 11 F4-b, ADR-0019 §(j)).

Composes :class:`opshub.connectors.onedrive_drive.scanner.OneDriveDriveScanner`
+ :func:`opshub.connectors.onedrive_drive.mapper.map_scanned_file`
into the :class:`opshub.connectors.base.Connector` Protocol contract.
Driven by the ``opshub onedrive_drive sync`` CLI; identical
control flow to the Phase 9 ``BoxDriveConnector`` (the local-FS
contract is shared per ADR-0019 §(j)).

Sync flow (ADR-0019 §(j) + §決定 (a)(c)(d)(g))
----------------------------------------------

1. Resolve ``root_path`` from
   :class:`opshub.core.config.OpsHubSettings.connectors.onedrive_drive`.
   ``None`` falls back to
   :func:`opshub.core.platform.onedrive_drive_default_root_path`
   (WSL2 → ``/mnt/onedrive``; macOS → ``~/OneDrive``). Linux
   native / unsupported platforms surface as :class:`ConfigError`
   with a pointer to ``docs/onedrive-drive-setup.md``.
2. Hydrate ``prior_fingerprints`` from the ``sources`` projection
   filtered by ``connector_name = 'onedrive_drive'`` (a separate
   SELECT from the box_drive prior map; the two connectors do not
   share fingerprint state).
3. Run the scan and forward each yielded file through
   :func:`map_scanned_file` → :meth:`SourceService.observe`.
4. Return :class:`SyncResult` with the per-file count and an
   informational UTC ISO cursor.

Cold-start budget (ADR-0001)
----------------------------

Top-level imports stay framework-only. :class:`OpsHubSettings`,
:func:`onedrive_drive_default_root_path`,
:class:`OneDriveDriveScanner`, and the ``sources`` projection table
are loaded lazily inside :meth:`sync` so ``opshub --help`` cold
start remains within the ADR-0001 ~300ms budget on installations
that never run a OneDrive sync.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from opshub.connectors.base import SyncResult
from opshub.connectors.onedrive_drive.mapper import map_scanned_file
from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.onedrive_drive.scanner import OneDriveDriveScanner

__all__ = ["OneDriveDriveConnector"]


class OneDriveDriveConnector:
    """Concrete :class:`~opshub.connectors.base.Connector` for local OneDrive scans.

    Structurally identical to
    :class:`opshub.connectors.box_drive.connector.BoxDriveConnector`;
    the only behavioural differences are:

    * ``name`` = ``"onedrive_drive"`` (vs ``"box_drive"``) so the
      CLI dispatcher routes ``opshub onedrive_drive sync``
      here.
    * Settings come from ``connectors.onedrive_drive`` rather than
      ``connectors.box_drive``.
    * Platform defaults come from
      :func:`onedrive_drive_default_root_path` (WSL2 → ``/mnt/onedrive``;
      macOS → ``~/OneDrive``).
    * ``prior_fingerprints`` is filtered to
      ``connector_name = 'onedrive_drive'`` rows.

    See the box_drive connector docstring for the per-step rationale
    that carries over verbatim (atomicity, cursor semantics,
    cold-start budget).
    """

    name = "onedrive_drive"

    def __init__(
        self,
        scanner_factory: Callable[[], OneDriveDriveScanner] | None = None,
    ) -> None:
        self._scanner_factory = scanner_factory

    def sync(self, context: ConnectorContext) -> SyncResult:
        """Run one OneDrive Drive sync pass."""
        scanner = self._resolve_scanner()
        prior_fingerprints = self._load_prior_fingerprints(context)

        observed_count = 0
        for scanned in scanner.scan(prior_fingerprints=prior_fingerprints):
            event = map_scanned_file(scanned, root_path=scanner.root_path)
            context.source_service.observe(
                connector_name=event.connector_name,
                external_id=event.external_id,
                source_type=event.source_type,
                title=event.title,
                url=event.url,
                summary=event.summary,
                fingerprint=event.fingerprint,
                # Thread the body + provenance the mapper stamped.
                # ADR-0019 §(b') opt-in extracts Office bodies;
                # stat-only paths set ``body = summary`` so the
                # ``SourceObserved.body`` ``min_length=1`` invariant
                # holds (epic #470 / #481).
                body=event.body,
                provenance_origin=event.provenance_origin,
                provenance_trust=event.provenance_trust,
            )
            observed_count += 1

        # Informational-only cursor (the fingerprint diff is the real
        # resume mechanism). Matches box_drive precedent.
        new_cursor = datetime.now(tz=UTC).isoformat()
        return SyncResult(observed_count=observed_count, new_cursor=new_cursor)

    # ------------------------------------------------------------------ helpers

    def _resolve_scanner(self) -> OneDriveDriveScanner:
        """Build the :class:`OneDriveDriveScanner` from settings.

        Test seam: when ``scanner_factory`` was provided to the
        constructor it short-circuits the full settings resolution.
        Production path mirrors :class:`BoxDriveConnector._resolve_scanner`
        (same shared-excludes merger, same fail-fast ConfigError on
        Linux-native hosts).

        Raises :class:`ConfigError` when the platform has no
        OneDrive default and the operator did not override.
        """
        if self._scanner_factory is not None:
            return self._scanner_factory()
        # Lazy imports preserve the ADR-0001 cold-start budget.
        from opshub.connectors.onedrive_drive.scanner import OneDriveDriveScanner
        from opshub.core.config import OpsHubSettings
        from opshub.core.excludes import load_excludes
        from opshub.core.platform import onedrive_drive_default_root_path

        settings = OpsHubSettings()
        cfg = settings.connectors.onedrive_drive
        # ADR-0020 §(b): shared ``excludes.yaml`` ``paths`` selector
        # augments the connector's inline ``exclude_globs``. Matches
        # the box_drive merger pattern verbatim — call
        # ``load_excludes()`` with no arguments so the loader resolves
        # ``default_config_dir()`` itself (Round 2 Cluster B M1).
        shared_paths = list(load_excludes().paths)
        exclude_globs = list(cfg.exclude_globs) + shared_paths
        root_path: Path | None = cfg.root_path
        if root_path is None:
            root_path = onedrive_drive_default_root_path()
        if root_path is None:
            raise ConfigError(
                "OneDrive root_path is not configured and this platform has no "
                "default (OneDrive provides no Linux client). Set "
                "`[connectors.onedrive_drive] root_path` in opshub.toml or run "
                "on WSL2 (/mnt/onedrive) / macOS (~/OneDrive). "
                "See docs/onedrive-drive-setup.md for setup instructions."
            )
        return OneDriveDriveScanner(
            root_path=root_path,
            exclude_globs=exclude_globs,
            max_depth=cfg.max_depth,
            follow_symlinks=cfg.follow_symlinks,
            max_files=cfg.max_files,
            content_extraction=cfg.content_extraction,
            # Phase 11 audit Cluster B: forward the
            # ``opshub.toml [office]`` overrides so the extractor's
            # per-call caps reflect operator-tuned values (matches the
            # box_drive precedent — the two local-FS connectors share
            # the OfficeSettings instance because both call sites are
            # the same extractor).
            office_settings=settings.office,
        )

    @staticmethod
    def _load_prior_fingerprints(context: ConnectorContext) -> dict[str, str]:
        """Hydrate ``{rel_path: fingerprint}`` from the ``sources`` projection.

        Filtered to ``connector_name = 'onedrive_drive'`` so the
        two local-FS connectors keep their fingerprint state
        independent (a file copied between the two mounts surfaces as
        an observation under each). NULL fingerprints (legacy /
        unseen) are skipped so the next sync re-yields the file and
        populates the column.
        """
        # Lazy imports keep ADR-0001 cold-start budget intact.
        from sqlalchemy import select

        from opshub.projections.sources import sources_table

        engine = getattr(context.source_service, "engine", None)
        if engine is None:
            engine = getattr(context.source_service, "_engine", None)
        if engine is None:
            return {}

        statement = select(
            sources_table.c.external_id,
            sources_table.c.fingerprint,
        ).where(sources_table.c.connector_name == "onedrive_drive")

        prior: dict[str, str] = {}
        with engine.connect() as conn:
            for external_id, fingerprint in conn.execute(statement):
                if fingerprint is None:
                    continue
                prior[external_id] = fingerprint
        return prior
