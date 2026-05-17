"""Phase 8 manual link CRUD lifecycle (E1 closeout, ADR-0017).

Drives ``opshub link add`` / ``link list`` / ``link remove`` end-to-end
through :class:`typer.testing.CliRunner` so the operator-facing
contract for the **manual** path (ADR-0017 §決定 (d)) is pinned by a
single integration walk.

Why a dedicated integration test (vs. the unit tests in
``tests/unit/cli/test_link.py``):

* The unit tests exercise the CLI commands with a real migrated
  engine + writer-side :class:`LinkService` but each verb is isolated
  in its own test. The integration walk asserts the **lifecycle**
  contract: ``add`` mints a row + an event, ``list`` surfaces the row
  with the correct filters, ``remove`` deletes the row but keeps the
  event, ``remove`` again on the missing id is a clean no-op with
  exit 0.
* The integration walk also drives the CLI through the same
  :func:`isolated_env` fixture every Phase 1-7 lifecycle uses so a
  regression in the wiring (``build_link_service`` / actor resolution
  / DB env var redirection) surfaces here even when the unit tests
  pass with a hand-rolled fixture.

ADR-0017 contracts pinned end-to-end:

- (d) Manual link CRUD emits ``LinkCreated`` / ``LinkDeleted`` events
  on the same event log every other operator action lands on (single
  source of truth, ADR-0002).
- (h) Hard delete — after ``link remove`` the projection row is
  physically gone (no ``deleted_at`` column to filter on).
- ``LinkDeleted`` on a non-existent id is **not** an error — the
  audit event is still appended (the operator's intent is recorded)
  but the CLI exits 0 with a "(no-op)" message.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import events_table
from opshub.projections.links import links_table

_PathsDict = dict[str, Path]


def _invoke(args: list[str]) -> tuple[int, str, str]:
    """Run ``opshub <args>`` through :class:`CliRunner` and return triplet."""
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


def _extract_link_id(add_stdout: str) -> str:
    """Parse the link id from ``opshub link add`` stdout.

    The CLI prints ``Link <short>... id=<full-ulid>``; we pull the
    full ULID off the suffix so subsequent ``link remove`` calls can
    address the same row.
    """
    for token in add_stdout.split():
        if token.startswith("id="):
            value = token.rstrip(")").removeprefix("id=")
            return value
    raise AssertionError(f"could not parse link id from: {add_stdout!r}")


def test_manual_link_lifecycle_add_list_remove_noop(
    isolated_env: _PathsDict,
) -> None:
    """Walk the manual link CRUD: add → list filters → remove → remove no-op.

    Sequence:

    1. ``opshub link add task:01J... source:01J... --type references`` →
       verify exit 0, projection has 1 row, events log has one
       ``link.created`` row whose ``aggregate_id`` matches the
       printed id.
    2. ``opshub link list --from task:01J...`` → row appears in JSON.
    3. ``opshub link list --type references`` → type filter works.
    4. ``opshub link list --type bogus`` → filter excludes the row.
    5. ``opshub link remove <id> --reason "wrong source"`` → projection
       row gone, events log has one ``link.deleted`` row whose
       ``aggregate_id`` matches the deleted id.
    6. ``opshub link remove <id>`` again → exit 0, "no-op" message,
       a **second** ``link.deleted`` event is appended (audit trail
       for the second attempt).
    """
    # ---- 1. add --------------------------------------------------------
    from_entity = "task:01J0000000000000000000FROM"
    to_entity = "source:01J00000000000000000000TO"
    code, add_out, err = _invoke(["link", "add", from_entity, to_entity, "--type", "references"])
    assert code == 0, add_out + (err or "")
    assert "added" in add_out, add_out
    link_id = _extract_link_id(add_out)
    assert len(link_id) == 26

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # 1a. projection row exists with the natural-key tuple supplied.
        with engine.connect() as conn:
            link_rows = conn.execute(select(links_table)).mappings().all()
        assert len(link_rows) == 1, link_rows
        row = link_rows[0]
        assert row["id"] == link_id
        assert row["from_entity_type"] == "task"
        assert row["from_entity_id"] == "01J0000000000000000000FROM"
        assert row["to_entity_type"] == "source"
        assert row["to_entity_id"] == "01J00000000000000000000TO"
        assert row["link_type"] == "references"

        # 1b. events log has one ``link.created`` event with the same id.
        with engine.connect() as conn:
            created_events = conn.execute(
                select(events_table).where(events_table.c.event_type == "link.created")
            ).all()
        assert len(created_events) == 1
        assert created_events[0].aggregate_id == link_id

        # ---- 2. list --from filter -----------------------------------
        code, list_out, err = _invoke(["link", "list", "--from", from_entity, "--format", "json"])
        assert code == 0, list_out + (err or "")
        payload: list[dict[str, object]] = json.loads(list_out)
        assert isinstance(payload, list)
        assert len(payload) == 1
        assert payload[0]["id"] == link_id

        # ---- 3. list --type filter (matches) -------------------------
        code, type_match_out, err = _invoke(
            ["link", "list", "--type", "references", "--format", "json"]
        )
        assert code == 0, type_match_out + (err or "")
        payload = json.loads(type_match_out)
        assert len(payload) == 1
        assert payload[0]["id"] == link_id

        # ---- 4. list --type filter (no match) ------------------------
        code, type_miss_out, err = _invoke(
            ["link", "list", "--type", "applied_to", "--format", "json"]
        )
        assert code == 0, type_miss_out + (err or "")
        payload = json.loads(type_miss_out)
        assert payload == []

        # ---- 5. remove with --reason ---------------------------------
        code, remove_out, err = _invoke(["link", "remove", link_id, "--reason", "wrong source"])
        assert code == 0, remove_out + (err or "")
        assert "removed" in remove_out, remove_out

        # 5a. projection row hard-deleted (ADR-0017 §決定 (h)).
        with engine.connect() as conn:
            remaining = conn.execute(select(links_table)).all()
        assert remaining == []

        # 5b. one ``link.deleted`` event with the same aggregate_id.
        with engine.connect() as conn:
            deleted_events = conn.execute(
                select(events_table).where(events_table.c.event_type == "link.deleted")
            ).all()
        assert len(deleted_events) == 1
        assert deleted_events[0].aggregate_id == link_id

        # ---- 6. remove (no-op on missing id) -------------------------
        code, noop_out, err = _invoke(["link", "remove", link_id])
        assert code == 0, noop_out + (err or "")
        # The user-facing message flags the no-op (audit event was
        # still appended — operators expect the trace).
        assert "no-op" in noop_out or "not found" in noop_out, noop_out

        # 6a. a second ``link.deleted`` event was appended (audit
        # trail is the contract even on no-op deletes).
        with engine.connect() as conn:
            deleted_events_after = conn.execute(
                select(events_table).where(events_table.c.event_type == "link.deleted")
            ).all()
        assert len(deleted_events_after) == 2
    finally:
        engine.dispose()
