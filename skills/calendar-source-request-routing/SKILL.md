---
name: calendar-source-request-routing
description: Route Calendar reads and approval-gated writes to PA.
version: 1.1.0
author: sisyphus-org
platforms: [linux, macos]
metadata:
  hermes:
    tags: [calendar, kanban, routing]
---

# Calendar Source Request Routing

Use `calendar_source_request` for Calendar requests. The source profile never asks the owner for an OAuth client, reads Google credentials, or calls Google APIs directly. The broker-dispatched `personal-assistant` owns Calendar access.

## Bounded reads

Call exactly one of `inventory`, `events`, or `freebusy` with `window` equal to `today`, `next-7-days`, or `next-30-days`. After `queued`, stop. On exact-session wake, replay the literal request once and report only the sanitized completed data.

## Approval-gated writes

Calendar and Linear are independent operations. A normal Calendar request must not create or require a Linear issue. Link them only when the owner explicitly supplies an SIS issue or asks for the connection.

1. For a standalone event, omit `linear_url`. Choose a stable safe `block_key` that includes the event date or another unique discriminator, for example `lavina-rusanovka-2026-09-06`, so unrelated events do not collide.
2. When the owner explicitly supplies `SIS-N` without its URL, resolve it through the Linear source route and pass only the returned canonical public `https://linear.app/.../issue/SIS-N/...` URL. Never pass Linear credentials or an internal ID.
3. Call `calendar_source_request` with `operation=create|update|delete`, exact `block_key`, `summary`, local Kyiv `start`/`end`, `details`, and optional canonical `linear_url`. Delete requires empty event fields.
4. When replay returns `phase=awaiting_approval`, show the exact `preview` to the owner. Do not approve implicitly or paraphrase away material fields. A standalone preview has an empty `linear_url`.
5. Only after an explicit approval in this same source session, call `calendar_source_request` with `operation=approve` and the exact opaque `approval_reference` returned with that preview.
6. After `queued`, stop. On wake, replay the exact approval call and report sanitized verified read-back.

Never copy workflow run IDs, task IDs, OAuth data, event IDs, artifact versions, checksums, before-state hashes, or internal routing fields into the human response. The public preview contains only the operation, block key, summary, details, Kyiv-aware start/end, timezone, and optional canonical Linear URL. If routing is unavailable, report the truthful capability error; never instruct the owner to upload an OAuth JSON file.

### Literal field preservation

When the owner supplies `linear_url` or `block_key`, copy it byte-for-byte into
the tool request after only the schema's ordinary whitespace handling. Never
shorten, expand, repair, or regenerate a Linear URL slug, even when it looks
truncated or differs from the issue title. Never derive `block_key` from the SIS
identifier, summary, or URL. If an explicit value fails tool validation, report
that validation error or ask the owner for a replacement; do not substitute a
different value. Before calling the tool, compare both outgoing values with the
owner's message. The preview must preserve them exactly; otherwise do not ask
for approval.

## Replay

Literal replay is required. The route derives one global semantic key and one exact source-session delivery key, so it reuses the same Kanban task and notification. Approval references are accepted only by the same profile/session that received the preview.

Never reconstruct, abbreviate, or guess an `approval_reference`. Keep the exact
opaque value from the plan tool result for the later approval and replay calls;
it remains internal and must not be printed to the owner. If compaction or lost
active context removes it, call `session_search()` without a cross-profile
selector and filter sessions to the current profile plus the exact Telegram
chat/thread represented by the active conversation. Require exactly one
matching session; zero or multiple matches fail closed. Recency must never
disambiguate multiple sessions. Read that session with
`session_search(session_id=<that exact session>)`. If the read is truncated,
scroll that same session around the matching plan message. Within it, require
exactly one `calendar_source_request` plan result whose owner-visible preview
matches every preview field exactly: `operation`, `block_key`, `summary`,
`details`, `start`, `end`, `timezone`, and `linear_url`. Compare the
timezone-aware `start` and `end` strings exactly as shown in the approved
preview, not against the pre-normalized request strings. Then copy the complete
`calendar-approval:v1:<64 lowercase hex>` value byte-for-byte from that tool
result. Never search by or reuse a partial hash prefix, derive a hash, or borrow
a reference from another preview/session. If session identity, full-preview
identity, or exact recovery is ambiguous, fail closed and request a fresh
preview instead of calling approve.
