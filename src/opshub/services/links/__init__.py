"""LinkService package (Phase 8 step C1, ADR-0017).

Hosts the read-only traversal API over the ``links`` projection
materialised by Phase 8 A2 (table) + B2 (``LinksExtractor`` dispatch).

The package layout mirrors :mod:`opshub.services.briefings` and
:mod:`opshub.services.proposals` so future link-related helpers (e.g.
graph rendering, expand integration) can sit alongside
:class:`LinkService` without re-shaping callers' imports.

Re-exports the public surface (:class:`Link`, :class:`LinkPath`,
:class:`GraphSubset`, :class:`LinkService`) at package level so
callers do not need to remember the inner module name.
"""

from opshub.services.links.service import (
    GraphSubset,
    Link,
    LinkPath,
    LinkService,
)

__all__ = ["GraphSubset", "Link", "LinkPath", "LinkService"]
