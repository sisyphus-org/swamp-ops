# SWE → Project Manager → Linear → SWE E2E

SIS-61 wires an ordinary SWE Telegram root-DM request into the already verified SIS-59 deterministic Linear lane and SIS-60 exact-session wake route. Hermes core is unchanged.

## User contract

Supported initial phrase:

```text
Добавь к SIS-61 комментарий: SIS-61 E2E proof A.
```

Expected visible result after the asynchronous wake:

```text
Комментарий добавлен и прочитан обратно. Изменение verified.
https://linear.app/…
```

The exact second submission is a verified no-op. It must reuse the same semantic idempotency key and existing Kanban task, perform no second Linear mutation, and produce no second comment.

Internal task IDs and raw `done` lifecycle text stay hidden on success. Telegram topics, passive Kanban pings and cross-profile delivery fallback are outside the contract.

## Components

### SWE source edge

`plugins/swe_linear_route` exposes only `swe_linear_request(request)`.

1. Accepts the exact bounded Russian comment form with one uppercase `SIS-N` target.
2. Rejects missing/fuzzy/lowercase targets, unsupported operations, oversized bodies, unknown fields and credential-shaped text before durable writes.
3. Produces exact `linear-command.v1`; command/correlation IDs are UUIDv4, while the task idempotency key is a stable SHA-256-derived semantic key.
4. Requires the live source context to be `profile=swe`, `platform=telegram`, `chat_type=dm`, exact numeric chat/user IDs, no thread ID and a valid exact Hermes session ID.
5. Creates one `project-manager` Kanban task in `triage` with that exact source session and force-loads `project-manager-linear-worker`.
6. Writes exactly one source-owned root-DM subscription with `delivery_mode=wake`, `thread_id=NULL`, `chat_type=dm` and metadata exactly `{"chat_type":"dm"}`.
7. Runs the bundled SIS-60 read-only route audit and fails closed in `triage` on drift.
8. Releases a passing typed task with the shipped `specify_triage_task` kernel transition. That moves `triage → todo`, runs `recompute_ready`, and must leave the parent-free task in `ready`. `promote_task` is not valid for `triage`.

The source plugin imports no Linear client and reads no Linear token.

`swe-linear-request-routing` reinforces the source behavior: call the route tool once, never mutate through Linear MCP, and let the later exact-session wake produce one normal SWE answer.

### Project Manager worker edge

`plugins/project_manager_linear` exposes only no-argument `pm_linear_execute()` and bundles the SIS-59 lane implementation.

1. Runs only when `HERMES_PROFILE=project-manager`, a real `HERMES_KANBAN_TASK` is present, `HERMES_KANBAN_RUN_ID` matches that task's current running worker run, and the dispatcher-provided absolute `HERMES_KANBAN_DB` explicitly resolves the task's board database.
2. Before Linear access, CAS-extends the dispatcher claim with `HERMES_KANBAN_CLAIM_LOCK` and heartbeats the exact expected run; a recovered or superseded worker is rejected without mutation or lifecycle write.
3. Reads `linear-command.v1` only from the exact persisted `linear-kanban-task.v1` body; model-supplied command fields are rejected.
4. Re-validates exact `linear-command.v1`.
5. Reads the Linear token only inside the PM process.
6. Performs deterministic plan, apply and exact read-back with the existing hash-only journal/comment-marker idempotency contract.
7. Requires `linear-result.v1.verified=true`.
8. Completes the current task itself with the typed result stored in `task.result` and a concise human summary; on failure it records one redacted typed blocker.

The force-loaded `project-manager-linear-worker` skill tells the PM model to call the typed tool once and make no direct Linear, terminal or second lifecycle call.

## Local verification

```bash
cd /Users/hermes/workspaces/swamp-ops

/Users/hermes/.hermes/hermes-agent/venv/bin/python \
  -m unittest discover -s tests -v

hermes plugins doctor plugins/swe_linear_route --ci
hermes plugins doctor plugins/project_manager_linear --ci

swamp model validate linear-command-lane-plan
swamp workflow validate linear-command-lane-plan
swamp model validate kanban-source-route-audit
swamp workflow validate kanban-source-route-audit

env -u HERMES_DELEGATED_CHILD_CONTEXT \
  /Users/hermes/.hermes/hermes-agent/venv/bin/python \
  scripts/sis61_local_route_smoke.py
```

