from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from . import linear_approval_contract as linear_approval


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
    ("swamp", "plan_linear_destructive_owner_approval"),
    ("swamp", "start_linear_destructive_owner_approval_attest"),
    ("swamp", "approve_linear_destructive_owner_approval_attest"),
    ("swamp", "approve_linear_delete_preview"),
    ("swamp", "approve_linear_bulk_preview"),
    ("swamp", "get_result"),
}

PLAN_MODEL = "github-cloudflare-repo-bootstrap"
PLAN_WORKFLOW = "github-cloudflare-repo-bootstrap"
APPLY_WORKFLOW = "github-cloudflare-repo-bootstrap-apply"
REPOSITORY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,54}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LINEAR_DELETE_APPROVAL_FIELDS = {
    "workflow", "model", "run_id", "artifact_version", "checksum",
    "intent_hash", "before_state_hash", "expires_at",
}
_LINEAR_DELETE_THREAD_LOCKS: dict[str, threading.Lock] = {}
_LINEAR_DELETE_THREAD_LOCKS_GUARD = threading.Lock()


def _validate_linear_delete_granted_policy(
    granted_policy: Any,
    preview: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    approval = (
        granted_policy.get("approval") if isinstance(granted_policy, dict) else None
    )
    version = approval.get("artifact_version") if isinstance(approval, dict) else None
    if (
        not isinstance(granted_policy, dict)
        or set(granted_policy) != {"mode", "approval"}
        or granted_policy.get("mode") != "owner_approved"
        or not isinstance(approval, dict)
        or set(approval) != _LINEAR_DELETE_APPROVAL_FIELDS
        or approval.get("workflow") != linear_approval.ATTEST_WORKFLOW
        or approval.get("model") != linear_approval.ATTEST_MODEL
        or not isinstance(approval.get("run_id"), str)
        or linear_approval.UUID.fullmatch(approval["run_id"]) is None
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or any(
            not isinstance(approval.get(field), str)
            or linear_approval.SHA256.fullmatch(approval[field]) is None
            for field in ("checksum", "intent_hash", "before_state_hash")
        )
        or approval.get("intent_hash")
        != linear_approval.canonical_sha256(preview.get("approval_intent"))
        or approval.get("before_state_hash") != preview.get("before_state_hash")
        or approval.get("expires_at") != preview.get("expires_at")
    ):
        raise BrokerError("Linear delete approval grant binding is invalid")
    try:
        linear_approval.validate_expiry_window(
            approval["expires_at"], now or datetime.now(timezone.utc)
        )
    except linear_approval.ContractError as exc:
        raise BrokerError("Linear delete approval grant is expired or invalid") from exc
    return granted_policy


@contextmanager
def _linear_delete_confirmation_lock(reference: str, root: Path | None):
    with _LINEAR_DELETE_THREAD_LOCKS_GUARD:
        thread_lock = _LINEAR_DELETE_THREAD_LOCKS.setdefault(reference, threading.Lock())
    with thread_lock:
        if root is None:
            yield
            return
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        lock_path = root / (reference.rsplit(":", 1)[-1] + ".lock")
        with lock_path.open("a", encoding="utf-8") as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        "swamp.plan_linear_destructive_owner_approval": {
            "intent", "before_state_hash", "expires_at"
        },
        "swamp.start_linear_destructive_owner_approval_attest": {
            "intent", "before_state_hash", "expires_at", "plan_run_id",
            "plan_checksum", "plan_artifact_version",
        },
        "swamp.approve_linear_destructive_owner_approval_attest": {"attest_run_id"},
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
    if operation_key == "swamp.plan_linear_destructive_owner_approval":
        if policy.get("swamp", {}).get("linearDestructiveApprovalPlanWorkflow") != linear_approval.PLAN_WORKFLOW:
            raise BrokerError("Linear destructive approval plan workflow is not allowed")
        try:
            encoded_intent = linear_approval.encode_intent(arguments.get("intent"))
            before_state_hash = arguments.get("before_state_hash")
            if not isinstance(before_state_hash, str) or linear_approval.SHA256.fullmatch(before_state_hash) is None:
                raise linear_approval.ContractError("before_state_hash must be SHA-256")
            expires_at = arguments.get("expires_at")
            linear_approval.parse_expiry(expires_at)
        except linear_approval.ContractError as exc:
            raise BrokerError(str(exc)) from exc
        return [
            "swamp", "workflow", "run", linear_approval.PLAN_WORKFLOW,
            "--input", f"intent={encoded_intent}",
            "--input", f"beforeStateHash={before_state_hash}",
            "--input", f"expiresAt={expires_at}", "--json",
        ]
    if operation_key == "swamp.start_linear_destructive_owner_approval_attest":
        if policy.get("swamp", {}).get("linearDestructiveApprovalAttestWorkflow") != linear_approval.ATTEST_WORKFLOW:
            raise BrokerError("Linear destructive approval attestation workflow is not allowed")
        try:
            encoded_intent = linear_approval.encode_intent(arguments.get("intent"))
            before_state_hash = arguments.get("before_state_hash")
            if not isinstance(before_state_hash, str) or linear_approval.SHA256.fullmatch(before_state_hash) is None:
                raise linear_approval.ContractError("before_state_hash must be SHA-256")
            expires_at = arguments.get("expires_at")
            linear_approval.parse_expiry(expires_at)
        except linear_approval.ContractError as exc:
            raise BrokerError(str(exc)) from exc
        plan_run_id = _uuid(arguments.get("plan_run_id"), "plan_run_id")
        plan_checksum = _checksum(arguments.get("plan_checksum"))
        plan_version = arguments.get("plan_artifact_version")
        if not isinstance(plan_version, int) or isinstance(plan_version, bool) or plan_version < 1:
            raise BrokerError("plan_artifact_version must be a positive integer")
        return [
            "swamp", "workflow", "run", linear_approval.ATTEST_WORKFLOW,
            "--input", f"intent={encoded_intent}",
            "--input", f"beforeStateHash={before_state_hash}",
            "--input", f"expiresAt={expires_at}",
            "--input", f"planRunId={plan_run_id}",
            "--input", f"planArtifactVersion:json={plan_version}",
            "--input", f"planChecksum={plan_checksum}", "--json",
        ]
    if operation_key == "swamp.approve_linear_destructive_owner_approval_attest":
        if policy.get("swamp", {}).get("linearDestructiveApprovalAttestWorkflow") != linear_approval.ATTEST_WORKFLOW:
            raise BrokerError("Linear destructive approval attestation workflow is not allowed")
        attest_run_id = _uuid(arguments.get("attest_run_id"), "attest_run_id")
        return [
            "swamp", "workflow", "approve", linear_approval.ATTEST_WORKFLOW,
            "approve-linear-destructive-intent", "--run", attest_run_id, "--json",
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


def _load_linear_approval_artifact(
    *,
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
    model: str,
    artifact_version: int,
    run_id: str,
) -> dict[str, Any]:
    payload = _json_command(
        runner,
        [
            "swamp", "data", "get", model, "result", "--version",
            str(artifact_version), "--json",
        ],
        workspace=workspace,
        timeout=60,
        error_prefix="Linear owner approval artifact retrieval",
    )
    owner = payload.get("ownerDefinition")
    content = payload.get("content")
    if (
        payload.get("modelName") != model
        or payload.get("name") != "result"
        or payload.get("version") != artifact_version
        or not isinstance(owner, dict)
        or owner.get("workflowRunId") != run_id
        or not isinstance(content, dict)
        or content.get("exitCode") != 0
        or not isinstance(content.get("stdout"), str)
    ):
        raise BrokerError("Linear owner approval artifact provenance is invalid")
    try:
        result = json.loads(content["stdout"])
    except json.JSONDecodeError as exc:
        raise BrokerError("Linear owner approval artifact content is invalid JSON") from exc
    if not isinstance(result, dict):
        raise BrokerError("Linear owner approval artifact content is invalid")
    return result


def _linear_plan_result(
    workflow_result: dict[str, Any],
    *,
    arguments: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
) -> dict[str, Any]:
    run_id, version = _extract_result_version(workflow_result)
    result = _load_linear_approval_artifact(
        runner=runner,
        workspace=workspace,
        model=linear_approval.PLAN_MODEL,
        artifact_version=version,
        run_id=run_id,
    )
    try:
        expected = linear_approval.build_plan(
            arguments["intent"],
            arguments["before_state_hash"],
            arguments["expires_at"],
        )
    except linear_approval.ContractError as exc:
        raise BrokerError(str(exc)) from exc
    if result != expected or not linear_approval.verify_artifact_checksum(result):
        raise BrokerError("Linear owner approval plan artifact binding is invalid")
    return {"workflowRunId": run_id, "artifactVersion": version, "plan": result}


def _register_linear_attest_gate(
    audit_path: Path | None,
    *,
    request: dict[str, Any],
    caller: str,
    attest_run_id: str,
) -> None:
    if audit_path is None:
        raise BrokerError("Linear owner approval attest requires an immutable audit path")
    arguments = request["arguments"]
    _append_jsonl(
        audit_path,
        {
            **_audit_base(request, caller, request["integration"] + "." + request["operation"], "awaiting_approval"),
            "event": "linear_owner_approval_gate",
            "attest_run_id": attest_run_id,
            "intent": linear_approval.encode_intent(arguments["intent"]),
            "before_state_hash": arguments["before_state_hash"],
            "expires_at": arguments["expires_at"],
            "plan_run_id": arguments["plan_run_id"],
            "plan_checksum": arguments["plan_checksum"],
            "plan_artifact_version": arguments["plan_artifact_version"],
        },
    )


def _linear_approval_step_status(history: dict[str, Any]) -> str | None:
    for job in history.get("jobs", []):
        if not isinstance(job, dict) or job.get("name") != "attest":
            continue
        for step in job.get("steps", []):
            if isinstance(step, dict) and step.get("name") == "approve-linear-destructive-intent":
                status = step.get("status")
                return status if isinstance(status, str) else None
    return None


def _approve_linear_attestation(
    audit_path: Path | None,
    *,
    request: dict[str, Any],
    caller: str,
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
    approve_command: list[str],
) -> dict[str, Any]:
    if caller != "owner":
        raise BrokerError("Linear destructive approval requires authenticated owner")
    if audit_path is None or not audit_path.exists():
        raise BrokerError("Linear owner approval run is not registered in immutable audit")
    attest_run_id = request["arguments"]["attest_run_id"]
    lock_path = audit_path.with_name(audit_path.name + ".linear-approval.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        gate: dict[str, Any] | None = None
        completed = False
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BrokerError("immutable audit contains invalid JSON") from exc
            if record.get("attest_run_id") != attest_run_id:
                continue
            if record.get("event") == "linear_owner_approval_gate":
                gate = record
            elif record.get("event") == "linear_owner_approval_result":
                completed = True
        if gate is None:
            raise BrokerError("Linear owner approval run is not registered")
        if completed:
            raise BrokerError("Linear owner approval run was already approved")
        history = _json_command(
            runner,
            ["swamp", "workflow", "history", "get", attest_run_id, "--json"],
            workspace=workspace,
            timeout=60,
            error_prefix="Linear owner approval history lookup",
        )
        expected_inputs = {
            "intent": gate.get("intent"),
            "beforeStateHash": gate.get("before_state_hash"),
            "expiresAt": gate.get("expires_at"),
            "planRunId": gate.get("plan_run_id"),
            "planArtifactVersion": gate.get("plan_artifact_version"),
            "planChecksum": gate.get("plan_checksum"),
        }
        if (
            history.get("id") != attest_run_id
            or history.get("workflowName") != linear_approval.ATTEST_WORKFLOW
            or history.get("inputs") != expected_inputs
            or history.get("status") != "suspended"
        ):
            raise BrokerError("Linear owner approval run is not suspended at the exact approval gate")
        try:
            linear_approval.validate_expiry_window(
                gate["expires_at"], datetime.now(timezone.utc)
            )
        except (KeyError, linear_approval.ContractError) as exc:
            raise BrokerError("Linear owner approval gate binding is invalid") from exc
        approval_status = _linear_approval_step_status(history)
        if approval_status == "waiting_approval":
            approved = _json_command(
                runner,
                approve_command,
                workspace=workspace,
                timeout=60,
                error_prefix="Linear owner approval",
            )
            if approved.get("runId") != attest_run_id:
                raise BrokerError("Linear owner approval returned the wrong run")
        elif approval_status != "succeeded":
            raise BrokerError(
                "Linear owner approval run is not suspended at the exact approval gate"
            )
        result = _json_command(
            runner,
            [
                "swamp", "workflow", "resume", linear_approval.ATTEST_WORKFLOW,
                "--run", attest_run_id, "--json",
            ],
            workspace=workspace,
            timeout=120,
            error_prefix="Linear owner approval attestation resume",
        )
        if result.get("id") != attest_run_id or result.get("status") != "succeeded":
            raise BrokerError("Linear owner approval attestation did not succeed")
        result_run_id, artifact_version = _extract_result_version(result)
        if result_run_id != attest_run_id:
            raise BrokerError("Linear owner approval attestation returned wrong run")
        attestation = _load_linear_approval_artifact(
            runner=runner,
            workspace=workspace,
            model=linear_approval.ATTEST_MODEL,
            artifact_version=artifact_version,
            run_id=attest_run_id,
        )
        try:
            intent = linear_approval.decode_intent(str(gate.get("intent")))
            expected = {
                "schemaVersion": linear_approval.ATTESTATION_SCHEMA_VERSION,
                "mode": "attestation",
                "decision": "owner_approved",
                "workflow": linear_approval.ATTEST_WORKFLOW,
                "model": linear_approval.ATTEST_MODEL,
                "plan": {
                    "workflow": linear_approval.PLAN_WORKFLOW,
                    "model": linear_approval.PLAN_MODEL,
                    "runId": gate["plan_run_id"],
                    "artifactVersion": gate["plan_artifact_version"],
                    "checksum": gate["plan_checksum"],
                },
                "intent": intent,
                "intentHash": linear_approval.canonical_sha256(intent),
                "beforeStateHash": gate["before_state_hash"],
                "expiresAt": gate["expires_at"],
            }
            expected["checksum"] = linear_approval.artifact_checksum(expected)
        except (KeyError, linear_approval.ContractError) as exc:
            raise BrokerError("Linear owner approval gate binding is invalid") from exc
        if attestation != expected or not linear_approval.verify_artifact_checksum(attestation):
            raise BrokerError("Linear owner approval attestation binding is invalid")
        policy_reference = {
            "mode": "owner_approved",
            "approval": {
                "workflow": linear_approval.ATTEST_WORKFLOW,
                "model": linear_approval.ATTEST_MODEL,
                "run_id": attest_run_id,
                "artifact_version": artifact_version,
                "checksum": attestation["checksum"],
                "intent_hash": attestation["intentHash"],
                "before_state_hash": attestation["beforeStateHash"],
                "expires_at": attestation["expiresAt"],
            },
        }
        _append_jsonl(
            audit_path,
            {
                **_audit_base(request, caller, request["integration"] + "." + request["operation"], "approved"),
                "event": "linear_owner_approval_result",
                "attest_run_id": attest_run_id,
                "attestation_checksum": attestation["checksum"],
            },
        )
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return {"status": "succeeded", "policy": policy_reference}


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


def _task_value(task: Any, field: str) -> Any:
    return task.get(field) if isinstance(task, dict) else getattr(task, field, None)


def _delete_preview_reference(
    task_id: str,
    session_id: str,
    result: dict[str, Any],
    source: dict[str, Any],
) -> str:
    binding = {
        "task_id": task_id,
        "session_id": session_id,
        "source": source,
        "approval_intent": result.get("approval_intent"),
        "before_state_hash": result.get("before_state_hash"),
        "expires_at": result.get("expires_at"),
    }
    return "linear-delete-approval:v1:" + linear_approval.canonical_sha256(binding)


def _bulk_preview_reference(
    task_id: str,
    session_id: str,
    result: dict[str, Any],
    source: dict[str, Any],
) -> str:
    binding = {
        "task_id": task_id,
        "session_id": session_id,
        "source": source,
        "approval_intent": result.get("approval_intent"),
        "before_state_hash": result.get("before_state_hash"),
        "expires_at": result.get("expires_at"),
    }
    return "linear-bulk-approval:v1:" + linear_approval.canonical_sha256(binding)


def _load_linear_delete_preview(
    reference: str,
    session_id: str,
    *,
    policy: dict[str, Any],
    kb: Any = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Load and revalidate one protected PM preview from the shared Kanban DB."""
    bulk_preview = bool(
        re.fullmatch(r"linear-bulk-approval:v1:[0-9a-f]{64}", reference)
    )
    if not bulk_preview and re.fullmatch(
        r"linear-delete-approval:v1:[0-9a-f]{64}", reference
    ) is None:
        raise BrokerError("Linear approval preview reference is invalid")
    preview_kind = (
        "linear_bulk_preview_ready"
        if bulk_preview
        else "linear_delete_preview_ready"
    )
    grant_kind = (
        "linear_bulk_approval_granted"
        if bulk_preview
        else "linear_delete_approval_granted"
    )
    attempt_kind = (
        "linear_bulk_approval_attempted"
        if bulk_preview
        else "linear_delete_approval_attempted"
    )
    preview_operation = (
        "preview_bulk_linear_operations"
        if bulk_preview
        else "preview_delete_linear_entity"
    )
    if kb is None:
        from hermes_cli import kanban_db as kb_module

        kb = kb_module
    conn = kb.connect(board="default")
    try:
        rows = conn.execute(
            "SELECT task_id, payload FROM task_events WHERE kind = ? AND payload LIKE ?",
            (preview_kind, f'%\"approval_reference\": \"{reference}\"%'),
        ).fetchall()
        if len(rows) != 1:
            raise BrokerError("Linear delete preview is missing or ambiguous")
        task_id = rows[0]["task_id"]
        preview_event = json.loads(rows[0]["payload"])
        task = kb.get_task(conn, task_id)
        grant_rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, grant_kind),
        ).fetchall()
        attempt_rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, attempt_kind),
        ).fetchall()
    finally:
        conn.close()
    if task is None:
        raise BrokerError("Linear delete preview task is missing")
    try:
        envelope = json.loads(_task_value(task, "body"))
        result = json.loads(_task_value(task, "result"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise BrokerError("Linear delete preview task is malformed") from exc
    source = preview_event.get("source") if isinstance(preview_event, dict) else None
    owner_identities = policy.get("ownerIdentities", [])
    owner_user_ids = {
        str(item.get("user_id"))
        for item in owner_identities
        if isinstance(item, dict)
        and item.get("source") == "telegram"
        and item.get("caller") == "owner"
    }
    command = envelope.get("command") if isinstance(envelope, dict) else None
    if (
        _task_value(task, "status") != "done"
        or _task_value(task, "assignee") != "project-manager"
        or _task_value(task, "session_id") != session_id
        or not isinstance(source, dict)
        or preview_event.get("schema_version")
        != ("linear-bulk-preview.v1" if bulk_preview else "linear-delete-preview.v1")
        or source.get("session_id") != session_id
        or source.get("platform") != "telegram"
        or source.get("user_id") not in owner_user_ids
        or not isinstance(command, dict)
        or command.get("operation") != preview_operation
        or command.get("source_profile") != source.get("profile")
        or not isinstance(result, dict)
        or result.get("operation") != preview_operation
        or result.get("verified") is not True
        or result.get("result") != "read"
        or result.get("target") != command.get("target")
        or result.get("approval_intent") != preview_event.get("approval_intent")
        or result.get("before_state_hash") != preview_event.get("before_state_hash")
        or result.get("expires_at") != preview_event.get("expires_at")
        or not isinstance(result.get("before_state_hash"), str)
        or linear_approval.SHA256.fullmatch(result["before_state_hash"]) is None
        or preview_event.get("approval_reference") != reference
        or (
            _bulk_preview_reference(task_id, session_id, result, source)
            if bulk_preview
            else _delete_preview_reference(task_id, session_id, result, source)
        )
        != reference
    ):
        raise BrokerError("Linear delete preview protected binding is invalid")
    current = now or datetime.now(timezone.utc)
    try:
        linear_approval.validate_expiry_window(
            result["expires_at"], current
        )
    except linear_approval.ContractError as exc:
        raise BrokerError("Linear delete preview is expired or has an invalid TTL") from exc
    if len(grant_rows) > 1:
        raise BrokerError("Linear delete approval grant is ambiguous")
    if len(attempt_rows) > 1:
        raise BrokerError("Linear delete approval attempt is ambiguous")
    approval_attempted = False
    if attempt_rows:
        try:
            attempt = json.loads(attempt_rows[0]["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise BrokerError("Linear delete approval attempt is malformed") from exc
        if attempt != {
            "schema_version": (
                "linear-bulk-approval-attempt.v1"
                if bulk_preview
                else "linear-delete-approval-attempt.v1"
            ),
            "approval_reference": reference,
            "preview_hash": linear_approval.canonical_sha256(preview_event),
        }:
            raise BrokerError("Linear delete approval attempt binding is invalid")
        approval_attempted = True
    granted_policy = None
    if grant_rows:
        try:
            grant = json.loads(grant_rows[0]["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise BrokerError("Linear delete approval grant is malformed") from exc
        if (
            not isinstance(grant, dict)
            or set(grant)
            != {"schema_version", "approval_reference", "preview_hash", "policy"}
            or grant.get("schema_version")
            != (
                "linear-bulk-approval.v1"
                if bulk_preview
                else "linear-delete-approval.v1"
            )
            or grant.get("approval_reference") != reference
            or grant.get("preview_hash")
            != linear_approval.canonical_sha256(preview_event)
        ):
            raise BrokerError("Linear delete approval grant binding is invalid")
        granted_policy = _validate_linear_delete_granted_policy(
            grant.get("policy"),
            {
                "approval_intent": result["approval_intent"],
                "before_state_hash": result["before_state_hash"],
                "expires_at": result["expires_at"],
            },
            now=current,
        )
    loaded = {
        "task_id": task_id,
        "session_id": session_id,
        "source_profile": source["profile"],
        "approval_reference": reference,
        "approval_intent": result["approval_intent"],
        "before_state_hash": result["before_state_hash"],
        "expires_at": result["expires_at"],
        "preview_hash": linear_approval.canonical_sha256(preview_event),
    }
    if granted_policy is not None:
        loaded["granted_policy"] = granted_policy
    if approval_attempted:
        loaded["approval_attempted"] = True
    return loaded


def _issue_linear_delete_attestation(
    preview: dict[str, Any],
    *,
    policy: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
    audit_path: Path | None,
) -> dict[str, Any]:
    """Run the dedicated Swamp plan/start/approve sequence inside the broker."""
    def invoke(operation: str, arguments: dict[str, Any], mode: str) -> dict[str, Any]:
        return execute_request(
            {
                "request_id": str(uuid4()),
                "integration": "swamp",
                "operation": operation,
                "arguments": arguments,
                "mode": mode,
            },
            caller="owner",
            policy=policy,
            runner=runner,
            workspace=workspace,
            audit_path=audit_path,
        )

    base = {
        "intent": preview["approval_intent"],
        "before_state_hash": preview["before_state_hash"],
        "expires_at": preview["expires_at"],
    }
    planned = invoke("plan_linear_destructive_owner_approval", base, "plan")["result"]
    plan = planned.get("plan") if isinstance(planned, dict) else None
    if not isinstance(plan, dict):
        raise BrokerError("Linear delete approval plan is invalid")
    started = invoke(
        "start_linear_destructive_owner_approval_attest",
        {
            **base,
            "plan_run_id": planned["workflowRunId"],
            "plan_checksum": plan["checksum"],
            "plan_artifact_version": planned["artifactVersion"],
        },
        "apply",
    )["result"]
    attest_run_id = started.get("id") if isinstance(started, dict) else None
    if not isinstance(attest_run_id, str):
        raise BrokerError("Linear delete approval attestation run is invalid")
    approved = invoke(
        "approve_linear_destructive_owner_approval_attest",
        {"attest_run_id": attest_run_id},
        "apply",
    )["result"]
    granted = approved.get("policy") if isinstance(approved, dict) else None
    if not isinstance(granted, dict) or granted.get("mode") != "owner_approved":
        raise BrokerError("Linear delete approval attestation policy is invalid")
    return granted


def _record_linear_delete_approval(
    preview: dict[str, Any],
    granted_policy: dict[str, Any],
    *,
    kb: Any = None,
) -> dict[str, Any]:
    """Persist one idempotent protected grant on the exact preview task."""
    if kb is None:
        from hermes_cli import kanban_db as kb_module

        kb = kb_module
    task_id = preview.get("task_id")
    bulk_preview = bool(
        re.fullmatch(
            r"linear-bulk-approval:v1:[0-9a-f]{64}",
            str(preview.get("approval_reference") or ""),
        )
    )
    grant_kind = (
        "linear_bulk_approval_granted"
        if bulk_preview
        else "linear_delete_approval_granted"
    )
    attempt_kind = (
        "linear_bulk_approval_attempted"
        if bulk_preview
        else "linear_delete_approval_attempted"
    )
    payload = {
        "schema_version": (
            "linear-bulk-approval.v1"
            if bulk_preview
            else "linear-delete-approval.v1"
        ),
        "approval_reference": preview.get("approval_reference"),
        "preview_hash": preview.get("preview_hash"),
        "policy": granted_policy,
    }
    conn = kb.connect(board="default")
    try:
        with kb.write_txn(conn):
            task = kb.get_task(conn, task_id)
            if (
                task is None
                or _task_value(task, "status") != "done"
                or _task_value(task, "session_id") != preview.get("session_id")
            ):
                raise BrokerError("Linear delete approval task binding is invalid")
            attempts = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                (task_id, attempt_kind),
            ).fetchall()
            expected_attempt = {
                "schema_version": (
                    "linear-bulk-approval-attempt.v1"
                    if bulk_preview
                    else "linear-delete-approval-attempt.v1"
                ),
                "approval_reference": preview.get("approval_reference"),
                "preview_hash": preview.get("preview_hash"),
            }
            if (
                len(attempts) != 1
                or json.loads(attempts[0]["payload"]) != expected_attempt
            ):
                raise BrokerError("Linear delete approval attempt is missing or invalid")
            rows = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                (task_id, grant_kind),
            ).fetchall()
            if rows:
                if len(rows) != 1 or json.loads(rows[0]["payload"]) != payload:
                    raise BrokerError("Linear delete approval replay conflicts")
                return {"ready": True}
            kb._append_event(
                conn,
                task_id,
                grant_kind,
                payload,
            )
    finally:
        conn.close()
    return {"ready": True}


def _record_linear_delete_approval_attempt(
    preview: dict[str, Any],
    *,
    kb: Any = None,
) -> dict[str, Any]:
    """Consume one opaque reference before any external approval side effect."""
    if kb is None:
        from hermes_cli import kanban_db as kb_module

        kb = kb_module
    task_id = preview.get("task_id")
    bulk_preview = bool(
        re.fullmatch(
            r"linear-bulk-approval:v1:[0-9a-f]{64}",
            str(preview.get("approval_reference") or ""),
        )
    )
    grant_kind = (
        "linear_bulk_approval_granted"
        if bulk_preview
        else "linear_delete_approval_granted"
    )
    attempt_kind = (
        "linear_bulk_approval_attempted"
        if bulk_preview
        else "linear_delete_approval_attempted"
    )
    payload = {
        "schema_version": (
            "linear-bulk-approval-attempt.v1"
            if bulk_preview
            else "linear-delete-approval-attempt.v1"
        ),
        "approval_reference": preview.get("approval_reference"),
        "preview_hash": preview.get("preview_hash"),
    }
    conn = kb.connect(board="default")
    try:
        with kb.write_txn(conn):
            task = kb.get_task(conn, task_id)
            if (
                task is None
                or _task_value(task, "status") != "done"
                or _task_value(task, "session_id") != preview.get("session_id")
            ):
                raise BrokerError("Linear delete approval attempt task binding is invalid")
            grants = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                (task_id, grant_kind),
            ).fetchall()
            if grants:
                raise BrokerError("Linear delete approval was already granted")
            rows = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                (task_id, attempt_kind),
            ).fetchall()
            if rows:
                if len(rows) != 1 or json.loads(rows[0]["payload"]) != payload:
                    raise BrokerError("Linear delete approval attempt conflicts")
                return {"claimed": True}
            kb._append_event(
                conn,
                task_id,
                attempt_kind,
                payload,
            )
    finally:
        conn.close()
    return {"claimed": True}


def execute_request(
    request: dict[str, Any],
    *,
    caller: str,
    policy: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
    audit_path: Path | None = None,
    session_id: str = "",
    preview_loader: Callable[[str, str], dict[str, Any] | None] | None = None,
    attestation_issuer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    approval_recorder: Callable[
        [dict[str, Any], dict[str, Any]], dict[str, Any]
    ] | None = None,
    approval_attempt_recorder: Callable[
        [dict[str, Any]], dict[str, Any]
    ] | None = None,
    approval_lock_root: Path | None = None,
) -> dict[str, Any]:
    operation_key = f"{request['integration']}.{request['operation']}"
    try:
        peer = policy.get("peers", {}).get(caller, {})
        if operation_key not in peer.get("operations", []):
            raise BrokerError("operation is not allowed for caller")
        if operation_key in {
            "swamp.approve_linear_delete_preview",
            "swamp.approve_linear_bulk_preview",
        }:
            bulk_approval = operation_key == "swamp.approve_linear_bulk_preview"
            approval_label = "Linear bulk" if bulk_approval else "Linear delete"
            reference_pattern = (
                r"linear-bulk-approval:v1:[0-9a-f]{64}"
                if bulk_approval
                else r"linear-delete-approval:v1:[0-9a-f]{64}"
            )
            if caller != "owner":
                raise BrokerError(
                    f"{approval_label} approval requires authenticated owner"
                )
            arguments = request.get("arguments")
            reference = (
                arguments.get("approval_reference")
                if isinstance(arguments, dict) and set(arguments) == {"approval_reference"}
                else None
            )
            if (
                not isinstance(reference, str)
                or re.fullmatch(reference_pattern, reference) is None
            ):
                raise BrokerError("Linear delete approval reference is invalid")
            if not session_id:
                raise BrokerError("Linear delete approval requires the exact owner session")
            if audit_path is None:
                raise BrokerError(
                    "Linear delete approval requires an immutable audit path"
                )
            if (
                preview_loader is None
                or attestation_issuer is None
                or approval_recorder is None
                or approval_attempt_recorder is None
            ):
                raise BrokerError("Linear delete approval trusted route is unavailable")
            with _linear_delete_confirmation_lock(reference, approval_lock_root):
                # Reload only after acquiring the per-reference lock so a concurrent
                # confirmation observes and reuses the first caller's recorded grant.
                preview = preview_loader(reference, session_id)
                if not isinstance(preview, dict) or preview.get("session_id") != session_id:
                    raise BrokerError("Linear delete preview is missing or belongs to another session")
                granted_policy = preview.get("granted_policy")
                if granted_policy is None:
                    if preview.get("approval_attempted") is True:
                        raise BrokerError(
                            "Linear delete approval outcome is unknown; request a fresh preview"
                        )
                    claimed = approval_attempt_recorder(preview)
                    if not isinstance(claimed, dict) or claimed.get("claimed") is not True:
                        raise BrokerError(
                            "Linear delete approval attempt was not safely recorded"
                        )
                    granted_policy = attestation_issuer(preview)
                granted_policy = _validate_linear_delete_granted_policy(
                    granted_policy, preview
                )
                recorded = approval_recorder(preview, granted_policy)
                if not isinstance(recorded, dict) or recorded.get("ready") is not True:
                    raise BrokerError("Linear delete approval was not safely recorded")
                _append_jsonl(
                    audit_path,
                    _audit_base(request, caller, operation_key, "ok"),
                )
            return {
                "request_id": request["request_id"],
                "caller": caller,
                "operation": operation_key,
                "mode": request["mode"],
                "status": "ok",
                "result": {
                    "approval_reference": reference,
                    "ready": True,
                },
            }
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
        if operation_key == "swamp.start_linear_destructive_owner_approval_attest":
            if audit_path is None:
                raise BrokerError("Linear owner approval attest requires an immutable audit path")
            try:
                _append_jsonl(
                    audit_path,
                    {
                        **_audit_base(request, caller, operation_key, "preflight"),
                        "event": "linear_owner_approval_preflight",
                    },
                )
            except OSError as exc:
                raise BrokerError("immutable audit path is not writable") from exc
            args = request["arguments"]
            loaded_plan = _load_linear_approval_artifact(
                runner=runner,
                workspace=workspace,
                model=linear_approval.PLAN_MODEL,
                artifact_version=args["plan_artifact_version"],
                run_id=args["plan_run_id"],
            )
            try:
                expected_plan = linear_approval.build_plan(
                    args["intent"], args["before_state_hash"], args["expires_at"]
                )
            except linear_approval.ContractError as exc:
                raise BrokerError(str(exc)) from exc
            if (
                loaded_plan != expected_plan
                or loaded_plan.get("checksum") != args["plan_checksum"]
                or not linear_approval.verify_artifact_checksum(loaded_plan)
            ):
                raise BrokerError("Linear owner approval plan does not match attest request")
        if operation_key == "swamp.approve_linear_destructive_owner_approval_attest":
            result = _approve_linear_attestation(
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
        elif operation_key == "swamp.plan_linear_destructive_owner_approval":
            result = _linear_plan_result(
                result,
                arguments=request["arguments"],
                runner=runner,
                workspace=workspace,
            )
        elif operation_key == "swamp.start_linear_destructive_owner_approval_attest":
            attest_run_id = result.get("id") if isinstance(result, dict) else None
            if not isinstance(attest_run_id, str) or result.get("status") != "suspended":
                raise BrokerError("Linear owner approval workflow did not suspend")
            _uuid(attest_run_id, "attest_run_id")
            _register_linear_attest_gate(
                audit_path,
                request=request,
                caller=caller,
                attest_run_id=attest_run_id,
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
        "swamp.start_linear_destructive_owner_approval_attest",
        "swamp.approve_linear_destructive_owner_approval_attest",
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
        "start_linear_destructive_owner_approval_attest",
        "approve_linear_destructive_owner_approval_attest",
        "approve_linear_delete_preview",
        "approve_linear_bulk_preview",
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
