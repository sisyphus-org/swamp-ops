#!/usr/bin/env python3
"""Read-only planner for the standard GitHub + Cloudflare repository bootstrap.

Phase 1 deliberately has no apply mode and makes only GitHub GET requests. It
accepts exactly one owner-provided value: the repository name. All other values
are versioned organization defaults.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

API_URL = "https://api.github.com"
REPOSITORY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,54}$")

STANDARD: dict[str, Any] = {
    "organization": "sisyphus-org",
    "visibility": "public",
    "templateRepository": "sisyphus-org/cloudflare-worker-template",
    "approvedTemplateRevision": None,
    "defaultBranch": "main",
    "environment": "branch-preview",
    "requiredReviewer": "alexxpetrov",
    "workersSubdomain": "sisyphus-org",
    "requiredOrgSecrets": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"],
    "requiredTemplateFiles": [
        ".github/workflows/ci.yml",
        ".github/workflows/deploy.yml",
        ".github/workflows/pr-preview.yml",
        ".gitignore",
        "README.md",
        "package.json",
        "package-lock.json",
        "astro.config.mjs",
        "tsconfig.json",
        "src/pages/index.astro",
        "src/worker.ts",
        "src/preview-placeholder.ts",
        "wrangler.jsonc",
        "wrangler.preview-bootstrap.jsonc",
        "wrangler.version-preview.jsonc",
        "docs/operations.md",
    ],
}


class ContractError(RuntimeError):
    """Input or live state violates the read-only planning contract."""


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def validate_repository(value: str) -> str:
    if not isinstance(value, str) or not REPOSITORY_PATTERN.fullmatch(value):
        raise ContractError(
            "repository must match ^[a-z][a-z0-9-]{1,54}$ so the derived preview Worker fits DNS limits"
        )
    preview_worker = f"{value}-preview"
    if len(value) > 63 or len(preview_worker) > 63:
        raise ContractError("derived Worker name exceeds the 63-character workers.dev limit")
    return value


@dataclass(frozen=True)
class ApiResult:
    status: int
    payload: Any


class GitHubClient:
    def __init__(self, token: str, endpoint: str = API_URL) -> None:
        token = token.strip()
        if not token:
            raise ContractError("GH_TOKEN/GITHUB_TOKEN is empty")
        self.endpoint = endpoint.rstrip("/")
        self.token = token

    def get(self, path: str) -> ApiResult:
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "swamp-github-cloudflare-repo-bootstrap",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return ApiResult(response.status, json.load(response))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except Exception:
                payload = {"message": "non-JSON GitHub API error"}
            return ApiResult(exc.code, payload)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ContractError(f"GitHub API request failed: {exc}") from exc


class GitHubReader(Protocol):
    def get(self, path: str) -> ApiResult: ...


def check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def content_path(repository: str, path: str, *, ref: str | None = None) -> str:
    encoded = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
    result = f"/repos/{repository}/contents/{encoded}"
    if ref is not None:
        result += "?" + urllib.parse.urlencode({"ref": ref})
    return result


def build_plan(repository: str, client: GitHubReader) -> dict[str, Any]:
    repository = validate_repository(repository)
    organization = STANDARD["organization"]
    target = f"{organization}/{repository}"
    template = STANDARD["templateRepository"]
    production_worker = repository
    preview_worker = f"{repository}-preview"

    checks: list[dict[str, str]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    actor_result = client.get("/user")
    actor = None
    if actor_result.status == 200 and isinstance(actor_result.payload, dict):
        candidate = actor_result.payload.get("login")
        if candidate == STANDARD["requiredReviewer"]:
            actor = candidate
            checks.append(check("github_auth", "pass", f"authenticated as {actor}"))
        elif isinstance(candidate, str) and candidate:
            blockers.append(
                f"GitHub actor must be {STANDARD['requiredReviewer']}, got {candidate}"
            )
            checks.append(check("github_auth", "block", f"unexpected actor: {candidate}"))
        else:
            blockers.append("GitHub authentication response did not include a login")
            checks.append(check("github_auth", "block", "authenticated actor is missing"))
    else:
        blockers.append("GitHub authentication could not be verified")
        checks.append(check("github_auth", "block", f"GitHub API HTTP {actor_result.status}"))

    target_result = client.get(f"/repos/{target}")
    if target_result.status == 404:
        checks.append(check("target_repository", "pass", f"{target} is available"))
    elif target_result.status == 200:
        blockers.append(f"target repository already exists: {target}")
        checks.append(check("target_repository", "block", f"{target} already exists"))
    else:
        blockers.append(f"target repository availability is unknown (HTTP {target_result.status})")
        checks.append(check("target_repository", "block", f"GitHub API HTTP {target_result.status}"))

    template_result = client.get(f"/repos/{template}")
    template_ready = False
    template_revision: str | None = None
    observed_template_revision: str | None = None
    approved_template_revision = STANDARD.get("approvedTemplateRevision")
    approved_revision_is_valid = isinstance(
        approved_template_revision, str
    ) and re.fullmatch(r"[0-9a-f]{40}", approved_template_revision)
    if not approved_revision_is_valid:
        blockers.append("approved template revision is not configured")
        checks.append(
            check(
                "approved_template_revision",
                "block",
                "set approvedTemplateRevision to a reviewed 40-character commit SHA",
            )
        )
    else:
        checks.append(
            check(
                "approved_template_revision",
                "pass",
                f"approved commit={approved_template_revision}",
            )
        )
    if template_result.status == 200 and isinstance(template_result.payload, dict):
        if template_result.payload.get("is_template") is not True:
            blockers.append(f"approved template is not marked as a GitHub template: {template}")
            checks.append(check("approved_template", "block", "is_template is false"))
        else:
            default_branch = template_result.payload.get("default_branch")
            if not isinstance(default_branch, str) or not default_branch:
                blockers.append(f"approved template default branch is missing: {template}")
                checks.append(check("approved_template", "block", "default_branch is missing"))
            else:
                encoded_branch = urllib.parse.quote(default_branch, safe="")
                revision_result = client.get(f"/repos/{template}/commits/{encoded_branch}")
                candidate_sha = (
                    revision_result.payload.get("sha")
                    if revision_result.status == 200
                    and isinstance(revision_result.payload, dict)
                    else None
                )
                if isinstance(candidate_sha, str) and re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
                    observed_template_revision = candidate_sha
                    if not approved_revision_is_valid:
                        checks.append(
                            check(
                                "approved_template",
                                "blocked",
                                f"observed {template}@{observed_template_revision}; approval pin is missing",
                            )
                        )
                    elif observed_template_revision != approved_template_revision:
                        blockers.append(
                            "approved template revision drifted: "
                            f"expected {approved_template_revision}, got {observed_template_revision}"
                        )
                        checks.append(
                            check(
                                "approved_template",
                                "block",
                                "default branch does not match the approved commit",
                            )
                        )
                    else:
                        template_revision = approved_template_revision
                        template_ready = True
                        checks.append(
                            check(
                                "approved_template",
                                "pass",
                                f"{template}@{template_revision} is the approved GitHub template",
                            )
                        )
                else:
                    blockers.append(f"approved template revision could not be verified: {template}")
                    checks.append(
                        check(
                            "approved_template",
                            "block",
                            f"revision lookup HTTP {revision_result.status}",
                        )
                    )
    elif template_result.status == 404:
        blockers.append(f"approved template repository is missing: {template}")
        checks.append(check("approved_template", "block", f"{template} not found"))
    else:
        blockers.append(f"approved template could not be verified (HTTP {template_result.status})")
        checks.append(check("approved_template", "block", f"GitHub API HTTP {template_result.status}"))

    if template_ready:
        missing_files: list[str] = []
        for required_path in STANDARD["requiredTemplateFiles"]:
            result = client.get(
                content_path(template, required_path, ref=template_revision)
            )
            if (
                result.status != 200
                or not isinstance(result.payload, dict)
                or result.payload.get("type") != "file"
                or result.payload.get("path") != required_path
            ):
                missing_files.append(required_path)
        if missing_files:
            blockers.append("approved template is missing required scaffold files")
            checks.append(
                check("template_scaffold", "block", "missing: " + ", ".join(sorted(missing_files)))
            )
        else:
            checks.append(check("template_scaffold", "pass", "all required scaffold files exist"))
    else:
        checks.append(check("template_scaffold", "blocked", "template prerequisite is not ready"))

    membership_result = client.get(f"/user/memberships/orgs/{organization}")
    if membership_result.status == 200 and isinstance(membership_result.payload, dict):
        state = membership_result.payload.get("state")
        role = membership_result.payload.get("role")
        if state == "active" and role == "admin":
            checks.append(check("organization_membership", "pass", "active admin membership"))
        elif state == "active":
            blockers.append(
                f"active GitHub organization role does not prove repository-create capability: {role}"
            )
            checks.append(check("organization_membership", "block", f"active role={role}"))
        else:
            blockers.append(f"GitHub organization membership is not active: {state}")
            checks.append(check("organization_membership", "block", f"state={state}"))
    else:
        blockers.append(
            "GitHub token cannot verify organization membership/repository-create capability"
        )
        checks.append(
            check("organization_membership", "block", f"GitHub API HTTP {membership_result.status}")
        )

    missing_secrets: list[str] = []
    unverifiable_secrets: list[str] = []
    selected_secrets: list[str] = []
    globally_available_secrets: list[str] = []
    private_only_secrets: list[str] = []
    for secret_name in STANDARD["requiredOrgSecrets"]:
        encoded_name = urllib.parse.quote(secret_name, safe="")
        secret_result = client.get(f"/orgs/{organization}/actions/secrets/{encoded_name}")
        if secret_result.status == 404:
            missing_secrets.append(secret_name)
        elif secret_result.status != 200 or not isinstance(secret_result.payload, dict):
            unverifiable_secrets.append(secret_name)
        else:
            visibility = secret_result.payload.get("visibility")
            if visibility == "selected":
                selected_secrets.append(secret_name)
            elif visibility == "all":
                globally_available_secrets.append(secret_name)
            elif visibility == "private":
                private_only_secrets.append(secret_name)
            else:
                unverifiable_secrets.append(secret_name)
    if missing_secrets:
        blockers.append("required organization Actions secret names are missing")
        checks.append(
            check("organization_secrets", "block", "missing: " + ", ".join(missing_secrets))
        )
    elif private_only_secrets:
        blockers.append("required organization Actions secrets are private-repository-only")
        checks.append(
            check(
                "organization_secrets",
                "block",
                "not available to the standard public repository: "
                + ", ".join(private_only_secrets),
            )
        )
    elif unverifiable_secrets:
        blockers.append("GitHub token cannot verify required organization Actions secret names")
        checks.append(
            check(
                "organization_secrets",
                "block",
                "unverifiable metadata: " + ", ".join(unverifiable_secrets),
            )
        )
    else:
        checks.append(
            check(
                "organization_secrets",
                "pass",
                "selected access required: "
                + (", ".join(selected_secrets) or "none")
                + "; globally available: "
                + (", ".join(globally_available_secrets) or "none")
                + "; values were not read",
            )
        )

    checks.append(
        check(
            "worker_names",
            "pass",
            f"production={production_worker}; preview={preview_worker}",
        )
    )
    checks.append(
        check(
            "preview_access_contract",
            "pass",
            "account-wide Previews only Access policy; no per-repository policy write planned",
        )
    )

    planned_actions = [
        {
            "order": 1,
            "action": "create_empty_repository",
            "target": target,
            "defaultBranch": STANDARD["defaultBranch"],
            "visibility": STANDARD["visibility"],
        },
        {
            "order": 2,
            "action": "materialize_approved_template_revision",
            "template": template,
            "templateRevision": template_revision,
            "destinationRepository": target,
        },
        {
            "order": 3,
            "action": "render_standard_placeholders",
            "productionWorker": production_worker,
            "previewWorker": preview_worker,
            "workersSubdomain": STANDARD["workersSubdomain"],
        },
        {
            "order": 4,
            "action": "create_initial_main_commit",
            "defaultBranch": STANDARD["defaultBranch"],
            "sourceTemplateRevision": template_revision,
        },
        {
            "order": 5,
            "action": "create_github_environment",
            "environment": STANDARD["environment"],
            "requiredReviewer": STANDARD["requiredReviewer"],
            "preventSelfReview": False,
        },
        {
            "order": 6,
            "action": "ensure_repository_access_to_org_secrets",
            "repository": target,
            "selectedSecretNames": selected_secrets,
            "alreadyGlobalSecretNames": globally_available_secrets,
        },
        {
            "order": 7,
            "action": "configure_repository_settings",
            "settings": {
                "deleteBranchOnMerge": True,
                "hasWiki": False,
                "allowSquashMerge": True,
            },
        },
        {
            "order": 8,
            "action": "verify_readback",
            "checks": [
                "repository/main",
                "required scaffold files",
                "branch-preview environment reviewer",
                "organization secret access by name",
                "no Cloudflare Git Builds integration",
            ],
        },
    ]

    required_approvals = [
        "Create a new GitHub repository under sisyphus-org",
        "Write the rendered template and initial main commit",
        "Create/configure the branch-preview GitHub Environment",
        "Ensure the new repository has access to required organization Actions secrets",
        "Change repository settings",
    ]

    if actor is None:
        warnings.append("future apply actor is unknown")

    return {
        "schemaVersion": 1,
        "mode": "plan",
        "readOnly": True,
        "ready": not blockers,
        "target": {
            "repository": target,
            "visibility": STANDARD["visibility"],
            "productionWorker": production_worker,
            "previewWorker": preview_worker,
            "productionUrl": f"https://{production_worker}.{STANDARD['workersSubdomain']}.workers.dev",
            "previewUrlPattern": (
                f"https://pr-<number>-<slug>-<hash>-{preview_worker}."
                f"{STANDARD['workersSubdomain']}.workers.dev"
            ),
        },
        "source": {
            "template": template,
            "templateRevision": template_revision,
            "observedTemplateRevision": observed_template_revision,
        },
        "standard": STANDARD,
        "checks": checks,
        "plannedActions": planned_actions,
        "requiredApprovals": required_approvals,
        "blockers": blockers,
        "warnings": warnings,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv or sys.argv[1:])
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        client = GitHubClient(token)
        emit(build_plan(args.repository, client))
        return 0
    except (ContractError, json.JSONDecodeError) as exc:
        emit({"schemaVersion": 1, "mode": "plan", "readOnly": True, "ready": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
