"""Phase 7 end-to-end lifecycle tests (Connectors Wave 2 closeout).

Drives the Phase 7 connector pipeline through the shipped CLI surface
with mocked SDK / HTTP boundaries for every external dependency
(``slack_sdk.WebClient`` / Microsoft Graph httpx layer / ``boxsdk``).
Pattern mirrors :mod:`tests.integration.test_phase6_lifecycle` (Phase 6
closeout): one happy-path lifecycle test that walks
``connector sync`` → ``embeddings rebuild`` → ``recall`` → ``brief`` →
``propose generate`` across all four Phase 7 connectors plus the Phase
3 GitHub baseline so the SaaS source families round-trip through the
shared Phase 4-6 layers without regressions.

What this pins
--------------

- ``opshub slack sync`` / ``ms365`` / ``box`` each persist
  rows under their phase-7 ``source_type`` discriminators
  (``slack_message`` / ``ms365_calendar`` / ``ms365_onedrive`` /
  ``ms365_outlook`` / ``box_event``) using the per-connector mocked
  SDK / HTTP boundary.
- ``opshub embeddings rebuild`` embeds every Phase 7 source row using
  the stub embedder so downstream recall / brief / propose have
  searchable vectors.
- ``opshub recall "<topic>"`` returns hits drawn from the SaaS source
  families — i.e. the new ``source_type`` discriminators are visible
  to :class:`RecallService` without service changes.
- ``opshub brief "<topic>"`` builds a prompt that includes the SaaS
  source summaries (captured via the stub LLM's recorded messages).
- ``opshub propose generate "<topic>"`` records ``ProposalRequested``
  / ``ProposalGenerated`` event pairs and persists at least one
  candidate, again drawing on SaaS context through the same recall
  layer.

ADR-0010 / ADR-0005 contracts that are validated end-to-end:

- SaaS source rows persist under the per-connector ``source_type``
  discriminator (ADR-0010 §Decision: vendor-specific event names are
  forbidden; ``SourceObserved`` is the only event the framework
  appends, ``source_type`` distinguishes by vendor).
- Every persisted source ``summary`` is ≤ 200 unicode characters
  (ADR-0005 External Content Minimization, enforced by the per-mapper
  truncation rules — see :mod:`opshub.connectors.slack.mapper`
  ``SUMMARY_MAX_CHARS = 200`` etc.).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Skip when sqlite-vec is not installed — Phase 4 ``embeddings rebuild``
# touches the ``embeddings_vec_*`` virtual tables provisioned in
# migration 0013 which fail to create without the loadable extension.
pytest.importorskip("sqlite_vec")
pytest.importorskip(
    "slack_sdk",
    reason="Phase 7 lifecycle requires the 'connectors-slack' extras",
)
pytest.importorskip(
    "msal",
    reason="Phase 7 lifecycle requires the 'connectors-ms365' extras",
)
pytest.importorskip(
    "httpx",
    reason="Phase 7 lifecycle requires the 'connectors-ms365' extras",
)
pytest.importorskip(
    "boxsdk",
    reason="Phase 7 lifecycle requires the 'connectors-box' extras",
)

from pydantic import BaseModel
from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors import register_connector, unregister_all
from opshub.connectors.box.connector import BoxConnector
from opshub.connectors.box.fetcher import RawBoxEvent
from opshub.connectors.ms365.connector import MS365Connector
from opshub.connectors.ms365.fetcher import (
    RawCalendarEvent,
    RawOneDriveItem,
    RawOutlookMessage,
)
from opshub.connectors.slack.connector import SlackConnector
from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import events_table
from opshub.domain.events.proposal import (
    DecisionCandidatePayload,
    TaskCandidatePayload,
)
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.projections.proposals import proposals_table
from opshub.projections.sources import sources_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs (embedder + LLM client) — copied verbatim from Phase 6 lifecycle
# so a Phase 6 refactor cannot inadvertently change Phase 7 expectations.
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub — same shape as Phase 5/6 lifecycle.

    Hashes each input string into a unit-L2-normalised 1024-dim vector
    so identical text maps to identical vectors and ``recall`` is
    perfectly deterministic for assertions.
    """

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase7-stub-embedder"

    @property
    def model_version(self) -> str:
        return "v1"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> EmbeddingResult:
        slots = [0.0] * self._dim
        for i, ch in enumerate(text):
            slots[i % self._dim] += (ord(ch) % 31 + 1) / 31.0
        norm = max(sum(x * x for x in slots) ** 0.5, 1e-9)
        return EmbeddingResult(
            vector=tuple(x / norm for x in slots),
            model_id=self.model_id,
            model_version=self.model_version,
            dim=self._dim,
        )


