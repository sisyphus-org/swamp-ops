---
name: personal-assistant-calendar-worker
description: Execute one persisted Calendar command in Personal Assistant.
version: 1.0.0
author: sisyphus-org
platforms: [linux, macos]
metadata:
  hermes:
    tags: [calendar, worker, kanban]
---

# Personal Assistant Calendar Worker

Call `pa_calendar_execute` exactly once with no arguments, then stop. The tool reads the authoritative `calendar-kanban-task.v1` and `calendar-command.v1` from the exact claimed Kanban task. Never reconstruct, edit, or pass a Calendar command from model text.

The tool validates the Personal Assistant profile, pinned board, task/run/claim binding, live lease, and owner-attested clean runtime revision before workflow access. It owns bounded reads, write preview creation, PII-free target-state snapshots, exact-session approval lookup, approval workflow suspension/approval/resume, a fresh pre-apply snapshot comparison, provider-atomic ETag preconditions, apply, verified read-back, replay handling, redaction, and terminal `calendar-result.v1` persistence. A per-command execution lock serializes load→execute→journal, and verified results are journaled atomically before Kanban completion so retry never repeats already-completed external work.

Do not call Linear, request Linear credentials, call another Calendar tool, inspect OAuth files, or complete/block the task yourself. The persisted command and deterministic tool are authoritative.