The smoke uses a temporary Kanban DB under this repository and removes it afterward. It performs no external/network write. Healthy output has one task, one subscription, exact source session, `taskStatus=ready`, `deliveryMode=wake`, `threadId=null`, and replay `already_in_flight`.

## Reviewed rollout

Roll out only an exact reviewed 40-character commit already merged to `origin/main`. Never install from a dirty feature checkout.

The owner performs the profile writes and SWE system-Gateway restart. The installation block must:

1. verify and detach `/Users/hermes/workspaces/swamp-ops-runtime` at `REVIEWED_SHA` with a clean tree and no `.swamp-sources.yaml`;
2. extract from the exact Git object, not mutable worktree bytes:
   - `plugins/swe_linear_route` → SWE profile plugins;
   - `skills/swe-linear-request-routing` → SWE profile skills;
   - `plugins/project_manager_linear` → Project Manager profile plugins;
   - `skills/project-manager-linear-worker` → Project Manager profile skills;
3. byte-compare every installed artifact against the staged Git archive;
4. run Plugin Doctor in each target profile;
5. enable `swe-linear-route` and `project-manager-linear` with no tool-override grant;
6. run `config check` for both profiles;
7. read back compact non-bundled plugin state;
8. restart only `system/local.hermes.gateway-swe`.

Project Manager has no resident Gateway requirement for this path: the broker dispatcher spawns a fresh PM worker process with the PM profile, installed plugin and force-loaded skill. Broker remains the sole dispatcher and receives no Linear or Telegram credential.

## Live success and replay proof

After rollout, the owner sends the supported phrase once from the ordinary SWE root DM, never a Telegram topic.

Operational read-back must prove:

- exactly one task with the semantic idempotency key;
- task `session_id` equals the exact creating SWE session;
- exactly one subscription owned by SWE with root-DM `wake` fields;
- route audit passes before dispatch and after the terminal cursor advances;
- broker is the only claimant and one PM run completes;
- `task.result` parses as `linear-result.v1` with `verified=true` and exact SIS-61 URL;
- Linear contains exactly one comment with the expected body and internal hash marker;
- the source SWE gateway logs one exact-session wake;
- SWE sends one normal human-facing answer and no passive Kanban message;
- audit/task/run/result/log evidence contains no credential-shaped data.

The owner then sends the exact same phrase again. Verify task/subscription/run/comment counts remain one and no second Linear mutation occurs. The source tool returns the existing verified result as `verified_no_op`.

## Failure probes

Run these operationally; do not ask the owner to break services manually beyond the one explicit restart command required by a probe.

- **Malformed command / missing target:** source tool rejects before task creation.
- **Credential-shaped body:** source tool rejects before durable task creation.
- **Route drift:** task remains in `triage`; broker cannot claim it.
- **PM auth failure:** PM tool performs no blind retry, records one redacted `capability` blocker, and SWE wakes with a human blocker response.
- **PM crash:** dispatcher failure accounting retries within policy; after a possible post-mutation crash, the marker/journal/read-first lane converges to verified no-op instead of adding another comment.
- **Broker restart:** the sole dispatcher lock is reacquired by broker; the same ready/running task is recovered with no duplicate claim or mutation.
- **SWE outage:** use the already verified SIS-60 procedure; the terminal event remains pending and retries only through SWE after recovery.

## Rollback

1. Stop accepting new SWE Linear requests.
2. Disable `swe-linear-route` and restart only the SWE Gateway.
3. Disable `project-manager-linear`; no PM Gateway restart is needed when none is resident.
4. Keep existing Kanban tasks/results and the Linear idempotency journal for reconciliation; do not delete evidence.
5. Do not enable broker/default delivery fallback, passive notifications or Telegram task topics.
6. Re-enable only from a reviewed commit after Plugin Doctor, suite, route smoke and exact profile read-back pass again.
