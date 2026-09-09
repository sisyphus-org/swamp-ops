"""Typed GitHub/Swamp ingress over the exact-session Kanban bus."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .route import SourceContext, validate_source_context


OWNER_PROFILE = "default"
PLUGIN_ROOT = Path(__file__).resolve().parent
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CREDENTIAL_SHAPES = (
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:SWAMP_API_KEY|GH_TOKEN|GITHUB_TOKEN)\s*[=:]", re.IGNORECASE),
)
OPERATIONS = {
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
APPLY_OPERATIONS = {
    "start_github_cloudflare_repository_apply",
    "approve_github_cloudflare_repository_apply",
    "start_linear_destructive_owner_approval_attest",
    "approve_linear_destructive_owner_approval_attest",
    "approve_linear_delete_preview",
    "approve_linear_bulk_preview",
}
OWNER_ONLY_OPERATIONS = {
    "approve_github_cloudflare_repository_apply",
    "approve_linear_destructive_owner_approval_attest",
    "approve_linear_delete_preview",
    "approve_linear_bulk_preview",
}
EXPECTED_WORKER_CONTRACT = {
    "profile": "operations-manager",
    "tool": "om_ops_execute",
    "mode": "execute_verify_read_back",
    "completion": "tool_completes_current_kanban_task",
}


class OperationsRouteError(RuntimeError):
    """The request or its persisted route violates the bounded contract."""


@dataclass(frozen=True)
class ParsedOperationsRequest:
    command: dict[str, Any]


UUIDFactory = Callable[[], str]


def _uuid4() -> str:
    return str(uuid.uuid4())


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _owner_identity() -> dict[str, str]:
    try:
        identity = json.loads(
            (PLUGIN_ROOT / "operations_owner_identity.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationsRouteError("operations owner identity contract is unavailable") from exc
    if (
        not isinstance(identity, dict)
        or set(identity) != {"source", "user_id", "caller"}
        or identity.get("source") != "telegram"
        or identity.get("caller") != "owner"
        or not isinstance(identity.get("user_id"), str)
        or not identity["user_id"].isdigit()
    ):
        raise OperationsRouteError("operations owner identity contract is invalid")
    return identity


def _caller(source: SourceContext) -> str:
    if source.profile == OWNER_PROFILE:
        if source.user_id != _owner_identity()["user_id"]:
            raise OperationsRouteError("default operations routing requires authenticated owner")
        return "owner"
    return source.profile


def validate_operations_request(payload: Any) -> dict[str, Any]:
    required = {"request_id", "integration", "operation", "arguments", "mode"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise OperationsRouteError("operations request fields are invalid")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or UUID.fullmatch(request_id) is None:
        raise OperationsRouteError("request_id must be a UUID")
    integration = payload.get("integration")
    operation = payload.get("operation")
    if (integration, operation) not in OPERATIONS:
        raise OperationsRouteError("operation is outside the bounded allowlist")
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict) or len(arguments) > 6:
        raise OperationsRouteError("arguments must be a bounded object")
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(serialized.encode("utf-8")) > 32_768:
        raise OperationsRouteError("operations request is too large")
    if any(pattern.search(serialized) for pattern in CREDENTIAL_SHAPES):
        raise OperationsRouteError("operations request contains credential-shaped data")
    expected_mode = "apply" if operation in APPLY_OPERATIONS else "plan"
    if payload.get("mode") != expected_mode:
        raise OperationsRouteError(f"operation requires mode={expected_mode}")
    return {
        "request_id": request_id,
        "integration": integration,
        "operation": operation,
        "arguments": arguments,
        "mode": expected_mode,
    }


def parse_operations_request(
    request: Any,
    *,
    source: SourceContext,
    uuid_factory: UUIDFactory = _uuid4,
) -> ParsedOperationsRequest:
    validated = validate_operations_request(request)
    caller = _caller(source)
    if validated["operation"] in OWNER_ONLY_OPERATIONS and caller != "owner":
        raise OperationsRouteError("operation requires authenticated owner")
    semantic = {
        "caller": caller,
        "integration": validated["integration"],
        "operation": validated["operation"],
        "arguments": validated["arguments"],
        "mode": validated["mode"],
    }
    command = {
        "schema_version": "operations-command.v1",
        "command_id": uuid_factory(),
        "idempotency_key": f"operations:v1:{_canonical_hash(semantic)[:32]}",
        "source_profile": source.profile,
        "source_session_id": source.session_id,
        "caller": caller,
        "request": validated,
    }
    return ParsedOperationsRequest(command)


def delivery_key(mutation_key: str, source: SourceContext) -> str:
    identity = {
        "mutation_key": mutation_key,
        "profile": source.profile,
        "platform": source.platform,
        "chat_id": source.chat_id,
        "user_id": source.user_id,
        "thread_id": source.thread_id,
        "session_id": source.session_id,
    }
    return f"operations-delivery:v1:{_canonical_hash(identity)[:32]}"


def build_operations_task_body(command: dict[str, Any]) -> str:
    return json.dumps(
        {
            "schema_version": "operations-kanban-task.v1",
            "command": command,
            "worker_contract": EXPECTED_WORKER_CONTRACT,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _load_completed(
    task: dict[str, Any], command: dict[str, Any], key: str, source: SourceContext
) -> dict[str, Any]:
    try:
        envelope = json.loads(task.get("body", ""))
        result = json.loads(task.get("result", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise OperationsRouteError("completed operations task is malformed") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schema_version", "command", "worker_contract"}
        or envelope.get("schema_version") != "operations-kanban-task.v1"
        or envelope.get("worker_contract") != EXPECTED_WORKER_CONTRACT
        or not isinstance(envelope.get("command"), dict)
    ):
        raise OperationsRouteError("completed operations task has an invalid envelope")
    persisted = envelope["command"]
    if any(
        persisted.get(field) != command.get(field)
        for field in (
            "schema_version",
            "idempotency_key",
            "source_profile",
            "source_session_id",
            "caller",
        )
    ):
        raise OperationsRouteError("completed operations replay does not match its command")
    persisted_request = persisted.get("request")
    incoming_request = command.get("request")
    if (
        not isinstance(persisted_request, dict)
        or not isinstance(incoming_request, dict)
        or any(
            persisted_request.get(field) != incoming_request.get(field)
            for field in ("integration", "operation", "arguments", "mode")
        )
    ):
        raise OperationsRouteError("completed operations replay does not match its command")
    expected_result_fields = {
        "schema_version",
        "command_id",
        "idempotency_key",
        "source_profile",
        "caller",
        "request_id",
        "integration",
        "operation",
        "mode",
        "status",
        "result",
        "verified",
    }
    if (
        task.get("idempotency_key") != key
        or task.get("session_id") != source.session_id
        or not isinstance(result, dict)
        or set(result) != expected_result_fields
        or result.get("schema_version") != "operations-result.v1"
        or result.get("verified") is not True
        or result.get("status") != "ok"
        or result.get("command_id") != persisted.get("command_id")
        or result.get("idempotency_key") != persisted.get("idempotency_key")
        or result.get("source_profile") != persisted.get("source_profile")
        or result.get("caller") != persisted.get("caller")
        or result.get("request_id") != persisted["request"].get("request_id")
        or result.get("integration") != persisted["request"].get("integration")
        or result.get("operation")
        != f"{persisted['request'].get('integration')}.{persisted['request'].get('operation')}"
        or result.get("mode") != persisted["request"].get("mode")
        or not isinstance(result.get("result"), (dict, list))
    ):
        raise OperationsRouteError("completed operations result is not bound to its command")
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if len(serialized.encode("utf-8")) > 65_536 or any(
        pattern.search(serialized) for pattern in CREDENTIAL_SHAPES
    ):
        raise OperationsRouteError("completed operations result contains unsafe data")
    return {
        "status": "completed",
        "operation": result["operation"],
        "result": result["result"],
    }


def route_operations_request(
    request: Any,
    *,
    source: SourceContext,
    board: Any,
    uuid_factory: UUIDFactory = _uuid4,
) -> dict[str, Any]:
    validate_source_context(source)
    command = parse_operations_request(
        request, source=source, uuid_factory=uuid_factory
    ).command
    key = delivery_key(command["idempotency_key"], source)
    task, created = board.get_or_create_task(
        key,
        title=(
            f"Operations {command['request']['integration']}."
            f"{command['request']['operation']}"
        ),
        body=build_operations_task_body(command),
        assignee="operations-manager",
        skills=["operations-manager-worker"],
        triage=True,
        idempotency_key=key,
        session_id=source.session_id,
        max_runtime_seconds=300,
    )
    if task.get("idempotency_key") != key or task.get("session_id") != source.session_id:
        raise OperationsRouteError("operations task is not bound to the exact source session")
    if not created:
        status = task.get("status")
        if status == "done":
            return _load_completed(task, command, key, source)
        if status == "blocked":
            return {
                "status": "blocked",
                "message": "GitHub or Swamp operation failed safely.",
            }
        if status in {"todo", "ready", "running", "review"}:
            return {"status": "queued"}
        if status != "triage":
            raise OperationsRouteError("operations task has an unsupported state")
    if task.get("status") != "triage":
        raise OperationsRouteError("new operations task did not remain in triage")
    board.set_wake_route(task["id"], source)
    if board.audit_route(task["id"], source).get("result") != "pass":
        raise OperationsRouteError("operations route audit failed; task remains in triage")
    board.release(task["id"], "exact operations source-session wake route verified")
    return {"status": "queued"}
