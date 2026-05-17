"""End-to-end Phase 2 lifecycle tests, split by workstream.

Each test function drives one Phase 2 coordination workstream through
the shipped CLI surface (``inbox`` / ``decision`` / ``lock`` /
``session`` + ``agent`` / ``handoff``), asserting only on observable
outputs (exit codes, stdout, projection rows, workspace files).

The split mirrors plan §1 confirmed item 6 + the Phase 2 readiness
recommendation: per-workstream test functions read better than one
monolithic lifecycle and give pytest's selector enough granularity to
re-run a single workstream when an investigation needs to.

The whole module imports nothing from ``opshub.cli.*`` beyond
:data:`opshub.cli.app.app` — that constraint is the whole point of the
lifecycle test, since it pins the *shipped* CLI contract rather than
implementation details. Internal modules (:mod:`opshub.projections.*`
tables, :mod:`opshub.db.engine`) are imported only to verify on-disk
projection state, never to drive the workflow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.agent_runs import agent_runs_table
from opshub.projections.handoffs import handoffs_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.locks import locks_table
from opshub.projections.work_sessions import work_sessions_table

# Type alias used by every helper to mean "the dict yielded by
# ``isolated_env`` in conftest.py" — kept local rather than promoted to a
# TypedDict because pytest fixture return types are not Protocol-friendly.
_PathsDict = dict[str, Path]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _invoke(args: list[str]) -> tuple[int, str, str]:
    """Run the OpsHub Typer app and return ``(exit_code, stdout, stderr)``.

    Tests build their CliRunner fresh per invocation so output captures
    are isolated. Click 8.2+ Result objects expose ``stdout`` and
    ``stderr`` as separate streams by default, so we can read both
    without flipping any mode flag. The args list is forwarded
    verbatim — every test reads as a sequence of CLI invocations the
    developer could paste into their shell.
    """
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


def _rows(engine: Engine, table: Any) -> list[dict[str, Any]]:
    """Return every row of ``table`` as plain dicts.

    Used by tests to make on-disk projection state legible without
    coupling to the SQLAlchemy ``Row`` API. The engine is created
    short-lived by the caller and disposed in a ``try/finally``.
    """
    with engine.connect() as conn:
        result = conn.execute(select(table)).mappings().all()
    return [dict(row) for row in result]


def _row_count(engine: Engine, table_name: str) -> int:
    """Return ``SELECT COUNT(*)`` for ``table_name``.

    Raw SQL rather than the ORM so the lifecycle test keeps reading
    "what's on disk" rather than "what's in the SQLAlchemy registry".
    """
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())


def _seed_inbox_item(summary: str) -> str:
    """Create a single inbox item via ``opshub inbox add`` and return its ULID."""
    code, out, _ = _invoke(["inbox", "add", summary])
    assert code == 0, out
    item_id = out.strip()
    assert len(item_id) == 26, f"expected 26-char ULID, got {item_id!r}"
    return item_id


# ----------------------------------------------------------------------
# Inbox triage
# ----------------------------------------------------------------------


def test_inbox_triage_lifecycle(isolated_env: _PathsDict) -> None:
    """``opshub inbox`` end-to-end: add → triage (3 paths) → list.

    Drives every shipped inbox CLI surface through one isolated env:

    1. ``inbox add`` emits a 26-char ULID; the projection row appears
       with ``state='pending'``.
    2. ``inbox triage <id> --to-task <title>`` flips the row to
       ``triaged_to_task`` *and* materialises the new task in the
       ``tasks`` projection (both events ride the same UoW per
       :class:`opshub.services.inbox_service.InboxService` docstring).
    3. ``inbox triage <id> --decision <reason>`` flips the row to
       ``triaged_to_decision`` and records the reason; no
       ``decisions`` row is written yet (the decision service is the
       only thing that may append :class:`DecisionRecorded`, per the
       cross-service note in the inbox service docstring).
    4. ``inbox triage <id> --discard <reason>`` flips the row to
       ``discarded`` and records the reason.
    5. ``inbox list --format json`` returns the documented row shape.
    """
    db_path = isolated_env["db_path"]

    # ---- 1. add ----------------------------------------------------------
    first_id = _seed_inbox_item("triage me — to task")
    second_id = _seed_inbox_item("triage me — to decision")
    third_id = _seed_inbox_item("triage me — discard")

    engine = create_engine_for_sqlite(db_path)
    try:
        rows = _rows(engine, inbox_items_table)
        by_id = {row["id"]: row for row in rows}
        assert {first_id, second_id, third_id} <= set(by_id)
        for item_id in (first_id, second_id, third_id):
            assert by_id[item_id]["state"] == "pending"
            assert by_id[item_id]["disposition"] is None
            assert by_id[item_id]["target_id"] is None
    finally:
        engine.dispose()

    # ---- 2. triage --to-task --------------------------------------------
    code, out, _ = _invoke(["inbox", "triage", first_id, "--to-task", "promoted from inbox"])
    assert code == 0, out
    # Stdout for the ``--to-task`` branch is the new task's ULID
    # (per ``opshub inbox triage`` docstring).
    new_task_id = out.strip()
    assert len(new_task_id) == 26
    assert new_task_id != first_id

    engine = create_engine_for_sqlite(db_path)
    try:
        inbox_rows = {row["id"]: row for row in _rows(engine, inbox_items_table)}
        assert inbox_rows[first_id]["state"] == "triaged_to_task"
        assert inbox_rows[first_id]["disposition"] == "to_task"
        assert inbox_rows[first_id]["target_id"] == new_task_id

        # The task projection row must exist too — both ItemTriaged and
        # TaskCreated ride the same Unit of Work.
        task_count = _row_count(engine, "tasks")
        assert task_count == 1
        # And the event log carries exactly two new events for this
        # triage (ItemTriaged + TaskCreated) on top of the three
        # ItemEnqueued events from the seeds above.
        assert _row_count(engine, "events") == 3 + 2
    finally:
        engine.dispose()

    # ---- 3. triage --decision -------------------------------------------
    code, out, _ = _invoke(["inbox", "triage", second_id, "--decision", "needs ADR"])
    assert code == 0, out
    # Stdout for the ``--decision`` branch is the pre-allocated
    # decision ULID (per ``opshub inbox triage`` docstring).
    decision_target_id = out.strip()
    assert len(decision_target_id) == 26

    engine = create_engine_for_sqlite(db_path)
    try:
        inbox_rows = {row["id"]: row for row in _rows(engine, inbox_items_table)}
        assert inbox_rows[second_id]["state"] == "triaged_to_decision"
        assert inbox_rows[second_id]["disposition"] == "decision"
        assert inbox_rows[second_id]["target_id"] == decision_target_id
        assert inbox_rows[second_id]["reason"] == "needs ADR"
        # The decision service is the only writer of ``decisions``
        # rows; the inbox triage --decision branch does NOT pre-create
        # one (see ``inbox_service`` docstring §"Cross-service note").
        assert _row_count(engine, "decisions") == 0
    finally:
        engine.dispose()

    # ---- 4. triage --discard --------------------------------------------
    code, out, _ = _invoke(["inbox", "triage", third_id, "--discard", "spam"])
    assert code == 0, out
    # Stdout for the discard branch is the item's own ULID (no
    # downstream target id exists).
    assert out.strip() == third_id

    engine = create_engine_for_sqlite(db_path)
    try:
        inbox_rows = {row["id"]: row for row in _rows(engine, inbox_items_table)}
        assert inbox_rows[third_id]["state"] == "discarded"
        assert inbox_rows[third_id]["disposition"] == "discard"
        assert inbox_rows[third_id]["target_id"] is None
        assert inbox_rows[third_id]["reason"] == "spam"
    finally:
        engine.dispose()

    # ---- 5. inbox list --format json ------------------------------------
    code, out, _ = _invoke(["inbox", "list", "--format", "json"])
    assert code == 0, out
    payload: list[dict[str, Any]] = json.loads(out)
    assert isinstance(payload, list)
    assert {row["id"] for row in payload} >= {first_id, second_id, third_id}
    # Documented JSON shape: every row must carry id / state / summary.
    for row in payload:
        for key in ("id", "summary", "state"):
            assert key in row, f"missing key {key!r} in inbox list JSON row"


# ----------------------------------------------------------------------
# Decisions
# ----------------------------------------------------------------------


def test_decisions_lifecycle(isolated_env: _PathsDict) -> None:
    """``opshub decision record`` → projection row → ``decision list --format json``.

    The decision aggregate is append-only (no edit / supersede in
    Phase 2), so a single ``record`` is enough to exercise the whole
    surface. The ULID echoed on stdout must round-trip through the
    ``decisions`` projection row keyed on the same id.
    """
    db_path = isolated_env["db_path"]

    code, out, _ = _invoke(["decision", "record", "adopt ADR-0013", "--context", "phase 2 step 5"])
    assert code == 0, out
    decision_id = out.strip()
    assert len(decision_id) == 26

    engine = create_engine_for_sqlite(db_path)
    try:
        rows = _rows(engine, table=_decisions_table())
        assert len(rows) == 1
        assert rows[0]["id"] == decision_id
        assert rows[0]["text"] == "adopt ADR-0013"
        assert rows[0]["context"] == "phase 2 step 5"
    finally:
        engine.dispose()

    code, out, _ = _invoke(["decision", "list", "--format", "json"])
    assert code == 0, out
    payload: list[dict[str, Any]] = json.loads(out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["id"] == decision_id


def _decisions_table() -> Any:
    """Lazy import of the ``decisions`` projection table.

    Keeps the top-level imports of this module tight (no Phase 2
    projection imports get evaluated by tests that don't read decisions).
    """
    from opshub.projections.decisions import decisions_table

    return decisions_table


# ----------------------------------------------------------------------
# Locks
# ----------------------------------------------------------------------


def _invoke_raw(args: list[str]) -> Any:
    """Run the OpsHub Typer app and return the raw ``click.testing.Result``.

    Error-path tests need :attr:`Result.exception` because the CLI's
    top-level :class:`OpsHubError` handler in :func:`opshub.cli.app.main`
    sits *outside* the Typer ``app`` object — :class:`CliRunner` invokes
    ``app`` directly, so on the error path the exception propagates back
    to the runner rather than being mapped to ``stderr``. Asserting on
    ``result.exception`` keeps the test honest about what the service
    layer actually raises.
    """
    runner = CliRunner()
    return runner.invoke(app, args)


def test_lock_lifecycle(isolated_env: _PathsDict) -> None:
    """``opshub lock`` end-to-end: acquire → conflict → project-reserved → release → list.

    Exercises the documented branches of
    :class:`opshub.services.lock_service.LockService.acquire`:

    1. Fresh ``task:<ulid>`` acquire returns a 26-char lock ULID and
       inserts a ``locks`` row with ``released_at IS NULL``.
    2. A different actor on the same scope raises
       :class:`opshub.core.errors.ConflictError` (caught by Typer's
       default exception handler under :class:`CliRunner` and surfaced
       as a non-zero ``exit_code``). The projection stays at one row.
    3. ``project:<ulid>`` is reserved (ADR-0013) and surfaces a
       :class:`NotImplementedError`; CLI exits non-zero.
    4. ``release`` populates ``released_at``; ``list --format json``
       reflects the now-empty active set.

    Step (1a) verifies idempotent reacquire (ADR-0013): the same
    ``(actor, work_session_id)`` reacquiring an existing lock returns
    the original ULID without appending a second event. The earlier
    naive-datetime bug in :func:`_reconstruct_acquired` is fixed by
    rehydrating SQLite-read datetimes at the :func:`_row_to_lock`
    boundary.
    """
    db_path = isolated_env["db_path"]

    # A task ULID for the lock scope. We do NOT create a real task —
    # the lock service only validates the ULID *shape*, not its
    # presence in the ``tasks`` projection (ADR-0013 keeps scope
    # validation cheap so locks can be taken before the underlying
    # task row materialises).
    from opshub.core.errors import ConflictError
    from opshub.core.ids import new_ulid

    task_ulid = new_ulid()

    # ---- 1. fresh acquire ------------------------------------------------
    code, out, _ = _invoke(["lock", "acquire", f"task:{task_ulid}", "--actor", "cli:alice"])
    assert code == 0, out
    lock_id = out.strip()
    assert len(lock_id) == 26

    engine = create_engine_for_sqlite(db_path)
    try:
        rows = _rows(engine, locks_table)
        assert len(rows) == 1
        assert rows[0]["id"] == lock_id
        assert rows[0]["actor"] == "cli:alice"
        assert rows[0]["scope_type"] == "task"
        assert rows[0]["scope_id"] == task_ulid
        assert rows[0]["released_at"] is None
    finally:
        engine.dispose()

    # ---- 1a. idempotent reacquire (ADR-0013): same owner → same ULID ----
    code, out_again, _ = _invoke(["lock", "acquire", f"task:{task_ulid}", "--actor", "cli:alice"])
    assert code == 0, out_again
    assert out_again.strip() == lock_id, "same owner reacquire must echo the original lock ULID"
    # No second row was inserted.
    engine = create_engine_for_sqlite(db_path)
    try:
        assert _row_count(engine, "locks") == 1
    finally:
        engine.dispose()

    # ---- 2. different actor → conflict ----------------------------------
    result = _invoke_raw(["lock", "acquire", f"task:{task_ulid}", "--actor", "cli:bob"])
    assert result.exit_code != 0
    # CliRunner stores the raised exception under ``.exception`` because
    # the OpsHubError handler in ``main()`` sits outside ``app``. The
    # ConflictError message includes the held scope and the holder's
    # actor string (see ``LockService.acquire``).
    assert isinstance(result.exception, ConflictError), repr(result.exception)
    assert "held by" in str(result.exception).lower(), str(result.exception)
    # The conflict must not insert a second row.
    engine = create_engine_for_sqlite(db_path)
    try:
        assert _row_count(engine, "locks") == 1
    finally:
        engine.dispose()

    # ---- 3. project: scope → NotImplementedError -------------------------
    other_ulid = new_ulid()
    result = _invoke_raw(["lock", "acquire", f"project:{other_ulid}", "--actor", "cli:alice"])
    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError), repr(result.exception)

    # ---- 4. release ------------------------------------------------------
    code, out, _ = _invoke(["lock", "release", lock_id, "--actor", "cli:alice"])
    assert code == 0, out

    engine = create_engine_for_sqlite(db_path)
    try:
        rows = _rows(engine, locks_table)
        assert len(rows) == 1
        assert rows[0]["id"] == lock_id
        assert rows[0]["released_at"] is not None
    finally:
        engine.dispose()

    # ---- 5. list --format json (active set is empty now) ----------------
    code, out, _ = _invoke(["lock", "list", "--format", "json"])
    assert code == 0, out
    payload: list[dict[str, Any]] = json.loads(out)
    # ``list_active`` excludes released locks (ADR-0013).
    assert payload == []


# ----------------------------------------------------------------------
# Work session + agent run bracket
# ----------------------------------------------------------------------


def test_session_run_bracket_lifecycle(isolated_env: _PathsDict) -> None:
    """``opshub session start`` → ``agent run begin`` → ``agent run end`` → ``session end``.

    The work-session bracket is the canonical Phase 2 step 6 surface:

    1. ``session start`` echoes the new session ULID and writes the
       state file at ``$XDG_STATE_HOME/opshub/current-session``.
    2. ``agent run begin <name>`` consumes the state-file session via
       :func:`opshub.cli._actor.resolve_owner`, so the ``agent_runs``
       row's ``work_session_id`` matches the active session.
    3. ``agent run end <id> --summary ...`` flips the agent run to
       ``state='ended'`` and records the summary.
    4. ``session end --summary ...`` flips the session row to
       ``state='ended'`` and clears the state file.
    """
    db_path = isolated_env["db_path"]
    state_dir = isolated_env["state_dir"]
    state_file = state_dir / "opshub" / "current-session"

    # ---- 1. session start ------------------------------------------------
    code, out, _ = _invoke(["session", "start", "--scope", "phase-2-e2e"])
    assert code == 0, out
    session_id = out.strip()
    assert len(session_id) == 26

    engine = create_engine_for_sqlite(db_path)
    try:
        rows = _rows(engine, work_sessions_table)
        assert len(rows) == 1
        assert rows[0]["id"] == session_id
        assert rows[0]["scope"] == "phase-2-e2e"
        assert rows[0]["state"] == "active"
        assert rows[0]["ended_at"] is None
    finally:
        engine.dispose()
    # ``session start`` must have written the state file under our
    # redirected $XDG_STATE_HOME (see ``isolated_env``).
    assert state_file.is_file(), f"state file missing at {state_file}"
    assert state_file.read_text(encoding="utf-8").strip() == session_id

    # ---- 2. agent run begin ---------------------------------------------
    code, out, _ = _invoke(["agent", "run", "begin", "claude"])
    assert code == 0, out
    run_id = out.strip()
    assert len(run_id) == 26

    engine = create_engine_for_sqlite(db_path)
    try:
        run_rows = _rows(engine, agent_runs_table)
        assert len(run_rows) == 1
        assert run_rows[0]["id"] == run_id
        assert run_rows[0]["agent_name"] == "claude"
        # ``work_session_id`` must auto-resolve from the state file.
        assert run_rows[0]["work_session_id"] == session_id
        assert run_rows[0]["state"] == "active"
    finally:
        engine.dispose()

    # ---- 3. agent run end -----------------------------------------------
    code, out, _ = _invoke(["agent", "run", "end", run_id, "--summary", "ok"])
    assert code == 0, out

    engine = create_engine_for_sqlite(db_path)
    try:
        run_rows = _rows(engine, agent_runs_table)
        assert len(run_rows) == 1
        assert run_rows[0]["state"] == "ended"
        assert run_rows[0]["summary"] == "ok"
        assert run_rows[0]["ended_at"] is not None
    finally:
        engine.dispose()

    # ---- 4. session end --------------------------------------------------
    code, out, _ = _invoke(["session", "end", "--summary", "done"])
    assert code == 0, out

    engine = create_engine_for_sqlite(db_path)
    try:
        rows = _rows(engine, work_sessions_table)
        assert len(rows) == 1
        assert rows[0]["state"] == "ended"
        assert rows[0]["summary"] == "done"
        assert rows[0]["ended_at"] is not None
    finally:
        engine.dispose()
    # ``session end`` clears the state file (see
    # :func:`opshub.cli._actor.clear_current_session`).
    assert not state_file.exists(), f"state file should be cleared, still at {state_file}"


# ----------------------------------------------------------------------
# Handoffs
# ----------------------------------------------------------------------


def test_handoff_lifecycle(isolated_env: _PathsDict) -> None:
    """``opshub handoff open`` → ``list`` → ``close`` → ``workspace generate``.

    The handoff aggregate's user-facing surface is three CLI commands
    plus the workspace renderer. We drive all four:

    1. ``open --from --to --topic`` echoes the new handoff ULID and
       inserts a row with ``state='open'``.
    2. ``list`` shows the open row (any format, default is fine for the
       smoke check).
    3. ``close <id> --note <text>`` flips the row to ``state='closed'``
       and records the note.
    4. ``workspace generate`` materialises per-handoff ``.md`` files
       plus the renderer's ``index.md`` under
       ``<workspace_root>/generated/handoffs/``.
    """
    db_path = isolated_env["db_path"]
    workspace_root = isolated_env["workspace_root"]

    code, out, _ = _invoke(
        [
            "handoff",
            "open",
            "--from",
            "agent:claude",
            "--to",
            "ozzy",
            "--topic",
            "review",
        ]
    )
    assert code == 0, out
    handoff_id = out.strip()
    assert len(handoff_id) == 26

    engine = create_engine_for_sqlite(db_path)
    try:
        rows = _rows(engine, handoffs_table)
        assert len(rows) == 1
        assert rows[0]["id"] == handoff_id
        assert rows[0]["from_actor"] == "agent:claude"
        assert rows[0]["to_actor"] == "ozzy"
        assert rows[0]["topic"] == "review"
        assert rows[0]["state"] == "open"
    finally:
        engine.dispose()

    code, out, _ = _invoke(["handoff", "list"])
    assert code == 0, out
    # The default ``table`` renderer prints at least the truncated ID.
    assert handoff_id[:8] in out, out

    code, out, _ = _invoke(["handoff", "close", handoff_id, "--note", "done"])
    assert code == 0, out

    engine = create_engine_for_sqlite(db_path)
    try:
        rows = _rows(engine, handoffs_table)
        assert rows[0]["state"] == "closed"
        assert rows[0]["note"] == "done"
        assert rows[0]["closed_at"] is not None
    finally:
        engine.dispose()

    code, out, _ = _invoke(["workspace", "generate"])
    assert code == 0, out

    handoffs_dir = workspace_root / "generated" / "handoffs"
    assert (handoffs_dir / f"{handoff_id}.md").is_file()
    assert (handoffs_dir / "index.md").is_file()


# ----------------------------------------------------------------------
# Cross-workstream idempotency
# ----------------------------------------------------------------------


def test_workspace_regenerate_is_idempotent_across_all_workstreams(
    isolated_env: _PathsDict,
) -> None:
    """Idempotency across every Phase 2 read model.

    Seeds one of each entity, then runs ``workspace generate`` and
    ``projections rebuild`` twice. ADR-0002 ("disposable read models")
    + ADR-0003 ("disposable workspace") both require: a second
    invocation on unchanged state must be a no-op.

    Concretely:

    * ``workspace generate`` (second call) must report ``wrote 0
      file(s)``.
    * ``projections rebuild`` (second call) must leave every projection
      row count unchanged.
    """
    db_path = isolated_env["db_path"]

    # Seed one of each entity via the CLI (matches what a real user would do).
    inbox_id = _seed_inbox_item("e2e seed item")
    code, _, _ = _invoke(["decision", "record", "e2e seed decision"])
    assert code == 0
    code, _, _ = _invoke(
        [
            "handoff",
            "open",
            "--from",
            "ozzy",
            "--to",
            "agent:claude",
            "--topic",
            "e2e seed handoff",
        ]
    )
    assert code == 0
    code, out, _ = _invoke(["session", "start", "--scope", "e2e seed session"])
    assert code == 0, out
    # Take a lock too, so the locks projection has a row to count.
    from opshub.core.ids import new_ulid

    lock_scope = f"task:{new_ulid()}"
    code, _, _ = _invoke(["lock", "acquire", lock_scope, "--actor", "cli:e2e"])
    assert code == 0

    # ``inbox_id`` is referenced by the asserts below; spell out the
    # dependency so future readers don't trim the seed line.
    assert len(inbox_id) == 26

    # ---- workspace generate twice ---------------------------------------
    code, out, _ = _invoke(["workspace", "generate"])
    assert code == 0, out
    # First run must write at least one file. (We don't pin an exact
    # number here — the per-renderer indexes are an implementation
    # detail of step 8; pinning them would tie this test to that step's
    # internals.)
    assert "wrote" in out
    assert "wrote 0 file(s)" not in out

    code, out, _ = _invoke(["workspace", "generate"])
    assert code == 0, out
    assert "wrote 0 file(s)" in out, out

    # ---- projections rebuild twice --------------------------------------
    engine = create_engine_for_sqlite(db_path)
    try:
        before = {
            "inbox_items": _row_count(engine, "inbox_items"),
            "decisions": _row_count(engine, "decisions"),
            "handoffs": _row_count(engine, "handoffs"),
            "work_sessions": _row_count(engine, "work_sessions"),
            "locks": _row_count(engine, "locks"),
        }
    finally:
        engine.dispose()

    code, out, _ = _invoke(["projections", "rebuild"])
    assert code == 0, out
    code, out, _ = _invoke(["projections", "rebuild"])
    assert code == 0, out

    engine = create_engine_for_sqlite(db_path)
    try:
        after = {
            "inbox_items": _row_count(engine, "inbox_items"),
            "decisions": _row_count(engine, "decisions"),
            "handoffs": _row_count(engine, "handoffs"),
            "work_sessions": _row_count(engine, "work_sessions"),
            "locks": _row_count(engine, "locks"),
        }
    finally:
        engine.dispose()

    assert before == after, f"projection row counts drifted on rebuild: {before} -> {after}"


# Re-export ``pytest`` so static analysers see this module is a pytest test
# (the import would otherwise read as unused).
_ = pytest
