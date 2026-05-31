# Google Calendar connector fixtures (Phase 14 G4)

Static Calendar API v3 response samples used by the unit tests under
`tests/unit/connectors/google_calendar/`. Each fixture mirrors the
shape Calendar returns for the named endpoint so the
`httpx.MockTransport` handlers in the tests can replay them verbatim.

Phase 14 audit cluster D2 (F-1) activated these fixtures from the
calendar `test_client.py` via the `_fixture(name)` helper pattern
that mirrors the Gmail-side `tests/unit/connectors/google_mail/test_client.py`
loader. Tests that pin response-independent behaviour (multi-page
cursor handoff, retry budget, sentinel emission, all-day shape) keep
their inline JSON because the fixture file shape would couple the pin
to fixture content drift — the asymmetry is intentional and matches
the Gmail-side split (`history_page.json` is activated; the
synthetic-pagination tests inline their bodies).

| Fixture                                  | Endpoint               | Purpose                                                                                                                                                                                                          |
| ---------------------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `events_single.json`                     | `events.list`          | A populated single timed event with `start.timeZone` / `location` / `description` / multi-attendee. Documents the real response shape — pins normaliser field coverage and the G-6 `timeZone` preservation contract. |
| `events_recurring_with_override.json`    | `events.list`          | A recurring **master** event plus its **override** entry (returned as distinct items under `singleEvents=false`). Pins the OQ3 / ADR-0010 §改訂 (l) §不変条件 3 invariant.                                              |
| `sync_token_gone.json`                   | `events.list` (410)    | Documented 410 envelope (`error.code=410` + `errors[].reason="fullSyncRequired"`). Pins the `SyncTokenExpiredError` recovery contract.                                                                           |

Per [Phase 14 plan §7.5](../../../docs/phase-14-plan.md) the fixture
set covers `single / recurring + override / 410 GONE`. All-day events
and complex RRULE / RDATE / EXDATE shapes are pinned via inline JSON
in `test_client.py` and synthetic `RawCalendarEvent` fixtures in
`test_mapper.py` (G-4) — keeping those as inline JSON avoids spawning
a fixture file per edge case while staying within the §7.5 listing.
