# Project Manager Linear command lane

SIS-59 introduces a deterministic, policy-bounded command lane for the `project-manager` profile. It accepts `linear-command.v2` JSON and emits `linear-result.v2` JSON.

## MVP contract

Required envelope:

```json
{
  "schema_version": "linear-command.v2",
  "command_id": "UUIDv4",
  "correlation_id": "UUIDv4",
  "idempotency_key": "linear:SIS-59:read:2026-08-28",
  "source_profile": "swe",
  "operation": "read_issue | change_state | add_comment | create_issue | converge_hierarchy",
  "target": {"type": "issue", "identifier": "SIS-N"},
  "change": {},
  "policy": {"mode": "standard"}
}
```

The validator rejects unknown fields, arbitrary GraphQL or MCP method names, URLs, fuzzy identifiers, arrays/bulk targets, unsupported operations and unbounded payloads. After exact issue resolution, the execution lane additionally rejects targets whose resolved team is not `SIS`. `idempotency_key` is 8–200 characters, starts with an alphanumeric character, and thereafter uses only `A-Za-z0-9:._/-`.

For mutations, `idempotency_key` is global: its canonical payload contains only `operation`, `target`, `change`, and `policy`. Source profile/session, command/correlation IDs, and delivery metadata are provenance, not mutation identity. They cannot alter the journal request hash or deterministic entity IDs. `source_profile` is nevertheless preserved in each run's `linear-result.v2` and audit trail.

The lane accepts only `linear-command.v2` and emits/accepts only `linear-result.v2`; the PM worker accepts only persisted `linear-kanban-task.v2`. Any other schema fails closed before mutation or lifecycle writes. Source-generated mutation keys are in the global `linear:v2` namespace and exact-source delivery keys are in `linear-delivery:v2`.

### Allowed operations

- `read_issue`: `change` must be empty.
- `change_state`: `change` must contain only `state`; exact allowlist is `Backlog`, `Todo`, `Research`, `In Progress`, `In Review`.
- `add_comment`: `change` must contain only `body`, 1–4000 characters. The public comment body remains exactly this user-authored text; reserved internal marker text cannot be supplied by the caller.
- `create_issue`: `target` is exactly `{"type":"team","identifier":"SIS"}` and `change` contains bounded `title`, `description`, exact `parent_identifier`, safe `state`, and `High|Medium|Low` priority. The public issue description remains exactly user-authored.
- `converge_hierarchy`: `target` is exactly `{"type":"team","identifier":"SIS"}` and `change` contains exactly one `project`, one `milestone`, and one top-level `issue`, with no entity UUID fields. Names/titles are 1–200 characters; optional descriptions are at most 10,000 characters; optional issue state uses the safe-state allowlist. The trusted PM lane derives canonical UUIDv4 IDs internally from the semantic idempotency key with separate project/milestone/issue domain strings. Slice 1 is create-only convergence: exact existing entities are verified no-ops, missing entities are created, and supplied-field drift or same-name/title collisions fail closed. Omitted optional fields are neither sent nor compared.

`Done`, `Canceled`, `Duplicate`, archive, delete, bulk and unrestricted structure changes are unavailable and fail closed. They remain owner-controlled instead of being enabled by an untrusted command field.

### SIS-77 hierarchy tracer contract (live proof post-deploy)

The first supported hierarchy shape is intentionally fixed and bounded. Unit tests cover plan/apply/read-back/replay and cross-profile convergence; a live SIS-77 tracer remains post-deploy evidence and is not claimed by this document:

```json
{
  "operation": "converge_hierarchy",
  "project": {
    "name": "health"
  },
  "milestone": {
    "name": "Подолог"
  },
  "issue": {
    "title": "Сходить в Solomia и записаться",
    "description": "https://solomia.in.ua"
  }
}
```

The source plugin validates and persists this discriminated request but has no Linear credential/client. The PM lane independently validates the resulting `linear-command.v2`, derives domain-separated entity IDs from its semantic idempotency key, performs bounded scoped lists for the exact `SIS` team, and never probes an absent deterministic project, milestone, or issue through a direct ID lookup. Random command/correlation UUIDs do not affect the semantic key or entity IDs.

