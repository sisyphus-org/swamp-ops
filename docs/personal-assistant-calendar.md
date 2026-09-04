# SIS-84 — Google Calendar Read and Managed Write Lanes

## Scope

The Personal Assistant Google Calendar integration is split into two deterministic lanes:

- a bounded **read-only** lane;
- a primary-calendar **create/update/delete** lane with a checksum-bound preview and mandatory manual approval.

The Calendar lane never receives Linear credentials and never calls Linear. The orchestrator resolves an exact task through the existing broker → Project Manager Linear route and passes the returned canonical `https://linear.app/.../issue/SIS-N/...` URL into the Calendar plan. Create/update validate that URL and write it into the event description as `Linear: <canonical URL>`; update/delete refuse to mutate an existing event unless it carries that exact link.

### Read allowlist

- **Calendar ID**: `primary` only.
- **Scopes**: `calendar.calendarlist.readonly`, `calendar.events`, `calendar.freebusy`.
  - `calendar.events` supports the bounded managed-write lane; possession of the scope does not authorize a write, which remains blocked until checksum-bound preview and explicit owner approval.
  - Zero non-Calendar scopes (Gmail, Drive, Docs, Sheets, Contacts, etc.).
- **Timezone**: all requests and output are normalized to `Europe/Kyiv`. The zoneinfo database resolves DST automatically: winter 2026 = UTC+02, summer 2026 = UTC+03.

### Write allowlist

- **Calendar ID**: `primary` only.
- **Operation**: create, update, or delete one timed event block only.
- **Required linkage**: one canonical `SIS-N` Linear issue URL in the event description.
- **Stable identity**: `eventId` is derived from a domain-separated SHA-256 of the Linear identifier plus safe `blockKey` (`primary` by default). It is independent of the plan checksum, so changed event fields still address the same block and one issue may own multiple named blocks.
- **Approval**: the plan workflow stores the exact event body and SHA-256 checksum in its protected, versioned Swamp artifact. A separate manual-approval workflow loads that exact artifact and emits a checksum-bound attestation. Apply accepts only fixed-format plan/approval run IDs, artifact versions, and checksums; it independently verifies both artifacts and the succeeded approval step before any Calendar mutation.
- **Read-back/replay**: create inserts only when absent; update requires the linked target and uses `events.update`; delete requires the linked target and accepts either HTTP 404 or Google's `status: cancelled` tombstone as verified absence. Exact create/update replay and already-absent delete are verified no-ops. A later approved create for the same block restores a linked cancelled tombstone with `status: confirmed` instead of attempting a duplicate insert. Google’s observed `Europe/Kiev` alias/offset serialization is accepted only when it represents the exact planned UTC instants. Ambiguous write responses are reconciled by deterministic GET.
- Recurrence, attendees, notifications, and non-primary calendars remain blocked.

### Protected data

- **Default read output never exposes** event titles, locations, descriptions, calendar IDs, event IDs, email addresses, or OAuth token paths. The direct CLI-only `--include-summary` option may retain titles solely in the profile-local `0600` payload; the committed read workflow does not expose that option.
- The exact write preview (including summary and description) is intentionally present **only** in the protected, versioned `google-calendar-write-plan` Swamp artifact so the owner can review the mutation. It is never included in normal Calendar read output or read artifacts.
- Normal read-workflow stdout remains sanitized and contains only: `operation`, `status`, `timezone`, `window`, `bounds`, `calendar_count`, `writable_calendar_count`, `event_count`, `all_day_events`, `recurring_events`, `busy_intervals`.

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

## Approval-gated Calendar create/update/delete

Build a read-only plan:

```bash
swamp workflow run google-calendar-write-plan \
  --input operation=create \
  --input blockKey=primary \
  --input summary='Review SIS-84' \
  --input start=2026-09-07T10:00 \
  --input end=2026-09-07T10:30 \
  --input linearUrl=https://linear.app/sisyphusx/issue/SIS-84/example
```

Review the exact event body and checksum in the protected, versioned plan result. Then run the separate approval workflow with only that artifact reference:

```bash
swamp workflow run google-calendar-write-approval \
  --input planRunId=<plan-run-uuid> \
  --input planArtifactVersion=<positive-version> \
  --input planChecksum=<reviewed-checksum>
```

For update use `operation=update` with the same `linearUrl` and `blockKey` plus the replacement `summary/start/end/details`. For delete use `operation=delete`, the same `linearUrl` and `blockKey`, and empty `summary/start/end/details`; any nonempty event field is rejected.

The approval workflow suspends at `approve-calendar-write`. If manually approved, it emits a versioned attestation bound to the exact plan artifact. Apply does not contain an approval gate and accepts no raw event fields:

```bash
swamp workflow run google-calendar-write-apply \
  --input planRunId=<plan-run-uuid> \
  --input planArtifactVersion=<positive-version> \
  --input planChecksum=<reviewed-checksum> \
  --input approvalRunId=<approval-run-uuid> \
  --input approvalArtifactVersion=<positive-version> \
  --input approvalChecksum=<attestation-checksum>
```

Apply reloads the exact plan (including `operation`, `blockKey`, and deterministic `eventId`), rebuilds it from exact workflow inputs, verifies the approval workflow and manual step succeeded, verifies attestation provenance and checksum binding, obtains an opaque in-process authorization token, and only then calls Calendar. Normal output contains only `operation`, `status`, `reused`, Linear identifier, and `blockKey`; it excludes title and description.

## Remaining future work

- Multi-calendar read/write (beyond primary).
- Sync/recurrence engine.

## Verification

All public seams are unit-tested. Every new observable behavior has a failing test
first (RED), then minimal code makes it pass (GREEN). The full test suite and
Swamp model/workflow validation are available on the `SIS-84` branch.