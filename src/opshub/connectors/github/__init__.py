"""GitHub connector (Phase 3 sub-issue B).

The package re-exports :func:`get_github_token` so callers can write
``from opshub.connectors.github import get_github_token`` without
reaching into the ``auth`` submodule.

Heavy dependencies (PyGithub / httpx / respx) are intentionally **not**
imported here. Sub-issue B steps B2 + B3 add fetch primitives and the
concrete connector implementation in dedicated submodules so the cold
import remains cheap.
"""

from opshub.connectors.github.auth import GITHUB_PAT_SECRET_KEY, get_github_token

__all__ = ["GITHUB_PAT_SECRET_KEY", "get_github_token"]
