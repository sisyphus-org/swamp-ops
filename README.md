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

- `general`: universal source-routing plugin/skill enabled, Telegram allowlist-only shared fallback, and no Linear MCP or `LINEAR_TOKEN` injection.
- `broker`: Telegram explicitly disabled, dispatcher left disabled for the separate cutover slice, and no Linear MCP or shared secret helper.
- `project-manager`: Linear MCP enabled through the shared command-secret fallback; Telegram explicitly disabled until the owner inserts a unique profile token and enables the adapter.

The plan reports role-specific required variables and dedicated Gateway safe roots. Secrets, Telegram tokens, model authentication, LaunchDaemon installation, and gateway service starts remain manual/approval-gated. Profile `.env` files are never created or copied by the workflow.

Script: `scripts/hermes_profile_bootstrap.py`. Verified 2026-08-27: role-specific plan runs, apply for `broker` and `project-manager`, config/model/STT checks, and refuse-on-existing behavior passed with real inputs.

## `chunked-qwen-stt`

Local long-voice transcription without the single-pass 1024-token cutoff. The deterministic runtime wrapper keeps short recordings on one Qwen request, splits longer audio into overlapping 180-second chunks, and conservatively merges transcripts without publishing partial output on chunk failure.

Swamp provides a read-only live rollout plan and a bounded real-audio smoke mode whose artifacts contain hashes and metrics, never transcript text:

```bash
swamp model validate chunked-qwen-stt
swamp workflow validate chunked-qwen-stt
swamp workflow run chunked-qwen-stt --input mode=plan
swamp workflow run chunked-qwen-stt --input mode=smoke
```

Source: `scripts/chunked_qwen_stt.py`. Operations/audit entry point: `scripts/chunked_stt_ops.py`. Runbook and rollback: [`docs/chunked-qwen-stt.md`](docs/chunked-qwen-stt.md).

## `kanban-dispatcher-audit`

Deterministic read-only production audit for the SIS-58 single-dispatcher topology. It checks the fixed seven-profile roster, requires every `kanban.dispatch_in_gateway` value to be explicit, requires only `broker=true`, and resolves the sole holder of `/Users/hermes/.hermes/kanban/.dispatcher.lock` back to its gateway profile.

```bash
swamp model validate kanban-dispatcher-audit
swamp workflow validate kanban-dispatcher-audit
swamp workflow run kanban-dispatcher-audit
```

The workflow performs no configuration or service writes. Cutover order, live verification, recovery proof and the explicit rollback to the previous `crypto-analyst` owner are documented in [`docs/kanban-dispatcher-cutover.md`](docs/kanban-dispatcher-cutover.md).

## `kanban-source-route-audit`

Read-only SIS-60 audit for one exact source-profile Telegram DM session thread. It requires source-owned `wake`, `chat_type=dm`, exact chat/user/thread identity, the matching persisted Hermes session, no per-task topic metadata and no pending terminal events. Broker ownership, wrong-thread/passive routes, duplicate subscriptions and unconsumed terminal events are reported as drift or failure.

```bash
swamp model validate kanban-source-route-audit
swamp workflow validate kanban-source-route-audit
swamp workflow run kanban-source-route-audit \
  --input board=<board> \
  --input task_id=<task-id> \
  --input source_profile=<profile> \
  --input chat_id=<chat-id> \
  --input user_id=<user-id> \
  --input source_thread_id=<source-thread-id> \
  --input source_session_id=<source-session-id>
```

Wake-only subscription setup, human-facing completion/blocker verification, source-Gateway outage testing and rollback are documented in [`docs/kanban-source-routing.md`](docs/kanban-source-routing.md).

## `linear-command-lane-plan`

Read-only Swamp entry point for the Project Manager `linear-command.v2` lane. It accepts only a bounded command slug under `commands/linear/`, resolves one exact `SIS-N` issue, records current state and emits a typed before/after `linear-result.v2` plan. Apply is deliberately absent from the workflow and uses the same deterministic script only after review.

```bash
LINEAR_TOKEN=... swamp model validate linear-command-lane-plan
LINEAR_TOKEN=... swamp workflow validate linear-command-lane-plan
LINEAR_TOKEN=... swamp workflow run linear-command-lane-plan --input command=<slug>
```

MVP operations are exact read, safe workflow-state change, one bounded comment, bounded issue creation in the `SIS` team under an exact parent, and SIS-77's create-only convergence for exactly one project → milestone → issue. The source/model request contains no hierarchy UUID fields: the trusted Project Manager derives separate deterministic UUIDv4 IDs for project, milestone and issue from the semantic idempotency key. Bulk/fuzzy targeting, `Done`, `Canceled`, `Duplicate`, move/update families, archive/delete and unrestricted structural writes fail closed. User text remains clean and exactly authored: replay protection uses deterministic Linear entity IDs, a hash-only idempotency journal and exact read-back verification rather than visible markers. Contract and evidence: [`docs/linear-command-lane.md`](docs/linear-command-lane.md).

