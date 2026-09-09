---
name: operations-source-request-routing
description: Route bounded GitHub/Swamp requests through broker to Operations Manager.
version: 1.0.0
---

# Operations source routing

Use `ops_broker` for every supported shared GitHub or Swamp operation. The tool validates the bounded request, creates or replays one exact-session Kanban task, and routes it through the credential-free `broker` dispatcher to `operations-manager`.

## Rules

- Never call GitHub or Swamp directly for shared operations from a source profile.
- Never request, read, copy, or expose `GH_TOKEN`, `GITHUB_TOKEN`, or `SWAMP_API_KEY`.
- Supply exactly `request_id`, `integration`, `operation`, `arguments`, and `mode`.
- Caller and owner identity are derived from the live source profile/session; never add identity fields.
- After `queued`, reply that the request is being handled and stop.
- On wake, replay the exact same request once and return only the sanitized operation result.
- Owner-only approvals are available only from the authenticated owner session in `default`.
- Do not bypass Kanban with direct A2A to `operations-manager`.
