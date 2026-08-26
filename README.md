# Swamp operations

init

## `hermes-profile-bootstrap`

Deterministic bootstrap of a new Hermes profile. Two-phase usage:

1. `swamp workflow run hermes-profile-bootstrap --input profile=<name>` — read-only **plan** (default in the committed workflow).
2. After reviewing the plan, the operator switches the step to `--mode apply` (or runs the script directly) to create the profile.

Writes are scoped to `/Users/hermes/.hermes/profiles/<name>/` only; existing profiles are never overwritten. Baseline includes the keyless free fallback chain (`laguna-s-2.1-free`, `nemotron-3.5-lightning-free` via `opencode-free`) and ru STT. Secrets, Telegram tokens, and LaunchDaemons stay manual (owner approval required).

Script: `scripts/hermes_profile_bootstrap.py`. Verified 2026-08-26: plan run, apply run, refuse-on-existing — all passed with real inputs.

## Repository rules

- Work on `agent/<name>` branches; commit verified changes before merge/review.
- Do not put secrets in this repository.
- Review and pin provenance before installing Swamp extensions.
