# Prompt-driven repository bootstrap

## Conversation contract

1. Owner: `Создай репозиторий/проект`.
2. If no name is present, the agent asks exactly: `Как назвать репозиторий?`
3. The owner supplies one lowercase kebab-case name, for example `example-site`.
4. The agent calls the broker plan operation and presents:
   - `sisyphus-org/<name>`;
   - production and preview Worker names;
   - template/rendered SHA-256 values;
   - warnings and blockers;
   - exact planned writes.
5. The agent asks approval for that exact plan. It must not infer approval from the original create request.
6. SWE may start the suspended apply but cannot approve it. Approval must come from the authenticated owner session in the trusted default Manager profile.
7. The owner's typed approval operation serializes the exact run with a broker lock, reads authoritative Swamp run/approval-step status, then approves or resumes only the missing stage. A completed run is attested without replay; failed, cancelled, unknown and competing states fail closed.
8. The agent reports success only after the workflow verifies the repository tree, production GitHub Actions deployment and `/api/health` runtime response.

## Typed broker requests

Plan:

```json
{
  "request_id": "<uuid>",
  "integration": "swamp",
  "operation": "plan_github_cloudflare_repository",
  "arguments": {"repository": "example-site"},
  "mode": "plan"
}
```

Start the checksum-bound apply after owner approval:

```json
{
  "request_id": "<new-uuid>",
  "integration": "swamp",
  "operation": "start_github_cloudflare_repository_apply",
  "arguments": {
    "repository": "example-site",
    "plan_run_id": "<plan-workflow-run-uuid>",
    "plan_checksum": "<64-lowercase-hex>",
    "artifact_version": 1
  },
  "mode": "apply"
}
```

Approve only the returned suspended run from the authenticated owner/default Manager session:

```json
{
  "request_id": "<new-uuid>",
  "integration": "swamp",
  "operation": "approve_github_cloudflare_repository_apply",
  "arguments": {"apply_run_id": "<suspended-apply-run-uuid>"},
  "mode": "apply"
}
```

## Fixed result

A successful repository includes:

- Astro + Cloudflare Workers application and tests;
- one `CI / verify` per PR, not a duplicate push run;
- one visible `Validate PR` check;
- CodeRabbit-compatible PR review flow;
- production deployment on `main`;
- credential-free PR build and approval-gated trusted preview publication;
- `branch-preview` Environment with required reviewer;
- deterministic Worker names and runtime health endpoint.

## Safety boundaries

- New repository only; existing repositories are rejected.
- No arbitrary shell, URLs, organization, reviewer, secret names, template path or workflow names.
- The production Worker is `<repository-prefix>-<96-bit nonce>` and is capped at Cloudflare's 54-character limit for scripts with previews enabled. Its exact name and URL are generated in the read-only plan, rendered into `wrangler.jsonc`, and bound by the plan/rendered checksums. This avoids collision with a pre-existing Worker without Cloudflare API or secret access.
- The bootstrap never reads or manages GitHub/Cloudflare secret values, metadata, visibility, or repository grants. Generated workflows reference fixed organization secret names; availability is proved only by deployment and runtime verification.
- SWE receives only an A2A peer token and typed results, never GitHub/Swamp/Cloudflare credentials.
- Plan performs reads only.
- Apply reloads the immutable artifact and rechecks plan, template and rendered checksums before its first write.
- No automatic destructive rollback. Partial-state recovery is a separately reviewed owner operation.