## Plan and apply

Plan is read-only:

```bash
LINEAR_TOKEN=... /Users/hermes/.hermes/hermes-agent/venv/bin/python \
  scripts/linear_command_lane.py \
  --command commands/linear/<slug>.json \
  --mode plan
```

The committed Swamp workflow exposes only plan:

```bash
LINEAR_TOKEN=... swamp workflow run linear-command-lane-plan \
  --input command=<slug>
```

After review, the deterministic apply uses the same command:

```bash
LINEAR_TOKEN=... HERMES_HOME=/Users/hermes/.hermes/profiles/project-manager \
  /Users/hermes/.hermes/hermes-agent/venv/bin/python \
  scripts/linear_command_lane.py \
  --command commands/linear/<slug>.json \
  --mode apply
```

The normal journal is `$HERMES_HOME/linear-command-lane/journal.json`. It stores only SHA-256 hashes of idempotency keys and global mutation requests; it stores no source profile, command/session/delivery identity, command body, comment text, or credential.

## Idempotency and recovery

- A mutation apply requires the hash-only journal. A journal-wide advisory file lock serializes the complete local read/check/mutate/read-back/journal sequence across processes, preventing concurrent same-key comments and lost journal updates.
- State changes read current state first. Already-converged state is a verified no-op, so replay after a crash cannot duplicate the mutation.
- Comments use a deterministic Linear UUID derived from the hashed idempotency key. Preflight resolves that ID only inside the exact issue's bounded comment list because Linear returns an error, not `null`, when `comment(id:)` targets a missing entity. Post-create read-back uses the exact ID and body. Replay is a verified no-op; the same key with a different request fails closed without exposing metadata in the comment.
- Issue creation uses a separate deterministic Linear UUID derived from the same hashed key. Preflight searches that ID only in the exact parent's bounded child list because Linear errors rather than returning `null` for a missing `issue(id:)`; post-create read-back verifies the exact ID, parent, title, description, state and priority. Replay after a post-create crash converges without a duplicate issue or visible metadata in the description.
- A local hash-only journal detects cross-operation or changed-global-request reuse after a successful verified apply, while allowing identical different-profile/session deliveries to converge under the same key.
- Apply always reads back the exact `SIS-N` target or deterministic entity ID. Missing/mismatched read-back is an error, never success.
- Hierarchy apply holds the same single journal-wide lock across preflight, all creates, exact scoped list read-backs, and journal commit. It creates only missing project → milestone → issue entities with internally derived UUIDv4s. A lost journal, crash after any intermediate create, literal replay, or concurrent replay converges through those IDs without duplicate entities; changed semantic content derives a different ID set and fails closed on same-name/title collisions. Every currently reachable project scope (projects, then milestones/issues when the project exists, plus an explicitly requested state) is read before the first write; each intermediate create is verified before the next write.
- Hierarchy plan/apply/no-op results contain typed `before` and `after` snapshots with canonical IDs, names/titles, supplied descriptions/state, structural project/milestone/team/parent IDs, and issue identifier/URL when present in live data. Plan actions never expose descriptions or hidden metadata; they contain at most fixed SHA-256 hashes and bounded lengths. `verified=true` is emitted only after exact scoped read-back matches all supplied scalar and structural fields (including a read-only already-converged plan/no-op).

## Historical SIS-59 operation evidence

The 2026-08-28 live SIS-59 run predates the SIS-77 protocol-version cutover and remains evidence for operation/read-back behavior, not for the current envelope version. The v2-only contract is covered by the current local tests and validation matrix.

- exact read returned a typed verified result with `state=In Progress`;
- the Swamp plan workflow completed successfully;
- an apply requesting current `In Progress` returned verified `no_op` with no state write;
- comment plan recorded before/after and body hash without exposing the body in the plan;
- first comment apply returned `applied` and exact-ID/body read-back passed;
- replay returned `no_op`; Linear read-back showed exactly one clean user-authored comment;
- a real state plan recorded `In Progress → In Review`, apply returned `applied` with exact read-back, and immediate replay returned verified `no_op`.

The final closure to `Done` remains outside the lane and owner/task-workflow controlled.
