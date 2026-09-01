# Linear owner-approved terminal, relation, archive, and delete changes

This repository exposes narrowly owner-approved destructive slices only:

1. move one exact `SIS-N` issue through existing `change_state` to exactly `Done`, `Canceled`, or `Duplicate`;
2. replace or clear the parent of one exact `SIS-N` issue through parent-only `update_issue`;
3. remove one exact existing issue relation by two exact `SIS-N` endpoints and `relation_type`;
4. replace/rewire one exact existing issue relation with one exact new endpoint/type relation;
5. archive one exact issue, SIS-scoped project, or workspace initiative;
6. delete/trash one exact issue, SIS-scoped project, project milestone, or workspace initiative.

All other terminal names, nonterminal owner approval, bulk targets, caller-selected cascade flags, arbitrary entity types, and destructive lifecycle operations remain unavailable.

## Trust boundaries

```text
source profile → exact approval reference + exact typed intent → PM Kanban task
source/SWE → ops_broker typed plan/start request → Swamp read-only plan
owner Telegram session → ops_broker owner-only approve → Swamp attestation
persisted linear-command.v2 → PM common approval gate → bounded Linear mutation
```

- Project Manager remains the sole holder of `LINEAR_TOKEN` and sole Linear mutation boundary.
- Swamp never receives `LINEAR_TOKEN`, calls Linear, or mutates Linear. It creates immutable plan and approval-attestation artifacts only.
- Broker caller identity comes from the authenticated session. A request body cannot claim owner identity.
- Only the policy-bound Telegram owner may approve. A2A peers may plan and start the suspended attestation workflow but cannot approve it.

## Exact approval intents

`linear-destructive-owner-approval-plan.v1` accepts only one of the three exact terminal state intents, a parent-only update, or one of these exact relation shapes:

```json
{
  "operation": "change_state",
  "target": {"type": "issue", "identifier": "SIS-77"},
  "change": {"state": "Duplicate"}
}
```

```json
{
  "operation": "remove_issue_relation",
  "target": {"type": "issue", "identifier": "SIS-77"},
  "change": {
    "related_identifier": "SIS-94",
    "relation_type": "blocked_by"
  }
}
```

```json
{
  "operation": "replace_issue_relation",
  "target": {"type": "issue", "identifier": "SIS-77"},
  "change": {
    "old_related_identifier": "SIS-94",
    "old_relation_type": "blocked_by",
    "new_related_identifier": "SIS-95",
    "new_relation_type": "related"
  }
}
```

Every endpoint is exact uppercase `SIS-N`, every relation type is exactly `blocks`, `blocked_by`, or `related`, and no endpoint may equal the target. Raw relation IDs are never accepted from source. No additional change field is allowed. The plan binds the exact canonical SHA-256 of the PM lane's full before-state relation inventory, including internal relation identity used only inside the trusted approval/executor boundary, plus a UTC RFC3339 expiry no more than 24 hours in the future. The public/source projection never exposes those IDs or inventory bytes.

The attestation is emitted only after the fixed workflow suspends at `approve-linear-destructive-intent`, the authenticated owner approves that exact run, and the immutable plan artifact is reloaded by fixed model, workflow run ID, artifact version, and checksum. Intent transport remains canonical bounded base64url. Broker commands remain fixed `shell=False` argv.

## Source policy reference

Without approval, source emits exactly `{"mode":"standard"}`. Relation removal and replacement reject that policy before Linear access. With approval, source accepts only the existing fixed structural reference shape and emits exactly:

```json
{"mode":"owner_approved","approval":{...fixed reference fields...}}
```

The reference contains only fixed workflow/model, attestation run UUID, positive artifact version, attestation checksum, intent hash, before-state hash, and expiry. Source exposes no arbitrary policy, approval boolean, path, manifest, shell text, caller-selected workflow/model, or raw relation ID. Policy remains part of semantic replay identity.

## PM relation behavior

PM resolves the target and every old/new endpoint independently and requires both/all issues to belong to the same exact `SIS` team. It canonicalizes `blocked_by` by reversing the API endpoints into Linear `blocks`; `related` is symmetric and canonicalized by identifier ordering. Removal requires exactly one matching live relation; zero and ambiguous matches fail closed. Replacement requires exactly one old match and at most one exact new match.

The before-state is the complete bounded `relations + inverseRelations` inventory, normalized and sorted by internal ID before hashing. Plans and source/public output contain only endpoint identifiers and user-facing relation types.

Linear's current schema was checked and exposes `issueRelationUpdate`, but the approved executor contract for this slice explicitly uses the existing deterministic create plus a fixed `issueRelationDelete` document. Rewire therefore:

1. records hash-only recovery state under the journal lock;
2. creates the exact deterministic new relation only when absent;
3. immediately reads it back by deterministic ID and verifies canonical endpoints/type;
4. deletes the one resolved old relation;
5. immediately re-inventories and requires the exact expected full inventory;
6. records verified completion only after convergence.

If old deletion fails, a newly created relation is deleted and the exact before inventory is re-read. If final read-back drifts, the lane restores the old relation and removes only the new relation it created, then either proves exact compensation or reports compensation failure closed. A partial rewire is never reported successful.

