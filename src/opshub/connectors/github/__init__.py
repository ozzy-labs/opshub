"""GitHub connector (Phase 3 sub-issue B).

Importing this module registers :class:`GitHubConnector` with the
global connector registry so the CLI driver
(``opshub connector sync github``) can discover it. The package also
re-exports :func:`get_github_token` and :data:`GITHUB_PAT_SECRET_KEY`
so callers can write ``from opshub.connectors.github import
get_github_token`` without reaching into the ``auth`` submodule.

Heavy dependencies (``httpx``, shipped in the ``connectors-github``
extra) are deferred: ``connector.py`` imports the ``api`` submodule (the
only ``httpx`` consumer) lazily inside :meth:`GitHubConnector.sync`, so
importing this package pulls *no* third-party connector SDK. The import
side effect here is limited to a single ``register_connector`` call,
which is cheap. This keeps the package import-clean (matching the
sibling Slack / MS365 / Box connectors and the cold-start guard) so an
operator who installed only another connector's extra can still import
``opshub.connectors.github`` for its registration side effect without
the ``httpx`` dependency present.
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
