from __future__ import annotations

import json
import os
import re
import sqlite3
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
        "Execute exactly one validated linear-command.v2 from the current "
        "project-manager Kanban task. The tool performs deterministic plan, "
        "apply, exact read-back, idempotency handling, and then completes or "
        "blocks the current task with a typed linear-result.v2 handoff."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    },
}

EXPECTED_WORKER_CONTRACT = {
    "profile": "project-manager",
    "tool": "pm_linear_execute",
    "mode": "plan_apply_read_back",
    "completion": "tool_completes_current_kanban_task",
}


class ProtocolVersionError(RuntimeError):
    """A persisted command/task/result uses a rejected protocol generation."""


def _load_lane() -> Any:
    from . import lane

    return lane


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
    task_id: str,
    run_id: int,
    db_path: Path,
    claim_lock: str,
) -> bool:
    """Extend the exact worker claim and prove its run still owns the task."""
    from hermes_cli import kanban_db as kb

    if not db_path.is_file():
        raise FileNotFoundError("pinned Kanban database does not exist")
    conn = kb.connect(db_path=db_path)
    try:
        if not kb.heartbeat_claim(conn, task_id, claimer=claim_lock):
            return False
        return bool(
            kb.heartbeat_worker(
                conn,
                task_id,
                note="project-manager Linear execution reserved",
                expected_run_id=run_id,
            )
        )
    finally:
        conn.close()


def _command_from_task(task: Any) -> dict[str, Any]:
    body = _task_value(task, "body")
    if not isinstance(body, str) or not body or len(body.encode("utf-8")) > 32768:
        raise RuntimeError("current Kanban task body is invalid")
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("current Kanban task body is invalid JSON") from exc
    if not isinstance(envelope, dict) or set(envelope) != {
        "schema_version",
        "command",
        "worker_contract",
    }:
        raise RuntimeError("current Kanban task envelope is invalid")
    if envelope.get("schema_version") == "linear-kanban-task.v1":
        raise ProtocolVersionError(
            "linear-kanban-task.v1 is rejected; expected linear-kanban-task.v2"
        )
    if (
        envelope.get("schema_version") != "linear-kanban-task.v2"
        or envelope.get("worker_contract") != EXPECTED_WORKER_CONTRACT
        or not isinstance(envelope.get("command"), dict)
    ):
        raise RuntimeError("current Kanban task envelope is invalid")
    command = envelope["command"]
    if command.get("schema_version") == "linear-command.v1":
        raise ProtocolVersionError(
            "linear-command.v1 is rejected; expected linear-command.v2"
        )
    return command


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
    if not isinstance(plan, dict) or plan.get("schema_version") != "linear-result.v2":
        raise ProtocolVersionError(
            "Linear plan did not return linear-result.v2; legacy results are rejected"
        )
    result = lane.execute_command(
        client,
        validated,
        mode="apply",
        journal_path=journal_path,
    )
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != "linear-result.v2"
        or result.get("verified") is not True
    ):
        if isinstance(result, dict) and result.get("schema_version") == "linear-result.v1":
            raise ProtocolVersionError(
                "Linear apply returned rejected linear-result.v1; expected linear-result.v2"
            )
        raise RuntimeError("Linear apply did not return a verified linear-result.v2")
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
    elif operation == "create_issue":
        lead = "Задача Linear создана и прочитана обратно. Изменение verified."
    elif operation == "converge_hierarchy":
        lead = "Иерархия Linear создана или уже сходилась; точное чтение обратно verified."
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
    raw_run_id = str(environ.get("HERMES_KANBAN_RUN_ID") or "")
    if not raw_run_id.isdigit() or int(raw_run_id) < 1:
        return json.dumps(
            {"status": "rejected", "error": "tool requires a current Kanban run"},
            sort_keys=True,
        )
    if args != {}:
        return json.dumps(
            {"status": "rejected", "error": "tool accepts no model-supplied command"},
            sort_keys=True,
        )

    raw_db_path = str(environ.get("HERMES_KANBAN_DB") or "").strip()
    db_path = Path(raw_db_path)
    if not db_path.is_absolute() or db_path.name != "kanban.db":
        return json.dumps(
            {"status": "rejected", "error": "tool requires a pinned Kanban database"},
            sort_keys=True,
        )
    claim_lock = str(environ.get("HERMES_KANBAN_CLAIM_LOCK") or "").strip()
    if not claim_lock or len(claim_lock) > 256:
        return json.dumps(
            {"status": "rejected", "error": "tool requires the current claim lock"},
            sort_keys=True,
        )

    task_loader: Callable[[str, Path], Any] = (
        kwargs.get("task_loader") or _load_current_task
    )
    try:
        task = task_loader(task_id, db_path)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return json.dumps(
            {
                "status": "rejected",
                "error": "current Kanban task could not be loaded",
                "error_class": type(exc).__name__,
            },
            sort_keys=True,
        )
    if (
        _task_value(task, "id") != task_id
        or _task_value(task, "assignee") != "project-manager"
        or _task_value(task, "status") != "running"
        or _task_value(task, "current_run_id") != int(raw_run_id)
    ):
        return json.dumps(
            {"status": "rejected", "error": "current Kanban task/run binding is invalid"},
            sort_keys=True,
        )

    preflight_error: Exception | None = None
    command: dict[str, Any] = {}
    lane: Any = None
    try:
        command = _command_from_task(task)
        lane_loader = kwargs.get("lane_loader") or _load_lane
        lane = lane_loader()
        lane.validate_command(command)
    except ProtocolVersionError as exc:
        return json.dumps(
            {"status": "rejected", "error": _sanitize_error(exc)},
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception as exc:
        preflight_error = exc

    run_reserver: Callable[[str, int, Path, str], bool] = (
        kwargs.get("run_reserver") or _reserve_current_run
    )
    try:
        reserved = run_reserver(task_id, int(raw_run_id), db_path, claim_lock)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return json.dumps(
            {
                "status": "rejected",
                "error": "current Kanban run could not be reserved",
                "error_class": type(exc).__name__,
            },
            sort_keys=True,
        )
    if not reserved:
        return json.dumps(
            {"status": "rejected", "error": "current Kanban run was superseded"},
            sort_keys=True,
        )

    lifecycle_factory: Callable[[str], Any] = (
        kwargs.get("lifecycle_factory") or HermesKanbanLifecycle
    )
    lifecycle = lifecycle_factory(task_id)
    try:
        if preflight_error is not None:
            raise preflight_error
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
    except ProtocolVersionError as exc:
        return json.dumps(
            {"status": "rejected", "error": _sanitize_error(exc)},
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
