# SIS-84 — Google Calendar Read Lane

## Scope

The Personal Assistant Google Calendar integration is split into a deterministic **read-only** lane (this issue) and a future **write** lane requiring manual approval.

### Read allowlist

- **Calendar ID**: `primary` only.
- **Scopes**: `calendar.calendarlist.readonly`, `calendar.events`, `calendar.freebusy`.
  - Zero non-Calendar scopes (Gmail, Drive, Docs, Sheets, Contacts, etc.).
- **Timezone**: all requests and output are normalized to `Europe/Kyiv`. The zoneinfo database resolves DST automatically: winter 2026 = UTC+02, summer 2026 = UTC+03.

### Write allowlist

- **Empty** in this slice. Any write operation (event create, update, delete) must be manually approved and is out of scope until the separate preview/approval slice is implemented.

### Protected data

- **Never exposed** to stdout or artifacts: no event titles, locations, descriptions, calendar IDs, event IDs, email addresses, or OAuth token paths.
- Stdout output contains only: `operation`, `status`, `timezone`, `window`, `bounds`, `calendar_count`, `writable_calendar_count`, `event_count`, `all_day_events`, `recurring_events`, `busy_intervals`.

## Live run commands

These are the public CLI entrypoints the Swamp workflow invokes.

### Deterministic smoke plan (no network, no AI)

```bash
/Users/hermes/.hermes/hermes-agent/venv/bin/python \
  scripts/google_calendar_read.py --operation smoke --window today \
  --profile personal-assistant
```

Expected stdout:
```json
{"operation":"smoke","status":"ok","timezone":"Europe/Kyiv","window":"today",
 "bounds":{"start":"2026-xx-xxT00:00:00+02:00","end":"2026-xx-xxT00:00:00+02:00"}}
```

### Calendar inventory (counts only, sanitized)

```bash
/Users/hermes/.hermes/hermes-agent/venv/bin/python \
  scripts/google_calendar_read.py --operation inventory --window today \
  --profile personal-assistant
```

Expected stdout: counts of calendars and writable calendars.

### Events read (30-day window, sanitized)

```bash
/Users/hermes/.hermes/hermes-agent/venv/bin/python \
  scripts/google_calendar_read.py --operation events --window next-30-days \
  --profile personal-assistant --live
```

Reads the primary calendar API, normalizes all-day / timed / recurring events to Kyiv-safe forms,
and prints a redacted payload. No event titles, locations, or descriptions appear in stdout.

## Manual approval gate for future writes

When a write lane is added, it must pass through an explicit owner-approval gate:

1. The workflow suspends and emits a checksum-bound plan diff + intent.
2. Owner approves the exact checksum-bound intent in Linear.
3. After approval, the task moves to the write slice with full manual review.

## Future work (out of scope)

- Calendar write/create operations with preview/approval gates.
- Multi-calendar read (beyond primary).
- OAuth scope expansion beyond Calendar-only.
- Sync/recurrence engine.

## Verification

All public seams are unit-tested. Every new observable behavior has a failing test
first (RED), then minimal code makes it pass (GREEN). The full test suite and
Swamp model/workflow validation are available on the `SIS-84` branch.