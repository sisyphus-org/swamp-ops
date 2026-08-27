# Swamp operations

Operational workflows and the reviewed Hermes operations-broker plugin. Runtime state under `.swamp/` and credentials are never committed.

## `ops-broker`

`plugins/ops_broker` is the narrow A2A capability surface for the trusted `default` Hermes Manager. It derives caller identity from the authenticated A2A session, validates an exact request contract, applies per-peer/target allowlists, executes only fixed `shell=False` argv, and writes secret-free audit records.

The initial surface is read-only: GitHub repository/PR/check reads and allowlisted Swamp identity/validation/run/result operations. `mode: apply`, arbitrary commands, URLs, credentials, and unlisted targets fail closed.

The deterministic Swamp smoke workflow is `ops-broker-readonly-smoke`. Installation, peer-token setup, A2A configuration, verification, recovery, and remote-access constraints are documented in [`docs/ops-broker.md`](docs/ops-broker.md).

## `hermes-profile-bootstrap`

Deterministic bootstrap of a new Hermes profile. Two-phase usage:

1. `swamp workflow run hermes-profile-bootstrap --input profile=<name>` — read-only **plan** (default in the committed workflow).
2. After reviewing the plan, the operator switches the step to `--mode apply` (or runs the script directly) to create the profile.

Writes are scoped to `/Users/hermes/.hermes/profiles/<name>/` only; existing profiles are never overwritten. The default baseline is `openai-codex/gpt-5.6-sol-900k`, local Qwen3-ASR (`ru`), terminal cwd under `/Users/hermes/workspaces`, Linear MCP, and the keyless free fallback chain (`laguna-s-2.1-free`, `nemotron-3.5-lightning-free` via `opencode-free`).

The plan result always lists the variables required before activation. `LINEAR_TOKEN` and `TELEGRAM_ALLOWED_USERS` default to `/Users/hermes/.hermes/.env` through Hermes' command secret source and are not duplicated into profile `.env` files. A profile may explicitly define either key to override the shared default. Each profile keeps its unique `TELEGRAM_BOT_TOKEN` plus optional role-scoped `GH_TOKEN`, `SWAMP_API_KEY`, or `XAI_API_KEY`. Secrets, Telegram tokens, model authentication, LaunchDaemons, and gateway starts remain manual/approval-gated.

Script: `scripts/hermes_profile_bootstrap.py`. Verified 2026-08-26: plan run, apply run, refuse-on-existing — all passed with real inputs.

## `linear-project-standard`

Organization-wide Linear hierarchy contract:

```text
Project → Milestones → Issues → Sub-issues
```

Each project instance is declared in `manifests/linear/<slug>.json`. The manifest contains no credentials and uses neutral structural names: milestones are the grouping layer inside a project, issues are the primary tracked work/entities, and sub-issues are the concrete child work.

Run through Swamp in two phases:

```bash
# Read-only live reconciliation plan
swamp workflow run linear-project-standard \
  --input manifest=books \
  --input mode=plan

# Apply only after reviewing the plan
swamp workflow run linear-project-standard \
  --input manifest=books \
  --input mode=apply
```

The workflow reads `LINEAR_TOKEN` from the calling environment, creates only missing objects, fails closed on ambiguous titles or hierarchy conflicts, and reads the project back after apply. Manifest titles must be unique across the declared hierarchy so reconciliation remains deterministic. It never moves, edits, archives, or deletes existing Linear objects. The initial `books` manifest demonstrates the standard with the project «Книги» and its milestone «Древнегреческая литература».

Recommended Linear board settings: group by **Project milestone** and turn **Sub-issues** off. The board then shows milestone groups with top-level issues as cards; sub-issues remain inside their parent issue. Milestones are assigned only to top-level issues; sub-issues belong to that grouping through their parent and do not receive a direct milestone assignment.

## Repository rules

- Work on `agent/<name>` branches; commit verified changes before merge/review.
- Do not put secrets in this repository.
- Review and pin provenance before installing Swamp extensions.
