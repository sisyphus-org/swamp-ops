# Project Manager Linear command lane

SIS-59 introduces a deterministic, policy-bounded command lane for the `project-manager` profile. It accepts `linear-command.v1` JSON and emits `linear-result.v1` JSON.

## MVP contract

Required envelope:

```json
{
  "schema_version": "linear-command.v1",
  "command_id": "UUIDv4",
  "correlation_id": "UUIDv4",
  "idempotency_key": "linear:SIS-59:read:2026-08-28",
  "source_profile": "swe",
  "operation": "read_issue | change_state | add_comment | create_issue",
  "target": {"type": "issue", "identifier": "SIS-N"},
  "change": {},
  "policy": {"mode": "standard"}
}
```

The validator rejects unknown fields, arbitrary GraphQL or MCP method names, URLs, fuzzy identifiers, arrays/bulk targets, unsupported operations and unbounded payloads. After exact issue resolution, the execution lane additionally rejects targets whose resolved team is not `SIS`. `idempotency_key` is 8–200 characters, starts with an alphanumeric character, and thereafter uses only `A-Za-z0-9:._/-`.

### Allowed operations

- `read_issue`: `change` must be empty.
- `change_state`: `change` must contain only `state`; exact allowlist is `Backlog`, `Todo`, `Research`, `In Progress`, `In Review`.
- `add_comment`: `change` must contain only `body`, 1–4000 characters. The public comment body remains exactly this user-authored text; legacy internal marker text is reserved and cannot be supplied by the caller.
- `create_issue`: `target` is exactly `{"type":"team","identifier":"SIS"}` and `change` contains bounded `title`, `description`, exact `parent_identifier`, safe `state`, and `High|Medium|Low` priority. The public issue description remains exactly user-authored.

`Done`, `Canceled`, `Duplicate`, archive, delete, bulk and unrestricted structure changes are unavailable and fail closed. They remain owner-controlled instead of being enabled by an untrusted command field.

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

The normal journal is `$HERMES_HOME/linear-command-lane/journal.json`. It stores only SHA-256 hashes of idempotency keys and semantic requests; it stores no command body, comment text or credential.

## Idempotency and recovery

- A mutation apply requires the hash-only journal. A journal-wide advisory file lock serializes the complete local read/check/mutate/read-back/journal sequence across processes, preventing concurrent same-key comments and lost journal updates.
- State changes read current state first. Already-converged state is a verified no-op, so replay after a crash cannot duplicate the mutation.
- Comments use a deterministic Linear UUID derived from the hashed idempotency key. Preflight resolves that ID only inside the exact issue's bounded comment list because Linear returns an error, not `null`, when `comment(id:)` targets a missing entity. Post-create read-back uses the exact ID and body. Replay is a verified no-op; the same key with a different request fails closed without exposing metadata in the comment.
- Issue creation uses a separate deterministic Linear UUID derived from the same hashed key, then verifies exact parent, title, description, state and priority. Replay after a post-create crash converges without a duplicate issue or visible metadata in the description.
- A local hash-only journal detects cross-operation or changed-request reuse after a successful verified apply.
- Apply always reads back the exact `SIS-N` target or deterministic entity ID. Missing/mismatched read-back is an error, never success.

## Live SIS-59 evidence

Verified 2026-08-28 against the real SIS-59 issue:

- exact read returned `linear-result.v1`, `state=In Progress`, `verified=true`;
- the Swamp plan workflow completed successfully;
- an apply requesting current `In Progress` returned verified `no_op` with no state write;
- comment plan recorded before/after and body hash without exposing the body in the plan;
- first comment apply returned `applied` and exact-ID/body read-back passed;
- replay returned `no_op`; Linear read-back showed exactly one clean user-authored comment;
- a real state plan recorded `In Progress → In Review`, apply returned `applied` with exact read-back, and immediate replay returned verified `no_op`.

The final closure to `Done` remains outside the lane and owner/task-workflow controlled.
