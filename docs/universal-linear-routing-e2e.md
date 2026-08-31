# Universal source profile → Project Manager → Linear E2E

SIS-68 defines the universal form of the SIS-61 SWE route for every current and future user-facing Hermes profile. Hermes core is unchanged. Local tests prove the contract; the live universal tracer remains a post-deploy gate and is not claimed here.

## Architecture

```text
exact source Telegram session
  → linear_source_request
  → linear-command.v2 in one exact-delivery Kanban task
  → broker (sole dispatcher, no Linear or Telegram credential)
  → project-manager / pm_linear_execute
  → Linear plan → mutation → exact read-back
  → exact source-owned wake subscription
  → one human-facing source-profile response
```

`broker` and `project-manager` are special profiles and must never receive the ordinary source ingress baseline.

SIS-77 uses one current contract: the source emits only `linear-command.v2` in `linear-kanban-task.v2`, and Project Manager emits and validates only `linear-result.v2`. The global mutation payload and exact delivery payload use `linear:v2` and `linear-delivery:v2`. Any other schema fails closed before mutation or lifecycle writes; no alternate source-side Linear route exists.

## Source plugin

`plugins/linear_source_route` provides the `linear-source-route` toolset with one tool, `linear_source_request`.

The plugin:

- derives the authoritative source profile from Hermes' resolved runtime home;
- accepts any syntactically valid user-facing profile name except `broker` and `project-manager`;
- requires Telegram DM context, exact numeric chat/user/thread IDs, and the persisted exact Hermes session ID;
- supports bounded comment, safe workflow-state change, issue creation under an exact `SIS-N` parent, create-only hierarchy convergence, standalone top-level creation in an exact existing project/milestone, one top-level issue plus 1–10 explicit sub-issues, and non-destructive exact-name initiative create/update/project-link operations without caller-controlled entity IDs;
- derives a global mutation key from only `operation`, `target`, `change`, and `policy`; source profile/session and command/correlation IDs are excluded;
- derives a separate delivery key from that mutation key plus the exact source profile/platform/chat/user/thread/session;
- atomically gets or creates one PM-assigned Kanban task by delivery key inside one Kanban write transaction;
- installs exactly one source-owned `wake` subscription and audits it before triage release;
- returns only public status and user-relevant target facts to the source model; task/run IDs, routing/idempotency keys, schema versions, worker state, route audits, hashes, UUIDs, internal entity IDs and raw PM payloads never cross the tool-result boundary;
- imports no Linear client and reads no Linear credential.

The paired `linear-source-request-routing` skill requires short factual replies: `Принято, выполняю.` while queued, then only the final user-visible result and canonical Linear URL. It forbids Kanban inspection after queueing, direct Linear mutation, generic GraphQL, terminal fallback, passive lifecycle narration, invented Telegram topics, or another profile's bot.

## Project Manager lane

`plugins/project_manager_linear` remains the only write-capable Linear lane. `pm_linear_execute()` reads the authoritative command from the persisted Kanban task and accepts no model-supplied command object.

Supported operations:

- `read_issue`;
- `change_state` to the safe non-terminal allowlist;
- `update_issue` for an explicit non-empty subset of description, safe state, and bounded priority on one exact `SIS-N`;
- `add_comment` with deterministic-ID replay protection and a clean user-authored body;
- `create_issue` in the `SIS` team under one exact `SIS-N` parent, with bounded title/description, safe state, and High/Medium/Low priority.
- `converge_hierarchy` for exactly one `SIS` project → milestone → top-level issue; it performs complete bounded scoped preflight, safe unique exact-name reuse for project/milestone, deterministic-ID-only issue handling, exact scoped-list read-back, and crash/concurrent replay convergence under the existing hash-only journal lock.
- `create_standalone_issue` for one top-level issue in an exact existing project/milestone, with explicit priority and absent parent; a unique exact-title legacy partial write may be safely adopted only inside the verified scope.
- `converge_issue_tree` for one top-level issue plus 1–10 explicit sub-issues, each with deterministic identity, exact parent read-back, crash recovery, and literal replay no-op.
- standalone `create_project`, `create_milestone`, `update_project`, and `update_milestone` operations with exact-name selection, deterministic create IDs, bounded name/description/target-date management, exact read-back, and names/dates-only source projection.
- `create_initiative`, `update_initiative`, and `link_project_to_initiative` with exact-name selection, deterministic initiative/link UUIDv4 IDs, exact read-back/no-op replay, and names/dates-only source projection. Unlink, archive/delete, initiative hierarchy/status/owner/labels/search/approval/bulk, and arbitrary-team operations are not exposed.

