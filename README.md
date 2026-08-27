# Swamp operations

init

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

The workflow reads `LINEAR_TOKEN` from the calling environment and fails closed before its first write if any manifest reference is missing, ambiguous, or inconsistent. For create-only entries (no `identifier`) it creates missing hierarchy objects as before. For an existing top-level issue, the manifest must supply both its explicit Linear identifier (for example `SIS-6`) and its exact title; only then may apply assign or reassign that issue's `projectMilestoneId`. The update mutation contains no status, project, parent, title, description, priority, assignee, archive, or delete fields. Existing state values may remain in manifests for documentation, but milestone updates never write status. Apply reads the project back and requires a second reconciliation plan to converge.

Committed manifests cover `Hermes Foundation`, `Hermes Experience`, `Home Infrastructure`, `Knowledge System`, `Crypto X Daily Intelligence Digest`, and `Книги`. `SIS-1…4`, `SIS-23`, `SIS-24`, `SIS-26`, and `SIS-27` are currently unprojected and intentionally absent: this workflow never changes project membership. The `books` manifest retains the existing Greek hierarchy and the identified Chaucer issue under the existing `Английская литература` milestone.

No existing issue/project is moved between projects, re-parented, status-changed, archived, or deleted. A sub-issue can never receive a direct milestone, including through an identified-issue entry. Manifest titles remain unique across each declared hierarchy, identifiers are unique and canonical, and read discovery refuses pagination beyond the supported bound.

Recommended Linear board settings: group by **Project milestone** and turn **Sub-issues** off. The board then shows milestone groups with top-level issues as cards; sub-issues remain inside their parent issue. Milestones are assigned only to top-level issues; sub-issues belong to that grouping through their parent and do not receive a direct milestone assignment.

## Repository rules

- Work on `agent/<name>` branches; commit verified changes before merge/review.
- Do not put secrets in this repository.
- Review and pin provenance before installing Swamp extensions.
