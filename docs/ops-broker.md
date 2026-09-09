# Operations broker

The dedicated headless `broker` Hermes profile owns the shared **operations broker**. User-facing profiles call it over authenticated localhost A2A and receive typed results without receiving broker `GH_TOKEN` or `SWAMP_API_KEY`.

`default` remains the owner-facing manager, not the shared executor. It retains a policy-limited local approval bridge only because destructive Linear previews are bound to the exact originating Telegram session and repository apply approval is owner-only. The bridge has no inbound A2A surface and its policy exposes only four approval operations. It is not a fallback shared broker.

## Trust boundary

```text
user-facing profile ── A2A peer token ──> broker A2A gateway
                                           │
                                           └─ ops-broker toolset only
                                                  │
                                    fixed argv + broker policy allowlist
                                          ┌───────┴───────┐
                                          │               │
                                      GitHub `gh`      Swamp CLI
```

A Hermes profile is not an OS sandbox. This broker limits delegated capability, but all gateways still run as the same macOS user. Untrusted build/package execution needs a separate OS user or VM plus a terminal sandbox.

## Security invariants

- A2A binds to `127.0.0.1` for the first rollout.
- Every caller uses a distinct A2A peer token; authenticated session identity, not request JSON, selects policy.
- The inbound A2A platform gets only the `ops-broker` toolset.
- The Agent Card advertises only `ops-broker`.
- The tool accepts five top-level fields and rejects `caller_profile`, arbitrary URLs, shell text, template paths, unknown fields, and non-UUID request IDs.
- Every command is a fixed argv list executed with `shell=False`.
- GitHub repositories, Swamp models, workflows, and data names are exact allowlists in `plugins/ops_broker/policy.json`.
- Read-only operations require `mode: plan`; repository bootstrap apply and Linear owner-attestation start/approve operations require `mode: apply`.
- The Linear owner-approval operations accept only a parent-only `update_issue`, exact single `remove_issue_relation`, or exact single `replace_issue_relation` intent. Relation intents contain only exact `SIS-N` endpoints and `blocks|blocked_by|related` types—never relation IDs. Every intent binds the exact before-state hash and expiry. Swamp produces an attestation only; it has no Linear credential and performs no Linear mutation.
- Repository bootstrap apply still accepts only repository name plus immutable plan run ID/checksum/artifact version and reloads exact artifact provenance before starting its suspended workflow.
- Approval can resume only an exact run registered in broker audit and serialized by a lock for the policy-bound authenticated owner Telegram session; SWE has no approval operation. Caller booleans, paths, manifest IDs, shell text, and source profiles cannot grant approval.
- Audit records contain caller, request ID, operation, mode, status, approval state and checksum/run identities, but never command output, stderr, environment, or credentials.
- `policy.json` is broker-only: no owner identity and no approval operations. `default` is an ordinary authenticated peer for plan/start operations.
- `policy-owner-bridge.json` is default-only: exact Telegram owner identity plus only `approve_github_cloudflare_repository_apply`, `approve_linear_destructive_owner_approval_attest`, `approve_linear_delete_preview`, and `approve_linear_bulk_preview`.
- Both policies bind the immutable runtime marker and audit path to `/Users/hermes/.hermes/profiles/broker/plugin-data/ops-broker/`; environment overrides cannot redirect a policy-pinned audit path.

## Initial operations

| Operation | Arguments | Automatic |
|---|---|---|
| `github.repository_access` | `repository` | yes |
| `github.list_pull_requests` | `repository` | yes |
| `github.pull_request_checks` | `repository`, `pull_request` | yes |
| `swamp.auth_whoami` | none | yes |
| `swamp.validate_model` | `model` | yes |
| `swamp.validate_workflow` | `workflow` | yes |
| `swamp.run_readonly_workflow` | `workflow` | yes, allowlisted workflows only |
| `swamp.plan_github_cloudflare_repository` | `repository` | `swe` or owner; read-only checksum-bound plan |
| `swamp.start_github_cloudflare_repository_apply` | `repository`, `plan_run_id`, `plan_checksum`, `artifact_version` | `swe` or owner; starts exact workflow suspended at manual approval |
| `swamp.approve_github_cloudflare_repository_apply` | `apply_run_id` | authenticated owner session only; locks and advances the exact run from authoritative Swamp state |
| `swamp.plan_linear_destructive_owner_approval` | exact bounded `intent`, `before_state_hash`, `expires_at` | `swe` or owner; read-only plan only |
| `swamp.start_linear_destructive_owner_approval_attest` | exact plan arguments plus `plan_run_id`, `plan_checksum`, `plan_artifact_version` | `swe` or owner; starts suspended attestation only |
| `swamp.approve_linear_destructive_owner_approval_attest` | `attest_run_id` | authenticated owner session only; emits one-use PM policy reference, never mutates Linear |
| `swamp.approve_linear_delete_preview` | opaque `approval_reference` | authenticated owner session only; reloads the exact protected PM preview, consumes the reference before external work, internally runs at most one fixed Linear plan/start/approve sequence, records the grant, and returns no attestation internals; unknown crash outcomes require a fresh preview |
| `swamp.approve_linear_bulk_preview` | opaque `approval_reference` | authenticated owner session only; reloads the exact ordered protected bulk preview, consumes once before the fixed plan/start/approve sequence, records/reuses one grant, and hides hashes and attestation internals |
| `swamp.get_result` | `model`, `name` | yes, allowlisted artifacts only |

