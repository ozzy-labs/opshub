"""GitHub connector (Phase 3 sub-issue B).

Importing this module registers :class:`GitHubConnector` with the
global connector registry so the CLI driver
(``opshub connector sync github``) can discover it. The package also
re-exports :func:`get_github_token` and :data:`GITHUB_PAT_SECRET_KEY`
so callers can write ``from opshub.connectors.github import
get_github_token`` without reaching into the ``auth`` submodule.

Heavy dependencies (httpx) are loaded by the connector / api submodules
themselves; the import side effect here is limited to a single
``register_connector`` call, which is cheap.
"""

from opshub.connectors._registry import register_connector
from opshub.connectors.github.auth import GITHUB_PAT_SECRET_KEY, get_github_token
from opshub.connectors.github.connector import GitHubConnector

__all__ = ["GITHUB_PAT_SECRET_KEY", "GitHubConnector", "get_github_token"]

# Register exactly once on first import. The registry's idempotency rule
# (registering the *same* instance twice is a no-op) makes this safe
# even when importers come in via several paths within a single
# process; registering a *different* instance under the same name
# would raise — which is what we want if a future refactor accidentally
# ships two GitHubConnector classes.
register_connector(GitHubConnector())
