"""Google Mail (Gmail) connector settings (re-export shim).

The canonical :class:`GoogleMailConnectorSettings` lives in
:mod:`opshub.core.config` alongside every other connector's settings
class — the project-wide convention (Phase 7 MS365 / Box / Teams +
Phase 11 OneDrive Drive / Box Drive + Phase 13 Google Workspace) is
"settings classes live in ``opshub.core.config`` so a single
``OpsHubSettings`` covers the operator surface". This shim re-exports
the canonical class under ``opshub.connectors.google_mail.settings``
so that:

1. Phase 14 plan §3 Sub-issue G3's "5 module 構成 (cursor + client +
   mapper + connector + settings)" reads consistently with the
   on-disk module list (mirrors the Phase 13 google_workspace
   re-export shim shape).
2. Future refactors that pull the settings *closer* to the connector
   (per-connector ``opshub.toml`` sections) can move the canonical
   class here without touching call sites.

The re-export is a single ``import``; the heavy ``pydantic`` /
``opshub.core.config`` load only happens when this module is
imported (the connector ``sync`` code path already pays for it).
"""

from __future__ import annotations

from opshub.core.config import GoogleMailConnectorSettings

__all__ = ["GoogleMailConnectorSettings"]
