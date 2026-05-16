"""Cold-start tripwire for ``opshub --help``.

ADR-0001 §Negative §1 sets a 300ms local target for the cold-start of
``opshub --help`` and lists the lazy-import discipline as the mechanism
that keeps the budget. This test is the CI tripwire that catches a
regression past a generous 800ms ceiling — CI / containerised runs are
routinely 2-3x slower than a developer's laptop for the first
invocation (cold filesystem, cold Python bytecode cache), and we only
measure once per run, so we deliberately set the threshold above the
local target.

The measured time is always printed to stdout so it surfaces in CI logs
as an early-warning signal: if the number trends toward 800ms across
PRs, the lazy-import discipline is fraying and a follow-up PR should
investigate.

The test runs ``python -m opshub --help`` via :func:`subprocess.run` so
it pays the real process-startup cost — invoking ``CliRunner`` in-proc
would skip the import phase, which is the exact thing we want to
measure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

# Generous ceiling: the ADR-0001 local target is 300ms, but containerised
# CI runners with cold caches are routinely 2-3x slower. 800ms catches a
# real regression (e.g. accidentally importing torch / sentence-transformers
# at module scope) without flaking on a busy CI agent.
_COLD_START_BUDGET_SECONDS = 0.8


@pytest.mark.skipif(
    os.environ.get("OPSHUB_SKIP_COLD_START") == "1",
    reason="OPSHUB_SKIP_COLD_START=1 (opt-out for slow filesystems / sandboxes)",
)
def test_opshub_help_cold_start_under_budget(capsys: pytest.CaptureFixture[str]) -> None:
    """``python -m opshub --help`` must return within ``_COLD_START_BUDGET_SECONDS``.

    The measured value is printed so CI logs surface it as a tripwire.
    """
    # ``PYTHONUNBUFFERED=1`` ensures the child process flushes stdout
    # promptly, which keeps wall-clock measurement aligned with the
    # observable user experience.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    start = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "opshub", "--help"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )
    elapsed = time.perf_counter() - start

    # Print outside the assertion so the value surfaces in CI logs even
    # when the test passes. ``capsys`` propagates this to pytest output.
    with capsys.disabled():
        print(f"\nopshub --help cold start: {elapsed * 1000:.1f}ms")

    assert completed.returncode == 0, (
        f"opshub --help exited {completed.returncode}\n"
        f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
    )
    # Sanity: the help text actually rendered.
    assert "opshub" in completed.stdout.lower()

    assert elapsed <= _COLD_START_BUDGET_SECONDS, (
        f"opshub --help cold start regressed to {elapsed * 1000:.1f}ms "
        f"(budget: {_COLD_START_BUDGET_SECONDS * 1000:.0f}ms). "
        "Check for new module-level imports in opshub.cli.* "
        "(ADR-0001 lazy-import rule)."
    )
