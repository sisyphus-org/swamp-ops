from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable

from .broker import (
    BrokerError,
    _issue_linear_delete_attestation,
    _load_linear_delete_preview,
    _record_linear_delete_approval,
    _record_linear_delete_approval_attempt,
    execute_request,
    resolve_caller,
    validate_request,
)


PLUGIN_ROOT = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = PLUGIN_ROOT.parents[1]

OPS_BROKER_SCHEMA = {
    "name": "ops_broker",
    "description": (
        "Execute one typed, policy-allowlisted GitHub or Swamp operation. "
        "Caller identity is derived from the authenticated A2A session. Read-only "
        "plans and checksum-bound repository apply/approval operations are available; "
        "shell commands, arbitrary URLs and credential requests are never accepted."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "request_id": {"type": "string", "format": "uuid"},
            "integration": {"type": "string", "enum": ["github", "swamp"]},
            "operation": {
                "type": "string",
                "enum": [
                    "repository_access",
                    "list_pull_requests",
                    "pull_request_checks",
                    "auth_whoami",
                    "validate_model",
                    "validate_workflow",
                    "run_readonly_workflow",
                    "plan_github_cloudflare_repository",
                    "start_github_cloudflare_repository_apply",
                    "approve_github_cloudflare_repository_apply",
                    "plan_linear_destructive_owner_approval",
                    "start_linear_destructive_owner_approval_attest",
                    "approve_linear_destructive_owner_approval_attest",
                    "approve_linear_delete_preview",
                    "approve_linear_bulk_preview",
                    "get_result",
                ],
            },
            "arguments": {"type": "object", "maxProperties": 6},
            "mode": {"type": "string", "enum": ["plan", "apply"]},
        },
        "required": [
            "request_id",
            "integration",
            "operation",
            "arguments",
            "mode",
        ],
    },
}


