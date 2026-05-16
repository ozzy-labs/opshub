"""Jinja2 ``Environment`` factory for OpsHub markdown rendering.

The factory is isolated in its own subpackage so that the rendering
configuration (autoescape rules, undefined behaviour, whitespace control)
lives in exactly one place. Both :mod:`opshub.markdown.tasks` and any
future templated outputs (status reports, agent reviews, etc.) should
construct their environment via :func:`env` rather than instantiating
``Environment`` directly.

Design choices:

* ``PackageLoader("opshub.markdown", "templates")`` resolves templates via
  :mod:`importlib.resources`, so they ship inside the built wheel without
  relying on the source checkout layout.
* ``autoescape=select_autoescape(disabled_extensions=("md",))`` keeps
  markdown output verbatim (no HTML-entity escaping on ``&`` / ``<``).
  Auto-escape is still enabled for any future ``.html`` templates.
* ``undefined=StrictUndefined`` makes typos in template variable names a
  hard error rather than silently rendering an empty string — important
  for a regeneration pipeline where output is the contract.
* ``keep_trailing_newline=True`` together with ``trim_blocks=True`` and
  ``lstrip_blocks=True`` produces compact, line-ending-stable markdown so
  the idempotency check (byte-equality between consecutive renders)
  doesn't trip on stray whitespace.
"""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

__all__ = ["env"]


def env() -> Environment:
    """Return a fresh Jinja2 ``Environment`` configured for OpsHub markdown."""
    return Environment(
        loader=PackageLoader("opshub.markdown", "templates"),
        autoescape=select_autoescape(disabled_extensions=("md",)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