The public `pm_linear_execute` path delegates the persisted task to `execute_claimed_task`. Before first apply, that boundary atomically records a durable 30-second apply lease bound to the approval checksum, intent hash, approved before-state hash, full canonical command hash, command/correlation/idempotency identities, source profile, and exact Kanban task ID. The existing live re-plan remains the final TOCTOU gate before that first claim. A concurrent caller cannot mint apply authority.

Relation and parent-only update paths durably prepare exact before/after recovery hashes before writing. After process death, only the same task and complete persisted command may re-enter after the lease expires, and only when the lane proves an exact prepared/intermediate/completed recovery state carrying the same approval, intent, and command hashes. A changed command ID, correlation ID, idempotency key, source profile, task, intent, policy artifact, or unexplained live state conflicts and fails closed. A prepared journal whose live state is still the original before-state is not recovery evidence and cannot revive a stale claim.

Verified read-back permanently marks the approval completed with its exact completed recovery-evidence hash. Completed approval can never authorize another mutation; an exact completed replay is accepted only as the already-converged verified no-op. Relation delete/create and reparent crash replay therefore need no fresh owner approval and issue no duplicate relation or second parent write.

Wrong/expired/forged approval, wrong intent, wrong before hash, changed live plan, wrong team, self relation, zero/ambiguous old match, ambiguous new match, read-back drift, partial failure, and recovery-journal conflict all fail closed.

## PM terminal-state behavior

Standard policy remains blocked for `Done`, `Canceled`, and `Duplicate`. Owner approval is accepted only on `change_state` with exactly one of those names. PM resolves the exact name inside the target issue's exact `SIS` team and requires one unique state with a non-empty internal ID. The live SIS schema inventory fixes semantic type compatibility as `Done=completed`, `Canceled=canceled`, and `Duplicate=duplicate`. Missing, duplicate-name, or wrong-type state inventory fails before mutation.

Apply uses the existing minimal `IssueUpdateInput {stateId}` mutation only. It writes prepared before/after state hashes before the API call, immediately reads the exact issue back, verifies state ID/name/type and every unmanaged field, and only then records completion. Crash after the write recovers through `execute_claimed_task` and the durable approval lease as one verified no-op; it never issues a second state mutation. Literal completed replay uses the same evidence and does not re-verify or re-consume approval.

## Archive/delete matrix and behavior

The exact supported matrix is deliberately asymmetric:

| operation | issue | project | milestone | initiative |
|---|---:|---:|---:|---:|
| `archive_linear_entity` | `issueArchive` | deprecated but live authenticated `projectArchive` | unsupported: authenticated schema exposes no milestone archive mutation | `initiativeArchive` |
| `delete_linear_entity` | `issueDelete(permanentlyDelete:false)` trash | `projectDelete` trash | `projectMilestoneDelete` | `initiativeDelete` trash |

Selectors are public names only: issue `{identifier: SIS-N}`; project `{name}` with exact unique workspace name and SIS team membership; milestone `{project,name}` with both exact and the project SIS-scoped; initiative `{name}` unique in the workspace. Raw IDs, account/team entities, arbitrary entities, bulk selectors, and cascade flags are not accepted.

Preflight reads the exact entity snapshot plus complete applicable impact: recursive issue children and relations; project issues, milestones, and initiative links; milestone issues; initiative projects. It canonicalizes and sorts affected entities, includes deterministic counts and affected public snapshots in the plan, and binds all of it into the approval before-state hash. Nonempty impact never blocks after the owner approves that exact snapshot. Apply still performs only the one fixed lifecycle mutation—never a caller-selected cascade or bulk document.

Archive read-back requires absence from normal inventory, exactly one `includeArchived:true` match with non-null `archivedAt`, unchanged unmanaged entity fields, and unchanged affected entities/links. Delete follows Linear's documented semantics: issue/project/initiative deletes are recoverable trash (30-day grace period), not claims of immediate physical erasure; milestone delete is the fixed delete mutation. Every delete must disappear from normal and archived-inclusive scoped inventory, return null from the fixed direct lookup, and match mutation payload evidence (`entity:null` or exact trusted `entityId`). Every impacted child/issue/milestone/project/link is re-read: child parents, issue project/milestone links, project milestones, initiative links, and issue relations may change only as defined by the deleted container; unrelated drift fails closed. Hash-only prepared/completed recovery remains bound to approval, intent, full command, before/impact hashes, and expected after hash.

The read-only live smoke `inventory_entity_destruction` introspects the current fixed mutation name and executes only exact snapshot/impact preflight. It emits selector, impact counts, and a before-state SHA-256, never internal entity IDs or mutation calls.

## Deliberately unavailable

Arbitrary terminal names, nonterminal owner approval, account/team archive or delete, milestone archive, bulk archive/delete, caller-selected cascade, permanent issue deletion, initiative unlink as a standalone operation, arbitrary GraphQL, arbitrary relation IDs, and all other destructive lifecycle operations remain rejected before mutation.
