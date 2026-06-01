"""CLI progress reporting for long-running commands (issue #316).

Long-running commands (``opshub connector sync <name>``,
``opshub embeddings rebuild`` / ``drain``, ``opshub projections rebuild``)
used to run silently until a one-line summary printed at the very end, so
an operator could not tell a slow sync from a hung one. This module gives
those commands a small, rich-backed progress surface in two shapes:

* :func:`indeterminate` — spinner + processed-item count + elapsed time,
  for streaming work whose total is unknown up front. Connector syncs
  paginate, so the item count is not known until the stream drains; the
  spinner + running count + elapsed clock still answer "is it moving, and
  how far has it got".
* :func:`determinate` — bar + percentage + ETA, for work whose total is
  known before the loop starts (event replay, embedding a counted set of
  pending entities). Wired up by the follow-up PRs for ``embeddings`` /
  ``projections``; shipped here as part of the shared foundation.

Design decisions (issue #316):

* **Output channel = stderr.** Progress renders on stderr via
  ``rich.console.Console(stderr=True)`` so the command's stdout result
  line stays clean and pipe/script friendly. Existing CLI tests assert on
  stdout substrings and keep passing unchanged.
* **TTY-gated.** When stderr is not a TTY (CI, pipes, redirects) progress
  is a no-op: no ANSI control codes leak into captured output. The
  ``--progress`` / ``--no-progress`` root flag (and the ``OPSHUB_PROGRESS``
  env var) override the auto-detection in either direction.
* **Cold start (ADR-0001).** ``rich`` is imported lazily inside the
  context managers, never at module load, so importing this module stays
  cheap and ``opshub --help`` never pays for rich. This module is
  ``_``-prefixed so it is reached only through a lazy import inside a
  command callback (the ``test_cli_imports`` static guard skips private
  modules; the ``test_cold_start`` wall-clock tripwire still applies, hence
  the lazy rich import).
* **structlog coexistence.** Connectors log sparingly (mostly WARNING), so
  an interleaved log line during an active progress display is rare and
  tolerable for the MVP; a future iteration can route logs through the
  progress console if it becomes a problem.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Generator

    from rich.console import Console
    from rich.progress import Progress, TaskID


__all__ = [
    "ProgressReporter",
    "determinate",
    "indeterminate",
    "set_preference",
]


# Resolved by the root Typer callback from ``--progress`` / ``--no-progress``.
# ``None`` means "auto-detect from the stderr TTY"; ``True`` / ``False``
# force the behaviour regardless of TTY.
_preference: bool | None = None

# Env var that lets an operator force progress on/off without the flag
# (handy in CI where stderr may be a pseudo-TTY but progress is noise).
_PROGRESS_ENV = "OPSHUB_PROGRESS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def set_preference(enabled: bool | None) -> None:
    """Record the ``--progress`` / ``--no-progress`` root-flag value.

    Called once by the root Typer callback before any subcommand runs.
    ``None`` (the flag's default) leaves the decision to TTY
    auto-detection. Because the root callback runs on every real command
    invocation, the preference is re-set each time and never leaks across
    invocations in a long-lived process (e.g. the test runner).
    """
    global _preference
    _preference = enabled


def _enabled() -> bool:
    """Decide whether to render progress for the current invocation.

    Precedence: explicit ``--progress`` / ``--no-progress`` flag, then the
    ``OPSHUB_PROGRESS`` env var, then stderr-TTY auto-detection.
    """
    if _preference is not None:
        return _preference
    raw = os.environ.get(_PROGRESS_ENV)
    if raw is not None:
        lowered = raw.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False
    return sys.stderr.isatty()


class ProgressReporter(Protocol):
    """Minimal surface a command uses to drive a progress display.

    Both the live (rich-backed) and disabled (no-op) reporters implement
    this, so a command body never branches on whether progress is on — it
    just calls :meth:`advance` per unit of work.
    """

    def advance(self, n: int = 1) -> None:
        """Add ``n`` to the completed count."""
        ...

    def update(self, *, total: int | None = None, description: str | None = None) -> None:
        """Adjust the total and/or the description mid-run."""
        ...


class _NoOpReporter:
    """Reporter returned when progress is disabled (non-TTY / --no-progress).

    Every method is a no-op so callers can drive it unconditionally.
    """

    def advance(self, n: int = 1) -> None:
        return None

    def update(self, *, total: int | None = None, description: str | None = None) -> None:
        return None


class _RichReporter:
    """Reporter backed by a live ``rich.progress.Progress`` task."""

    def __init__(self, progress: Progress, task_id: TaskID) -> None:
        self._progress = progress
        self._task_id = task_id

    def advance(self, n: int = 1) -> None:
        self._progress.advance(self._task_id, n)

    def update(self, *, total: int | None = None, description: str | None = None) -> None:
        # Only forward the fields the caller actually set: rich treats a
        # bare ``update(...)`` arg of ``None`` inconsistently across
        # versions, so passing an explicit ``total=None`` could clobber a
        # known total. Building the kwargs dict avoids that ambiguity.
        fields: dict[str, Any] = {}
        if total is not None:
            fields["total"] = total
        if description is not None:
            fields["description"] = description
        if fields:
            self._progress.update(self._task_id, **fields)


@contextmanager
def indeterminate(
    description: str,
    *,
    console: Console | None = None,
) -> Generator[ProgressReporter]:
    """Spinner + processed-item count + elapsed time for unknown-total work.

    Yields a :class:`ProgressReporter`; call :meth:`ProgressReporter.advance`
    once per processed item. When progress is disabled the yielded reporter
    is a no-op, so the caller's loop body is identical either way.

    ``console`` is injectable for tests (pass a ``Console`` wrapping a
    ``StringIO`` with ``force_terminal=True``); production callers leave it
    ``None`` so the default stderr console is used.
    """
    if not _enabled():
        yield _NoOpReporter()
        return

    from rich.console import Console as _Console
    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    active_console = console or _Console(stderr=True)
    columns = (
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("{task.completed} item(s)"),
        TimeElapsedColumn(),
    )
    with Progress(*columns, console=active_console) as progress:
        task_id = progress.add_task(description, total=None)
        yield _RichReporter(progress, task_id)


@contextmanager
def determinate(
    total: int,
    description: str,
    *,
    console: Console | None = None,
) -> Generator[ProgressReporter]:
    """Bar + percentage + ETA for work whose total is known up front.

    A ``total`` of ``0`` (nothing to do) still renders a completed bar so
    the operator gets confirmation the command ran rather than a silent
    exit; callers that prefer to skip the display entirely can guard on
    their own count first.

    ``console`` is injectable for tests; see :func:`indeterminate`.
    """
    if not _enabled():
        yield _NoOpReporter()
        return

    from rich.console import Console as _Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
        TimeRemainingColumn,
    )

    active_console = console or _Console(stderr=True)
    columns = (
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
    )
    with Progress(*columns, console=active_console) as progress:
        task_id = progress.add_task(description, total=total)
        yield _RichReporter(progress, task_id)
