from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

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
        audit_path_value = policy.get("auditPath")
        if audit_path_value is not None:
            if not isinstance(audit_path_value, str) or not audit_path_value:
                raise BrokerError("audit path policy must be a non-empty string")
            audit_path = Path(audit_path_value).expanduser().resolve()
        else:
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


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="ops_broker",
        toolset="ops-broker",
        schema=OPS_BROKER_SCHEMA,
        handler=handle_ops_broker,
        description=OPS_BROKER_SCHEMA["description"],
        emoji="🛡️",
    )
