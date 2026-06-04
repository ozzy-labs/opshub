"""Time helpers — always tz-aware UTC.

`datetime.utcnow()` is forbidden across the codebase: it drops tz info and is
deprecated in Python 3.12+. Use `now_utc()` for the current instant and
`to_utc(dt)` to normalise any datetime crossing module boundaries.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from opshub.core.errors import ValidationError


def now_utc() -> datetime:
    """Return the current instant as a tz-aware UTC datetime."""
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a tz-aware UTC datetime.

    Naive datetimes are rejected: callers must be explicit about timezone to
    avoid silent UTC assumptions that bite in cross-tz audit trails.
    """
    if dt.tzinfo is None:
        raise ValidationError("naive datetime is not allowed; attach tzinfo before to_utc()")
    return dt.astimezone(UTC)


#: ``since`` relative-form pattern. Accepts ``<N>d`` (days) and ``<N>w``
#: (weeks). Months / years are intentionally unsupported — their
#: calendar semantics are ambiguous for a cutoff filter and ``90d`` /
#: ``365d`` cover the practical range without extra surface to test.
#: Lifted verbatim from the original ``cli._slack_conversations`` home
#: (Phase 20, #459) so both the ``opshub slack conversations --since``
#: CLI flag and the ``[connectors.slack] sync_since`` / per-channel
#: ``since`` config floor parse identical grammar.
_SINCE_RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([dw])\s*$")


def parse_since(raw: str, *, field: str = "--since") -> datetime:
    """Parse a ``since`` value into a tz-aware UTC :class:`datetime.datetime`.

    Accepts two forms (see :data:`_SINCE_RELATIVE_RE` for the relative
    grammar):

    * Relative: ``"7d"`` / ``"2w"`` → ``now_utc() - timedelta(...)``.
      ``"0d"`` is permitted and resolves to "now" (a degenerate but
      harmless filter). Relative values are evaluated **at call time**,
      so a ``[connectors.slack] sync_since = "90d"`` floor advances with
      each sync run rather than freezing at config-load time.
    * Absolute: any ISO 8601 string :func:`datetime.fromisoformat`
      accepts, plus the convenience that a trailing ``Z`` (UTC zulu) is
      rewritten to ``+00:00`` so ``"2026-05-01T00:00:00Z"`` parses
      cleanly. tz-naive inputs are interpreted as UTC.

    Raises :class:`~opshub.core.errors.ValidationError` for empty input,
    unknown forms, and malformed numerics. ``field`` is interpolated into
    the message so each caller surfaces its own vocabulary — the CLI
    ``--since`` callback passes the default and re-wraps the
    :class:`ValidationError` into :class:`typer.BadParameter` (exit code
    2), while :class:`~opshub.core.config.SlackConnectorSettings`
    re-wraps it into :class:`~opshub.core.errors.ConfigError` with the
    ``[connectors.slack] sync_since`` label (Phase 20, #459 / ADR-0036).
    """
    if not raw or not raw.strip():
        raise ValidationError(f"{field} must not be empty")

    text = raw.strip()
    relative = _SINCE_RELATIVE_RE.match(text)
    if relative is not None:
        amount = int(relative.group(1))
        unit = relative.group(2)
        # ``\d+`` is unbounded, so a typo like ``99999999999d`` would
        # propagate to :class:`timedelta` and raise :class:`OverflowError`.
        # Translate it into the documented usage-error vocabulary.
        try:
            delta = timedelta(days=amount) if unit == "d" else timedelta(weeks=amount)
        except OverflowError as exc:
            raise ValidationError(
                f"{field} {raw!r} is too far in the past; use an ISO date "
                "(e.g. '2026-05-01') for cutoffs beyond a few centuries."
            ) from exc
        return now_utc() - delta

    iso_text = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError as exc:
        raise ValidationError(
            f"{field} {raw!r} is not a recognised value: expected a relative "
            "duration like '7d' / '2w' or an ISO date like '2026-05-01'."
        ) from exc

    if parsed.tzinfo is None:
        # Naive inputs default to UTC so callers can write ``2026-05-01``
        # without manually annotating timezone (ADR-0027 keeps internal
        # tz handling on UTC).
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def since_to_ts(dt: datetime) -> str:
    """Render a tz-aware datetime as a Slack ``"seconds.microseconds"`` ts.

    Slack's ``conversations.history`` ``oldest`` parameter and the
    connector's per-channel resume cursor are both ``ts`` strings
    compared via :func:`float`. Converting a :func:`parse_since` result
    through this helper lets the Slack connector feed a date floor into
    the same ``_max_ts`` comparison it already uses for cursors
    (Phase 20, ADR-0036).
    """
    return f"{dt.timestamp():.6f}"