def default_runner(argv: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _path_from_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def _verify_runtime_workspace(policy: dict[str, Any], workspace: Path) -> None:
    revision_file_value = policy.get("workspaceRevisionFile")
    if not isinstance(revision_file_value, str) or not revision_file_value:
        raise BrokerError("runtime workspace revision attestation is not configured")
    revision_file = Path(revision_file_value).expanduser().resolve()
    expected_revision = revision_file.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        raise BrokerError("runtime workspace revision attestation is invalid")

    head = default_runner(
        ["git", "rev-parse", "HEAD"], cwd=workspace, timeout=10
    )
    if head["returncode"] != 0 or head["stdout"].strip() != expected_revision:
        raise BrokerError("runtime workspace HEAD does not match attestation")
    status = default_runner(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=workspace,
        timeout=10,
    )
    if status["returncode"] != 0 or status["stdout"].strip():
        raise BrokerError("runtime workspace is not clean")
    if (workspace / ".swamp-sources.yaml").exists():
        raise BrokerError("runtime workspace has a local Swamp source override")


def handle_ops_broker(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        request = validate_request(args)
        hermes_home = _path_from_env("HERMES_HOME", Path.home() / ".hermes")
        policy_path = PLUGIN_ROOT / "policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        session_id = str(kwargs.get("session_id") or "")
        owner_identities = policy.get("ownerIdentities", [])
        if not isinstance(owner_identities, list):
            raise BrokerError("owner identities policy must be a list")
        caller = resolve_caller(
            session_id,
            hermes_home / "state.db",
            owner_identities,
        )
        configured_workspace = Path(
            str(policy.get("workspace") or DEFAULT_WORKSPACE)
        ).expanduser().resolve()
        workspace = configured_workspace
        _verify_runtime_workspace(policy, workspace)
        audit_path = _path_from_env(
            "OPS_BROKER_AUDIT",
            hermes_home / "plugin-data" / "ops-broker" / "audit.jsonl",
        )
        preview_loader = kwargs.get("preview_loader") or (
            lambda reference, exact_session: _load_linear_delete_preview(
                reference,
                exact_session,
                policy=policy,
            )
        )
        attestation_issuer = kwargs.get("attestation_issuer") or (
            lambda preview: _issue_linear_delete_attestation(
                preview,
                policy=policy,
                runner=default_runner,
                workspace=workspace,
                audit_path=audit_path,
            )
        )
        approval_recorder = kwargs.get("approval_recorder") or (
            lambda preview, granted: _record_linear_delete_approval(
                preview,
                granted,
            )
        )
        approval_attempt_recorder = kwargs.get("approval_attempt_recorder") or (
            lambda preview: _record_linear_delete_approval_attempt(preview)
        )
        result = execute_request(
            request,
            caller=caller,
            policy=policy,
            runner=default_runner,
            workspace=workspace,
            audit_path=audit_path,
            session_id=session_id,
            preview_loader=preview_loader,
            attestation_issuer=attestation_issuer,
            approval_recorder=approval_recorder,
            approval_attempt_recorder=approval_attempt_recorder,
            approval_lock_root=audit_path.parent / "linear-delete-confirmation-locks",
        )
        return json.dumps(result, sort_keys=True)
    except (
        BrokerError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as exc:
        return json.dumps(
            {"status": "rejected", "error": str(exc)}, sort_keys=True
        )


OM_OPS_EXECUTE_SCHEMA = {
    "name": "om_ops_execute",
    "description": (
        "Execute exactly one persisted operations-command.v1 from the current "
        "Operations Manager Kanban task. Accepts no model-supplied command."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    },
}
EXPECTED_WORKER_CONTRACT = {
    "profile": "operations-manager",
    "tool": "om_ops_execute",
    "mode": "execute_verify_read_back",
    "completion": "tool_completes_current_kanban_task",
}
TASK_ID = re.compile(r"^t_[a-f0-9]{8,}$")
COMMAND_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
COMMAND_KEY = re.compile(r"^operations:v1:[0-9a-f]{32}$")
SOURCE_PROFILE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
OWNER_USER_ID = "442308262"


def _task_value(task: Any, field: str) -> Any:
    return task.get(field) if isinstance(task, dict) else getattr(task, field, None)


def _load_current_task(task_id: str, db_path: Path) -> Any:
    from hermes_cli import kanban_db as kb

    if not db_path.is_file():
        raise FileNotFoundError("pinned Kanban database does not exist")
    conn = kb.connect(db_path=db_path)
    try:
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None:
        raise RuntimeError("current Kanban task was not found")
    return task


def _reserve_current_run(
    task_id: str, run_id: int, db_path: Path, claim_lock: str
) -> bool:
    from hermes_cli import kanban_db as kb

    conn = kb.connect(db_path=db_path)
    try:
        if not kb.heartbeat_claim(conn, task_id, claimer=claim_lock):
            return False
        return bool(
            kb.heartbeat_worker(
                conn,
                task_id,
                note="operations-manager execution reserved",
                expected_run_id=run_id,
            )
        )
    finally:
        conn.close()


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _command_from_task(task: Any) -> dict[str, Any]:
    body = _task_value(task, "body")
    if not isinstance(body, str) or not body or len(body.encode("utf-8")) > 32_768:
        raise RuntimeError("current operations task body is invalid")
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("current operations task body is invalid JSON") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schema_version", "command", "worker_contract"}
        or envelope.get("schema_version") != "operations-kanban-task.v1"
        or envelope.get("worker_contract") != EXPECTED_WORKER_CONTRACT
        or not isinstance(envelope.get("command"), dict)
    ):
        raise RuntimeError("current operations task envelope is invalid")
    command = envelope["command"]
    if set(command) != {
        "schema_version",
        "command_id",
        "idempotency_key",
        "source_profile",
        "source_session_id",
        "caller",
        "request",
    }:
        raise RuntimeError("operations-command.v1 fields are invalid")
    if (
        command.get("schema_version") != "operations-command.v1"
        or not isinstance(command.get("command_id"), str)
        or COMMAND_UUID.fullmatch(command["command_id"]) is None
        or not isinstance(command.get("idempotency_key"), str)
        or COMMAND_KEY.fullmatch(command["idempotency_key"]) is None
        or not isinstance(command.get("source_profile"), str)
        or SOURCE_PROFILE.fullmatch(command["source_profile"]) is None
        or command.get("source_profile") in {
            "broker", "project-manager", "personal-assistant", "operations-manager"
        }
        or command.get("source_session_id") != _task_value(task, "session_id")
        or command.get("caller")
        != ("owner" if command.get("source_profile") == "default" else command.get("source_profile"))
        or not isinstance(command.get("request"), dict)
    ):
        raise RuntimeError("operations-command.v1 is invalid")
    command = dict(command)
    command["request"] = validate_request(command["request"])
    semantic = {
        "caller": command["caller"],
        "integration": command["request"]["integration"],
        "operation": command["request"]["operation"],
        "arguments": command["request"]["arguments"],
        "mode": command["request"]["mode"],
    }
    expected_key = f"operations:v1:{_canonical_hash(semantic)[:32]}"
    if command["idempotency_key"] != expected_key:
        raise RuntimeError("operations command idempotency binding is invalid")
    return command


def _verify_source_route(
    task: Any, command: dict[str, Any], db_path: Path
) -> bool:
    from hermes_cli import kanban_db as kb

    conn = kb.connect(db_path=db_path)
    try:
        rows = conn.execute(
            "SELECT platform, chat_id, thread_id, user_id, chat_type, "
            "notifier_profile, delivery_mode FROM kanban_notify_subs WHERE task_id = ?",
            (_task_value(task, "id"),),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        return False
    row = rows[0]
    source_profile = command["source_profile"]
    if (
        _task_value(task, "created_by") != source_profile
        or row["platform"] != "telegram"
        or row["chat_type"] != "dm"
        or row["notifier_profile"] != source_profile
        or row["delivery_mode"] != "wake"
        or not str(row["chat_id"]).isdigit()
        or not str(row["thread_id"]).isdigit()
        or not str(row["user_id"]).isdigit()
    ):
        return False
    if command["caller"] == "owner":
        if source_profile != "default" or str(row["user_id"]) != OWNER_USER_ID:
            return False
    elif command["caller"] != source_profile:
        return False
    identity = {
        "mutation_key": command["idempotency_key"],
        "profile": source_profile,
        "platform": str(row["platform"]),
        "chat_id": str(row["chat_id"]),
        "user_id": str(row["user_id"]),
        "thread_id": str(row["thread_id"]),
        "session_id": command["source_session_id"],
    }
    expected_delivery = f"operations-delivery:v1:{_canonical_hash(identity)[:32]}"
    return _task_value(task, "idempotency_key") == expected_delivery


class HermesKanbanLifecycle:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id

    @staticmethod
    def _require_ok(raw: str, action: str) -> None:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError(payload.get("error") or f"Kanban {action} failed")

    def complete(self, *, summary: str, result: str) -> None:
        from tools.kanban_tools import _handle_complete

        self._require_ok(
            _handle_complete(
                {"task_id": self.task_id, "summary": summary, "result": result}
            ),
            "complete",
        )

    def block(self, *, reason: str, kind: str) -> None:
        from tools.kanban_tools import _handle_block

        self._require_ok(
            _handle_block({"task_id": self.task_id, "reason": reason, "kind": kind}),
            "block",
        )


def _safe_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:500]


def handle_om_ops_execute(args: dict[str, Any], **kwargs: Any) -> str:
    """Execute only the command persisted on the claimed Operations Manager task."""
    environ = kwargs.get("environ") or os.environ
    if str(environ.get("HERMES_PROFILE") or "") != "operations-manager":
        return json.dumps(
            {"status": "rejected", "error": "tool requires operations-manager profile"},
            sort_keys=True,
        )
    if args != {}:
        return json.dumps(
            {"status": "rejected", "error": "tool accepts no model-supplied command"},
            sort_keys=True,
        )
    task_id = str(environ.get("HERMES_KANBAN_TASK") or "")
    raw_run_id = str(environ.get("HERMES_KANBAN_RUN_ID") or "")
    if TASK_ID.fullmatch(task_id) is None:
        return json.dumps(
            {"status": "rejected", "error": "tool requires a current Kanban task"},
            sort_keys=True,
        )
    if not raw_run_id.isdigit() or int(raw_run_id) < 1:
        return json.dumps(
            {"status": "rejected", "error": "tool requires a current Kanban run"},
            sort_keys=True,
        )
    db_path = Path(str(environ.get("HERMES_KANBAN_DB") or ""))
    claim_lock = str(environ.get("HERMES_KANBAN_CLAIM_LOCK") or "").strip()
    if not db_path.is_absolute() or db_path.name != "kanban.db":
        return json.dumps(
            {"status": "rejected", "error": "tool requires a pinned Kanban database"},
            sort_keys=True,
        )
    if not claim_lock or len(claim_lock) > 256:
        return json.dumps(
            {"status": "rejected", "error": "tool requires the current claim lock"},
            sort_keys=True,
        )
    loader: Callable[[str, Path], Any] = kwargs.get("task_loader") or _load_current_task
    try:
        task = loader(task_id, db_path)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return json.dumps(
            {"status": "rejected", "error": "current Kanban task could not be loaded", "error_class": type(exc).__name__},
            sort_keys=True,
        )
    if (
        _task_value(task, "id") != task_id
        or _task_value(task, "assignee") != "operations-manager"
        or _task_value(task, "status") != "running"
        or _task_value(task, "current_run_id") != int(raw_run_id)
    ):
        return json.dumps(
            {"status": "rejected", "error": "current Kanban task/run binding is invalid"},
            sort_keys=True,
        )
    if not str(environ.get("GH_TOKEN") or "").strip() or not str(
        environ.get("SWAMP_API_KEY") or ""
    ).strip():
        return json.dumps(
            {"status": "rejected", "error": "operations-manager profile-local credentials are unavailable"},
            sort_keys=True,
        )
    try:
        command = _command_from_task(task)
    except Exception as exc:
        return json.dumps(
            {"status": "rejected", "error": _safe_error(exc)},
            ensure_ascii=False,
            sort_keys=True,
        )
    route_verifier = kwargs.get("route_verifier") or _verify_source_route
    try:
        if not route_verifier(task, command, db_path):
            raise RuntimeError("operations source route binding is invalid")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return json.dumps(
            {"status": "rejected", "error": _safe_error(exc)},
            ensure_ascii=False,
            sort_keys=True,
        )
    reserver = kwargs.get("run_reserver") or _reserve_current_run
    try:
        if not reserver(task_id, int(raw_run_id), db_path, claim_lock):
            raise RuntimeError("current Kanban run was superseded")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return json.dumps(
            {"status": "rejected", "error": _safe_error(exc)},
            ensure_ascii=False,
            sort_keys=True,
        )
    lifecycle = (kwargs.get("lifecycle_factory") or HermesKanbanLifecycle)(task_id)
    try:
        policy = kwargs.get("policy")
        if policy is None:
            policy = json.loads((PLUGIN_ROOT / "policy.json").read_text(encoding="utf-8"))
        workspace = Path(
            str(kwargs.get("workspace") or policy.get("workspace") or DEFAULT_WORKSPACE)
        ).expanduser().resolve()
        (kwargs.get("workspace_verifier") or _verify_runtime_workspace)(policy, workspace)
        hermes_home = Path(
            str(environ.get("HERMES_HOME") or "/Users/hermes/.hermes/profiles/operations-manager")
        )
        audit_path = hermes_home / "plugin-data" / "ops-broker" / "audit.jsonl"
        request = command["request"]
        runner = kwargs.get("runner") or default_runner
        execution = execute_request(
            request,
            caller=command["caller"],
            policy=policy,
            runner=runner,
            workspace=workspace,
            audit_path=audit_path,
            session_id=command["source_session_id"],
            preview_loader=lambda reference, exact_session: _load_linear_delete_preview(
                reference, exact_session, policy=policy
            ),
            attestation_issuer=lambda preview: _issue_linear_delete_attestation(
                preview, policy=policy, runner=runner, workspace=workspace, audit_path=audit_path
            ),
            approval_recorder=lambda preview, granted: _record_linear_delete_approval(preview, granted),
            approval_attempt_recorder=lambda preview: _record_linear_delete_approval_attempt(preview),
            approval_lock_root=audit_path.parent / "linear-delete-confirmation-locks",
        )
        result = {
            "schema_version": "operations-result.v1",
            "command_id": command["command_id"],
            "idempotency_key": command["idempotency_key"],
            "source_profile": command["source_profile"],
            "caller": execution["caller"],
            "request_id": execution["request_id"],
            "integration": request["integration"],
            "operation": execution["operation"],
            "mode": execution["mode"],
            "status": execution["status"],
            "result": execution["result"],
            "verified": execution["status"] == "ok",
        }
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        lifecycle.complete(summary="GitHub/Swamp operation completed and verified.", result=result_json)
        return json.dumps(
            {"status": "completed", "task_id": task_id, "verified": True, "result": result},
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception as exc:
        reason = _safe_error(exc)
        try:
            lifecycle.block(reason=f"Operations command failed: {reason}", kind="capability")
        except Exception as block_exc:
            return json.dumps(
                {"status": "rejected", "error": "Operations command failed and Kanban blocker could not be recorded", "block_error": type(block_exc).__name__},
                sort_keys=True,
            )
        return json.dumps(
            {"status": "blocked", "task_id": task_id, "error": reason},
            ensure_ascii=False,
            sort_keys=True,
        )


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="om_ops_execute",
        toolset="ops-broker",
        schema=OM_OPS_EXECUTE_SCHEMA,
        handler=handle_om_ops_execute,
        description=OM_OPS_EXECUTE_SCHEMA["description"],
        emoji="🛡️",
    )
