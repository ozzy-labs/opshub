# Gmail connector fixtures (Phase 14 G3)

Static Gmail API response samples used by the unit tests under
`tests/unit/connectors/google_mail/`. Each fixture mirrors the shape
Gmail returns for the named endpoint so the `httpx.MockTransport`
handlers in the tests can replay them verbatim.

| Fixture                              | Endpoint                                       | Purpose                                                                                                                                                       |
| ------------------------------------ | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `message_text_plain_only.json`       | `users.messages.get(format='full')`            | Single-part `text/plain` message — pins the simplest body shape (no MIME tree).                                                                               |
| `message_text_html_only.json`        | `users.messages.get(format='full')`            | Single-part `text/html` message — pins the fallback path when no `text/plain` part exists. The mapper should retain the HTML verbatim (no stripping).         |
| `message_multipart_alternative.json` | `users.messages.get(format='full')`            | `multipart/alternative` container with both `text/plain` and `text/html` parts — pins the "text/plain preferred over text/html" decision (Phase 14 plan OQ4). |
| `message_with_attachment.json`       | `users.messages.get(format='full')`            | `multipart/mixed` with a `text/plain` part + a binary attachment (`attachmentId` set, no `data`). Pins that the mapper ignores attachment parts.              |
| `message_no_labels.json`             | `users.messages.get(format='full')`            | Message with no `labelIds` — pins the `[Labels: ...]` stanza dropping cleanly when there are no labels.                                                       |
| `history_page.json`                  | `users.history.list(startHistoryId=...)`       | A single history page containing `messagesAdded` + `labelsAdded` entries (overlapping message ids exercise the dedup behaviour in `_iter_message_ids`).       |
| `profile.json`                       | `users.getProfile`                             | Mailbox profile response (`historyId` is the only field the connector reads).                                                                                 |
| `messages_list_page.json`            | `users.messages.list(q='after:<epoch>')`       | Initial-sync / TTL-fallback list page (each entry carries only `id` + `threadId`, matching the documented API shape).                                         |

Body bytes inside `message_*` fixtures use Gmail's URL-safe base64
encoding (the same encoding `users.messages.get` returns); the
mapper decodes them via `base64.urlsafe_b64decode`. All sample
bodies are short, opshub-internal-only text so the fixtures contain
no real PII / message content.
