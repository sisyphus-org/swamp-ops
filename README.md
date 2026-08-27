# Swamp operations

init

## `hermes-profile-bootstrap`

Deterministic bootstrap of a new Hermes profile. Two-phase usage:

1. `swamp workflow run hermes-profile-bootstrap --input profile=<name>` — read-only **plan** (default in the committed workflow).
2. After reviewing the plan, the operator switches the step to `--mode apply` (or runs the script directly) to create the profile.

Writes are scoped to `/Users/hermes/.hermes/profiles/<name>/` only; existing profiles are never overwritten. The default baseline is `openai-codex/gpt-5.6-sol-900k`, local Qwen3-ASR (`ru`), terminal cwd under `/Users/hermes/workspaces`, Linear MCP, and the keyless free fallback chain (`laguna-s-2.1-free`, `nemotron-3.5-lightning-free` via `opencode-free`).

The plan result always lists the variables required before activation. `LINEAR_TOKEN` and the default `TELEGRAM_ALLOWED_USERS` are shared from `/Users/hermes/.hermes/.env` through Hermes' command secret source and are never duplicated into a profile `.env`. A profile may explicitly define `TELEGRAM_ALLOWED_USERS` to override the shared allowlist. Each profile keeps its unique `TELEGRAM_BOT_TOKEN` plus optional role-scoped `GH_TOKEN`, `SWAMP_API_KEY`, or `XAI_API_KEY`. Secrets, Telegram tokens, model authentication, LaunchDaemons, and gateway starts remain manual/approval-gated.

Script: `scripts/hermes_profile_bootstrap.py`. Verified 2026-08-26: plan run, apply run, refuse-on-existing — all passed with real inputs.

## Repository rules

- Work on `agent/<name>` branches; commit verified changes before merge/review.
- Do not put secrets in this repository.
- Review and pin provenance before installing Swamp extensions.
