"""``opshub web ...`` subcommands (Phase 21-C, ADR-0037 / ADR-0031).

The web connector (Phase 21-C, ADR-0037 / ADR-0010 §Phase 21 改訂) renders
each URL the operator lists in ``[connectors.web] pages`` with the
Playwright browser core and persists the rendered DOM text as a
``web_page`` source. It has *no* OAuth / token surface — a public Web page
needs no auth (the ``connect_over_cdp`` escape hatch for authenticated
sessions is reserved for a future phase, ADR-0037 §決定 (b)). Operators
configure the URL list via ``[connectors.web] pages`` in ``opshub.toml``
and provision Chromium once with ``playwright install chromium``.

Per ADR-0031 §決定 (6) (the box_drive precedent), this module ships
``sync`` **only** — there is no ``auth`` sub-app. ``opshub web auth set``
falls through Typer's default "No such command 'auth'" exit-2 path rather
than being intercepted with a no-op reject, which is the cleaner UX
(operator sees ``Usage:`` explicitly).
"""

from __future__ import annotations

import typer

web_app = typer.Typer(
    name="web",
    help=(
        "Browser-rendered Web page connector (Phase 21-C, ADR-0037). "
        "No auth surface — configure [connectors.web] pages in opshub.toml "
        "and run 'playwright install chromium' once."
    ),
    no_args_is_help=True,
)


@web_app.command("sync")
def web_sync() -> None:
    """Render every ``[connectors.web] pages`` URL and observe changed pages.

    Each URL is fetched with the Playwright browser core (headless
    Chromium, ADR-0037), the rendered DOM text is extracted, and a
    ``web_page`` source is persisted. Change detection uses a SHA-256
    fingerprint of the extracted body (ADR-0010 §Phase 21 改訂): an
    unchanged page emits no event, so re-running the sync is idempotent. A
    single dead URL is logged at WARN and skipped; the other pages still
    sync.

    Authentication: none — public pages only. When Chromium is not
    installed the command fails with a ``ConfigError`` pointing at
    ``playwright install chromium`` (ADR-0037 §決定 (g)).

    Progress renders on stderr when attached to a TTY (ADR-0026); override
    with ``--progress`` / ``--no-progress`` or ``OPSHUB_PROGRESS``.
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("web")