Comments, created issues, hierarchy entities, standalone issues, and every declared sub-issue use domain-separated deterministic Linear UUIDv4 IDs derived inside Project Manager from the global mutation key. The unique exact team key `SIS` identifies the team independently of its mutable display name. Project/milestone reuse requires unique exact-name, scope, and supplied-description verification. The PM journal request hash excludes source profile, command/correlation IDs, session, and delivery metadata, so identical requests from different source sessions converge globally while retaining distinct exact-wake tasks. Mutation text remains exactly user-authored; shared read-back comparison permits only the confirmed deterministic Linear plain-URL serialization. Failed read-back returns only allowlisted mismatched field names. No operation sets `verified=true` until every supplied scalar and structural relationship is read back exactly, and literal replay after a partial write produces no duplicate.

## Bootstrap invariant

The default `hermes-profile-bootstrap` role now:

- installs `plugins/linear_source_route` and `skills/linear-source-request-routing` inside the new profile;
- enables `linear-source-route` with `allow_tool_override=false`;
- keeps `kanban.dispatch_in_gateway=false`;
- injects only the shared Telegram allowlist, never `LINEAR_TOKEN`;
- does not configure Linear MCP;
- reports required source-Gateway and broker restarts plus config/model/STT/PM read-back/wake/replay gates.

The `project-manager` role retains Linear credential/MCP configuration. The `broker` role retains neither.

## Local verification

```bash
cd /Users/hermes/workspaces/swamp-ops

/Users/hermes/.hermes/hermes-agent/venv/bin/python \
  -m unittest discover -s tests -v

hermes plugins doctor plugins/linear_source_route --ci
hermes plugins doctor plugins/project_manager_linear --ci

# PM credential context only; source profiles must never run this directly.
HERMES_PROFILE=project-manager python scripts/linear_pm_readonly_smoke.py \
  --live --operation inventory_linear \
  --entity-type issues --entity-type projects \
  --entity-type milestones --entity-type initiatives \
  --exclude-archived

swamp model validate hermes-profile-bootstrap
swamp workflow validate hermes-profile-bootstrap
swamp model validate linear-command-lane-plan
swamp workflow validate linear-command-lane-plan
swamp model validate kanban-source-route-audit
swamp workflow validate kanban-source-route-audit

env -u HERMES_DELEGATED_CHILD_CONTEXT \
  /Users/hermes/.hermes/hermes-agent/venv/bin/python \
  scripts/linear_source_local_route_smoke.py
```

The local route smoke performs no network mutation. Healthy output reports one ready task, one wake subscription, the exact session/thread, and replay without a second task. The live PM smoke uses only fixed read queries, exhausts pagination, emits safe counts, and creates no journal entry. Source profiles remain credential-free: no `LINEAR_TOKEN`, Linear MCP, GraphQL, or direct read client. The concurrent temporary-DB test separately races one delivery and proves exactly one task and one subscription.

## Reviewed rollout for existing profiles

Roll out only from an exact reviewed 40-character commit merged to `origin/main`. Never install mutable worktree bytes.

For each of `default`, `ideas`, `swe`, `books`, and `crypto-analyst`, one at a time:

1. Extract `plugins/linear_source_route` and `skills/linear-source-request-routing` from the reviewed Git object and byte-compare the installed copy.
2. Remove the obsolete `swe-linear-route`/SWE skill where present.
3. Use `hermes config unset mcp_servers.linear`; never hand-edit `config.yaml`.
4. Replace the source profile's command secret helper with the Telegram-allowlist-only helper using `hermes config set`; prove resolved source config contains no `LINEAR_TOKEN` or general Linear MCP.
5. Enable `linear-source-route` with no tool-override grant and run Plugin Doctor plus `config check` in that profile.
6. Restart the affected source Gateway.
7. Restart broker after the new source toolset is installed so its long-lived worker resolver sees the current catalog.
8. Verify new PIDs/readiness, sole dispatcher ownership, and the real PM child toolset.
9. Post-deploy, from the profile's real existing Telegram thread, submit one unique bounded comment command, then the literal replay.
10. Record the still-required live tracer evidence: one delivery task, one subscription, one PM mutation/no-op sequence, exact source-session wake, and no credential-shaped audit data.

Do not remove the old direct Linear surface from the next profile until the current profile passes the complete gate.

## Future-profile proof

For a disposable user-facing profile:

1. run Swamp plan twice and compare stable routing/security fields;
2. review and run apply;
3. verify installed plugin/skill and absence of Linear MCP/token injection;
4. provide the profile-specific bot token and model auth without copying secrets;
5. install/start its dedicated Gateway;
6. run config check, real model response, real Russian STT, Plugin Doctor, source-Gateway restart, broker restart, PM read-back, exact-session wake, and literal replay;
7. remove the disposable profile only through the reviewed profile-deletion procedure after evidence is recorded.

## Rollback

1. Stop accepting new source Linear requests for the affected profile.
2. Disable `linear-source-route` and restart only that source Gateway.
3. Preserve Kanban tasks/results and the PM idempotency journal/markers for reconciliation.
4. Do not enable broker/default delivery fallback, passive notifications, direct source Linear mutation, or per-task Telegram topics.
5. Re-enable only from a reviewed commit after Plugin Doctor, tests, route audit, source restart, broker restart, and exact profile read-back pass again.
