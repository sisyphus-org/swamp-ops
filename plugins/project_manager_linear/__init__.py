from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable


TASK_ID = re.compile(r"^t_[a-f0-9]{8,}$")
SUMMARY_LINEAR_URL = re.compile(
    r"^https://linear\.app/[A-Za-z0-9_-]+/issue/(SIS-[1-9][0-9]*)/"
    r"[A-Za-z0-9][A-Za-z0-9_-]*$"
)
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
    if envelope.get("schema_version") != "linear-kanban-task.v2":
        raise ProtocolVersionError(
            "unsupported task schema; expected linear-kanban-task.v2"
        )
    if (
        envelope.get("worker_contract") != EXPECTED_WORKER_CONTRACT
        or not isinstance(envelope.get("command"), dict)
    ):
        raise RuntimeError("current Kanban task envelope is invalid")
    command = envelope["command"]
    if command.get("schema_version") != "linear-command.v2":
        raise ProtocolVersionError(
            "unsupported command schema; expected linear-command.v2"
        )
    return command


def execute_pm_command(
    command: dict[str, Any],
    *,
    lane: Any,
    client: Any,
    journal_path: Path,
    task_id: str | None = None,
    approval_now: Any = None,
    approval_lease_seconds: int = 30,
) -> dict[str, Any]:
    """Run deterministic plan then apply and require verified exact read-back."""
    validated = lane.validate_command(command)
    if validated.get("operation") in {
        "read_issue",
        "inventory_sub_issues",
        "search_linear",
        "inventory_linear",
    }:
        result = lane.execute_command(
            client,
            validated,
            mode="apply",
            journal_path=None,
        )
        if (
            not isinstance(result, dict)
            or result.get("schema_version") != "linear-result.v2"
            or result.get("verified") is not True
            or result.get("result") != "read"
        ):
            raise RuntimeError("Linear read did not return a verified linear-result.v2")
        return {"plan": result, "result": result}
    plan = lane.execute_command(
        client,
        validated,
        mode="plan",
        journal_path=journal_path,
    )
    if not isinstance(plan, dict) or plan.get("schema_version") != "linear-result.v2":
        raise ProtocolVersionError(
            "Linear plan returned an unsupported schema; expected linear-result.v2"
        )
    if (
        validated["policy"].get("mode") == "owner_approved"
        and task_id is None
        and validated["operation"] in {"remove_issue_relation", "replace_issue_relation"}
        and plan.get("recovered") is True
    ):
        operation = validated["operation"]
        change = validated["change"]
        identifier = validated["target"]["identifier"]
        target = plan.get("target")
        after = plan.get("after")
        if operation == "remove_issue_relation":
            expected_target = {
                "type": "issue_relation",
                "identifier": identifier,
                "related_identifier": change["related_identifier"],
                "relation_type": change["relation_type"],
            }
            expected_after = {
                "identifier": identifier,
                "related_identifier": change["related_identifier"],
                "relation_type": change["relation_type"],
                "present": False,
            }
        else:
            expected_target = {
                "type": "issue_relation_replacement",
                "old": {
                    "identifier": identifier,
                    "related_identifier": change["old_related_identifier"],
                    "relation_type": change["old_relation_type"],
                },
                "new": {
                    "identifier": identifier,
                    "related_identifier": change["new_related_identifier"],
                    "relation_type": change["new_relation_type"],
                },
            }
            expected_after = dict(expected_target["new"])
        if (
            plan.get("operation") != operation
            or target != expected_target
            or after != expected_after
            or plan.get("result") != "no_op"
            or plan.get("no_op") is not True
            or plan.get("verified") is not True
            or plan.get("plan") != []
            or not isinstance(plan.get("before"), dict)
        ):
            raise RuntimeError("recovered Linear relation replay is invalid")
        return {"plan": plan, "result": plan}
    owner_approval_authorization = None
    approval_journal = journal_path.with_name("owner-approval-apply-journal.json")
    if validated["policy"].get("mode") == "owner_approved":
        from .approval import (
            ApprovalError,
            claim_owner_approval,
            completed_owner_approval_matches,
            consume_owner_approval,
            contract,
            recover_owner_approval,
            verify_owner_approval,
        )

        expected_intent = {
            "operation": validated["operation"],
            "target": validated["target"],
            "change": validated["change"],
        }
        durable_recovery = task_id is not None and plan.get("recovered") is True
        if durable_recovery:
            assert task_id is not None
            identity_fields = (
                "command_id",
                "correlation_id",
                "idempotency_key",
                "source_profile",
                "operation",
            )
            if (
                any(plan.get(field) != validated[field] for field in identity_fields)
                or not isinstance(plan.get("recovery_evidence"), dict)
            ):
                raise ApprovalError("owner approval recovery result binding is invalid")
            if (
                plan.get("result") == "no_op"
                and plan.get("no_op") is True
                and plan.get("verified") is True
                and plan.get("plan") == []
                and completed_owner_approval_matches(
                    validated["policy"],
                    command=validated,
                    task_id=task_id,
                    journal_path=approval_journal,
                    recovery_evidence=plan["recovery_evidence"],
                )
            ):
                return {"plan": plan, "result": plan}
            owner_approval_authorization = recover_owner_approval(
                validated["policy"],
                command=validated,
                task_id=task_id,
                journal_path=approval_journal,
                recovery_evidence=plan["recovery_evidence"],
                now=approval_now,
                lease_seconds=approval_lease_seconds,
            )
        else:
            before_state_hash = contract.canonical_sha256(plan.get("before"))
            verified_approval = verify_owner_approval(
                validated["policy"],
                expected_intent=expected_intent,
                expected_before_state_hash=before_state_hash,
            )
            # Verification is intentionally non-consuming. Re-read Linear
            # immediately afterwards and bind apply to the exact same state and plan.
            live_plan = lane.execute_command(
                client,
                validated,
                mode="plan",
                journal_path=journal_path,
            )
            if (
                not isinstance(live_plan, dict)
                or live_plan.get("schema_version") != "linear-result.v2"
            ):
                raise ProtocolVersionError(
                    "Linear re-plan returned an unsupported schema; expected linear-result.v2"
                )
            approved_plan_binding = {
                "operation": plan.get("operation"),
                "target": plan.get("target"),
                "plan": plan.get("plan"),
            }
            live_plan_binding = {
                "operation": live_plan.get("operation"),
                "target": live_plan.get("target"),
                "plan": live_plan.get("plan"),
            }
            approved_target = approved_plan_binding["target"]
            operation = expected_intent["operation"]
            target_identifier = expected_intent["target"].get("identifier")
            change = expected_intent["change"]
            approved_before_matches = isinstance(plan.get("before"), dict)
            live_before_matches = isinstance(live_plan.get("before"), dict)
            if operation == "bulk_linear_operations":
                approved_target_matches = approved_target == expected_intent["target"]
                expected_items = change.get("items")
                approved_before_matches = (
                    isinstance(plan.get("before"), list)
                    and isinstance(expected_items, list)
                    and len(plan["before"]) == len(expected_items)
                )
                live_before_matches = (
                    isinstance(live_plan.get("before"), list)
                    and isinstance(expected_items, list)
                    and len(live_plan["before"]) == len(expected_items)
                )
            elif operation in {"archive_linear_entity", "delete_linear_entity"}:
                approved_target_matches = approved_target == expected_intent["target"]
                approved_before_matches = isinstance(plan.get("before"), dict)
                live_before_matches = isinstance(live_plan.get("before"), dict)
            elif operation == "remove_issue_relation":
                approved_target_matches = approved_target == {
                    "type": "issue_relation",
                    "identifier": target_identifier,
                    "related_identifier": change["related_identifier"],
                    "relation_type": change["relation_type"],
                }
            elif operation == "replace_issue_relation":
                approved_target_matches = approved_target == {
                    "type": "issue_relation_replacement",
                    "old": {
                        "identifier": target_identifier,
                        "related_identifier": change["old_related_identifier"],
                        "relation_type": change["old_relation_type"],
                    },
                    "new": {
                        "identifier": target_identifier,
                        "related_identifier": change["new_related_identifier"],
                        "relation_type": change["new_relation_type"],
                    },
                }
            else:
                approved_target_matches = (
                    isinstance(approved_target, dict)
                    and approved_target.get("type")
                    == expected_intent["target"]["type"]
                    and approved_target.get("identifier") == target_identifier
                )
            if (
                not approved_before_matches
                or approved_plan_binding["operation"] != expected_intent["operation"]
                or not approved_target_matches
                or approved_plan_binding["plan"] is None
                or not live_before_matches
                or contract.canonical_sha256(live_plan.get("before"))
                != before_state_hash
                or contract.canonical_sha256(live_plan_binding)
                != contract.canonical_sha256(approved_plan_binding)
            ):
                raise ApprovalError(
                    "owner approval live before-state or operation plan/target drifted"
                )
            # This is the last action before entering the mutation path.
            if task_id is None:
                owner_approval_authorization = consume_owner_approval(
                    verified_approval,
                    journal_path=journal_path.with_name("owner-approval-journal.json"),
                )
            else:
                owner_approval_authorization = claim_owner_approval(
                    verified_approval,
                    command=validated,
                    task_id=task_id,
                    journal_path=approval_journal,
                    now=approval_now,
                    lease_seconds=approval_lease_seconds,
                )
    apply_kwargs = {
        "mode": "apply",
        "journal_path": journal_path,
    }
    if owner_approval_authorization is not None:
        apply_kwargs["owner_approval_authorization"] = owner_approval_authorization
    result = lane.execute_command(client, validated, **apply_kwargs)
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != "linear-result.v2"
        or result.get("verified") is not True
    ):
        if isinstance(result, dict) and result.get("schema_version") != "linear-result.v2":
            raise ProtocolVersionError(
                "Linear apply returned an unsupported schema; expected linear-result.v2"
            )
        raise RuntimeError("Linear apply did not return a verified linear-result.v2")
    if task_id is not None and owner_approval_authorization is not None:
        from .approval import complete_owner_approval

        complete_owner_approval(
            owner_approval_authorization,
            journal_path=approval_journal,
            recovery_evidence=result.get("recovery_evidence"),
        )
    return {"plan": plan, "result": result}


