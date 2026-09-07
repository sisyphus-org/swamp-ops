---
name: calendar-source-request-routing
description: Route Calendar reads and explicit-intent writes to PA.
version: 1.2.1
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

## Explicit-intent writes

Calendar and Linear are independent operations. A normal Calendar request must not create or require a Linear issue. Link them only when the owner explicitly supplies an SIS issue or asks for the connection.

An explicit owner instruction to create or add a Calendar event is the authorization for that exact bounded creation. Do **not** ask for a second confirmation after the create request is already clear. Update and delete retain the existing preview/approval flow because they modify or remove an existing target.

Before calling the tool, resolve required fields from the current request and unambiguous conversation context:

- title/summary;
- local Kyiv date and start time;
- end time or a clearly established duration;
- exact target identity for update/delete.

Ask one focused clarification only when a required value is genuinely missing or has multiple plausible interpretations. Do not ask merely because the value appeared in the previous message, can be derived from a relative phrase such as `через час после этой`, or uses a conventional duration already established by the referenced event.

1. For a standalone event, omit `linear_url`. Choose a stable safe `block_key` that includes the event date/time or another unique discriminator so separate events do not collide.
2. When the owner explicitly supplies `SIS-N` without its URL, resolve it through the Linear source route and pass only the returned canonical public `https://linear.app/.../issue/SIS-N/...` URL. Never pass Linear credentials or an internal ID.
3. For create, call `calendar_source_request` once with exact `block_key`, `summary`, local Kyiv `start`/`end`, `details`, and optional canonical `linear_url`.
4. After `queued`, stop. On exact-session wake, replay the literal create request once and report only sanitized verified read-back.

The routed worker still performs a protected plan, before-state snapshot, attestation workflow, fresh snapshot comparison, provider mutation, and exact read-back in one run. The source agent never receives workflow run IDs, OAuth data, event IDs, artifact versions, checksums, before-state hashes, or internal routing fields. If routing is unavailable, report the truthful capability error; never instruct the owner to upload an OAuth JSON file.

### Preview-gated update and delete

For a new update or delete request, retain the existing two-step flow:

1. Call `calendar_source_request` with the exact update/delete fields. Delete requires empty event fields.
2. After `queued`, stop. On exact-session wake, replay that literal request once to obtain the exact machine preview and opaque approval reference.
3. Show the material target/action fields to the owner and ask for explicit confirmation.
4. Only after confirmation in the same source session, call `operation=approve` with the exact opaque reference.
5. After `queued`, stop. On wake, replay the exact approval call and report sanitized verified read-back.

`operation=approve` also remains available for create previews issued before this single-step contract was deployed. A literal same-session replay of such a create must return the existing protected preview; it must never create a new direct-execution task. Never create a new preview-first flow for an ordinary create.

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

Literal replay is required. The route derives one global semantic key and one exact source-session delivery key, so it reuses the same Kanban task, verified external result, and notification without another mutation.

### Legacy approval recovery

Approval references from pre-1.2.0 previews are accepted only by the same profile/session that received the preview.

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
exactly one `calendar_source_request` plan result whose complete machine preview
matches every preview field exactly: `operation`, `block_key`, `summary`,
`details`, `start`, `end`, `timezone`, and `linear_url`. Compare the
timezone-aware `start` and `end` strings exactly as shown in the approved
machine preview, not against the localized owner-facing rendering or the
pre-normalized request strings. Then copy the complete
`calendar-approval:v1:<64 lowercase hex>` value byte-for-byte from that tool
result. Never search by or reuse a partial hash prefix, derive a hash, or borrow
a reference from another preview/session. If session identity, full-preview
identity, or exact recovery is ambiguous, fail closed and request a fresh
preview instead of calling approve.