The repository template is versioned locally under `templates/github-cloudflare-app`. It is rendered only with the validated repository name, a plan-generated 96-bit-nonce production Worker target, and a deterministic short preview Worker name. Plan binds template, exact Worker target and rendered manifests by SHA-256. The unique production target prevents overwrite collision without Cloudflare API access. The bootstrap does not query organization membership or read/manage secret values, metadata, visibility or grants. Apply creates new repositories only; adopt-existing and overwrite are intentionally unsupported. Verification proves the exact main tree, `branch-preview` reviewer, production Actions success and runtime health before success is reported; fixed organization secret-name availability is inferred only from that deployment/runtime proof.

## Installation and activation

The cutover is one reviewed rollout, not an in-place copy from a mutable checkout.

### 1. Preconditions

- Use one exact reviewed 40-character commit already merged to `origin/main`.
- Keep `/Users/hermes/workspaces/swamp-ops-runtime` clean, detached, and free of `.swamp-sources.yaml`.
- Create a rollback package for code/config only; never copy `.env` files.
- Prove the existing default A2A endpoint and every current peer before changing ownership.
- The owner issues new, separately scoped `GH_TOKEN` and `SWAMP_API_KEY` credentials for `broker`; do not copy default values.
- Rotate every local A2A peer token during the move. The broker `.env` receives the server mapping; each user-facing profile receives only its own token.

### 2. Install the two policy surfaces

Extract `plugins/ops_broker` from the exact reviewed Git object twice:

- Broker executor: `/Users/hermes/.hermes/profiles/broker/plugins/ops_broker`, keeping reviewed `policy.json`.
- Default owner bridge: `/Users/hermes/.hermes/plugins/ops_broker`, replacing installed `policy.json` with reviewed `policy-owner-bridge.json` renamed to `policy.json`.

Both installations must pass Plugin Doctor and byte comparison against the selected Git objects. Atomically write the reviewed SHA only to:

```text
/Users/hermes/.hermes/profiles/broker/plugin-data/ops-broker/runtime-revision
```

Both policy surfaces use the broker-owned audit path. Default has no independent operations-broker audit or runtime marker after cutover.

### 3. Configure profiles through `hermes config set`

Broker:

```bash
HERMES_HOME=/Users/hermes/.hermes/profiles/broker hermes config set gateway.platforms.a2a.enabled true
HERMES_HOME=/Users/hermes/.hermes/profiles/broker hermes config set gateway.platforms.a2a.extra.port 9900
HERMES_HOME=/Users/hermes/.hermes/profiles/broker hermes config set gateway.platforms.a2a.extra.advertised_toolsets '["ops-broker"]'
HERMES_HOME=/Users/hermes/.hermes/profiles/broker hermes config set platform_toolsets.a2a '["ops-broker"]'
HERMES_HOME=/Users/hermes/.hermes/profiles/broker hermes config set plugins.enabled '["ops-broker"]'
HERMES_HOME=/Users/hermes/.hermes/profiles/broker hermes config check
```

Default:

```bash
hermes config set gateway.platforms.a2a.enabled false
hermes config set platform_toolsets.a2a '[]'
hermes config check
```

Keep the owner bridge enabled only for normal default Telegram sessions. Every user-facing peer keeps the same logical `ops-broker` A2A agent URL (`http://127.0.0.1:9900`) but receives a fresh token. Add `default` as a separate ordinary peer; it receives plan/start capabilities, never approval capability.

### 4. Owner-controlled service switch

Port `127.0.0.1:9900` cannot have two owners. The owner performs the reviewed cutover from an external session:

1. restart default after its inbound A2A adapter is disabled;
2. restart `system/local.hermes.gateway-broker` after broker A2A is enabled and credentials are present;
3. restart each user-facing source Gateway after its peer token is rotated.

Do not restart default from an active agent session and do not use `hermes gateway install` for broker or secondary profiles.

### 5. Verification

- `lsof -nP -iTCP:9900 -sTCP:LISTEN` resolves to the broker Gateway PID.
- The Agent Card advertises exactly `ops-broker` and bearer auth.
- Dispatcher audit still reports only broker and one lock holder.
- Broker config has Telegram disabled and no Linear/Google credential surface.
- Default config has inbound A2A disabled; its installed policy contains only the four owner approval operations.
- `swamp auth whoami` and one bounded GitHub read succeed using broker-local credentials without printing values.
- `default`, `swe`, `ideas`, `books`, and `crypto-analyst` each complete a real read-only A2A call.
- Negative calls reject unknown peers, arbitrary operations/targets, approval from A2A, and credential retrieval.
- One generated destructive Linear preview is approved from the exact default Telegram session through the owner bridge; Project Manager consumes the grant and exact read-back succeeds on a disposable target.
- Literal replay creates no second external mutation.
- Audit output contains all expected caller identities and no credential-shaped data.

## Recovery and rotation

- Plugin failure: disable broker inbound A2A, leave the default owner bridge enabled only for existing exact approvals, and restore the reviewed pre-cutover default endpoint from the code/config rollback package.
- Revoke one peer: remove that identity from broker `A2A_PEER_TOKENS` and `A2A_TRUSTED_PEERS`, restart broker, then rotate only that profile's `OPS_BROKER_A2A_TOKEN` before re-enabling it.
- Suspected broker compromise: disable broker inbound A2A, rotate all peer tokens, then rotate the broker-scoped `GH_TOKEN` and `SWAMP_API_KEY`. Rotate default credentials separately only if the owner bridge or default profile was also exposed.
- Policy change: update both policy files as applicable, rerun unit tests and Plugin Doctor, reinstall the reviewed profile-specific packages, restart broker for executor changes or default for owner-bridge changes, and repeat negative tests.

## Remote machines

Remote access is a separate follow-up. Do not bind `0.0.0.0` as part of this rollout. A remote design must use an authenticated private network or TLS reverse proxy, explicit bind address, per-machine tokens, trusted-peer allowlist, rate limits, and a fresh threat-model review.
