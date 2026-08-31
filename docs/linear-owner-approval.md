# Linear destructive owner-approval foundation

This repository contains the **approval foundation only** for future bounded destructive Linear operations. It does not expose or execute archive, delete, terminal-state, reparent, relation-delete, bulk, arbitrary GraphQL, or arbitrary shell operations.

## Trust boundaries

```text
source/SWE → ops_broker typed plan/start request → Swamp read-only plan
owner Telegram session → ops_broker owner-only approve → Swamp attestation
future persisted linear-command.v2 → Project Manager fixed verifier → Linear mutation (not implemented)
```

- Project Manager remains the sole holder of `LINEAR_TOKEN` and the sole future Linear mutation boundary.
- Swamp never receives `LINEAR_TOKEN`, calls Linear, or mutates Linear. It creates immutable plan and approval-attestation artifacts only.
- Broker caller identity comes from the authenticated session. A request body cannot claim owner identity.
- Only the policy-bound Telegram owner may approve. A2A peers, including `swe`, may plan and start the suspended attestation workflow but cannot approve it.

## Artifact contracts

`linear-destructive-owner-approval-plan.v1` is deterministic for three exact values:

1. one validated future destructive intent (`operation`, one exact `SIS-N` target, bounded operation-specific `change`);
2. one SHA-256 hash of the exact before-state;
3. one UTC RFC3339 expiry no more than 24 hours in the future.

The plan is read-only and includes its schema version, exact intent and intent hash, before-state hash, expiry, exact planned action, approval prompt, and canonical SHA-256 checksum.

`linear-destructive-owner-approval-attestation.v1` is emitted only after the apply/attestation workflow suspends at `approve-linear-destructive-intent`, the authenticated owner approves that exact run, and the same immutable plan artifact is reloaded by fixed model name, workflow run ID, artifact version, and checksum. The attestation repeats and checksums every binding. It is an approval fact, not a Linear write.

Intent is transported to Swamp as canonical base64url with a bounded character pattern. The broker accepts exact typed objects and constructs fixed argv with `shell=False`; callers cannot supply command strings, paths, source profiles, raw manifest IDs, or approval booleans.

## PM policy reference and fail-closed verification

The existing policy remains unchanged and valid:

```json
{"mode":"standard"}
```

The validator also understands an exact `owner_approved` reference containing only fixed workflow/model, attestation run ID, positive artifact version, attestation checksum, intent hash, before-state hash, and expiry. This is structural recognition only: no destructive operation was added to the Linear operation allowlist or source route.

Before any future destructive mutation, PM must:

1. build the normal read-only Linear plan;
2. derive the exact semantic intent hash and canonical before-state hash;
3. load only the fixed Swamp workflow history and fixed attestation model/version from `/Users/hermes/workspaces/swamp-ops-runtime` (never a policy- or caller-supplied path);
4. require the exact run to have succeeded and the explicit approval step to have succeeded;
5. bind workflow, model, run, version, checksum, intent, before-state, expiry, and original plan inputs exactly without consuming the approval;
6. immediately re-plan from live Linear and require the canonical before-state hash plus operation, target, and concrete lane plan to equal the approved original plan;
7. reject expired, suspended, unapproved, malformed, mismatched, or drifted attestations without consuming them;
8. atomically consume the attestation checksum in a hash-only locked journal immediately before mutation and pass the resulting opaque authorization into the lane; `owner_approved` apply fails without it.

Concurrent replay can therefore mint only one apply authorization. Drift discovered by the live re-plan consumes nothing and performs no Linear mutation.

A caller-provided `approved: true`, manifest ID, artifact path, shell command, policy path, or source profile never grants approval.

## Deliberately unavailable

The following remain rejected before Linear mutation: terminal states, archive, issue delete, project/initiative destructive lifecycle operations, issue reparent as a newly approved destructive path, relation delete, unlink, bulk targets, and arbitrary queries. Enabling any one of them requires a separate reviewed vertical slice with operation-specific preflight, mutation, exact read-back, replay, source routing, and live proof.
