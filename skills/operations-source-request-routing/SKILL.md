---
name: operations-source-request-routing
description: Route bounded GitHub/Swamp requests through broker to Operations Manager.
version: 1.1.0
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

## GitHub branch and PR publication

Only an authenticated `default` owner session may publish reviewed code. SWE prepares and verifies commits but does not authorize external publication. Use two ordered operations and preserve every value exactly between them:

1. `github.publish_branch`, `mode=apply`, with exactly `repository`, `branch`, `head_sha`, `base`, and `base_sha`. `branch` is the exact `SIS-N`; both SHA values are verified lowercase 40-character commits; `base` is `main`. The executor checks exact fetch and push origin URLs, binds the local branch and `origin/main` to both SHAs, requires the head to descend from `base_sha`, disables hooks/follow-tags/submodule push, performs one non-force exact-ref push, and reads the exact remote ref back.
2. After completion, `github.upsert_pull_request`, `mode=apply`, with the same five fields plus exact `title` and `body`. The title begins with the same `SIS-N` followed by a space or colon; the body contains that ticket's complete canonical `https://linear.app/sisyphusx/issue/SIS-N/<slug>` URL as a standalone link. The executor creates at most one open same-repository PR or updates its base/title/body, explicitly creates a non-draft PR, and reads repository identities, number, URL, branch, base, head, title, body, state, and draft status back.

Publication uses a global semantic lock and hash-only journal. Ambiguous push/create/update outcomes are reconciled by exact read-back; one blocked delivery is requeued once for bounded reconciliation. Never invent a commit SHA, publish a free-form branch, change base, force-push, merge, close, delete a branch, or put credentials in title/body. Run repository tests and inspect the diff before publication. After PR creation, use `github.pull_request_checks` with the returned PR number; CodeRabbit and CI handling remain governed by the repository workflow.