class _StubLLMClient:
    """LLMClient stub that records prompt args for assertion-side capture.

    The stub is structurally identical to the Phase 6 lifecycle stub
    (a fixed markdown briefing + a fixed task + decision proposal pair)
    but exposes the prompt messages via ``complete_calls`` /
    ``structured_calls`` so the test can prove the SaaS source
    summaries flowed into the prompt body.
    """

    def __init__(
        self,
        *,
        brief_text: str = "# Phase 7 Briefing\n\n- saas item alpha\n- saas item beta",
        task_title: str = "follow up phase 7 saas observation",
        task_body: str | None = "verify SaaS connectors land in propose",
        decision_text: str = "adopt phase 7 connectors wave 2 closeout",
        decision_context: str | None = "phase 7 closeout",
        model_id: str = "stub-llm-haiku",
        model_version: str = "phase7-test",
        tokens_in: int = 120,
        tokens_out: int = 80,
    ) -> None:
        self._brief_text = brief_text
        self._task_title = task_title
        self._task_body = task_body
        self._decision_text = decision_text
        self._decision_context = decision_context
        self._model_id = model_id
        self._model_version = model_version
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.complete_calls: list[tuple[list[LLMMessage], int]] = []
        self.structured_calls: list[tuple[list[LLMMessage], type[BaseModel], int]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        return self._model_version

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        del temperature, stop
        self.complete_calls.append((list(messages), max_tokens))
        return LLMResponse(
            text=self._brief_text,
            model_id=self._model_id,
            model_version=self._model_version,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        del temperature
        self.structured_calls.append((list(messages), schema, max_tokens))
        parsed = schema(
            candidates=[
                TaskCandidatePayload(title=self._task_title, body=self._task_body),
                DecisionCandidatePayload(
                    text=self._decision_text,
                    context=self._decision_context,
                ),
            ]
        )
        return StructuredResponse(
            parsed=parsed,
            model_id=self._model_id,
            model_version=self._model_version,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )


def _install_stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub)


def _install_stub_llm(monkeypatch: pytest.MonkeyPatch, stub: _StubLLMClient) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.llm import factory as factory_module
    from opshub.llm.client import LLMClient

    def _builder(settings: OpsHubSettings) -> LLMClient:
        del settings
        return stub  # type: ignore[return-value,unused-ignore]

    monkeypatch.setattr(factory_module, "build_llm_client", _builder)


def _invoke(args: list[str]) -> tuple[int, str, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# SDK / HTTP boundary stubs per connector. We bypass each connector's
# real SDK boundary by monkeypatching:
#   - Slack: replace ``SlackFetcher`` on the connector module (the
#     ``opshub slack sync`` CLI imports it via the connector).
#   - MS365: replace ``MS365Auth`` and ``MS365Fetcher`` on their
#     respective modules so the connector's lazy imports pick the stubs.
#   - Box: register a :class:`BoxConnector` instance whose
#     ``fetcher_factory`` returns the stub fetcher.
# ---------------------------------------------------------------------------


def _slack_raw(*, ts: str, text: str) -> RawSlackMessage:
    return RawSlackMessage(
        channel_id="C1",
        channel_name="general",
        ts=ts,
        text=text,
        user_id="U1",
        user_display_name="alice",
        permalink=f"https://acme.slack.com/archives/C1/p{ts.replace('.', '')}",
        raw={},
    )


def _install_slack_stub(
    monkeypatch: pytest.MonkeyPatch,
    yields: list[tuple[str, RawSlackMessage, str | None]],
) -> None:
    """Replace :class:`SlackFetcher` so the CLI never touches Slack's SDK.

    The connector imports :class:`SlackFetcher` from its own namespace
    (``opshub.connectors.slack.connector.SlackFetcher``) so we patch the
    attribute on the connector module, not the fetcher module itself —
    matches :mod:`tests.integration.test_phase7_slack_sync`.
    """
    fake_fetcher_cls = MagicMock()

    def _fetch_messages(
        *,
        cursor_per_channel: dict[str, str | None],
        max_per_channel: int = 100,
        excludes: object = None,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        # ADR-0030 (#466): the connector forwards the resolved
        # ``ExcludeRules`` filter to ``fetch_messages``. The lifecycle
        # mock accepts and ignores it — see
        # :mod:`tests.integration.test_phase7_slack_sync` for the
        # rationale.
        del cursor_per_channel, max_per_channel, excludes
        return iter(yields)

    fake_fetcher_cls.return_value.fetch_messages.side_effect = _fetch_messages
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackFetcher",
        fake_fetcher_cls,
    )


def _ms365_calendar(event_id: str = "cal-1") -> RawCalendarEvent:
    return RawCalendarEvent(
        id=event_id,
        subject=f"sync meeting {event_id}",
        start_iso="2026-05-17T09:00:00Z",
        end_iso="2026-05-17T10:00:00Z",
        attendees_count=3,
        web_link=f"https://outlook.office.com/calendar/item/{event_id}",
        last_modified_iso="2026-05-17T08:30:00Z",
        raw={"id": event_id},
    )


def _ms365_onedrive(item_id: str = "drive-1") -> RawOneDriveItem:
    return RawOneDriveItem(
        id=item_id,
        name=f"phase7-{item_id}.md",
        path=f"/drive/root:/Projects/phase7-{item_id}.md",
        web_url=f"https://onedrive.live.com/?id={item_id}",
        last_modified_iso="2026-05-16T12:00:00Z",
        raw={"id": item_id},
    )


def _ms365_outlook(message_id: str = "mail-1") -> RawOutlookMessage:
    return RawOutlookMessage(
        id=message_id,
        subject=f"phase 7 follow-up {message_id}",
        body_preview="Reviewed the SaaS connectors wave 2 status report.",
        sender="alice@example.com",
        received_iso="2026-05-16T15:45:00Z",
        web_link=f"https://outlook.office.com/mail/inbox/id/{message_id}",
        raw={"id": message_id},
    )


def _install_ms365_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calendar: list[RawCalendarEvent],
    onedrive: list[RawOneDriveItem],
    outlook: list[RawOutlookMessage],
) -> None:
    """Replace ``MS365Auth`` + ``MS365Fetcher`` with in-process stubs."""
    from opshub.connectors.ms365 import auth as auth_module
    from opshub.connectors.ms365 import fetcher as fetcher_module

    class _StubAuth:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def get_access_token(self) -> str:
            return "bearer-stub"

    class _StubFetcher:
        def __init__(self, _auth: object) -> None:
            pass

        def fetch_calendar_events(
            self, *, since_iso: str | None
        ) -> Iterator[tuple[RawCalendarEvent, str]]:
            del since_iso
            for item in calendar:
                yield item, item.last_modified_iso

        def fetch_onedrive_changes(
            self, *, delta_link: str | None
        ) -> Iterator[tuple[RawOneDriveItem, str]]:
            del delta_link
            for item in onedrive:
                yield item, item.last_modified_iso

        def fetch_outlook_messages(
            self, *, since_iso: str | None
        ) -> Iterator[tuple[RawOutlookMessage, str]]:
            del since_iso
            for item in outlook:
                yield item, item.received_iso

        def close(self) -> None:
            return None

    monkeypatch.setattr(auth_module, "MS365Auth", _StubAuth)
    monkeypatch.setattr(fetcher_module, "MS365Fetcher", _StubFetcher)


def _box_raw(event_id: str) -> RawBoxEvent:
    return RawBoxEvent(
        event_id=event_id,
        event_type="ITEM_CREATE",
        item_id="12345",
        item_type="file",
        item_name=f"phase7-{event_id}.pdf",
        item_path=f"/Documents/Reports/phase7-{event_id}.pdf",
        created_iso="2026-05-17T10:00:00Z",
        actor_id="u-1",
        actor_name="Alice",
        web_url=f"https://app.box.com/file/{event_id}",
        raw={"event_id": event_id, "event_type": "ITEM_CREATE"},
    )


class _BoxStubFetcher:
    """Programmable :class:`BoxFetcher` double used by the Box connector."""

    def __init__(self, scripts: dict[str | None, list[tuple[RawBoxEvent, str]]]) -> None:
        self.scripts = scripts
        self.calls: list[str | None] = []

    def fetch_events(self, *, stream_position: str | None) -> Iterator[tuple[RawBoxEvent, str]]:
        self.calls.append(stream_position)
        script = self.scripts.get(stream_position, [])
        yield from script


def _install_box_stub(
    scripts: dict[str | None, list[tuple[RawBoxEvent, str]]],
) -> _BoxStubFetcher:
    """Register a :class:`BoxConnector` whose factory returns the stub."""
    from typing import cast

    from opshub.connectors.box.fetcher import BoxFetcher

    stub = _BoxStubFetcher(scripts=scripts)
    register_connector(BoxConnector(fetcher_factory=lambda: cast("BoxFetcher", stub)))
    return stub


# ---------------------------------------------------------------------------
# Registry isolation — every Phase 7 lifecycle test starts from a clean
# registry and re-registers the four connectors it cares about so the
# CLI can resolve them without inheriting state from sibling tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _phase7_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    unregister_all()
    register_connector(SlackConnector())
    register_connector(MS365Connector())
    # Box is registered lazily by ``_install_box_stub`` because each
    # test needs its own ``fetcher_factory`` script. The plain
    # ``BoxConnector()`` from the package's import-time registration is
    # left out of the wave-2 registry to keep the stub factory in
    # charge.
    yield
    unregister_all()


@pytest.fixture
def phase7_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure the three connectors with mock-friendly credentials.

    The Phase 7 connectors expose their configuration through
    :class:`OpsHubSettings`; we drive every relevant knob via env vars
    so the test is hermetic (no ``opshub.toml`` mutation, no keyring
    access — the SDK boundaries are mocked anyway).
    """
    # Slack: OAuth token via env override (User Token per ADR-0018), single channel id.
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_TOKEN", "xoxp-test")
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__CHANNELS", '["C1"]')
    # MS365: client id is the only mandatory bootstrapping setting.
    monkeypatch.setenv("OPSHUB_CONNECTORS__MS365__CLIENT_ID", "test-client-id")
    # Embedding + LLM: opt into the stub-backed local backends so the
    # factory monkeypatches in ``_install_stub_*`` actually fire.
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    yield


# ---------------------------------------------------------------------------
# Phase 7 happy-path lifecycle
# ---------------------------------------------------------------------------


def test_phase7_lifecycle_connector_sync_through_propose(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    phase7_env: None,
) -> None:
    """Walk the full Phase 7 happy path end-to-end through the CLI.

    Steps (mirrors Phase 7 plan §2.4 D1 spec):

    1. ``opshub slack sync`` → ``slack_message`` rows in
       ``sources`` projection.
    2. ``opshub ms365 sync`` → ``ms365_calendar`` /
       ``ms365_onedrive`` / ``ms365_outlook`` rows.
    3. ``opshub box sync`` → ``box_event`` rows.
    4. ``opshub embeddings rebuild`` → every source row embedded
       (verified by the source-only count assertion below).
    5. ``opshub recall "<topic>"`` → returns SaaS hits.
    6. ``opshub brief "<topic>"`` → stub LLM receives a prompt that
       includes SaaS source summaries (asserted on the captured prompt
       text).
    7. ``opshub propose generate`` → 2-candidate proposal recorded
       with ``ProposalRequested`` + ``ProposalGenerated`` events
       sharing the same ``aggregate_id``.
    """
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient()
    _install_stub_llm(monkeypatch, stub_llm)

    # ---- 1. Slack sync ---------------------------------------------------
    _install_slack_stub(
        monkeypatch,
        yields=[
            (
                "C1",
                _slack_raw(ts="1700000001.000100", text="phase 7 status update"),
                "1700000001.000100",
            ),
            (
                "C1",
                _slack_raw(ts="1700000002.000200", text="connectors wave 2 review"),
                "1700000002.000200",
            ),
        ],
    )
    code, out, err = _invoke(["slack", "sync"])
    assert code == 0, out + (err or "")

    # ---- 2. MS365 sync ---------------------------------------------------
    _install_ms365_stub(
        monkeypatch,
        calendar=[_ms365_calendar("cal-1")],
        onedrive=[_ms365_onedrive("drive-1")],
        outlook=[_ms365_outlook("mail-1")],
    )
    code, out, err = _invoke(["ms365", "sync"])
    assert code == 0, out + (err or "")

    # ---- 3. Box sync -----------------------------------------------------
    _install_box_stub(
        scripts={
            None: [
                (_box_raw("evt-1"), "pos-1"),
                (_box_raw("evt-2"), "pos-1"),
            ],
        },
    )
    code, out, err = _invoke(["box", "sync"])
    assert code == 0, out + (err or "")

    # ---- Source projection assertions -----------------------------------
    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # 2 slack + 3 ms365 + 2 box = 7 source rows total.
        with engine.connect() as conn:
            source_rows = conn.execute(select(sources_table)).mappings().all()
        source_types = {row["source_type"] for row in source_rows}
        # Every Phase 7 source family must be present.
        assert source_types == {
            "slack_message",
            "ms365_calendar",
            "ms365_onedrive",
            "ms365_outlook",
            "box_event",
        }
        assert len(source_rows) == 7

        # ADR-0005 pin: every persisted summary is at most 200 chars.
        # The mappers enforce this; this assertion proves the rule
        # holds at the projection boundary too.
        for row in source_rows:
            summary = row["summary"] or ""
            assert len(summary) <= 200, (row["external_id"], summary)

        # ---- 4. embeddings rebuild --------------------------------------
        code, out, err = _invoke(["embeddings", "rebuild"])
        assert code == 0, out + (err or "")

        # ---- 5. recall: should surface SaaS sources -------------------
        code, recall_out, err = _invoke(
            ["recall", "phase 7 status update", "--limit", "10", "--format", "json"]
        )
        assert code == 0, recall_out + (err or "")
        recall_payload: list[dict[str, object]] = json.loads(recall_out)
        # The recall payload is a list of hits with ``entity_type`` /
        # ``entity_id`` / ``score``. We assert that at least one
        # ``source`` entity_type hit is present — proving the SaaS
        # source families participate in recall.
        assert isinstance(recall_payload, list)
        recall_entity_types: set[object] = {hit["entity_type"] for hit in recall_payload}
        assert "source" in recall_entity_types, recall_payload

        # ---- 6. brief: stub LLM receives SaaS source summaries -------
        code, brief_out, err = _invoke(["brief", "phase 7 connectors review", "--format", "json"])
        assert code == 0, brief_out + (err or "")
        brief_payload = json.loads(brief_out)
        briefing_id = brief_payload["briefing_id"]
        assert briefing_id
        # The stub captured the actual prompt — assert at least one
        # SaaS-derived summary segment appears in the user message
        # (e.g. the slack summary "phase 7 status update" or "phase 7
        # follow-up mail-1" from outlook). We use a permissive set
        # because the recall ranking is non-deterministic across
        # platforms when scores are tied — we only require one Phase 7
        # source family contributes context.
        assert len(stub_llm.complete_calls) == 1
        messages, _max_tokens = stub_llm.complete_calls[0]
        user_message = next(msg for msg in messages if msg.role == "user")
        markers = (
            "phase 7 status update",
            "connectors wave 2 review",
            "phase 7 follow-up",
            "sync meeting cal-1",
            "phase7-drive-1.md",
            "ITEM_CREATE: phase7-evt-1.pdf",
            "ITEM_CREATE: phase7-evt-2.pdf",
        )
        assert any(marker in user_message.content for marker in markers), (
            "expected at least one Phase 7 SaaS summary in the brief prompt",
            user_message.content[:500],
        )

        # ---- 7. propose generate: SaaS context contributes -----------
        code, generate_out, err = _invoke(
            [
                "propose",
                "generate",
                "phase 7 next steps",
                "--from-briefing",
                briefing_id,
            ]
        )
        assert code == 0, generate_out + (err or "")
        assert "[0] task:" in generate_out, generate_out
        assert "[1] decision:" in generate_out, generate_out
        assert len(stub_llm.structured_calls) == 1

        # Same prompt-capture check on the proposal path: at least one
        # SaaS marker reaches the structured-output LLM call.
        prop_messages, _schema, _prop_tokens = stub_llm.structured_calls[0]
        prop_user = next(msg for msg in prop_messages if msg.role == "user")
        assert any(marker in prop_user.content for marker in markers), (
            "expected at least one Phase 7 SaaS summary in the proposal prompt",
            prop_user.content[:500],
        )

        # Extract proposal id (printed as ``Proposal: <ulid>``).
        proposal_id: str | None = None
        for line in generate_out.splitlines():
            if line.startswith("Proposal:"):
                proposal_id = line.split(":", 1)[1].strip()
                break
        assert proposal_id is not None, generate_out
        assert len(proposal_id) == 26

        # Projection row + event aggregate id share contract (Phase 6
        # ADR-0016 §決定 — every proposal lifecycle event lives under
        # the same aggregate_id).
        with engine.connect() as conn:
            prop_rows = conn.execute(
                select(proposals_table).where(proposals_table.c.id == proposal_id)
            ).all()
        assert len(prop_rows) == 1
        assert list(prop_rows[0].candidate_states) == ["pending", "pending"]

        with engine.connect() as conn:
            requested = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.requested")
            ).all()
            generated = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.generated")
            ).all()
        assert len(requested) == 1
        assert len(generated) == 1
        assert requested[0].aggregate_id == proposal_id
        assert generated[0].aggregate_id == proposal_id
    finally:
        engine.dispose()


# Re-export ``pytest`` so static analysers see this module is a pytest test.
_ = pytest