def execute_claimed_task(
    command: dict[str, Any],
    *,
    task_id: str,
    lane: Any,
    client: Any,
    journal_path: Path,
    approval_now: Any = None,
    approval_lease_seconds: int = 30,
) -> dict[str, Any]:
    """Execute one exact persisted command under its durable claimed-task identity."""
    return execute_pm_command(
        command,
        lane=lane,
        client=client,
        journal_path=journal_path,
        task_id=task_id,
        approval_now=approval_now,
        approval_lease_seconds=approval_lease_seconds,
    )


def human_summary(result: dict[str, Any]) -> str:
    """Render the concise source-agent handoff without internal task IDs."""
    target = result.get("target") if isinstance(result, dict) else None
    identifier = target.get("identifier") if isinstance(target, dict) else ""
    candidate_url = target.get("url") if isinstance(target, dict) else ""
    match = SUMMARY_LINEAR_URL.fullmatch(candidate_url) if isinstance(candidate_url, str) else None
    url = (
        candidate_url
        if match is not None and isinstance(identifier, str) and match.group(1) == identifier
        else ""
    )
    operation = result.get("operation")
    if operation == "bulk_linear_operations":
        counts = result.get("counts")
        total = counts.get("total") if isinstance(counts, dict) else None
        lead = (
            f"Пакет Linear выполнен: {total} операций."
            if isinstance(total, int) and not isinstance(total, bool)
            else "Пакет Linear выполнен."
        )
    elif operation == "read_issue":
        lead = "Задача Linear прочитана."
    elif operation == "search_linear":
        lead = "Поиск Linear выполнен."
    elif operation == "inventory_linear":
        lead = "Инвентаризация Linear выполнена."
    elif result.get("result") == "no_op" or result.get("no_op") is True:
        lead = "Запрос уже выполнен; повторных изменений не потребовалось."
    elif operation == "create_issue":
        lead = "Задача Linear создана."
    elif operation == "create_standalone_issue":
        lead = "Самостоятельная задача Linear создана."
    elif operation == "converge_issue_tree":
        lead = "Дерево задач Linear готово."
    elif operation == "converge_hierarchy":
        lead = "Иерархия Linear готова."
    elif operation == "create_project":
        lead = "Проект Linear создан или уже существует."
    elif operation == "create_milestone":
        lead = "Этап проекта Linear создан или уже существует."
    elif operation == "update_project":
        lead = "Проект Linear обновлён."
    elif operation == "update_milestone":
        lead = "Этап проекта Linear обновлён."
    elif operation == "create_initiative":
        lead = "Инициатива Linear создана или уже существует."
    elif operation == "update_initiative":
        lead = "Инициатива Linear обновлена."
    elif operation == "link_project_to_initiative":
        lead = "Проект Linear добавлен в инициативу."
    elif operation == "archive_linear_entity":
        lead = "Объект Linear архивирован."
    elif operation == "delete_linear_entity":
        lead = "Объект Linear удалён в соответствии с семантикой Linear."
    elif operation == "change_state":
        lead = "Статус Linear изменён."
    elif operation == "update_issue":
        lead = "Поля задачи Linear обновлены."
    else:
        lead = "Комментарий добавлен."
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
        execution = execute_claimed_task(
            command,
            task_id=task_id,
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