## `linear-source-route` + `project-manager-linear`

SIS-68 defines universal routing for every current and future user-facing profile. `linear-source-route` derives a global mutation key from operation/target/change/policy, then a distinct delivery key from the full exact source identity. It atomically gets or creates one typed triage task by delivery key, writes one source-owned `wake` route, requires the route audit, and only then releases the task. Different profiles/sessions therefore get separate wake tasks while Project Manager shares deterministic mutation/entity identity and converges globally. The plugin has no Linear client or credential. Local tests prove this contract; the live universal tracer remains post-deploy.

SIS-77 uses one current protocol only: source commands are `linear-command.v2`, persisted PM envelopes are `linear-kanban-task.v2`, and PM results/replay validation are `linear-result.v2`. Mutation keys use the global `linear:v2` namespace and delivery keys use `linear-delivery:v2`, retaining the same source-independent mutation payload and exact source-identity delivery payload. Any non-current schema fails closed; no alternate Linear mutation route exists.

The default `hermes-profile-bootstrap` role installs/enables the universal plugin and routing skill, configures no Linear MCP/token for the source profile, and reports mandatory source-Gateway/broker restart plus PM read-back/wake/replay gates. Full local verification, reviewed per-profile rollout, future-profile proof and rollback: [`docs/universal-linear-routing-e2e.md`](docs/universal-linear-routing-e2e.md).

## `linear-project-standard`

Organization-wide Linear hierarchy contract:

```text
Project → Milestones → Issues → Sub-issues
```

Each project instance is declared in `manifests/linear/<slug>.json`. The manifest contains no credentials and uses neutral structural names: milestones are the grouping layer inside a project, issues are the primary tracked work/entities, and sub-issues are the concrete child work.

The operator-only Swamp workflow is a read-only inventory/planning surface:

```bash
# Read-only live reconciliation plan
swamp workflow run linear-project-standard \
  --input manifest=books
```

The workflow reads `LINEAR_TOKEN` only for bounded live discovery. It has no mode input and always invokes `--mode plan`; the CLI rejects `--mode apply` before manifest, token, client, network, or write access. Its `LinearClient` accepts only the five explicit fixed read queries, and the module exports no mutation constants, create/update helpers, or apply function. Reusable validation, discovery, and planner functions remain. Project Manager is the only Linear mutation boundary and the only place where reviewed create-only hierarchy convergence can execute.

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

The bootstrap broker capability is available only to authenticated callers `swe` and `owner`. It validates the repository slug and constructs fixed `shell=False` argv; callers cannot select another workflow, add arbitrary Swamp inputs, provide URLs, shell, template paths, or credentials. SWE may plan/start but cannot approve. The policy-bound owner Telegram session is the only approval caller. Neither caller receives `GH_TOKEN`, `GITHUB_TOKEN`, or `SWAMP_API_KEY`.

All project behavior is a versioned local template under `templates/github-cloudflare-app`: Astro + Workers, one PR CI, `Validate PR`, CodeRabbit-compatible PR review, production deploy on `main`, and approval-gated branch previews. Plan computes the generic template checksum, rendered repository checksum, full file manifest, a unique 96-bit-nonce production Worker target, derived preview Worker name, Environment reviewer and exact future writes. The nonce-bound production Worker and URL are visible in and bound by the approved plan, preventing a new bootstrap from overwriting a pre-existing Worker without querying Cloudflare. The bootstrap never reads or manages organization membership, secret values, secret metadata, visibility or grants; repository-create capability is proved by approved apply, and fixed secret-name availability is proved only by production deployment plus runtime verification.

Apply is a separate `github-cloudflare-repo-bootstrap-apply` workflow. It accepts only repository name, immutable plan run ID, plan checksum and artifact version; `additionalProperties: false` rejects everything else. The workflow suspends at `manual_approval`, then creates only a previously absent repository, exact initial tree, `branch-preview` Environment and fixed repository settings. Trusted workflows reference fixed organization secret names without reading their values. Apply automatically verifies the main tree, production Actions run and `/api/health`. Existing repositories are never adopted or overwritten, and destructive rollback is never automatic.

Agent interaction contract: when a request such as “create a repository/project” has no name, ask only `Как назвать репозиторий?`; after receiving a valid lowercase kebab-case name, run the read-only plan and present target, warnings, checksums and planned writes. SWE may start but cannot approve apply. The exact suspended run is approved/resumed only by the authenticated owner session in the trusted default Manager after explicit approval.

Script: `scripts/github_cloudflare_repo_bootstrap.py`. Workflows: `github-cloudflare-repo-bootstrap` and `github-cloudflare-repo-bootstrap-apply`.

## Repository rules

- Work on `agent/<name>` branches; commit verified changes before merge/review.
- Do not put secrets in this repository.
- Review and pin provenance before installing Swamp extensions.
