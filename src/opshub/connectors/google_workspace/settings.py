"""Google Workspace connector settings (re-export shim).

The canonical :class:`GoogleWorkspaceConnectorSettings` lives in
:mod:`opshub.core.config` alongside every other connector's settings
class — the project-wide convention (Phase 7 MS365 / Box / Teams +
Phase 11 OneDrive Drive / Box Drive) is "settings classes live in
``opshub.core.config`` so a single ``OpsHubSettings`` covers the
operator surface". This shim re-exports the canonical class under
``opshub.connectors.google_workspace.settings`` so that:

1. Phase 13 plan §3 Sub-issue G3's "5 module 構成 (auth + client +
   cursor + mapper + connector + settings)" reads consistently with
   the on-disk module list.
2. Future refactors that pull the settings *closer* to the connector
   (per-connector ``opshub.toml`` sections) can move the canonical
   class here without touching call sites.

The re-export is a single ``import``; the heavy ``pydantic`` /
``opshub.core.config`` load only happens when this module is
imported (the connector ``sync`` code path already pays for it).
"""

from __future__ import annotations

from opshub.core.config import GoogleWorkspaceConnectorSettings

__all__ = ["GoogleWorkspaceConnectorSettings"]
