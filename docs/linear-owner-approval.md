# Linear owner-approved destructive reparenting and clear

This repository implements one destructive Linear slice: replacing the existing parent of one exact `SIS-N` issue, or clearing that parent, through the existing `update_issue` operation. Archive/delete, terminal states, relation deletion, bulk, arbitrary GraphQL, arbitrary shell, and every other destructive operation remain unavailable.

## Trust boundaries

```text
source profile → exact approval reference + parent-only update_issue → PM Kanban task
source/SWE → ops_broker typed plan/start request → Swamp read-only plan
owner Telegram session → ops_broker owner-only approve → Swamp attestation
persisted linear-command.v2 → PM common approval gate → bounded Linear parentId mutation
```

- Project Manager remains the sole holder of `LINEAR_TOKEN` and sole Linear mutation boundary.
- Swamp never receives `LINEAR_TOKEN`, calls Linear, or mutates Linear. It creates immutable plan and approval-attestation artifacts only.
- Broker caller identity comes from the authenticated session. A request body cannot claim owner identity.
- Only the policy-bound Telegram owner may approve. A2A peers may plan and start the suspended attestation workflow but cannot approve it.

## Exact approval intent

`linear-destructive-owner-approval-plan.v1` now accepts exactly:

```json
{
  "operation": "update_issue",
  "target": {"type": "issue", "identifier": "SIS-77"},
  "change": {"parent_identifier": null}
}
```

`parent_identifier` may instead be one exact uppercase `SIS-N` string. No second change field is allowed. The plan also binds one exact SHA-256 hash of the lane's before-state and one UTC RFC3339 expiry no more than 24 hours in the future. It is deterministic, read-only, and checksum-bound.

`linear-destructive-owner-approval-attestation.v1` is emitted only after the fixed workflow suspends at `approve-linear-destructive-intent`, the authenticated owner approves that exact run, and the immutable plan artifact is reloaded by fixed model, workflow run ID, artifact version, and checksum. The attestation is an approval fact, not a Linear write.

Intent is transported as canonical bounded base64url. Broker commands are fixed argv with `shell=False`; callers cannot supply command strings, paths, source profiles, raw manifest IDs, or approval booleans.

## Source policy reference

Without `approval`, source emits exactly:

```json
{"mode":"standard"}
```

With the one fixed structural `approval` object, source emits exactly:

```json
{"mode":"owner_approved","approval":{...fixed reference fields...}}
```

The reference contains only fixed workflow/model, attestation run UUID, positive artifact version, attestation checksum, intent hash, before-state hash, and expiry. It is accepted only on a parent-only `update_issue`. Source exposes no arbitrary `policy`, approval boolean, path, manifest, shell text, or caller-selected workflow/model.

The semantic idempotency key includes the complete policy. Literal approval replay therefore preserves identity, while standard and owner-approved requests cannot collide.

## PM gate and lane behavior

Standard mode keeps the existing safe attach behavior: attaching a currently top-level issue to one exact same-team, non-self, non-cyclic parent is allowed. Standard clear and replacement remain blocked.

For owner-approved clear or replacement, PM:

1. builds the normal read-only lane plan;
2. derives the exact semantic intent hash and canonical before-state hash;
3. loads only the fixed Swamp workflow history and attestation model/version from `/Users/hermes/workspaces/swamp-ops-runtime`;
4. requires the exact run and explicit approval step to have succeeded;
5. binds workflow, model, run, version, checksum, parent-only intent, before-state, expiry, and original plan inputs;
6. immediately re-plans live Linear and requires the same before-state hash, operation, target, and concrete plan;
7. atomically consumes the attestation checksum immediately before apply;
8. passes the opaque consumed authorization to the lane.

The lane cannot be called directly with a boolean or forged authorization. It preserves exact target/team, self-parent, ancestry/cycle, and parent-shape checks. Apply sends only `parentId`—including explicit `null` for clear—then immediately reads the exact target back. The requested parent must match exactly and all unmanaged issue fields must remain unchanged. A literal already-converged replay is a verified no-op.

Wrong, expired, forged, already-consumed, changed-before-state, plan/target drift, cycle, wrong-team, and read-back drift all fail closed.

## Deliberately unavailable

Terminal states, archive, issue deletion, relation deletion, unlink, project/initiative destructive lifecycle operations, bulk targets, and arbitrary queries remain rejected before Linear mutation. The approval contract does not authorize them.
