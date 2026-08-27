from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID


class BrokerError(ValueError):
    pass


ALLOWED_OPERATIONS = {
    ("github", "repository_access"),
    ("github", "list_pull_requests"),
    ("github", "pull_request_checks"),
    ("swamp", "auth_whoami"),
    ("swamp", "validate_model"),
    ("swamp", "validate_workflow"),
    ("swamp", "run_readonly_workflow"),
    ("swamp", "plan_github_cloudflare_repository"),
    ("swamp", "get_result"),
}


def resolve_caller(session_id: str, state_db: Path) -> str:
    connection = sqlite3.connect(state_db)
    try:
        row = connection.execute(
            "SELECT source, user_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row or row[0] != "a2a" or not row[1]:
        raise BrokerError("caller identity is not an authenticated A2A peer")
    return str(row[1])


def _allowed_repository(arguments: dict[str, Any], policy: dict[str, Any]) -> str:
    repository = arguments.get("repository")
    if repository not in policy.get("github", {}).get("repositories", []):
        raise BrokerError("repository is not allowed")
    return str(repository)


def build_command(
    operation_key: str, arguments: dict[str, Any], policy: dict[str, Any]
) -> list[str]:
    if not isinstance(arguments, dict):
        raise BrokerError("arguments must be an object")
    expected_arguments = {
        "github.repository_access": {"repository"},
        "github.list_pull_requests": {"repository"},
        "github.pull_request_checks": {"repository", "pull_request"},
        "swamp.auth_whoami": set(),
        "swamp.validate_model": {"model"},
        "swamp.validate_workflow": {"workflow"},
        "swamp.run_readonly_workflow": {"workflow"},
        "swamp.plan_github_cloudflare_repository": {"repository"},
        "swamp.get_result": {"model", "name"},
    }
    if operation_key not in expected_arguments:
        raise BrokerError("operation has no executor")
    if set(arguments) != expected_arguments[operation_key]:
        raise BrokerError("unexpected arguments for operation")
    if operation_key == "github.repository_access":
        repository = _allowed_repository(arguments, policy)
        return ["gh", "api", f"repos/{repository}"]
    if operation_key == "github.list_pull_requests":
        repository = _allowed_repository(arguments, policy)
        return ["gh", "api", f"repos/{repository}/pulls"]
    if operation_key == "github.pull_request_checks":
        repository = _allowed_repository(arguments, policy)
        pull_request = arguments.get("pull_request")
        if not isinstance(pull_request, int) or isinstance(pull_request, bool) or pull_request < 1:
            raise BrokerError("pull_request must be a positive integer")
        return [
            "gh",
            "pr",
            "checks",
            str(pull_request),
            "--repo",
            repository,
            "--json",
            "name,state,link,bucket,event,workflow",
        ]
    if operation_key == "swamp.auth_whoami":
        return ["swamp", "auth", "whoami", "--json"]
    if operation_key == "swamp.validate_model":
        model = arguments.get("model")
        if model not in policy.get("swamp", {}).get("models", []):
            raise BrokerError("model is not allowed")
        return ["swamp", "model", "validate", str(model), "--json"]
    if operation_key in {"swamp.validate_workflow", "swamp.run_readonly_workflow"}:
        workflow = arguments.get("workflow")
        if workflow not in policy.get("swamp", {}).get("workflows", []):
            raise BrokerError("workflow is not allowed")
        action = "validate" if operation_key == "swamp.validate_workflow" else "run"
        return ["swamp", "workflow", action, str(workflow), "--json"]
    if operation_key == "swamp.plan_github_cloudflare_repository":
        workflow = "github-cloudflare-repo-bootstrap"
        if workflow != policy.get("swamp", {}).get("repositoryBootstrapWorkflow"):
            raise BrokerError("repository bootstrap workflow is not allowed")
        repository = arguments.get("repository")
        if not isinstance(repository, str) or re.fullmatch(
            r"[a-z][a-z0-9-]{1,54}", repository
        ) is None:
            raise BrokerError("repository must match ^[a-z][a-z0-9-]{1,54}$")
        return [
            "swamp",
            "workflow",
            "run",
            workflow,
            "--input",
            f"repository={repository}",
            "--json",
        ]
    if operation_key == "swamp.get_result":
        model = arguments.get("model")
        name = arguments.get("name")
        allowed = policy.get("swamp", {}).get("data", [])
        if {"model": model, "name": name} not in allowed:
            raise BrokerError("data result is not allowed")
        return ["swamp", "data", "get", str(model), str(name), "--json"]
    raise BrokerError("operation has no executor")


def _append_audit(
    audit_path: Path | None,
    *,
    request: dict[str, Any],
    caller: str,
    operation: str,
    status: str,
) -> None:
    if audit_path is None:
        return
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request["request_id"],
        "caller": caller,
        "operation": operation,
        "mode": request["mode"],
        "status": status,
        "approval_state": "not_required",
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _repository_plan_result(
    workflow_result: dict[str, Any],
    *,
    expected_repository: str,
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
) -> dict[str, Any]:
    if not isinstance(workflow_result, dict):
        raise BrokerError("repository bootstrap run returned an invalid result")
    workflow_run_id = workflow_result.get("id")
    if not isinstance(workflow_run_id, str) or not workflow_run_id:
        raise BrokerError("repository bootstrap run did not return an id")
    versions: list[int] = []
    for job in workflow_result.get("jobs", []):
        if not isinstance(job, dict):
            continue
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            for artifact in step.get("dataArtifacts", []):
                if (
                    isinstance(artifact, dict)
                    and artifact.get("name") == "result"
                    and isinstance(artifact.get("version"), int)
                    and not isinstance(artifact.get("version"), bool)
                    and artifact["version"] > 0
                ):
                    versions.append(artifact["version"])
    if len(versions) != 1:
        raise BrokerError("repository bootstrap run did not produce exactly one result artifact")
    version = versions[0]
    command = [
        "swamp",
        "data",
        "get",
        "github-cloudflare-repo-bootstrap",
        "result",
        "--version",
        str(version),
        "--json",
    ]
    completed = runner(command, cwd=workspace, timeout=60)
    if completed["returncode"] != 0:
        raise BrokerError("repository bootstrap result retrieval failed")
    try:
        artifact = json.loads(completed["stdout"])
    except json.JSONDecodeError as exc:
        raise BrokerError("repository bootstrap result returned invalid JSON") from exc
    if not isinstance(artifact, dict):
        raise BrokerError("repository bootstrap result returned an invalid JSON object")
    owner = artifact.get("ownerDefinition")
    content = artifact.get("content")
    if (
        artifact.get("modelName") != "github-cloudflare-repo-bootstrap"
        or artifact.get("name") != "result"
        or artifact.get("version") != version
        or not isinstance(owner, dict)
        or owner.get("workflowRunId") != workflow_run_id
        or not isinstance(content, dict)
        or content.get("exitCode") != 0
        or not isinstance(content.get("stdout"), str)
    ):
        raise BrokerError("repository bootstrap result provenance is invalid")
    try:
        plan = json.loads(content["stdout"])
    except json.JSONDecodeError as exc:
        raise BrokerError("repository bootstrap plan returned invalid JSON") from exc
    target = plan.get("target") if isinstance(plan, dict) else None
    if (
        not isinstance(plan, dict)
        or plan.get("schemaVersion") != 1
        or plan.get("mode") != "plan"
        or plan.get("readOnly") is not True
        or not isinstance(plan.get("ready"), bool)
        or not isinstance(plan.get("blockers"), list)
    ):
        raise BrokerError("repository bootstrap plan violated the read-only contract")
    if (
        not isinstance(target, dict)
        or target.get("repository") != expected_repository
    ):
        raise BrokerError("repository bootstrap plan target does not match request")
    return {
        "workflowRunId": workflow_run_id,
        "artifactVersion": version,
        "plan": plan,
    }


def execute_request(
    request: dict[str, Any],
    *,
    caller: str,
    policy: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    operation_key = f"{request['integration']}.{request['operation']}"
    try:
        peer = policy.get("peers", {}).get(caller, {})
        if operation_key not in peer.get("operations", []):
            raise BrokerError("operation is not allowed for caller")

        command = build_command(operation_key, request["arguments"], policy)
        completed = runner(command, cwd=workspace, timeout=60)
        accepted_returncodes = {0, 8} if operation_key == "github.pull_request_checks" else {0}
        if completed["returncode"] not in accepted_returncodes:
            raise BrokerError("operation execution failed")
        try:
            result = json.loads(completed["stdout"])
        except json.JSONDecodeError as exc:
            raise BrokerError("operation returned invalid JSON") from exc
        if operation_key == "swamp.plan_github_cloudflare_repository":
            result = _repository_plan_result(
                result,
                expected_repository=(
                    "sisyphus-org/" + str(request["arguments"]["repository"])
                ),
                runner=runner,
                workspace=workspace,
            )
    except Exception:
        _append_audit(
            audit_path,
            request=request,
            caller=caller,
            operation=operation_key,
            status="rejected",
        )
        raise

    response = {
        "request_id": request["request_id"],
        "caller": caller,
        "operation": operation_key,
        "mode": request["mode"],
        "status": "ok",
        "result": result,
    }
    _append_audit(
        audit_path,
        request=request,
        caller=caller,
        operation=operation_key,
        status="ok",
    )
    return response


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    required_fields = {"request_id", "integration", "operation", "arguments", "mode"}
    unexpected = set(payload) - required_fields
    if unexpected:
        raise BrokerError("unexpected request fields")
    missing = required_fields - set(payload)
    if missing:
        raise BrokerError("missing request fields")
    try:
        UUID(str(payload["request_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BrokerError("request_id must be a UUID") from exc
    integration = payload["integration"]
    operation = payload["operation"]
    if (integration, operation) not in ALLOWED_OPERATIONS:
        raise BrokerError("operation is not allowed")
    mode = payload["mode"]
    if mode != "plan":
        raise BrokerError("apply mode is not available for read-only operations")
    return {
        "request_id": payload["request_id"],
        "integration": integration,
        "operation": operation,
        "arguments": payload["arguments"],
        "mode": mode,
    }
