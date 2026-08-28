from __future__ import annotations

import fcntl
import hashlib
import json
import os
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
    ("swamp", "start_github_cloudflare_repository_apply"),
    ("swamp", "approve_github_cloudflare_repository_apply"),
    ("swamp", "get_result"),
}

PLAN_MODEL = "github-cloudflare-repo-bootstrap"
PLAN_WORKFLOW = "github-cloudflare-repo-bootstrap"
APPLY_WORKFLOW = "github-cloudflare-repo-bootstrap-apply"
REPOSITORY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,54}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def resolve_caller(
    session_id: str,
    state_db: Path,
    owner_identities: list[dict[str, str]] | None = None,
) -> str:
    connection = sqlite3.connect(state_db)
    try:
        row = connection.execute(
            "SELECT source, user_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row or not row[0] or not row[1]:
        raise BrokerError("caller identity is not authenticated")
    source, user_id = str(row[0]), str(row[1])
    if source == "a2a":
        if user_id == "owner":
            raise BrokerError("A2A identity collides with reserved privileged principal")
        return user_id
    for identity in owner_identities or []:
        if (
            isinstance(identity, dict)
            and identity.get("source") == source
            and str(identity.get("user_id")) == user_id
            and identity.get("caller") == "owner"
        ):
            return "owner"
    raise BrokerError("caller identity is not an authenticated A2A peer or owner")


def _uuid(value: Any, name: str) -> str:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BrokerError(f"{name} must be a UUID") from exc
    return str(value)


def _repository(value: Any) -> str:
    if not isinstance(value, str) or REPOSITORY_PATTERN.fullmatch(value) is None:
        raise BrokerError("repository must match ^[a-z][a-z0-9-]{1,54}$")
    return value


def _checksum(value: Any) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise BrokerError("plan_checksum must be a SHA-256 hex digest")
    return value


def _version(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BrokerError("artifact_version must be a positive integer")
    return value


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
        "swamp.start_github_cloudflare_repository_apply": {
            "repository",
            "plan_run_id",
            "plan_checksum",
            "artifact_version",
        },
        "swamp.approve_github_cloudflare_repository_apply": {"apply_run_id"},
        "swamp.get_result": {"model", "name"},
    }
    if operation_key not in expected_arguments:
        raise BrokerError("operation has no executor")
    if set(arguments) != expected_arguments[operation_key]:
        raise BrokerError("unexpected arguments for operation")

    if operation_key == "github.repository_access":
        return ["gh", "api", f"repos/{_allowed_repository(arguments, policy)}"]
    if operation_key == "github.list_pull_requests":
        return ["gh", "api", f"repos/{_allowed_repository(arguments, policy)}/pulls"]
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
        if policy.get("swamp", {}).get("repositoryBootstrapWorkflow") != PLAN_WORKFLOW:
            raise BrokerError("repository bootstrap workflow is not allowed")
        repository = _repository(arguments.get("repository"))
        return [
            "swamp",
            "workflow",
            "run",
            PLAN_WORKFLOW,
            "--input",
            f"repository={repository}",
            "--json",
        ]
    if operation_key == "swamp.start_github_cloudflare_repository_apply":
        if policy.get("swamp", {}).get("repositoryBootstrapApplyWorkflow") != APPLY_WORKFLOW:
            raise BrokerError("repository bootstrap apply workflow is not allowed")
        repository = _repository(arguments.get("repository"))
        plan_run_id = _uuid(arguments.get("plan_run_id"), "plan_run_id")
        plan_checksum = _checksum(arguments.get("plan_checksum"))
        artifact_version = _version(arguments.get("artifact_version"))
        return [
            "swamp",
            "workflow",
            "run",
            APPLY_WORKFLOW,
            "--input",
            f"repository={repository}",
            "--input",
            f"planRunId={plan_run_id}",
            "--input",
            f"planChecksum={plan_checksum}",
            "--input",
            f"artifactVersion:json={artifact_version}",
            "--json",
        ]
    if operation_key == "swamp.approve_github_cloudflare_repository_apply":
        if policy.get("swamp", {}).get("repositoryBootstrapApplyWorkflow") != APPLY_WORKFLOW:
            raise BrokerError("repository bootstrap apply workflow is not allowed")
        apply_run_id = _uuid(arguments.get("apply_run_id"), "apply_run_id")
        return [
            "swamp",
            "workflow",
            "approve",
            APPLY_WORKFLOW,
            "approve-create",
            "--run",
            apply_run_id,
            "--json",
        ]
    if operation_key == "swamp.get_result":
        model = arguments.get("model")
        name = arguments.get("name")
        if {"model": model, "name": name} not in policy.get("swamp", {}).get("data", []):
            raise BrokerError("data result is not allowed")
        return ["swamp", "data", "get", str(model), str(name), "--json"]
    raise BrokerError("operation has no executor")


def _canonical_plan_checksum(plan: dict[str, Any]) -> str:
    unsigned = dict(plan)
    unsigned.pop("checksum", None)
    encoded = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_result_version(workflow_result: dict[str, Any]) -> tuple[str, int]:
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
    return workflow_run_id, versions[0]


def _load_plan_artifact(
    *,
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
    artifact_version: int,
    plan_run_id: str,
    plan_checksum: str | None = None,
    expected_repository: str,
) -> dict[str, Any]:
    completed = runner(
        [
            "swamp",
            "data",
            "get",
            PLAN_MODEL,
            "result",
            "--version",
            str(artifact_version),
            "--json",
        ],
        cwd=workspace,
        timeout=60,
    )
    if completed["returncode"] != 0:
        raise BrokerError("repository bootstrap result retrieval failed")
    try:
        artifact = json.loads(completed["stdout"])
    except json.JSONDecodeError as exc:
        raise BrokerError("repository bootstrap result returned invalid JSON") from exc
    owner = artifact.get("ownerDefinition") if isinstance(artifact, dict) else None
    content = artifact.get("content") if isinstance(artifact, dict) else None
    if (
        not isinstance(artifact, dict)
        or artifact.get("modelName") != PLAN_MODEL
        or artifact.get("name") != "result"
        or artifact.get("version") != artifact_version
        or not isinstance(owner, dict)
        or owner.get("workflowRunId") != plan_run_id
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
    checksum = plan.get("checksum") if isinstance(plan, dict) else None
    if (
        not isinstance(plan, dict)
        or plan.get("schemaVersion") != 2
        or plan.get("mode") != "plan"
        or plan.get("readOnly") is not True
        or not isinstance(plan.get("ready"), bool)
        or not isinstance(plan.get("blockers"), list)
        or not isinstance(checksum, str)
        or checksum != _canonical_plan_checksum(plan)
        or not isinstance(target, dict)
    ):
        raise BrokerError("repository bootstrap plan contract is invalid")
    if target.get("repository") != expected_repository:
        raise BrokerError("repository bootstrap plan target does not match request")
    if plan_checksum is not None and checksum != plan_checksum:
        raise BrokerError("repository bootstrap plan checksum does not match approval")
    return plan


def _repository_plan_result(
    workflow_result: dict[str, Any],
    *,
    expected_repository: str,
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
) -> dict[str, Any]:
    workflow_run_id, version = _extract_result_version(workflow_result)
    plan = _load_plan_artifact(
        runner=runner,
        workspace=workspace,
        artifact_version=version,
        plan_run_id=workflow_run_id,
        expected_repository=expected_repository,
    )
    return {"workflowRunId": workflow_run_id, "artifactVersion": version, "plan": plan}


def _append_jsonl(path: Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _audit_base(request: dict[str, Any], caller: str, operation: str, status: str) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request["request_id"],
        "caller": caller,
        "operation": operation,
        "mode": request["mode"],
        "status": status,
    }


def _register_apply_gate(
    audit_path: Path | None,
    *,
    request: dict[str, Any],
    caller: str,
    apply_run_id: str,
) -> None:
    if audit_path is None:
        raise BrokerError("apply operations require an immutable audit path")
    arguments = request["arguments"]
    _append_jsonl(
        audit_path,
        {
            **_audit_base(request, caller, "swamp.start_github_cloudflare_repository_apply", "awaiting_approval"),
            "event": "apply_gate",
            "apply_run_id": apply_run_id,
            "repository": arguments["repository"],
            "plan_run_id": arguments["plan_run_id"],
            "plan_checksum": arguments["plan_checksum"],
            "artifact_version": arguments["artifact_version"],
        },
    )


def _json_command(
    runner: Callable[..., dict[str, Any]],
    argv: list[str],
    *,
    workspace: Path,
    timeout: int,
    error_prefix: str,
) -> dict[str, Any]:
    completed = runner(argv, cwd=workspace, timeout=timeout)
    if completed["returncode"] != 0:
        raise BrokerError(f"{error_prefix} failed")
    try:
        payload = json.loads(completed["stdout"])
    except json.JSONDecodeError as exc:
        raise BrokerError(f"{error_prefix} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BrokerError(f"{error_prefix} returned non-object JSON")
    return payload


def _approval_step_status(history: dict[str, Any]) -> str | None:
    for job in history.get("jobs", []):
        if not isinstance(job, dict) or job.get("name") != "apply":
            continue
        for step in job.get("steps", []):
            if isinstance(step, dict) and step.get("name") == "approve-create":
                status = step.get("status")
                return status if isinstance(status, str) else None
    return None


def _approve_registered_apply(
    audit_path: Path | None,
    *,
    request: dict[str, Any],
    caller: str,
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
    approve_command: list[str],
) -> dict[str, Any]:
    if caller != "owner":
        raise BrokerError("apply approval requires authenticated owner")
    if audit_path is None or not audit_path.exists():
        raise BrokerError("approved apply run is not registered in the immutable audit")
    apply_run_id = request["arguments"]["apply_run_id"]
    lock_path = audit_path.with_name(audit_path.name + ".apply.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        gate: dict[str, Any] | None = None
        completed = False
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BrokerError("immutable audit contains invalid JSON") from exc
            if record.get("apply_run_id") != apply_run_id:
                continue
            if record.get("event") == "apply_gate":
                gate = record
            elif record.get("event") == "apply_result":
                completed = True
        if gate is None:
            raise BrokerError("apply run is not registered")
        if completed:
            raise BrokerError("apply run was already approved")

        history_command = [
            "swamp",
            "workflow",
            "history",
            "get",
            apply_run_id,
            "--json",
        ]
        history = _json_command(
            runner,
            history_command,
            workspace=workspace,
            timeout=60,
            error_prefix="apply workflow history lookup",
        )
        if (
            history.get("id") != apply_run_id
            or history.get("workflowName") != APPLY_WORKFLOW
        ):
            raise BrokerError("apply workflow history returned the wrong run")
        expected_inputs = {
            "repository": gate.get("repository"),
            "planRunId": gate.get("plan_run_id"),
            "planChecksum": gate.get("plan_checksum"),
            "artifactVersion": gate.get("artifact_version"),
        }
        if history.get("inputs") != expected_inputs:
            raise BrokerError("apply workflow history does not match bound inputs")

        status = history.get("status")
        if status == "succeeded":
            result = history
        elif status == "suspended":
            approval_status = _approval_step_status(history)
            if approval_status == "waiting_approval":
                approved = _json_command(
                    runner,
                    approve_command,
                    workspace=workspace,
                    timeout=60,
                    error_prefix="apply workflow approval",
                )
                if approved.get("runId") != apply_run_id:
                    raise BrokerError("apply workflow approval returned the wrong run")
                _append_jsonl(
                    audit_path,
                    {
                        **_audit_base(
                            request, caller, "swamp.approve_github_cloudflare_repository_apply", "approval_recorded"
                        ),
                        "event": "approval_recorded",
                        "apply_run_id": apply_run_id,
                    },
                )
            elif approval_status != "succeeded":
                raise BrokerError("apply workflow is not waiting at the expected approval step")
            result = _json_command(
                runner,
                [
                    "swamp",
                    "workflow",
                    "resume",
                    APPLY_WORKFLOW,
                    "--run",
                    apply_run_id,
                    "--json",
                ],
                workspace=workspace,
                timeout=600,
                error_prefix="approved apply workflow resume",
            )
        else:
            raise BrokerError("apply workflow is not safely resumable")

        if result.get("id") != apply_run_id or result.get("status") != "succeeded":
            raise BrokerError("approved apply workflow did not finish succeeded")
        _append_jsonl(
            audit_path,
            {
                **_audit_base(
                    request, caller, "swamp.approve_github_cloudflare_repository_apply", "approved"
                ),
                "event": "apply_result",
                "apply_run_id": apply_run_id,
                "repository": gate.get("repository"),
                "plan_checksum": gate.get("plan_checksum"),
            },
        )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return result


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

        if operation_key == "swamp.start_github_cloudflare_repository_apply":
            if audit_path is None:
                raise BrokerError("apply operations require an immutable audit path")
            try:
                _append_jsonl(
                    audit_path,
                    {
                        **_audit_base(request, caller, operation_key, "preflight"),
                        "event": "apply_preflight",
                    },
                )
            except OSError as exc:
                raise BrokerError("immutable audit path is not writable") from exc
            args = request["arguments"]
            approved_plan = _load_plan_artifact(
                runner=runner,
                workspace=workspace,
                artifact_version=args["artifact_version"],
                plan_run_id=args["plan_run_id"],
                plan_checksum=args["plan_checksum"],
                expected_repository=f"sisyphus-org/{args['repository']}",
            )
            if approved_plan.get("ready") is not True or approved_plan.get("blockers") != []:
                raise BrokerError("repository bootstrap plan is not ready for apply")
        if operation_key == "swamp.approve_github_cloudflare_repository_apply":
            result = _approve_registered_apply(
                audit_path,
                request=request,
                caller=caller,
                runner=runner,
                workspace=workspace,
                approve_command=command,
            )
            return {
                "request_id": request["request_id"],
                "caller": caller,
                "operation": operation_key,
                "mode": request["mode"],
                "status": "ok",
                "result": result,
            }

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
                expected_repository=f"sisyphus-org/{request['arguments']['repository']}",
                runner=runner,
                workspace=workspace,
            )
        elif operation_key == "swamp.start_github_cloudflare_repository_apply":
            apply_run_id = result.get("id") if isinstance(result, dict) else None
            if (
                not isinstance(apply_run_id, str)
                or result.get("status") != "suspended"
            ):
                raise BrokerError("apply workflow did not suspend at manual approval")
            _uuid(apply_run_id, "apply_run_id")
            _register_apply_gate(
                audit_path,
                request=request,
                caller=caller,
                apply_run_id=apply_run_id,
            )
    except Exception:
        _append_jsonl(audit_path, _audit_base(request, caller, operation_key, "rejected"))
        raise

    response = {
        "request_id": request["request_id"],
        "caller": caller,
        "operation": operation_key,
        "mode": request["mode"],
        "status": "ok",
        "result": result,
    }
    if operation_key not in {
        "swamp.start_github_cloudflare_repository_apply",
        "swamp.approve_github_cloudflare_repository_apply",
    }:
        _append_jsonl(audit_path, _audit_base(request, caller, operation_key, "ok"))
    return response


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    required_fields = {"request_id", "integration", "operation", "arguments", "mode"}
    unexpected = set(payload) - required_fields
    if unexpected:
        raise BrokerError("unexpected request fields")
    missing = required_fields - set(payload)
    if missing:
        raise BrokerError("missing request fields")
    _uuid(payload["request_id"], "request_id")
    integration = payload["integration"]
    operation = payload["operation"]
    if (integration, operation) not in ALLOWED_OPERATIONS:
        raise BrokerError("operation is not allowed")
    mode = payload["mode"]
    apply_operations = {
        "start_github_cloudflare_repository_apply",
        "approve_github_cloudflare_repository_apply",
    }
    expected_mode = "apply" if operation in apply_operations else "plan"
    if mode != expected_mode:
        if expected_mode == "plan":
            raise BrokerError("apply mode is not available for read-only operations")
        raise BrokerError("apply operation requires mode=apply")
    return {
        "request_id": payload["request_id"],
        "integration": integration,
        "operation": operation,
        "arguments": payload["arguments"],
        "mode": mode,
    }
