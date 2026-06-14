"""Tests for ``opshub.connectors.teams.mapper`` (Phase 11 F5).

Pins:

1. ``SOURCE_TYPE = "teams_message"`` (ADR-0010 §改訂 (a)).
2. ``SUMMARY_MAX_CHARS = 200`` (matches the Phase 7 Slack precedent).
3. The mapped kwargs match :meth:`SourceService.observe`'s signature
   verbatim (connector_name / external_id / source_type / title /
   summary / url / body / provenance_origin / provenance_trust).
4. ``body`` carries the **full** plain-text body; ``summary`` carries
   the truncated preview. ADR-0020 contract.
5. ``provenance_origin = "external"`` + ``provenance_trust =
   "untrusted"`` are stamped unconditionally so an LLM consumer never
   treats Teams content as instructions (ADR-0020 §(e)).
6. HTML bodies are stripped to plain text; explicit ``<br>`` /
   ``</p>`` / ``</div>`` collapse to newlines so multi-paragraph
   previews render as multi-line text.
7. Truncation appends ``"…"`` (single Unicode char) and the
   exact-boundary case returns the original verbatim.
"""

from __future__ import annotations

from typing import Any

from opshub.connectors.teams.fetcher import RawTeamsChatMessage
from opshub.connectors.teams.mapper import (
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    map_chat_message,
)

# ----- constants ---------------------------------------------------------


def test_source_type_pinned_to_teams_message() -> None:
    """The ``sources`` projection discriminator must be ``teams_message``.

    Changing this is a breaking change for any persisted Teams source
    rows. ADR-0010 §改訂 (a) documents the contract.
    """
    assert SOURCE_TYPE == "teams_message"


def test_summary_max_chars_pinned_to_200() -> None:
    """The 200-char preview cap is the Phase 7 Slack convention.

    ADR-0020 retains the **full** body on a separate field; the
    summary stays bounded at 200 so the at-a-glance preview remains
    legible.
    """
    assert SUMMARY_MAX_CHARS == 200


# ----- helpers -----------------------------------------------------------


def _raw(
    *,
    msg_id: str = "1700000000001",
    chat_id: str = "19:abc@thread.v2",
    chat_topic: str = "Project Alpha",
    body_html: str = "<p>hello there</p>",
    body_content_type: str = "html",
    sender_display_name: str = "Alice",
    sender_id: str = "user-alice",
    created_iso: str = "2026-01-01T00:00:00Z",
    last_modified_iso: str = "2026-01-01T00:00:00Z",
    web_url: str = "https://teams.microsoft.com/l/message/abc",
) -> RawTeamsChatMessage:
    return RawTeamsChatMessage(
        id=msg_id,
        chat_id=chat_id,
        chat_topic=chat_topic,
        body_html=body_html,
        body_content_type=body_content_type,
        sender_display_name=sender_display_name,
        sender_id=sender_id,
        created_datetime_iso=created_iso,
        last_modified_iso=last_modified_iso,
        web_url=web_url,
        raw={},
    )


# ----- mapping shape -----------------------------------------------------


def test_map_chat_message_returns_observe_kwargs_shape() -> None:
    """The mapped dict carries every keyword :meth:`observe` accepts."""
    kwargs = map_chat_message(_raw())
    expected_keys = {
        "connector_name",
        "external_id",
        "source_type",
        "title",
        "summary",
        "url",
        "body",
        "provenance_origin",
        "provenance_trust",
        # Phase 25-A (ADR-0010 §改訂): cross-connector author normalisation.
        "author_handle",
        "author_display",
    }
    assert set(kwargs) == expected_keys


def test_map_chat_message_pins_connector_name() -> None:
    """``connector_name = "teams"`` is the registry / projection key."""
    assert map_chat_message(_raw())["connector_name"] == "teams"


def test_map_chat_message_uses_chat_id_msg_id_natural_key() -> None:
    """``external_id = f"{chat_id}:{msg_id}"`` is Graph's natural key.

    ``id`` is unique within a chat; compounding with the chat id keeps
    the key unique across the workspace.
    """
    raw = _raw(chat_id="19:abc@thread.v2", msg_id="1700000000001")
    assert map_chat_message(raw)["external_id"] == "19:abc@thread.v2:1700000000001"


def test_map_chat_message_uses_source_type_constant() -> None:
    """``source_type`` is :data:`SOURCE_TYPE` verbatim."""
    assert map_chat_message(_raw())["source_type"] == SOURCE_TYPE


