"""Regression tests for the Phase 25-D MCP handlers (epic #566).

The 秘書化 v1 surface adds three reads (``commitment.list`` /
``person.list`` / ``catchup``) and five writes (``commitment.scan`` /
``commitment.resolve`` / ``commitment.dismiss`` / ``person.merge`` /
``person.split``). Like the recall handler (and unlike the projection-table
reads in :mod:`tests.unit.mcp.test_read_handlers`), every Phase 25-D
handler goes through ``opshub.cli._wiring.build_*_service``, which resolve
their own engine from settings. We therefore exercise the real handlers
against a **migrated SQLite DB** wired through the same isolated env the
``opshub commitment`` / ``opshub person`` CLI tests use — so a typo in the
service contract (column name, filter arg, actor) surfaces here rather
than only in the e2e lifecycle test.

The tests are ``async def`` because the handlers are async
(``ToolHandler = Callable[..., Awaitable[str]]``) and ``asyncio_mode =
"auto"`` collects them automatically.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert
from sqlalchemy.engine import Engine

from opshub.core.errors import OpsHubError
from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.mcp._tools import (
    build_catchup_handler,
    build_commitment_list_handler,
    build_person_list_handler,
)
from opshub.mcp._writes import (
    build_commitment_dismiss_handler,
    build_commitment_resolve_handler,
    build_person_merge_handler,
    build_person_split_handler,
)
from opshub.projections.commitments import commitments_table
from opshub.projections.sources import sources_table

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"
_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))
    # The reads / transition writes never call the LLM; default the
    # backend off so no test accidentally needs a key.
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "disabled")
    return db_path


def _migrate_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


@pytest.fixture
def initialised_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    db_path = _isolate_env(monkeypatch, tmp_path)
    _migrate_db(db_path)
    yield db_path


def _engine(db_path: Path) -> Engine:
    """A throwaway engine for seeding; the handlers resolve their own."""
    return create_engine_for_sqlite(db_path)


def _seed_commitment(
    db_path: Path,
    *,
    direction: str = "owed_to_me",
    state: str = "open",
    counterparty: str | None = None,
    text: str = "review the PR",
    due: str | None = None,
) -> str:
    commitment_id = new_ulid()
    engine = _engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(commitments_table).values(
                    id=commitment_id,
                    source_id=new_ulid(),
                    source_type="slack_message",
                    direction=direction,
                    counterparty=counterparty,
                    due=due,
                    text=text,
                    confidence="medium",
                    state=state,
                    model_id="stub-llm",
                    tokens_in=0,
                    tokens_out=0,
                    extracted_at=_T0,
                    updated_at=_T0,
                )
            )
    finally:
        engine.dispose()
    return commitment_id


def _seed_source(
    db_path: Path, *, connector: str, external_id: str, handle: str, display: str
) -> None:
    engine = _engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(sources_table).values(
                    id=new_ulid(),
                    connector_name=connector,
                    external_id=external_id,
                    source_type="message",
                    title="msg",
                    url=None,
                    summary=None,
                    observed_at=_T0,
                    updated_at=_T0,
                    fingerprint=None,
                    body="hello",
                    provenance_origin=None,
                    provenance_trust=None,
                    author_handle=handle,
                    author_display=display,
                    author_connector=connector,
                )
            )
    finally:
        engine.dispose()


def _parse(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


# ---- commitment.list ------------------------------------------------------


async def test_commitment_list_returns_items_envelope(initialised_env: Path) -> None:
    cid = _seed_commitment(initialised_env, text="ship the release", due="2026-06-20")
    handler = build_commitment_list_handler(_engine(initialised_env))
    payload = _parse(await handler({}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert payload["total"] == 1
    assert items[0]["id"] == cid
    assert items[0]["text"] == "ship the release"
    assert items[0]["due"] == "2026-06-20"
    assert items[0]["direction"] == "owed_to_me"
    assert items[0]["state"] == "open"
    # ADR-0022 §(d) pagination envelope.
    assert payload["truncated"] is False
    assert payload["next_offset"] is None


async def test_commitment_list_filters_by_direction_and_state(initialised_env: Path) -> None:
    _seed_commitment(initialised_env, direction="i_owe", state="open", text="i owe one")
    _seed_commitment(initialised_env, direction="owed_to_me", state="open", text="owed me one")
    _seed_commitment(initialised_env, direction="owed_to_me", state="resolved", text="done")

    handler = build_commitment_list_handler(_engine(initialised_env))

    owed_open = _parse(await handler({"direction": "owed_to_me", "state": "open"}))
    texts = {row["text"] for row in cast("list[dict[str, Any]]", owed_open["items"])}
    assert texts == {"owed me one"}

    i_owe = _parse(await handler({"direction": "i_owe"}))
    assert {row["text"] for row in cast("list[dict[str, Any]]", i_owe["items"])} == {"i owe one"}


async def test_commitment_list_filters_by_person_ref(initialised_env: Path) -> None:
    pid = new_ulid()
    _seed_commitment(initialised_env, counterparty=f"person:{pid}", text="with alice")
    _seed_commitment(initialised_env, counterparty=None, text="no counterparty")

    handler = build_commitment_list_handler(_engine(initialised_env))

    # Bare ULID is accepted and prefixed (mirrors the CLI).
    by_bare = _parse(await handler({"person": pid}))
    assert {row["text"] for row in cast("list[dict[str, Any]]", by_bare["items"])} == {"with alice"}
    # Explicit ``person:`` ref also works.
    by_ref = _parse(await handler({"person": f"person:{pid}"}))
    assert {row["text"] for row in cast("list[dict[str, Any]]", by_ref["items"])} == {"with alice"}


# ---- person.list ----------------------------------------------------------


async def test_person_list_resolves_and_returns_items(initialised_env: Path) -> None:
    _seed_source(
        initialised_env, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alice"
    )
    _seed_source(initialised_env, connector="github", external_id="42", handle="bob", display="Bob")

    handler = build_person_list_handler(_engine(initialised_env))
    payload = _parse(await handler({}))
    items = cast("list[dict[str, Any]]", payload["items"])
    names = {row["display_name"] for row in items}
    assert names == {"Alice", "Bob"}
    # Each person carries its bundled identities.
    idents = {
        (i["connector"], i["handle"])
        for row in items
        for i in cast("list[dict[str, Any]]", row["identities"])
    }
    assert ("slack", "U_a") in idents
    assert ("github", "bob") in idents


async def test_person_list_is_idempotent_across_calls(initialised_env: Path) -> None:
    _seed_source(
        initialised_env,
        connector="google_mail",
        external_id="g1",
        handle="alice@example.com",
        display="Alice",
    )
    handler = build_person_list_handler(_engine(initialised_env))
    first = _parse(await handler({}))
    second = _parse(await handler({}))
    # Re-running ``resolve`` binds nothing new — the person set is stable.
    assert first["total"] == 1
    assert second["total"] == 1


# ---- catchup (stub until Phase 25-E) --------------------------------------


async def test_catchup_handler_is_not_yet_implemented(initialised_env: Path) -> None:
    handler = build_catchup_handler(_engine(initialised_env))
    with pytest.raises(OpsHubError) as excinfo:
        await handler({})
    # The error must name the next sub-issue so an operator who calls it
    # early gets a clear signal rather than a misleading empty digest.
    assert "25-E" in str(excinfo.value) or "#570" in str(excinfo.value)


# ---- commitment.resolve / dismiss -----------------------------------------


async def test_commitment_resolve_flips_state(initialised_env: Path) -> None:
    cid = _seed_commitment(initialised_env, state="open")
    handler = build_commitment_resolve_handler(_engine(initialised_env))
    payload = _parse(await handler({"commitment_id": cid}))
    assert payload == {"ok": True, "commitment_id": cid, "state": "resolved"}

    list_payload = _parse(await build_commitment_list_handler(_engine(initialised_env))({}))
    row = cast("list[dict[str, Any]]", list_payload["items"])[0]
    assert row["state"] == "resolved"


async def test_commitment_resolve_missing_raises(initialised_env: Path) -> None:
    handler = build_commitment_resolve_handler(_engine(initialised_env))
    with pytest.raises(OpsHubError):
        await handler({"commitment_id": new_ulid()})


async def test_commitment_dismiss_flips_state_with_reason(initialised_env: Path) -> None:
    cid = _seed_commitment(initialised_env, state="open")
    handler = build_commitment_dismiss_handler(_engine(initialised_env))
    payload = _parse(await handler({"commitment_id": cid, "reason": "not a real ask"}))
    assert payload == {"ok": True, "commitment_id": cid, "state": "dismissed"}


# ---- person.merge / split -------------------------------------------------


async def test_person_merge_returns_survivor(initialised_env: Path) -> None:
    # Two fuzzy-distinct persons (different connectors, never auto-merged).
    _seed_source(
        initialised_env, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alice"
    )
    _seed_source(
        initialised_env, connector="github", external_id="42", handle="a_login", display="Alice"
    )
    list_handler = build_person_list_handler(_engine(initialised_env))
    persons = cast("list[dict[str, Any]]", _parse(await list_handler({}))["items"])
    assert len(persons) == 2
    a, b = sorted(p["id"] for p in persons)

    merge_handler = build_person_merge_handler(_engine(initialised_env))
    payload = _parse(await merge_handler({"person_a": b, "person_b": a}))
    # Lexicographically-smaller id survives regardless of arg order.
    assert payload["ok"] is True
    assert payload["survivor_id"] == a

    after = cast("list[dict[str, Any]]", _parse(await list_handler({}))["items"])
    assert len(after) == 1
    survivor = after[0]
    bundled = {(i["connector"], i["handle"]) for i in survivor["identities"]}
    assert ("slack", "U_a") in bundled
    assert ("github", "a_login") in bundled


async def test_person_merge_self_raises(initialised_env: Path) -> None:
    _seed_source(
        initialised_env, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alice"
    )
    persons = cast(
        "list[dict[str, Any]]",
        _parse(await build_person_list_handler(_engine(initialised_env))({}))["items"],
    )
    pid = persons[0]["id"]
    handler = build_person_merge_handler(_engine(initialised_env))
    with pytest.raises(OpsHubError):
        await handler({"person_a": pid, "person_b": pid})


async def test_person_split_detaches_identity(initialised_env: Path) -> None:
    # Two email identities sharing the same address auto-bundle (ADR-0043),
    # giving one person with two identities we can then split apart.
    _seed_source(
        initialised_env,
        connector="google_mail",
        external_id="g1",
        handle="alice@example.com",
        display="Alice",
    )
    _seed_source(
        initialised_env,
        connector="ms365",
        external_id="m1",
        handle="alice@example.com",
        display="Alice",
    )
    list_handler = build_person_list_handler(_engine(initialised_env))
    before = cast("list[dict[str, Any]]", _parse(await list_handler({}))["items"])
    assert len(before) == 1
    assert len(before[0]["identities"]) == 2

    split_handler = build_person_split_handler(_engine(initialised_env))
    payload = _parse(await split_handler({"connector": "ms365", "handle": "alice@example.com"}))
    assert payload["ok"] is True
    new_id = payload["new_person_id"]
    assert len(new_id) == 26

    after = cast("list[dict[str, Any]]", _parse(await list_handler({}))["items"])
    assert len(after) == 2


async def test_person_split_missing_identity_raises(initialised_env: Path) -> None:
    handler = build_person_split_handler(_engine(initialised_env))
    with pytest.raises(OpsHubError):
        await handler({"connector": "slack", "handle": "U_nope"})


# ---- redaction through the server boundary --------------------------------

# Every tool name the registry materialises — ``build_tool_specs`` indexes
# each one out of ``handlers`` so the dispatch test must supply a handler
# (real or stub) for all of them. Kept in lockstep with
# ``tests/unit/mcp/test_registry_policy._TOOL_NAMES`` (27 tools after 25-D).
_ALL_TOOL_NAMES: tuple[str, ...] = (
    "recall.search",
    "task.list",
    "inbox.list",
    "decision.list",
    "task.create",
    "inbox.add",
    "connector.sync",
    "brief",
    "graph.related",
    "graph.trace",
    "graph.expand",
    "source.list",
    "source.get",
    "embeddings.find_duplicates",
    "propose.generate",
    "search",
    "propose.apply",
    "slack.demand.list",
    "browser.fetch",
    "commitment.list",
    "person.list",
    "catchup",
    "commitment.scan",
    "commitment.resolve",
    "commitment.dismiss",
    "person.merge",
    "person.split",
)


async def test_commitment_list_output_is_redacted_through_dispatch(
    initialised_env: Path,
) -> None:
    """A token-shaped string in a commitment body is scrubbed by dispatch.

    Commitment text is mined from arbitrary ingested source bodies, so a
    leaky message could embed a token-shaped run. The server's
    :func:`opshub.mcp.server.dispatch_tool_call` runs every handler's
    output through :func:`opshub.mcp._redact.redact_secrets`; this test
    drives the real ``commitment.list`` spec through that dispatch path
    (Phase 25-D adds new read surface, so the redaction-on-the-boundary
    invariant must be re-confirmed for it). The 200-char snippet cap keeps
    the token inside the preview, so redaction is the only line of defence.
    """
    from opshub.mcp._registry import build_tool_specs
    from opshub.mcp.server import dispatch_tool_call

    leaked = "ghp_abcdef0123456789ABCDEF0123456789abcd"
    _seed_commitment(initialised_env, text=f"send the {leaked} token to carol")

    async def _stub(_arguments: Any) -> str:
        return "{}"

    handlers: dict[str, Any] = dict.fromkeys(_ALL_TOOL_NAMES, _stub)
    handlers["commitment.list"] = build_commitment_list_handler(_engine(initialised_env))
    specs = build_tool_specs(handlers=handlers)
    specs_by_name = {s.name: s for s in specs}

    content = await dispatch_tool_call(specs_by_name, "commitment.list", {})
    assert len(content) == 1
    text = content[0].text
    assert leaked not in text, "dispatch must redact a token in commitment text"
    # The non-secret context survives so the agent still sees the row.
    assert "send the" in text
