from __future__ import annotations

import json
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