def test_title_uses_sender_and_chat_topic() -> None:
    """Title is ``"<sender> in <topic>"`` — human-recognisable label."""
    raw = _raw(sender_display_name="Bob", chat_topic="Project Beta")
    assert map_chat_message(raw)["title"] == "Bob in Project Beta"


def test_title_falls_back_to_chat_id_when_topic_missing() -> None:
    """When Graph omits ``chatTopic`` (1:1 chats) we fall back to the chat id."""
    raw = _raw(chat_topic="", chat_id="19:dm@thread.v2")
    assert map_chat_message(raw)["title"] == "Alice in 19:dm@thread.v2"


def test_title_uses_system_label_for_blank_sender() -> None:
    """No sender → ``"system"`` so the title field stays non-empty.

    The fetcher already drops body-less system messages, but a future
    Graph schema change might surface a sender-less message with a
    body — defensive fallback keeps the field bounded.
    """
    raw = _raw(sender_display_name="", body_html="<p>system notice</p>", sender_id="x")
    assert map_chat_message(raw)["title"] == "system in Project Alpha"


# ----- body / summary / provenance --------------------------------------


def test_html_body_is_stripped_for_summary_and_body() -> None:
    """HTML markup is dropped from both ``summary`` and ``body`` fields."""
    raw = _raw(body_html="<p>Hello <b>world</b></p>", body_content_type="html")
    kwargs = map_chat_message(raw)
    assert kwargs["summary"] == "Hello world"
    assert kwargs["body"] == "Hello world"


def test_plain_text_body_passes_through_unchanged() -> None:
    """``contentType == "text"`` bypasses the HTML stripper.

    Graph can emit plain-text bodies (e.g. some bot frameworks); the
    mapper must not mangle them by running the HTML stripper anyway.
    """
    raw = _raw(body_html="< not html >", body_content_type="text")
    kwargs = map_chat_message(raw)
    assert kwargs["body"] == "< not html >"


def test_html_breaks_collapse_to_newlines() -> None:
    """``<br>`` / ``</p>`` / ``</div>`` collapse to ``\\n`` for readability."""
    raw = _raw(
        body_html="<div>line one<br>line two</div><p>para two</p>",
        body_content_type="html",
    )
    kwargs = map_chat_message(raw)
    # Order preserved, paragraph break visible.
    body: str = kwargs["body"]
    assert "line one" in body
    assert "line two" in body
    assert "para two" in body
    assert "\n" in body


def test_html_entities_are_unescaped() -> None:
    """``&amp;`` / ``&lt;`` round-trip to ASCII so previews read correctly."""
    raw = _raw(body_html="<p>Tom &amp; Jerry &lt;3</p>", body_content_type="html")
    kwargs = map_chat_message(raw)
    assert kwargs["body"] == "Tom & Jerry <3"


def test_summary_truncates_long_body_with_ellipsis() -> None:
    """Bodies longer than 200 chars truncate to ``199 + "…"``."""
    long_text = "a" * 500
    raw = _raw(body_html=long_text, body_content_type="text")
    kwargs = map_chat_message(raw)
    summary: str = kwargs["summary"]
    assert len(summary) == SUMMARY_MAX_CHARS
    assert summary.endswith("…")
    # The full body stays intact on the body field.
    assert kwargs["body"] == long_text


def test_summary_exact_boundary_is_returned_verbatim() -> None:
    """A body of exactly ``SUMMARY_MAX_CHARS`` chars is not truncated.

    Otherwise the ellipsis would push past the cap and defeat the
    truncation contract — the same boundary pin the Slack mapper has.
    """
    body = "b" * SUMMARY_MAX_CHARS
    raw = _raw(body_html=body, body_content_type="text")
    kwargs = map_chat_message(raw)
    assert kwargs["summary"] == body
    assert len(kwargs["summary"]) == SUMMARY_MAX_CHARS


def test_provenance_is_external_untrusted() -> None:
    """All Teams content is ``external`` + ``untrusted`` per ADR-0020 §(e)."""
    kwargs = map_chat_message(_raw())
    assert kwargs["provenance_origin"] == "external"
    assert kwargs["provenance_trust"] == "untrusted"


