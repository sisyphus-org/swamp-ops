# Source-profile Kanban routing

SIS-60 keeps Kanban task traffic in a profile-owned Telegram task topic. The broker owns dispatch only; it never owns Telegram delivery.

## Contract

For `/kanban create` from a Telegram root DM, Hermes must perform these steps in order:

1. Create the Kanban task and obtain its exact `t_<8 hex>` ID.
2. Create a fresh Telegram private-chat topic named with that task ID.
3. Send the task ID as the first content message in the new topic.
4. Only after the seed succeeds, create one notification subscription with:
   - `platform=telegram`;
   - exact `chat_id` and created `thread_id`;
   - `chat_type=thread`;
   - `notifier_profile=<source profile>`;
   - `delivery_mode=notify+wake`;
   - `delivery_metadata.thread_id=<created thread>`;
   - `delivery_metadata.telegram_dm_topic_created_for_send=true`.

A missing adapter, failed topic creation, or failed seed leaves the task without a notification subscription. It must never create a root-chat subscription. The `telegram_dm_topic_created_for_send` marker makes a later `Thread not found` fail closed; the notifier rewinds its pre-send cursor claim so a later tick can retry rather than posting to root.

Existing Telegram topics keep their exact source routing and do not create a replacement topic during task creation. Non-DM Telegram channels/groups are not treated as root DMs.

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
  --input thread_id=<thread-id>
```

Healthy output has `result=pass`, `readOnly=true`, the exact source route, `notify+wake`, the fail-closed metadata marker, and no `pending_terminal_events`. The audit rejects broker ownership, root/no-thread routing, legacy direct-topic metadata, duplicate subscriptions, malformed identifiers, and path traversal.

## Live verification

For both completion and blocker:

1. Confirm the topic's first message is the task ID.
2. Run the audit before allowing execution.
3. Allow the task to reach the expected terminal event.
4. Run the audit again; `pending_terminal_events` must be empty and the cursor must have advanced.
5. Inspect the source profile gateway log for `kanban notifier: woke agent for <task>`.
6. Confirm no `Thread <id> not found` warning occurs and the synthetic wake uses a session keyed to the exact topic.

## Source gateway outage

1. Stop only the source profile Gateway; leave the broker dispatcher running.
2. Produce a terminal event.
3. Verify the subscription cursor is unchanged and no other profile sends the event.
4. Restart the source Gateway.
5. Verify the pending event is retried into the exact topic and the cursor advances only after successful delivery.

Secondary profile Gateways are system LaunchDaemons, so stop/start requires the owner. Never stop the broker during this test.

## Rollback

If task-topic creation is unhealthy, disable chat-originated task creation or use an already verified existing topic. Do not restore root-chat subscription fallback. Existing Kanban tasks and events remain durable and can be reconciled after the source route is repaired.
