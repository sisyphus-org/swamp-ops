from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable


TASK_ID = re.compile(r"^t_[a-f0-9]{8,}$")
CREDENTIAL_PATTERNS = (
    re.compile(r"Authorization:\s*(?:Bearer|Basic)\s+\S+", re.IGNORECASE),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\blin_api_[A-Za-z0-9_-]{16,}\b"),
)

PM_LINEAR_EXECUTE_SCHEMA = {
    "name": "pm_linear_execute",
    "description": (
        "Execute exactly one validated linear-command.v1 from the current "
        "project-manager Kanban task. The tool performs deterministic plan, "
        "apply, exact read-back, idempotency handling, and then completes or "
        "blocks the current task with a typed linear-result.v1 handoff."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "string", "const": "linear-command.v1"},
                    "command_id": {"type": "string", "format": "uuid"},
                    "correlation_id": {"type": "string", "format": "uuid"},
                    "idempotency_key": {
                        "type": "string",
                        "pattern": "^[A-Za-z0-9][A-Za-z0-9:._/-]{7,199}$",
                    },
                    "source_profile": {"type": "string", "const": "swe"},
                    "operation": {
                        "type": "string",
                        "enum": ["read_issue", "change_state", "add_comment"],
                    },
                    "target": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "type": {"type": "string", "const": "issue"},
                            "identifier": {
                                "type": "string",
                                "pattern": "^SIS-[1-9][0-9]*$",
                            },
                        },
                        "required": ["type", "identifier"],
                    },
                    "change": {"type": "object", "maxProperties": 1},
                    "policy": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "mode": {"type": "string", "const": "standard"}
                        },
                        "required": ["mode"],
                    },
                },
                "required": [
                    "schema_version",
                    "command_id",
                    "correlation_id",
                    "idempotency_key",
                    "source_profile",
                    "operation",
                    "target",
                    "change",
                    "policy",
                ],
                "description": "The exact linear-command.v1 object from the task body.",
            }
        },
        "required": ["command"],
    },
}


def _load_lane() -> Any:
    from . import lane

    return lane


def execute_pm_command(
    command: dict[str, Any],
    *,
    lane: Any,
    client: Any,
    journal_path: Path,
) -> dict[str, Any]:
    """Run deterministic plan then apply and require verified exact read-back."""
    validated = lane.validate_command(command)
    plan = lane.execute_command(
        client,
        validated,
        mode="plan",
        journal_path=journal_path,
    )
    result = lane.execute_command(
        client,
        validated,
        mode="apply",
        journal_path=journal_path,
    )
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != "linear-result.v1"
        or result.get("verified") is not True
    ):
        raise RuntimeError("Linear apply did not return a verified linear-result.v1")
    return {"plan": plan, "result": result}


def human_summary(result: dict[str, Any]) -> str:
    """Render the concise source-agent handoff without internal task IDs."""
    target = result.get("target") if isinstance(result, dict) else None
    url = target.get("url") if isinstance(target, dict) else ""
    operation = result.get("operation")
    if operation == "read_issue":
        lead = "Задача Linear прочитана. Результат verified."
    elif result.get("result") == "no_op" or result.get("no_op") is True:
        lead = "Запрос уже выполнен; повторная мутация не потребовалась. Изменение verified."
    elif operation == "change_state":
        lead = "Статус Linear изменён и прочитан обратно. Изменение verified."
    else:
        lead = "Комментарий добавлен и прочитан обратно. Изменение verified."
    return f"{lead}\n{url}" if url else lead


def _sanitize_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    marker = "[credential-redacted]"
    for pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub(marker, text)
    if len(text) <= 500:
        return text
    truncated = text[:500]
    if marker in text and marker not in truncated:
        return text[: 500 - len(marker)].rstrip() + marker
    return truncated


class HermesKanbanLifecycle:
    """Complete/block the current worker task through shipped lifecycle handlers."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id

    @staticmethod
    def _require_ok(raw: str, action: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Kanban {action} returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise RuntimeError(error or f"Kanban {action} failed")

    def complete(self, *, summary: str, result: str) -> None:
        from tools.kanban_tools import _handle_complete

        raw = _handle_complete(
            {"task_id": self.task_id, "summary": summary, "result": result}
        )
        self._require_ok(raw, "complete")

    def block(self, *, reason: str, kind: str) -> None:
        from tools.kanban_tools import _handle_block

        raw = _handle_block(
            {"task_id": self.task_id, "reason": reason, "kind": kind}
        )
        self._require_ok(raw, "block")


def handle_pm_linear_execute(args: dict[str, Any], **kwargs: Any) -> str:
    """Execute the typed lane only inside the assigned PM Kanban worker."""
    environ = kwargs.get("environ") or os.environ
    profile = str(environ.get("HERMES_PROFILE") or "")
    task_id = str(environ.get("HERMES_KANBAN_TASK") or "")
    if profile != "project-manager":
        return json.dumps(
            {"status": "rejected", "error": "tool requires project-manager profile"},
            sort_keys=True,
        )
    if not TASK_ID.fullmatch(task_id):
        return json.dumps(
            {"status": "rejected", "error": "tool requires a current Kanban task"},
            sort_keys=True,
        )
    if not isinstance(args, dict) or set(args) != {"command"}:
        return json.dumps(
            {"status": "rejected", "error": "tool input must contain exactly command"},
            sort_keys=True,
        )

    lifecycle_factory: Callable[[str], Any] = (
        kwargs.get("lifecycle_factory") or HermesKanbanLifecycle
    )
    lifecycle = lifecycle_factory(task_id)
    try:
        command = args["command"]
        if not isinstance(command, dict):
            raise RuntimeError("command must be an object")
        lane_loader = kwargs.get("lane_loader") or _load_lane
        lane = lane_loader()
        client_factory = kwargs.get("client_factory") or lane.LinearClient
        token = str(environ.get("LINEAR_TOKEN") or "")
        client = client_factory(token)
        hermes_home = Path(
            str(environ.get("HERMES_HOME") or "/Users/hermes/.hermes/profiles/project-manager")
        )
        journal = hermes_home / "linear-command-lane" / "journal.json"
        execution = execute_pm_command(
            command,
            lane=lane,
            client=client,
            journal_path=journal,
        )
        result = execution["result"]
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        lifecycle.complete(summary=human_summary(result), result=result_json)
        return json.dumps(
            {
                "status": "completed",
                "task_id": task_id,
                "verified": True,
                "result": result,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception as exc:
        reason = _sanitize_error(exc)
        try:
            lifecycle.block(reason=f"Linear command failed: {reason}", kind="capability")
        except Exception as block_exc:
            return json.dumps(
                {
                    "status": "rejected",
                    "error": "Linear command failed and Kanban blocker could not be recorded",
                    "block_error": type(block_exc).__name__,
                },
                sort_keys=True,
            )
        return json.dumps(
            {
                "status": "blocked",
                "task_id": task_id,
                "error": reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="pm_linear_execute",
        toolset="project-manager-linear",
        schema=PM_LINEAR_EXECUTE_SCHEMA,
        handler=handle_pm_linear_execute,
        description=PM_LINEAR_EXECUTE_SCHEMA["description"],
        emoji="📋",
    )
