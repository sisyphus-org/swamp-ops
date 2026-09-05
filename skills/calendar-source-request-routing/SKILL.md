---
name: calendar-source-request-routing
description: Route Calendar reads and approval-gated writes to PA.
version: 1.0.2
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

1. Resolve any supplied `SIS-N` through the Linear source route first when a canonical URL was not already supplied. Pass only the canonical public `https://linear.app/.../issue/SIS-N/...` URL; never pass Linear credentials or an internal ID.
2. Call `calendar_source_request` with `operation=create|update|delete`, exact `block_key`, `summary`, local Kyiv `start`/`end`, canonical `linear_url`, and `details`. Delete requires empty event fields.
3. When replay returns `phase=awaiting_approval`, show the exact `preview` to the owner. Do not approve implicitly or paraphrase away material fields.
4. Only after an explicit approval in this same source session, call `calendar_source_request` with `operation=approve` and the exact opaque `approval_reference` returned with that preview.
5. After `queued`, stop. On wake, replay the exact approval call and report sanitized verified read-back.

Never copy workflow run IDs, task IDs, OAuth data, event IDs, artifact versions, checksums, before-state hashes, or internal routing fields into the human response. The public preview contains only the operation, block key, summary, details, Kyiv-aware start/end, timezone, and canonical Linear URL. If routing is unavailable, report the truthful capability error; never instruct the owner to upload an OAuth JSON file.

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
selector, choose the unique most recent Telegram session whose preview contains
the current owner's replay request, then read it with
`session_search(session_id=<that exact session>)`. If the read is truncated,
scroll that same session around the matching plan message. Require a unique
`calendar_source_request` plan result with the same `block_key` and `linear_url`,
then copy the complete `calendar-approval:v1:<64 lowercase hex>` value
byte-for-byte from that tool result. Never search by or reuse a partial hash
prefix, derive a hash, or borrow a reference from another preview/session. If
session identity or exact recovery is ambiguous, fail closed and request a
fresh preview instead of calling approve.
