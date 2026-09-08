# SIS-123 — Universal Calendar routing runtime and rollout

## Status

**Production E2E: NOT COMPLETE.** This branch contains local implementation, deterministic test coverage, and read-only Calendar snapshot proof only. It performs no profile installation, gateway restart, Calendar mutation, approval, or cleanup tracer.

The Calendar workflow baseline is the reviewed runtime revision `b46867c677ad1ae2aefb515b7cb6662c101f316c` or an explicitly reviewed successor. Before rollout, compare the deployed immutable Calendar models/workflows to that revision.

## Runtime boundary

1. Every supported user-facing profile loads `linear-source-route`, which now exposes both `linear_source_request` and `calendar_source_request`.
2. `calendar_source_request` validates a closed request shape, derives a global semantic key plus an exact source profile/chat/user/thread/session delivery key, atomically creates or reuses one Kanban task, installs one source-owned wake subscription, audits it, and releases it to `personal-assistant`.
3. The headless Personal Assistant loads `personal-assistant-calendar`; its no-argument `pa_calendar_execute` reads only the exact persisted claimed task, verifies the pinned DB/task/run/claim lease, renews the lease before external steps, verifies an owner-attested clean runtime revision, then invokes fixed Swamp workflows.
4. Source and broker contain no Google client or credential loading. Personal Assistant contains no Linear client or credential loading. Canonical Linear URLs cross the boundary as public data only.

## Protocol v1

- `calendar-command.v1`: command ID, global idempotency key, source profile, bounded operation, and exact request.
- `calendar-kanban-task.v1`: the command plus the fixed Personal Assistant no-argument worker contract.
- `calendar-result.v1`: command-bound verified result. Reads contain only bounded counts/metadata; new explicit-intent writes return only sanitized verified read-back. Legacy plans retain their exact protected preview and opaque `calendar-approval:v1:<sha256>` reference for backward-compatible completion.

Other versions and extra fields fail closed. Secret-shaped values are rejected or redacted before persistence/delivery.

## Supported operations

- Reads: `inventory`, `events`, and `freebusy`; windows are only `today`, `next-7-days`, and `next-30-days`.
- Explicit-intent write: a clear authenticated owner instruction authorizes one exact bounded primary-calendar create, update, or delete; the worker executes plan → snapshot → attestation → fresh snapshot → apply → read-back in one run without a second conversational confirmation. Standalone events omit `linear_url`; an explicitly requested canonical public Linear issue URL remains optional and is preserved in linked descriptions.
- Clarification: the source asks only when title, date/time/duration, or exact update/delete target is genuinely missing or ambiguous after considering the conversation context. The tool itself still requires fully resolved exact fields and never guesses them.
- Legacy approval: `approve` accepts opaque references from create/update/delete previews issued before the single-step contract and remains bound to the same exact source session. New writes never start a preview-first flow.

The underlying Calendar apply lane uses a deterministic event ID and exact read-back. A read-only snapshot workflow binds the target's complete pre-mutation provider state into the opaque source-session approval reference, the worker rechecks it immediately before apply, and update/delete/restore use the observed event ETag as an atomic `If-Match` precondition. Verified results are written atomically to a profile-local `0600` completion journal; one per-command execution lock serializes load→execute→journal so concurrent retries cannot duplicate external work. A lifecycle failure can therefore retry without repeating external work. Replays reuse the Kanban delivery task; create/update/delete replay converges to a verified no-op rather than a duplicate event or mutation.

## Local verification

```bash
/Users/hermes/.hermes/hermes-agent/venv/bin/python -m unittest \
  tests.test_calendar_source_route \
  tests.test_calendar_source_plugin \
  tests.test_personal_assistant_calendar_plugin \
  tests.test_hermes_profile_bootstrap \
  tests.test_google_calendar_read \
  tests.test_google_calendar_write -v

/Users/hermes/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```

These tests use temporary databases and fake workflow boundaries. They must not be described as production E2E proof.

On 2026-09-04 the feature worktree ran `google-calendar-write-snapshot` twice against the deterministic `SIS-123 + sis-123-e2e` target. Both runs succeeded, produced protected `google-calendar-write` result artifact versions 5 and 6 with the exact public shape `beforeStateHash, blockKey, linearIssue, operation, status`, and returned the same before-state hash. This is read-only real-input evidence, not the required source-profile production tracer.

## Owner-gated rollout checklist

1. Review the branch diff and all tests; deploy source plugin/skill to each supported general profile and worker plugin/skill only to `personal-assistant` using the bootstrap workflow. Write the exact deployed 40-character Git revision to `/Users/hermes/.hermes/profiles/personal-assistant/plugin-data/personal-assistant-calendar/runtime-revision` with owner-only permissions.
2. Verify source and broker profiles have no Google OAuth/token files or Google client configuration. Verify Personal Assistant has no `LINEAR_TOKEN`, Linear MCP, or Linear client.
3. Validate Plugin Doctor output and read back both registered tools. Prove the runtime attestation matches `HEAD`, the runtime tree is clean, and `.swamp-sources.yaml` is absent. Restart only owner-approved target gateways/dispatcher; never restart the default Gateway from an agent session.
4. Audit exact source session/thread wake routing before release.
5. From one non-default profile, run bounded read inventory/events/freebusy.
6. Run clear source-profile create, update, and delete instructions; verify each apply/read-back without an intermediate preview and literal-replay each without additional task/subscription/event/mutation/notification counts.
7. Verify one genuinely ambiguous request asks a focused clarification and creates no task. Verify exact cleanup delete absence plus delete replay no-op.
8. Record actual task/run/subscription cursors, immutable workflow artifact versions/checksums, sanitized source replies, and cleanup evidence in the SIS-123 project note. Only then may the production E2E criterion be marked complete.

No production claim is made by this repository state.