def test_empty_body_falls_back_to_title() -> None:
    """Empty text body → ``body=title`` (epic #470 / issue #481).

    The fetcher's ``_normalise_chat_message`` already drops empty
    bodies, but the mapper must defend against future paths that
    forward a sender-only message with no body. epic #470 / #481
    promoted :class:`SourceObserved.body` to ``min_length=1``; the
    mapper now substitutes the composed title so the projection still
    receives a non-empty body (ADR-0010 §不変条件 metadata-only rule).
    """
    raw = _raw(body_html="", body_content_type="text")
    kwargs: dict[str, Any] = map_chat_message(raw)
    assert kwargs["body"] == kwargs["title"]
    assert kwargs["body"] and kwargs["body"].strip()


def test_url_passes_through_web_url() -> None:
    """``url`` is the Graph ``webUrl`` verbatim — projection accepts ``""``."""
    raw = _raw(web_url="https://teams.microsoft.com/l/message/xyz")
    assert map_chat_message(raw)["url"] == "https://teams.microsoft.com/l/message/xyz"


def test_map_chat_message_normalises_empty_plain_text_to_none() -> None:
    """Empty ``plain_text`` → ``summary=None`` / ``body=None`` (issue #332 PR 2).

    The Slack mapper normalises empty text to ``None`` on both
    ``summary`` and ``body``; the Teams mapper must do the same so a
    sender-only Teams chat message (no body, or HTML that strips to
    empty such as ``"<div></div>"``) does not leak ``summary=""`` into
    :meth:`SourceService.observe` and trip the ``ItemEnqueued.summary``
    ``min_length=1`` validation. Two fixture shapes are exercised here
    so the contract is pinned for both content types Graph emits:

    1. ``body_content_type="text"`` with ``body_html=""`` — the empty
       string passes through ``_to_plain_text`` verbatim.
    2. ``body_content_type="html"`` with ``body_html="<div></div>"`` —
       the HTML stripper collapses the markup to an empty string.

    Both must produce ``summary is None`` / ``body is None`` while
    preserving the rest of the kwargs shape (title, external_id,
    connector_name, source_type) so downstream callers see a
    structurally identical row apart from the two normalised fields.
    """
    # Case 1: plain text body that is literally empty.
    raw_text_empty = _raw(
        body_html="",
        body_content_type="text",
        sender_display_name="Alice",
        chat_topic="Project Alpha",
    )
    kwargs_text: dict[str, Any] = map_chat_message(raw_text_empty)
    assert kwargs_text["summary"] is None
    # epic #470 / issue #481: empty body falls back to title.
    assert kwargs_text["body"] == kwargs_text["title"]
    assert kwargs_text["title"] == "Alice in Project Alpha"
    assert kwargs_text["external_id"] == "19:abc@thread.v2:1700000000001"
    assert kwargs_text["connector_name"] == "teams"
    assert kwargs_text["source_type"] == SOURCE_TYPE

    # Case 2: HTML body that strips to an empty string.
    raw_html_strips_empty = _raw(
        body_html="<div></div>",
        body_content_type="html",
        sender_display_name="Alice",
        chat_topic="Project Alpha",
    )
    kwargs_html: dict[str, Any] = map_chat_message(raw_html_strips_empty)
    assert kwargs_html["summary"] is None
    assert kwargs_html["body"] == kwargs_html["title"]
    assert kwargs_html["title"] == "Alice in Project Alpha"
    assert kwargs_html["external_id"] == "19:abc@thread.v2:1700000000001"
    assert kwargs_html["connector_name"] == "teams"
    assert kwargs_html["source_type"] == SOURCE_TYPE


