#!/usr/bin/env python3
"""Plan, apply and verify a standard GitHub + Cloudflare application repository.

The only owner-provided project value is the repository name. Template files,
organization, reviewer, environment, secret names and Workers subdomain are
versioned here. Apply always reloads and verifies an immutable Swamp plan
artifact before its first external write.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

API_URL = "https://api.github.com"
REPOSITORY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,54}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "templates" / "github-cloudflare-app"
IMPLEMENTATION_PATHS = (
    "scripts/github_cloudflare_repo_bootstrap.py",
    "workflows/workflow-github-cloudflare-repo-bootstrap-apply.yaml",
    "models/command/shell/github-cloudflare-repo-bootstrap.yaml",
    "plugins/ops_broker/__init__.py",
    "plugins/ops_broker/broker.py",
    "plugins/ops_broker/policy.json",
)

STANDARD: dict[str, Any] = {
    "organization": "sisyphus-org",
    "visibility": "public",
    "defaultBranch": "main",
    "environment": "branch-preview",
    "requiredReviewer": "alexxpetrov",
    "workersSubdomain": "sisyphus-org",
    "requiredOrgSecrets": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"],
    "templateDirectory": "templates/github-cloudflare-app",
}


class ContractError(RuntimeError):
    """Input, artifact, template or live state violated the fixed contract."""


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def validate_repository(value: str) -> str:
    if not isinstance(value, str) or REPOSITORY_PATTERN.fullmatch(value) is None:
        raise ContractError("repository must match ^[a-z][a-z0-9-]{1,54}$")
    return value


def derive_production_worker(repository: str, nonce: str) -> str:
    repository = validate_repository(repository)
    if not re.fullmatch(r"[0-9a-f]{24}", nonce):
        raise ContractError("production Worker nonce must be 96-bit lowercase hex")
    prefix_length = 54 - 1 - len(nonce)
    prefix = repository[:prefix_length].rstrip("-")
    return f"{prefix}-{nonce}"


def derive_preview_worker(repository: str) -> str:
    repository = validate_repository(repository)
    prefix = repository[:12].rstrip("-") or "app"
    suffix = hashlib.sha256(repository.encode()).hexdigest()[:8]
    value = f"prv-{prefix}-{suffix}"
    if len(value) > 27 or re.fullmatch(r"[a-z][a-z0-9-]*", value) is None:
        raise ContractError("derived preview Worker name is invalid")
    return value


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def load_template_files(root: Path = TEMPLATE_ROOT) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink():
        raise ContractError("versioned template directory is missing or unsafe")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ContractError("template symlinks are not allowed")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise ContractError("template path escaped its root")
        files[relative] = path.read_bytes()
    if not files or len(files) > 100:
        raise ContractError("template must contain between 1 and 100 files")
    return files


def load_implementation_files(root: Path = ROOT) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for relative in IMPLEMENTATION_PATHS:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"versioned implementation file is missing or unsafe: {relative}")
        files[relative] = path.read_bytes()
    return files


def render_template_files(
    repository: str,
    template_files: dict[str, bytes],
    production_worker: str,
) -> dict[str, bytes]:
    repository = validate_repository(repository)
    if not isinstance(production_worker, str) or re.fullmatch(
        r"[a-z][a-z0-9-]{0,53}", production_worker
    ) is None:
        raise ContractError("production Worker name is invalid")
    preview_worker = derive_preview_worker(repository)
    rendered: dict[str, bytes] = {}
    for path, content in template_files.items():
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError(f"template file is not UTF-8 text: {path}") from exc
        text = text.replace("__REPOSITORY__", repository)
        text = text.replace("__PRODUCTION_WORKER__", production_worker)
        text = text.replace("__PREVIEW_WORKER__", preview_worker)
        if any(
            marker in text
            for marker in (
                "__REPOSITORY__",
                "__PRODUCTION_WORKER__",
                "__PREVIEW_WORKER__",
            )
        ):
            raise ContractError(f"unresolved template placeholder: {path}")
        rendered[path] = text.encode("utf-8")
    return rendered


def file_manifest(files: dict[str, bytes]) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "gitBlobSha": _git_blob_sha(content),
            "size": len(content),
        }
        for path, content in sorted(files.items())
    ]


def files_checksum(files: dict[str, bytes]) -> str:
    return _canonical_sha256(file_manifest(files))


def _plan_checksum(plan: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(plan)
    unsigned.pop("checksum", None)
    return _canonical_sha256(unsigned)


def verify_plan_checksum(plan: dict[str, Any]) -> bool:
    checksum = plan.get("checksum")
    return (
        isinstance(checksum, str)
        and SHA256_PATTERN.fullmatch(checksum) is not None
        and checksum == _plan_checksum(plan)
    )


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

    def request(self, method: str, path: str, payload: Any | None = None) -> ApiResult:
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "swamp-github-cloudflare-bootstrap",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.endpoint}{path}", headers=headers, data=data, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                parsed = json.loads(raw) if raw else {}
                return ApiResult(response.status, parsed)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"message": "non-JSON GitHub API error"}
            return ApiResult(exc.code, parsed)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ContractError(f"GitHub API request failed: {exc}") from exc

    def get(self, path: str) -> ApiResult:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> ApiResult:
        return self.request("POST", path, payload)

    def put(self, path: str, payload: Any | None = None) -> ApiResult:
        return self.request("PUT", path, {} if payload is None else payload)

    def patch(self, path: str, payload: Any) -> ApiResult:
        return self.request("PATCH", path, payload)


class GitHubApi(Protocol):
    def get(self, path: str) -> ApiResult: ...
    def post(self, path: str, payload: Any) -> ApiResult: ...
    def put(self, path: str, payload: Any | None = None) -> ApiResult: ...
    def patch(self, path: str, payload: Any) -> ApiResult: ...


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _require_status(result: ApiResult, allowed: set[int], operation: str) -> Any:
    if result.status not in allowed:
        raise ContractError(f"{operation} failed with GitHub HTTP {result.status}")
    return result.payload


def build_plan(repository: str, client: GitHubApi) -> dict[str, Any]:
    repository = validate_repository(repository)
    organization = STANDARD["organization"]
    target = f"{organization}/{repository}"
    preview_worker = derive_preview_worker(repository)
    production_nonce = secrets.token_hex(12)
    production_worker = derive_production_worker(repository, production_nonce)
    template = load_template_files()
    implementation = load_implementation_files()
    rendered = render_template_files(repository, template, production_worker)
    checks: list[dict[str, str]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    actor_result = client.get("/user")
    actor = actor_result.payload.get("login") if actor_result.status == 200 and isinstance(actor_result.payload, dict) else None
    if actor == STANDARD["requiredReviewer"]:
        checks.append(_check("github_auth", "pass", f"authenticated as {actor}"))
    else:
        blockers.append("GitHub actor is not the required owner")
        checks.append(_check("github_auth", "block", f"actor={actor!r}"))

    target_result = client.get(f"/repos/{target}")
    if target_result.status == 404:
        checks.append(_check("target_repository", "pass", f"{target} is available"))
    elif target_result.status == 200:
        blockers.append(f"target repository already exists: {target}")
        checks.append(_check("target_repository", "block", "already exists"))
    else:
        blockers.append("target repository availability is unknown")
        checks.append(_check("target_repository", "block", f"HTTP {target_result.status}"))

    checks.append(
        _check(
            "repository_create_capability",
            "deferred",
            "proved by the approval-gated create call; no membership metadata read",
        )
    )

    reviewer = client.get(f"/users/{STANDARD['requiredReviewer']}")
    reviewer_id = reviewer.payload.get("id") if reviewer.status == 200 and isinstance(reviewer.payload, dict) else None
    if isinstance(reviewer_id, int) and reviewer_id > 0:
        checks.append(_check("environment_reviewer", "pass", f"user id={reviewer_id}"))
    else:
        blockers.append("required Environment reviewer could not be resolved")
        checks.append(_check("environment_reviewer", "block", f"HTTP {reviewer.status}"))

    checks.append(
        _check(
            "cloudflare_credentials",
            "deferred",
            "fixed organization secret names are referenced by workflows; access and values are verified only by production deployment",
        )
    )

    plan: dict[str, Any] = {
        "schemaVersion": 2,
        "mode": "plan",
        "readOnly": True,
        "ready": not blockers,
        "target": {
            "repository": target,
            "name": repository,
            "visibility": STANDARD["visibility"],
            "productionNonce": production_nonce,
            "productionWorker": production_worker,
            "previewWorker": preview_worker,
            "productionUrl": f"https://{production_worker}.{STANDARD['workersSubdomain']}.workers.dev",
        },
        "source": {
            "templateDirectory": STANDARD["templateDirectory"],
            "templateChecksum": files_checksum(template),
            "implementationChecksum": files_checksum(implementation),
            "implementationManifest": file_manifest(implementation),
            "renderedChecksum": files_checksum(rendered),
            "renderedManifest": file_manifest(rendered),
        },
        "standard": STANDARD,
        "resolved": {
            "reviewerId": reviewer_id,
        },
        "checks": checks,
        "plannedActions": [
            "create public GitHub repository",
            "create exact rendered initial main tree and commit",
            "create branch-preview Environment with required reviewer",
            "use fixed organization secret names and verify access through production deployment",
            "configure merge and repository settings",
            "verify main tree, production deploy and runtime",
        ],
        "requiredApproval": "Create and configure the exact repository described by this checksum-bound plan",
        "blockers": blockers,
        "warnings": warnings,
    }
    plan["checksum"] = _plan_checksum(plan)
    return plan


def _validate_bound_plan(
    repository: str,
    plan: dict[str, Any],
    checksum: str,
) -> dict[str, bytes]:
    if (
        not isinstance(plan, dict)
        or plan.get("schemaVersion") != 2
        or plan.get("mode") != "plan"
        or plan.get("readOnly") is not True
        or plan.get("ready") is not True
        or plan.get("blockers") != []
        or plan.get("checksum") != checksum
        or not verify_plan_checksum(plan)
    ):
        raise ContractError("approved plan is not ready or checksum-bound")
    target = plan.get("target", {})
    if target.get("repository") != f"{STANDARD['organization']}/{repository}":
        raise ContractError("approved plan target does not match request")
    production_nonce = target.get("productionNonce")
    if not isinstance(production_nonce, str):
        raise ContractError("approved plan production Worker nonce is missing")
    production_worker = derive_production_worker(repository, production_nonce)
    if (
        target.get("productionWorker") != production_worker
        or target.get("productionUrl")
        != f"https://{production_worker}.{STANDARD['workersSubdomain']}.workers.dev"
    ):
        raise ContractError("approved production Worker target is inconsistent")
    template = load_template_files()
    implementation = load_implementation_files()
    rendered = render_template_files(repository, template, production_worker)
    source = plan.get("source", {})
    if source.get("templateChecksum") != files_checksum(template):
        raise ContractError("versioned template changed after approval")
    if source.get("implementationChecksum") != files_checksum(implementation):
        raise ContractError("versioned apply implementation changed after approval")
    if source.get("implementationManifest") != file_manifest(implementation):
        raise ContractError("versioned apply implementation manifest changed after approval")
    if source.get("renderedChecksum") != files_checksum(rendered):
        raise ContractError("rendered template changed after approval")
    if source.get("renderedManifest") != file_manifest(rendered):
        raise ContractError("rendered file manifest changed after approval")
    return rendered


def load_bound_plan(*, artifact_version: int, plan_run_id: str, plan_checksum: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "swamp",
            "data",
            "get",
            "github-cloudflare-repo-bootstrap",
            "result",
            "--version",
            str(artifact_version),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise ContractError("approved plan artifact retrieval failed")
    try:
        artifact = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("approved plan artifact returned invalid JSON") from exc
    owner = artifact.get("ownerDefinition") if isinstance(artifact, dict) else None
    content = artifact.get("content") if isinstance(artifact, dict) else None
    if (
        not isinstance(artifact, dict)
        or artifact.get("modelName") != "github-cloudflare-repo-bootstrap"
        or artifact.get("name") != "result"
        or artifact.get("version") != artifact_version
        or not isinstance(owner, dict)
        or owner.get("workflowRunId") != plan_run_id
        or not isinstance(content, dict)
        or content.get("exitCode") != 0
        or not isinstance(content.get("stdout"), str)
    ):
        raise ContractError("approved plan artifact provenance is invalid")
    try:
        plan = json.loads(content["stdout"])
    except json.JSONDecodeError as exc:
        raise ContractError("approved plan content returned invalid JSON") from exc
    if not isinstance(plan, dict) or plan.get("checksum") != plan_checksum:
        raise ContractError("approved plan checksum binding is invalid")
    return plan


def apply_plan(
    repository: str,
    plan: dict[str, Any],
    plan_run_id: str,
    plan_checksum: str,
    client: GitHubApi,
) -> dict[str, Any]:
    repository = validate_repository(repository)
    rendered = _validate_bound_plan(repository, plan, plan_checksum)
    organization = STANDARD["organization"]
    target = f"{organization}/{repository}"

    availability = client.get(f"/repos/{target}")
    if availability.status != 404:
        raise ContractError("target repository is no longer absent; refusing overwrite")

    created = _require_status(
        client.post(
            f"/orgs/{organization}/repos",
            {
                "name": repository,
                "private": False,
                "has_issues": True,
                "has_projects": False,
                "has_wiki": False,
                "auto_init": True,
                "allow_squash_merge": True,
            },
        ),
        {201},
        "create repository",
    )
    repository_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(repository_id, int) or repository_id < 1:
        raise ContractError("created repository id is missing")

    initial = _require_status(
        client.get(f"/repos/{target}/commits/main"),
        {200},
        "read initialized main commit",
    )
    initial_sha = initial.get("sha") if isinstance(initial, dict) else None
    if not isinstance(initial_sha, str) or re.fullmatch(r"[0-9a-f]{40}", initial_sha) is None:
        raise ContractError("initialized main commit SHA is invalid")

    tree_entries: list[dict[str, Any]] = []
    for path, content in sorted(rendered.items()):
        blob = _require_status(
            client.post(
                f"/repos/{target}/git/blobs",
                {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            ),
            {201},
            f"create blob {path}",
        )
        sha = blob.get("sha") if isinstance(blob, dict) else None
        if not isinstance(sha, str):
            raise ContractError(f"blob SHA is missing for {path}")
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})

    tree = _require_status(
        client.post(f"/repos/{target}/git/trees", {"tree": tree_entries}),
        {201},
        "create initial tree",
    )
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    commit = _require_status(
        client.post(
            f"/repos/{target}/git/commits",
            {
                "message": "[verified] initialize standard Cloudflare application",
                "tree": tree_sha,
                "parents": [initial_sha],
            },
        ),
        {201},
        "create initial commit",
    )
    commit_sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(commit_sha, str) or re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None:
        raise ContractError("initial commit SHA is invalid")
    _require_status(
        client.patch(
            f"/repos/{target}/git/refs/heads/main",
            {"sha": commit_sha, "force": False},
        ),
        {200},
        "update main branch",
    )

    reviewer_id = plan.get("resolved", {}).get("reviewerId")
    _require_status(
        client.put(
            f"/repos/{target}/environments/{STANDARD['environment']}",
            {
                "prevent_self_review": False,
                "reviewers": [{"type": "User", "id": reviewer_id}],
                "deployment_branch_policy": None,
            },
        ),
        {200},
        "configure branch-preview Environment",
    )
    _require_status(
        client.patch(
            f"/repos/{target}",
            {
                "default_branch": "main",
                "delete_branch_on_merge": True,
                "has_wiki": False,
                "allow_squash_merge": True,
            },
        ),
        {200},
        "configure repository settings",
    )
    return {
        "schemaVersion": 2,
        "mode": "apply",
        "status": "created",
        "repository": target,
        "repositoryId": repository_id,
        "mainSha": commit_sha,
        "planRunId": plan_run_id,
        "planChecksum": plan_checksum,
        "productionUrl": plan["target"]["productionUrl"],
        "rollback": "Archive or delete the newly created repository manually after reviewing partial external state; no automatic destructive rollback is performed.",
    }


def _tree_matches_plan(repository: str, plan: dict[str, Any], client: GitHubApi) -> tuple[bool, str | None]:
    target = f"{STANDARD['organization']}/{repository}"
    main = client.get(f"/repos/{target}/commits/main")
    main_sha = main.payload.get("sha") if main.status == 200 and isinstance(main.payload, dict) else None
    tree = client.get(f"/repos/{target}/git/trees/main?recursive=1")
    if tree.status != 200 or not isinstance(tree.payload, dict):
        return False, main_sha
    observed = {
        item.get("path"): item.get("sha")
        for item in tree.payload.get("tree", [])
        if isinstance(item, dict) and item.get("type") == "blob"
    }
    expected = {item["path"]: item["gitBlobSha"] for item in plan["source"]["renderedManifest"]}
    return observed == expected, main_sha


def _environment_has_reviewer(payload: Any, reviewer_id: int) -> bool:
    if not isinstance(payload, dict):
        return False
    for rule in payload.get("protection_rules", []):
        if not isinstance(rule, dict):
            continue
        for item in rule.get("reviewers", []):
            if not isinstance(item, dict):
                continue
            reviewer = item.get("reviewer")
            if isinstance(reviewer, dict) and reviewer.get("id") == reviewer_id:
                return True
    return False


def verify_repository(
    repository: str,
    plan: dict[str, Any],
    plan_checksum: str,
    client: GitHubApi,
    *,
    wait_seconds: int = 180,
) -> dict[str, Any]:
    repository = validate_repository(repository)
    _validate_bound_plan(repository, plan, plan_checksum)
    target = f"{STANDARD['organization']}/{repository}"
    repo = client.get(f"/repos/{target}")
    if repo.status != 200 or not isinstance(repo.payload, dict):
        raise ContractError("repository readback failed")
    tree_ok, main_sha = _tree_matches_plan(repository, plan, client)
    if not tree_ok or not isinstance(main_sha, str):
        raise ContractError("main tree does not match the approved rendered manifest")
    environment = client.get(f"/repos/{target}/environments/{STANDARD['environment']}")
    reviewer_id = plan["resolved"]["reviewerId"]
    reviewer_ok = environment.status == 200 and _environment_has_reviewer(
        environment.payload, reviewer_id
    )
    if not reviewer_ok:
        raise ContractError("branch-preview Environment reviewer readback failed")

    deadline = time.monotonic() + max(0, wait_seconds)
    deploy_run: dict[str, Any] | None = None
    while True:
        encoded = urllib.parse.urlencode({"event": "push", "head_sha": main_sha, "per_page": 20})
        runs = client.get(f"/repos/{target}/actions/workflows/deploy.yml/runs?{encoded}")
        candidates = runs.payload.get("workflow_runs", []) if runs.status == 200 and isinstance(runs.payload, dict) else []
        deploy_run = next((item for item in candidates if isinstance(item, dict)), None)
        if deploy_run and deploy_run.get("status") == "completed":
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(5)
    if not deploy_run or deploy_run.get("conclusion") != "success":
        raise ContractError("production deployment did not complete successfully")

    production_url = plan["target"]["productionUrl"]
    try:
        health_request = urllib.request.Request(
            production_url + "/api/health",
            headers={
                "User-Agent": "swamp-runtime-verifier/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(health_request, timeout=20) as response:
            health_status = response.status
            health_payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ContractError(f"production runtime verification failed: {exc}") from exc
    if health_status != 200 or health_payload != {"status": "ok"}:
        raise ContractError("production runtime health response is invalid")

    return {
        "schemaVersion": 2,
        "mode": "verify",
        "status": "verified",
        "repository": target,
        "mainSha": main_sha,
        "deployRunId": deploy_run.get("id"),
        "deployRunUrl": deploy_run.get("html_url"),
        "productionUrl": production_url,
        "health": health_payload,
        "templateChecksum": plan["source"]["templateChecksum"],
        "implementationChecksum": plan["source"]["implementationChecksum"],
        "renderedChecksum": plan["source"]["renderedChecksum"],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--mode", choices=("plan", "apply", "verify"), default="plan")
    parser.add_argument("--plan-run-id")
    parser.add_argument("--plan-checksum")
    parser.add_argument("--artifact-version", type=int)
    args = parser.parse_args(argv)
    bindings = (args.plan_run_id, args.plan_checksum, args.artifact_version)
    if args.mode == "plan" and any(value is not None for value in bindings):
        parser.error("plan mode does not accept apply bindings")
    if args.mode in {"apply", "verify"} and any(value is None for value in bindings):
        parser.error("apply/verify mode requires plan run id, checksum and artifact version")
    return args


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    try:
        args = parse_args(argv or sys.argv[1:])
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        client = GitHubClient(token)
        if args.mode == "plan":
            result = build_plan(args.repository, client)
        else:
            if not UUID_PATTERN.fullmatch(str(args.plan_run_id)):
                raise ContractError("plan_run_id must be a UUID")
            if not SHA256_PATTERN.fullmatch(str(args.plan_checksum)):
                raise ContractError("plan_checksum must be a SHA-256 hex digest")
            if not isinstance(args.artifact_version, int) or args.artifact_version < 1:
                raise ContractError("artifact_version must be positive")
            plan = load_bound_plan(
                artifact_version=args.artifact_version,
                plan_run_id=args.plan_run_id,
                plan_checksum=args.plan_checksum,
            )
            if args.mode == "apply":
                result = apply_plan(
                    args.repository,
                    plan,
                    args.plan_run_id,
                    args.plan_checksum,
                    client,
                )
            else:
                result = verify_repository(
                    args.repository, plan, args.plan_checksum, client
                )
        emit(result)
        return 0
    except (ContractError, json.JSONDecodeError) as exc:
        emit(
            {
                "schemaVersion": 2,
                "mode": args.mode if args is not None else "unknown",
                "status": "rejected",
                "error": str(exc),
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
