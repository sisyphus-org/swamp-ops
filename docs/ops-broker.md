# Operations Manager GitHub/Swamp lane

SIS-62 moves shared GitHub and Swamp execution out of `default` without turning the Kanban `broker` into a privileged profile.

## Role map

| profile | transport | execution | credentials | prohibited |
|---|---|---|---|---|
| `broker` | sole Kanban dispatcher/task bus | dispatch only | no GitHub, Swamp, Linear, Google, or Telegram credentials | integration clients, owner approval, profile work |
| `operations-manager` | receives broker-dispatched operations tasks as a spawned worker; no standalone Gateway | sole bounded GitHub/Swamp executor | separately scoped profile-local `GH_TOKEN` and `SWAMP_API_KEY` | Telegram, Gateway dispatch, Kanban dispatch, Linear, Calendar |
| `default` | source task + exact-session wake | source validation and owner authority only | source/Telegram credentials only after cutover | shared GitHub/Swamp execution |
| other source profiles | source task + exact-session wake | source validation only | profile-local source credentials | shared GitHub/Swamp execution |
| `project-manager` | receives Linear tasks | Linear only | `LINEAR_TOKEN` | GitHub/Swamp/Calendar |
| `personal-assistant` | receives Calendar tasks | Calendar only | Google OAuth | GitHub/Swamp/Linear |

A Hermes profile is not an OS sandbox. This separation is a capability and credential boundary between trusted profile roles; untrusted code still requires an OS user, VM, or terminal sandbox.

## Data flow

```text
exact Telegram source session
  → `ops_broker` in `linear-source-route`
  → operations-command.v1 / operations-kanban-task.v1
  → broker (dispatch only)
  → operations-manager + operations-manager-worker
  → `om_ops_execute` with no arguments
  → fixed argv + exact policy + attested runtime
  → operations-result.v1
  → exact source-session wake and literal replay
```

The public source tool keeps the existing five-field request contract:

```json
{
  "request_id": "<uuid>",
  "integration": "github|swamp",
  "operation": "<allowlisted operation>",
  "arguments": {},
  "mode": "plan|apply"
}
```

Caller identity is not accepted in the request. The source plugin derives it from the resolved runtime profile and exact Telegram session. `default` maps to `owner` only for the policy-bound Telegram owner ID. The Operations Manager worker independently verifies the persisted task creator, source session, one exact `wake` subscription, notifier profile, and owner ID before execution.

The semantic mutation key excludes the caller-provided request UUID and source delivery metadata. A separate delivery key binds profile, platform, chat, user, thread, and session. Literal replay returns the same verified task result and creates no second task.

## Security invariants

- `broker` has no operations plugin, A2A executor listener, provider credential, or owner principal.
- Source profiles have no shared `GH_TOKEN`, `GITHUB_TOKEN`, `SWAMP_API_KEY`, executor tool, or direct Operations Manager A2A route.
- `operations-manager` is the only profile with `om_ops_execute`; the worker accepts `{}` only and reads the command from the claimed Kanban task.
- The worker requires canonical profile name `operations-manager`, exact task/run/claim binding, exact source wake route, and profile-local credential presence before provider execution.
- GitHub repositories, Swamp models/workflows/data, modes, and owner-only operations remain policy allowlisted.
- Commands use fixed `shell=False` argv. Arbitrary shell, URLs, environment access, caller fields, and credentials are rejected.
- Runtime code comes only from the clean detached `/Users/hermes/workspaces/swamp-ops-runtime` checkout matching the Operations Manager revision marker.
- Audit state lives under `/Users/hermes/.hermes/profiles/operations-manager/plugin-data/ops-broker/` and contains no command output or credentials.
- Project Manager and Personal Assistant boundaries do not change.

## Repository components

- Source routing: `plugins/linear_source_route/ops_route.py`
- Source tool registration: `plugins/linear_source_route/__init__.py` (`ops_broker`)
- Executor and fixed policy engine: `plugins/ops_broker/`
- Worker tool: `om_ops_execute`
- Source skill: `skills/operations-source-request-routing/`
- Worker skill: `skills/operations-manager-worker/`
- Profile bootstrap: `scripts/hermes_profile_bootstrap.py`

## Reviewed rollout sequence

Do not deploy from a feature worktree. The rollout begins only after the PR is merged and the exact `origin/main` SHA has passed CI and CodeRabbit.

1. Materialize/stage the exact reviewed Git object and verify the detached immutable runtime is clean and has no `.swamp-sources.yaml`.
2. Create the canonical profile with the reviewed bootstrap in plan mode, review the contract, then apply into the previously absent profile directory.
3. Owner creates `/Users/hermes/.hermes/profiles/operations-manager/.env` (`0600`) with newly issued, separately scoped `GH_TOKEN` and `SWAMP_API_KEY`. Never copy values from `default`.
4. Install the reviewed `ops_broker` tree only in `operations-manager`; install the reviewed `linear_source_route` plus source skill in each source profile.
5. Remove/disable the legacy direct `ops-broker` executor from `default` in the same availability-preserving cutover. Do not run both public `ops_broker` tools in one profile.
6. Write the Operations Manager runtime revision marker only after byte-for-byte plugin checks and Plugin Doctor pass.
7. Validate every changed profile config before restart.
8. No Operations Manager Gateway is installed or restarted. Owner restarts each changed source Gateway, then `broker` last so its long-lived worker-toolset resolver sees `om_ops_execute` in the worker profile.
9. Revoke the legacy default GitHub/Swamp credentials only after routed live verification succeeds and no rollback is needed.

Gateway/LaunchDaemon changes remain owner-controlled. The repository workflow does not write `.env`, install services, or restart processes.

## Live acceptance

Record evidence for each source profile:

1. `ops_broker` creates one task assigned to `operations-manager` with exact source session/thread and one `wake` subscription.
2. Broker process only dispatches; it has no provider variables, operations plugin, or executor tool.
3. Worker process runs as `operations-manager`, calls `om_ops_execute`, and uses its profile-local credentials.
4. Real `github.repository_access` and `swamp.validate_workflow` return verified read-back.
5. Negative requests reject caller fields, arbitrary targets/workflows, shell, credentials, wrong modes, and owner-only operations from non-owner profiles.
6. Literal source replay returns the same result with unchanged task/run/external-mutation counts.
7. Default and ordinary source profiles cannot execute the provider operation locally or connect directly to Operations Manager.
8. Audit contains the authenticated caller and operation but no credential-shaped content.

## Rollback

Before credential revocation, rollback is:

1. stop new source routing;
2. restore the exact previously attested default plugin/config bytes;
3. restore the previous default runtime marker;
4. restart changed source Gateways and default, then broker last;
5. verify the old direct read-only path and negative probes;
6. only then remove the incomplete Operations Manager activation.

After legacy credentials are revoked, rollback requires fresh owner-issued default credentials; never copy the Operations Manager secrets back. Preserve task and audit evidence when an apply outcome is unknown, and do not replay non-idempotent approval starts without a fresh trusted preview.
