# Swamp operations

Operational workflows and the reviewed Hermes operations-broker plugin. Runtime state under `.swamp/` and credentials are never committed.

## `ops-broker`

`plugins/ops_broker` is the narrow A2A capability surface for the trusted `default` Hermes Manager. It derives caller identity from the authenticated A2A session, validates an exact request contract, applies per-peer/target allowlists, executes only fixed `shell=False` argv, and writes secret-free audit records.

The initial surface is read-only: GitHub repository/PR/check reads and allowlisted Swamp identity/validation/run/result operations. `mode: apply`, arbitrary commands, URLs, credentials, and unlisted targets fail closed.

The deterministic Swamp smoke workflow is `ops-broker-readonly-smoke`. Installation, peer-token setup, A2A configuration, verification, recovery, and remote-access constraints are documented in [`docs/ops-broker.md`](docs/ops-broker.md).

## `hermes-profile-bootstrap`

Deterministic bootstrap of a new Hermes profile. Two-phase usage:

1. `swamp workflow run hermes-profile-bootstrap --input profile=<name> --input role=<general|broker|project-manager>` — read-only **plan** (default in the committed workflow).
2. After reviewing the plan, run the deterministic script with the same profile and role plus `--mode apply`; it creates only the new profile directory and refuses overwrite.

Writes are scoped to `/Users/hermes/.hermes/profiles/<name>/` only; existing profiles are never overwritten. Every role receives config version 38, `openai-codex/gpt-5.6-sol-900k`, local Qwen3-ASR (`ru`), terminal cwd under `/Users/hermes/workspaces`, and the keyless free fallback chain (`laguna-s-2.1-free`, `nemotron-3.5-lightning-free` via `opencode-free`).

Role baselines fail closed:

- `general`: the established Linear MCP + shared Linear/Telegram allowlist fallback contract.
- `broker`: Telegram explicitly disabled, dispatcher left disabled for the separate cutover slice, and no Linear MCP or shared secret helper.
- `project-manager`: Linear MCP enabled through the shared command-secret fallback; Telegram explicitly disabled until the owner inserts a unique profile token and enables the adapter.

The plan reports role-specific required variables and dedicated Gateway safe roots. Secrets, Telegram tokens, model authentication, LaunchDaemon installation, and gateway service starts remain manual/approval-gated. Profile `.env` files are never created or copied by the workflow.

Script: `scripts/hermes_profile_bootstrap.py`. Verified 2026-08-27: role-specific plan runs, apply for `broker` and `project-manager`, config/model/STT checks, and refuse-on-existing behavior passed with real inputs.

## `kanban-dispatcher-audit`

Deterministic read-only production audit for the SIS-58 single-dispatcher topology. It checks the fixed seven-profile roster, requires every `kanban.dispatch_in_gateway` value to be explicit, requires only `broker=true`, and resolves the sole holder of `/Users/hermes/.hermes/kanban/.dispatcher.lock` back to its gateway profile.

```bash
swamp model validate kanban-dispatcher-audit
swamp workflow validate kanban-dispatcher-audit
swamp workflow run kanban-dispatcher-audit
```

The workflow performs no configuration or service writes. Cutover order, live verification, recovery proof and the explicit rollback to the previous `crypto-analyst` owner are documented in [`docs/kanban-dispatcher-cutover.md`](docs/kanban-dispatcher-cutover.md).

## `kanban-source-route-audit`

Read-only SIS-60 audit for one exact source-profile Telegram root-DM session. It requires source-owned `wake`, `chat_type=dm`, exact chat/user identity, `thread_id=null`, no topic metadata and no pending terminal events. Broker ownership, topic/passive routes, duplicate subscriptions and unconsumed terminal events are reported as drift or failure.

```bash
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

Wake-only subscription setup, human-facing completion/blocker verification, source-Gateway outage testing and rollback are documented in [`docs/kanban-source-routing.md`](docs/kanban-source-routing.md).

## `linear-command-lane-plan`

Read-only Swamp entry point for the Project Manager `linear-command.v1` lane. It accepts only a bounded command slug under `commands/linear/`, resolves one exact `SIS-N` issue, records current state and emits a typed before/after `linear-result.v1` plan. Apply is deliberately absent from the workflow and uses the same deterministic script only after review.

```bash
LINEAR_TOKEN=... swamp model validate linear-command-lane-plan
LINEAR_TOKEN=... swamp workflow validate linear-command-lane-plan
LINEAR_TOKEN=... swamp workflow run linear-command-lane-plan --input command=<slug>
```

MVP operations are exact read, safe workflow-state change, and one bounded comment. Bulk/fuzzy targeting, `Done`, `Canceled`, `Duplicate`, archive/delete and structural writes fail closed. Apply uses marker-based replay protection, a hash-only idempotency journal and exact read-back verification. Contract, examples and SIS-59 live evidence: [`docs/linear-command-lane.md`](docs/linear-command-lane.md).

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

## `github-cloudflare-repo-bootstrap`

Read-only Phase 1 planner for the standard Cloudflare repository bootstrap. The owner-facing interaction has exactly one required question: the repository name.

```text
Owner: create a repository
Agent: what should the repository be called?
Owner: example-site
```

The SWE agent then sends one typed A2A request to `ops-broker`:

```json
{
  "request_id": "<fresh-uuid>",
  "integration": "swamp",
  "operation": "plan_github_cloudflare_repository",
  "arguments": {"repository": "example-site"},
  "mode": "plan"
}
```

The broker operation is available only to authenticated caller `swe`. It validates the repository slug and constructs one fixed `shell=False` argv for `github-cloudflare-repo-bootstrap`; callers cannot select another workflow, add arbitrary Swamp inputs, or request apply mode. SWE never receives `SWAMP_API_KEY` and does not invoke the Swamp CLI directly.

All other values are versioned defaults: organization `sisyphus-org`, public visibility, approved GitHub template plus an exact reviewed commit SHA, derived production/preview Worker names, `branch-preview` Environment, reviewer `alexxpetrov`, required Cloudflare org secret names, CI, production deploy, approval-gated protected preview, and no Cloudflare Git Builds integration. Until the template exists and `approvedTemplateRevision` is set to a reviewed 40-character SHA, every plan fails closed.

Phase 1 performs GitHub GET requests only. It validates repository-name availability, the template default branch against the approved SHA, scaffold files at that exact SHA, organization membership visibility, required secret names, Worker/DNS names and the preview Access contract. The JSON result lists exact future writes, blockers and approval gates. The future write plan creates an empty repository and materializes the approved template tree instead of calling the mutable GitHub template-generation endpoint. There is no `apply` input or write path.

An apply phase may be added only after two manually invoked read-only runs are reviewed and narrow write approvals are defined. No webhooks, schedules, secret values or modifications of existing repositories are allowed.

Script: `scripts/github_cloudflare_repo_bootstrap.py`.

## Repository rules

- Work on `agent/<name>` branches; commit verified changes before merge/review.
- Do not put secrets in this repository.
- Review and pin provenance before installing Swamp extensions.
