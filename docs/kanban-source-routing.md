# Source-profile Kanban routing

SIS-60 keeps Kanban execution internal and returns the user-facing result through the ordinary Telegram DM session of the originating profile. Telegram task topics are not part of the contract. The broker owns dispatch only; it never owns Telegram delivery.

## Contract

For a task created from a Telegram root DM, the source profile must:

1. Create the Kanban task through `kanban_create(..., triage=true)` and obtain its exact `t_<8 hex>` ID. Triage is the setup gate: the dispatcher must not claim the task yet.
2. Keep the task and raw terminal event internal; do not create a Telegram topic or send a passive Kanban status ping.
3. Upsert exactly one notification subscription with:
   - `platform=telegram`;
   - exact source `chat_id` and `user_id`;
   - `thread_id=NULL`;
   - `chat_type=dm`;
   - `notifier_profile=<source profile>`;
   - `delivery_mode=wake`;
   - delivery metadata either absent or exactly `{"chat_type":"dm"}`; no thread/topic/reply fields.
4. Require the task's persisted `session_id` to equal the exact source Hermes session that created it. A missing or different session ID fails closed before dispatch.
5. On completion, blocker or another wake-worthy terminal event, let the source-profile gateway resume that exact source session and inject the worker handoff.
6. Let the awakened source agent read the task/result and send one concise human-facing answer in its own voice.

Hermes already supports `wake` as a first-class notification mode. For push adapters it intentionally skips the passive platform message; the wake is the delivery. A failed wake rewinds the claimed cursor so a later notifier tick retries instead of losing or rerouting the event.

The stock `/kanban create` and model-tool auto-subscribe paths initially use `notify+wake`. Until Hermes exposes a configurable auto-subscribe default, the supported operational path is to immediately upsert the same subscription through the shipped CLI:

```bash
hermes kanban --board <board> notify-subscribe <task-id> \
  --platform telegram \
  --chat-id <chat-id> \
  --user-id <user-id> \
  --chat-type dm \
  --notifier-profile <source-profile> \
  --delivery-mode wake
```

`notify-subscribe` uses the existing root-DM subscription key and updates the route rather than creating a second route. Source profiles must complete this step and require an audit pass while the task remains in triage. Only then may they run `hermes kanban --board <board> promote <task-id> "source route verified"`. A setup failure leaves the task in triage for reconciliation; it must not be delivered by broker, default or another profile.

## Read-only audit

```bash
cd /Users/hermes/workspaces/swamp-ops
swamp model validate kanban-source-route-audit
swamp workflow validate kanban-source-route-audit
swamp workflow run kanban-source-route-audit \
  --input board=<board> \
  --input task_id=<task-id> \
  --input source_profile=<profile> \
  --input chat_id=<chat-id> \
  --input user_id=<user-id> \
  --input source_session_id=<source-session-id>
```

Healthy output has `result=pass`, `readOnly=true`, the exact source profile/chat/user route, the exact persisted source `session_id`, `thread_id=null`, `chat_type=dm`, `delivery_mode=wake`, metadata absent or exactly `{"chat_type":"dm"}`, and no `pending_terminal_events`. The audit rejects broker ownership, topic/thread/reply metadata, passive notification modes, wrong participant or session identity, duplicate subscriptions, malformed identifiers, path traversal and unconsumed terminal events.

## User-facing behavior

- Success: the source agent sends one concise result with verified fields and the external target link. The internal task ID is omitted unless useful for diagnosis.
- Blocker/error: the source agent explains the blocker and includes the task ID for traceability.
- Inspection on demand: use `/kanban list --mine` and `/kanban show <task-id>`.
- Telegram `/topic` remains an optional user-managed conversation feature, never an execution-transport requirement.

## Live verification

For both completion and blocker:

1. Create the task in triage so the worker cannot execute yet.
2. Upsert the root-DM wake-only subscription.
3. Run the audit and require `result=pass`.
4. Explicitly promote the task and allow it to reach the expected terminal event.
5. Confirm no raw `Kanban <task-id> done/blocked` passive message appears.
6. Confirm the source profile gateway logs `kanban notifier: woke agent for <task>` and the exact root-DM session is reused.
7. Confirm the source agent sends one human-facing result through its own bot.
8. Run the audit again; `pending_terminal_events` must be empty and the cursor must have advanced.
9. Confirm broker/default/other profile bots sent nothing.

## Source gateway outage

1. Stop only the source profile Gateway; leave the broker dispatcher running.
2. Produce a terminal event.
3. Verify the subscription cursor is unchanged and no other profile/root route delivers the event.
4. Restart the source Gateway.
5. Verify wake-only delivery retries in the exact root-DM session, the source agent replies once, and the cursor advances only after successful wake.

Secondary profile Gateways are system LaunchDaemons, so stop/start requires the owner. Never stop the broker during this test.

## Rollback

If wake-only routing is unhealthy, stop new source-routed task creation and reconcile the durable board. Do not enable broker/default fallback and do not create per-task Telegram topics. Existing tasks and events remain durable and can be retried after the exact source-profile route is repaired.