def test_map_chat_message_normalises_whitespace_only_to_none() -> None:
    """Whitespace-only ``plain_text`` → ``summary=None`` (issue #337).

    The follow-up to issue #332 / PR #335 (which fixed strictly empty
    ``plain_text``) widens the empty→``None`` rule to cover
    whitespace-only bodies. Three shapes are exercised here so the
    contract is pinned across the realistic ways a sender-only message
    can deliver whitespace:

    1. ``body_content_type="text"`` with ``body_html="  "`` — a plain
       whitespace run passes through ``_to_plain_text`` verbatim.
    2. ``body_content_type="html"`` with ``body_html="<p>  </p>"`` —
       the HTML stripper currently collapses this shape to ``""`` (the
       paragraph close becomes a newline, per-line strip drops the
       trailing whitespace line); the ``.strip()`` defense remains as
       a future-proofing guard if the stripper ever forwards whitespace
       residue.
    3. ``body_content_type="text"`` with ``body_html="\t\n"`` — a mix
       of tabs and newlines, exercising the non-space whitespace
       characters ``str.strip`` covers.

    All three must produce ``summary is None`` while leaving the rest
    of the kwargs shape (title, external_id, connector_name,
    source_type) untouched. Critically, ``body`` is **not** whitespace-
    normalised — ADR-0020 retain-everything keeps the verbatim
    whitespace on the body field so a downstream consumer can still
    distinguish a sender-only message from a no-body message. Body
    asserts therefore reflect what ``plain_text or None`` actually
    returns for each shape (truthy whitespace stays; the HTML strip
    case still collapses to ``None`` because the existing stripper
    already returns ``""`` for ``"<p>  </p>"``). This is the symmetric
    mirror of the Slack mapper whitespace fix that lands in the paired
    PR for issue #337.
    """
    # Case 1: plain whitespace, text content type — passes through
    # ``_to_plain_text`` verbatim; body retains the whitespace.
    raw_plain_ws = _raw(
        body_html="  ",
        body_content_type="text",
        sender_display_name="Alice",
        chat_topic="Project Alpha",
    )
    kwargs_plain: dict[str, Any] = map_chat_message(raw_plain_ws)
    assert kwargs_plain["summary"] is None
    # ADR-0020 retain-everything: whitespace body is preserved verbatim.
    assert kwargs_plain["body"] == "  "
    assert kwargs_plain["title"] == "Alice in Project Alpha"
    assert kwargs_plain["external_id"] == "19:abc@thread.v2:1700000000001"
    assert kwargs_plain["connector_name"] == "teams"
    assert kwargs_plain["source_type"] == SOURCE_TYPE

    # Case 2: HTML body that strips to whitespace residue.
    # The existing ``_to_plain_text`` already collapses
    # ``"<p>  </p>"`` to ``""`` (so body is ``None``); the
    # ``.strip()`` on summary remains as defence-in-depth in case the
    # HTML stripper ever forwards residual whitespace.
    raw_html_ws = _raw(
        body_html="<p>  </p>",
        body_content_type="html",
        sender_display_name="Alice",
        chat_topic="Project Alpha",
    )
    kwargs_html_ws: dict[str, Any] = map_chat_message(raw_html_ws)
    assert kwargs_html_ws["summary"] is None
    # epic #470 / issue #481: when the HTML strips to nothing the
    # mapper falls back to the title.
    assert kwargs_html_ws["body"] == kwargs_html_ws["title"]
    assert kwargs_html_ws["title"] == "Alice in Project Alpha"
    assert kwargs_html_ws["external_id"] == "19:abc@thread.v2:1700000000001"
    assert kwargs_html_ws["connector_name"] == "teams"
    assert kwargs_html_ws["source_type"] == SOURCE_TYPE

    # Case 3: tab/newline whitespace, text content type — exercises
    # the non-space whitespace characters that ``str.strip`` covers.
    raw_tab_newline = _raw(
        body_html="\t\n",
        body_content_type="text",
        sender_display_name="Alice",
        chat_topic="Project Alpha",
    )
    kwargs_tab_newline: dict[str, Any] = map_chat_message(raw_tab_newline)
    assert kwargs_tab_newline["summary"] is None
    # ADR-0020 retain-everything: tab/newline body is preserved verbatim.
    assert kwargs_tab_newline["body"] == "\t\n"
    assert kwargs_tab_newline["title"] == "Alice in Project Alpha"
    assert kwargs_tab_newline["external_id"] == "19:abc@thread.v2:1700000000001"
    assert kwargs_tab_newline["connector_name"] == "teams"
    assert kwargs_tab_newline["source_type"] == SOURCE_TYPE


# ---------------------------------------------------------------------------
# Phase 25-A (ADR-0010 §改訂): cross-connector author normalisation.
# ---------------------------------------------------------------------------


def test_author_fields_from_sender() -> None:
    """The Teams sender id/name flow onto ``author_handle`` / ``author_display``."""
    kwargs = map_chat_message(_raw(sender_id="user-alice", sender_display_name="Alice"))
    assert kwargs["author_handle"] == "user-alice"
    assert kwargs["author_display"] == "Alice"


def test_author_fields_none_for_system_message() -> None:
    """A sender-less (system) message leaves both author fields ``None``."""
    kwargs = map_chat_message(_raw(sender_id="", sender_display_name=""))
    assert kwargs["author_handle"] is None
    assert kwargs["author_display"] is None
