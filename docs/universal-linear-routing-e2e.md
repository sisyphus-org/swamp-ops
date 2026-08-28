# Universal source profile → Project Manager → Linear E2E

SIS-68 generalizes the SIS-61 SWE tracer bullet for every current and future user-facing Hermes profile. Hermes core is unchanged.

## Architecture

```text
exact source Telegram session
  → linear_source_request
  → linear-command.v1 in one Kanban task
  → broker (sole dispatcher, no Linear or Telegram credential)
  → project-manager / pm_linear_execute
  → Linear plan → mutation → exact read-back
  → exact source-owned wake subscription
  → one human-facing source-profile response
```

`broker` and `project-manager` are special profiles and must never receive the ordinary source ingress baseline.

## Source plugin

`plugins/linear_source_route` provides the `linear-source-route` toolset with one tool, `linear_source_request`.

The plugin:

- derives the authoritative source profile from Hermes' resolved runtime home;
- accepts any syntactically valid user-facing profile name except `broker` and `project-manager`;
- requires Telegram DM context, exact numeric chat/user/thread IDs, and the persisted exact Hermes session ID;
- supports bounded comment, safe workflow-state change, and issue creation under an exact `SIS-N` parent;
- creates or replays one PM-assigned Kanban task using a semantic idempotency key;
- installs exactly one source-owned `wake` subscription and audits it before triage release;
- imports no Linear client and reads no Linear credential.

The paired `linear-source-request-routing` skill forbids direct Linear mutation, generic GraphQL, terminal fallback, passive Kanban pings, invented Telegram topics, or another profile's bot.

## Project Manager lane

`plugins/project_manager_linear` remains the only write-capable Linear lane. `pm_linear_execute()` reads the authoritative command from the persisted Kanban task and accepts no model-supplied command object.

Supported operations:

- `read_issue`;
- `change_state` to the safe non-terminal allowlist;
- `add_comment` with marker-backed replay protection;
- `create_issue` in the `SIS` team under one exact `SIS-N` parent, with bounded title/description, safe state, and High/Medium/Low priority.

Issue creation stores an internal idempotency marker in the description and searches only the exact parent's bounded child set before writing. A literal replay returns the existing verified issue; a key collision or ambiguous marker fails closed. All writes require exact read-back before `linear-result.v1.verified=true`.

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

The local smoke performs no network mutation. Healthy output reports one ready task, one wake subscription, the exact session/thread, and replay without a second task.

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
9. From the profile's real existing Telegram thread, submit one unique bounded comment command, then the literal replay.
10. Read back one task, one subscription, one PM run, one Linear mutation, exact source-session wake, and no credential-shaped audit data.

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
