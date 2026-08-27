from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .broker import BrokerError, execute_request, resolve_caller, validate_request


PLUGIN_ROOT = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = PLUGIN_ROOT.parents[1]

OPS_BROKER_SCHEMA = {
    "name": "ops_broker",
    "description": (
        "Execute one typed, policy-allowlisted read-only GitHub or Swamp operation. "
        "Caller identity is derived from the authenticated A2A session. This tool "
        "does not accept shell commands, arbitrary URLs, credential requests, or apply mode."
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
                    "get_result",
                ],
            },
            "arguments": {"type": "object", "maxProperties": 4},
            "mode": {"type": "string", "enum": ["plan"]},
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


def handle_ops_broker(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        request = validate_request(args)
        hermes_home = _path_from_env("HERMES_HOME", Path.home() / ".hermes")
        session_id = str(kwargs.get("session_id") or "")
        caller = resolve_caller(session_id, hermes_home / "state.db")
        policy_path = _path_from_env("OPS_BROKER_POLICY", PLUGIN_ROOT / "policy.json")
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        configured_workspace = Path(
            str(policy.get("workspace") or DEFAULT_WORKSPACE)
        ).expanduser().resolve()
        workspace = _path_from_env("OPS_BROKER_WORKSPACE", configured_workspace)
        audit_path = _path_from_env(
            "OPS_BROKER_AUDIT",
            hermes_home / "plugin-data" / "ops-broker" / "audit.jsonl",
        )
        result = execute_request(
            request,
            caller=caller,
            policy=policy,
            runner=default_runner,
            workspace=workspace,
            audit_path=audit_path,
        )
        return json.dumps(result, sort_keys=True)
    except (
        BrokerError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
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
