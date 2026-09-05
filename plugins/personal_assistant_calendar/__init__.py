"""Privileged Personal Assistant Calendar worker for persisted Kanban commands."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


TASK_ID = re.compile(r"^t_[a-f0-9]{8,}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
APPROVAL_REFERENCE = re.compile(r"^calendar-approval:v1:[a-f0-9]{64}$")
GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")
PUBLIC_ISSUE_URL = re.compile(
    r"^https://linear\.app/[A-Za-z0-9_-]+/issue/(SIS-[1-9][0-9]*)/"
    r"[A-Za-z0-9][A-Za-z0-9_-]*$"
)
RUNTIME_WORKSPACE = Path("/Users/hermes/workspaces/swamp-ops-runtime")
RUNTIME_REVISION_FILE = Path(
    "/Users/hermes/.hermes/profiles/personal-assistant/plugin-data/"
    "personal-assistant-calendar/runtime-revision"
)
CREDENTIAL_PATTERNS = (
    re.compile(r"Authorization:\s*\S+(?:\s+\S+)?", re.IGNORECASE),
    re.compile(r"\b(?:ya29\.|1//)[A-Za-z0-9._-]{6,}\b"),
    re.compile(r'"(?:client_secret|refresh_token|access_token)"\s*:\s*"[^"]*"', re.IGNORECASE),
)
EXPECTED_WORKER_CONTRACT = {
    "profile": "personal-assistant",
    "tool": "pa_calendar_execute",
    "mode": "workflow_plan_approval_apply_read_back",
    "completion": "tool_completes_current_kanban_task",
}
PA_CALENDAR_EXECUTE_SCHEMA = {
    "name": "pa_calendar_execute",
    "description": (
        "Execute exactly one persisted calendar-command.v1 from the current "
        "Personal Assistant Kanban task. Accepts no model-supplied command."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}


class CalendarWorkerError(RuntimeError):
    """The persisted Calendar job failed a bounded worker contract."""


class CalendarRunSuperseded(CalendarWorkerError):
    """The exact claimed run no longer owns the task and must not write lifecycle state."""


def _verify_runtime_workspace(
    workspace: Path,
    revision_file: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    expected_workspace: Path = RUNTIME_WORKSPACE,
) -> None:
    """Fail closed unless the Calendar workflows come from one attested clean revision."""
    if workspace.resolve() != expected_workspace.resolve():
        raise CalendarWorkerError("Calendar runtime workspace is not the approved checkout")
    if (workspace / ".swamp-sources.yaml").exists():
        raise CalendarWorkerError("Calendar runtime has a local Swamp source override")
    try:
        revision = revision_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CalendarWorkerError("Calendar runtime revision attestation is unavailable") from exc
    if GIT_REVISION.fullmatch(revision) is None:
        raise CalendarWorkerError("Calendar runtime revision attestation is invalid")
    common = {
        "cwd": workspace,
        "capture_output": True,
        "text": True,
        "check": False,
        "timeout": 30,
    }
    head = runner(["git", "rev-parse", "HEAD"], **common)
    status = runner(
        ["git", "status", "--porcelain", "--untracked-files=all"], **common
    )
    if (
        head.returncode != 0
        or status.returncode != 0
        or head.stdout.strip() != revision
        or status.stdout.strip()
    ):
        raise CalendarWorkerError("Calendar runtime checkout is not attested and clean")


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
        raise CalendarWorkerError("current Kanban task was not found")
    return task


def _reserve_current_run(task_id: str, run_id: int, db_path: Path, claim_lock: str) -> bool:
    from hermes_cli import kanban_db as kb

    if not db_path.is_file():
        raise FileNotFoundError("pinned Kanban database does not exist")
    conn = kb.connect(db_path=db_path)
    try:
        if not kb.heartbeat_claim(conn, task_id, claimer=claim_lock):
            return False
        return bool(kb.heartbeat_worker(
            conn, task_id, note="personal-assistant Calendar execution reserved",
            expected_run_id=run_id,
        ))
    finally:
        conn.close()


def _validate_command_request(command: dict[str, Any]) -> None:
    operation = command["operation"]
    request = command["request"]
    if operation in {"inventory", "events", "freebusy"}:
        if set(request) != {"window"} or request.get("window") not in {
            "today", "next-7-days", "next-30-days"
        }:
            raise CalendarWorkerError("Calendar read request is invalid")
        return
    if operation == "approve_write":
        reference = request.get("approval_reference")
        if (
            set(request) != {"approval_reference"}
            or not isinstance(reference, str)
            or APPROVAL_REFERENCE.fullmatch(reference) is None
        ):
            raise CalendarWorkerError("Calendar approval request is invalid")
        return
    expected = {"operation", "block_key", "summary", "start", "end", "linear_url", "details"}
    if set(request) != expected or request.get("operation") not in {"create", "update", "delete"}:
        raise CalendarWorkerError("Calendar write request is invalid")
    for field, maximum in (("block_key", 64), ("summary", 200), ("start", 19), ("end", 19), ("linear_url", 500), ("details", 4000)):
        value = request.get(field)
        if not isinstance(value, str) or len(value) > maximum or any(
            ord(char) < 32 and char not in "\n\t" for char in value
        ):
            raise CalendarWorkerError(f"Calendar write {field} is invalid")
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", request["block_key"]) is None:
        raise CalendarWorkerError("Calendar write block_key is invalid")
    if request["linear_url"] and PUBLIC_ISSUE_URL.fullmatch(request["linear_url"]) is None:
        raise CalendarWorkerError("Calendar write Linear URL is invalid")
    serialized = json.dumps(request, ensure_ascii=False)
    if any(pattern.search(serialized) for pattern in CREDENTIAL_PATTERNS):
        raise CalendarWorkerError("Calendar write request contains credential-shaped data")


def _command_from_task(task: Any) -> dict[str, Any]:
    body = _task_value(task, "body")
    if not isinstance(body, str) or not body or len(body.encode("utf-8")) > 32_768:
        raise CalendarWorkerError("current Kanban task body is invalid")
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CalendarWorkerError("current Kanban task body is invalid JSON") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schema_version", "command", "worker_contract"}
        or envelope.get("schema_version") != "calendar-kanban-task.v1"
        or envelope.get("worker_contract") != EXPECTED_WORKER_CONTRACT
        or not isinstance(envelope.get("command"), dict)
    ):
        raise CalendarWorkerError("current Calendar task envelope is invalid")
    command = envelope["command"]
    if set(command) != {
        "schema_version", "command_id", "source_profile", "operation", "request", "idempotency_key"
    }:
        raise CalendarWorkerError("calendar-command.v1 fields are invalid")
    if (
        command.get("schema_version") != "calendar-command.v1"
        or not isinstance(command.get("command_id"), str)
        or UUID.fullmatch(command["command_id"]) is None
        or not isinstance(command.get("idempotency_key"), str)
        or re.fullmatch(r"calendar:v1:[a-f0-9]{32}", command["idempotency_key"]) is None
        or command.get("operation") not in {"inventory", "events", "freebusy", "plan_write", "approve_write"}
        or not isinstance(command.get("request"), dict)
    ):
        raise CalendarWorkerError("calendar-command.v1 is invalid")
    _validate_command_request(command)
    return command


def _load_approval_plan(reference: str, db_path: Path) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb

    if not db_path.is_file():
        raise FileNotFoundError("pinned Kanban database does not exist")
    conn = kb.connect(db_path=db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'done' AND result LIKE ? ORDER BY created_at DESC",
            (f'%"approval_reference": "{reference}"%',),
        ).fetchall()
        if len(rows) != 1:
            raise CalendarWorkerError("approval reference is missing or ambiguous")
        task = kb.get_task(conn, rows[0]["id"])
    finally:
        conn.close()
    if task is None:
        raise CalendarWorkerError("approval plan task is missing")
    return _validated_approval_plan(task, reference)


def _validated_approval_plan(task: Any, reference: str) -> dict[str, Any]:
    """Validate the complete persisted plan task before authorizing approval."""
    if (
        _task_value(task, "status") != "done"
        or _task_value(task, "assignee") != "personal-assistant"
        or not isinstance(_task_value(task, "session_id"), str)
        or not isinstance(reference, str)
        or APPROVAL_REFERENCE.fullmatch(reference) is None
    ):
        raise CalendarWorkerError("approval plan task binding is invalid")
    command = _command_from_task(task)
    if command.get("operation") != "plan_write":
        raise CalendarWorkerError("approval plan command binding is invalid")
    try:
        result = json.loads(_task_value(task, "result"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CalendarWorkerError("approval plan result is invalid") from exc
    expected_result_fields = {
        "schema_version", "command_id", "idempotency_key", "source_profile",
        "operation", "phase", "outcome", "preview", "approval_reference",
        "plan_reference", "verified",
    }
    preview = result.get("preview") if isinstance(result, dict) else None
    plan = (
        _plan_reference(result.get("plan_reference"), require_before_state=True)
        if isinstance(result, dict)
        else None
    )
    session_id = _task_value(task, "session_id")
    request = command["request"]
    if (
        not isinstance(result, dict)
        or set(result) != expected_result_fields
        or result.get("schema_version") != "calendar-result.v1"
        or result.get("command_id") != command["command_id"]
        or result.get("idempotency_key") != command["idempotency_key"]
        or result.get("source_profile") != command["source_profile"]
        or result.get("operation") != "plan_write"
        or result.get("phase") != "awaiting_approval"
        or result.get("outcome") != "planned"
        or result.get("verified") is not True
        or not isinstance(preview, dict)
        or set(preview) != {
            "operation", "block_key", "summary", "details", "start", "end",
            "timezone", "linear_url",
        }
        or preview.get("operation") != request["operation"]
        or preview.get("block_key") != request["block_key"]
        or preview.get("linear_url") != request["linear_url"]
        or preview.get("timezone") != "Europe/Kyiv"
        or result.get("approval_reference") != reference
        or plan is None
        or reference != _approval_token(command, plan, session_id)
    ):
        raise CalendarWorkerError("approval plan result binding is invalid")
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 65_536 or any(
        pattern.search(serialized) for pattern in CREDENTIAL_PATTERNS
    ):
        raise CalendarWorkerError("approval plan result contains unsafe data")
    return {
        "source_profile": command["source_profile"],
        "session_id": session_id,
        "approval_reference": reference,
        "plan_reference": plan,
        "request": dict(command["request"]),
    }


def _sanitize_error(exc: BaseException) -> str:
    # Provider, subprocess, OAuth and Calendar exceptions may contain event
    # titles, local paths, account data or credential forms we do not yet know.
    # The source receives the truthful capability state, never exception text.
    return "safe capability error"


def _plan_reference(
    value: Any, *, require_before_state: bool = False
) -> dict[str, Any]:
    expected = {"run_id", "artifact_version", "checksum"}
    if require_before_state:
        expected.add("before_state_hash")
    if not isinstance(value, dict) or set(value) != expected:
        raise CalendarWorkerError("Calendar plan workflow reference is invalid")
    if not isinstance(value.get("run_id"), str) or UUID.fullmatch(value["run_id"]) is None:
        raise CalendarWorkerError("Calendar plan run reference is invalid")
    version = value.get("artifact_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CalendarWorkerError("Calendar plan artifact version is invalid")
    if not isinstance(value.get("checksum"), str) or SHA256.fullmatch(value["checksum"]) is None:
        raise CalendarWorkerError("Calendar plan checksum is invalid")
    if require_before_state and (
        not isinstance(value.get("before_state_hash"), str)
        or SHA256.fullmatch(value["before_state_hash"]) is None
    ):
        raise CalendarWorkerError("Calendar before-state hash is invalid")
    return dict(value)


def _approval_token(command: dict[str, Any], plan: dict[str, Any], session_id: str) -> str:
    binding = {
        "command_id": command["command_id"],
        "idempotency_key": command["idempotency_key"],
        "plan": plan,
        "session_id": session_id,
    }
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"calendar-approval:v1:{digest}"


def _base_result(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "calendar-result.v1",
        "command_id": command["command_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "operation": command["operation"],
        "verified": True,
    }


def _public_plan_preview(request: dict[str, Any], plan: Any) -> dict[str, Any]:
    if (
        not isinstance(plan, dict)
        or plan.get("schemaVersion") != 1
        or plan.get("mode") != "plan"
        or plan.get("readOnly") is not True
        or plan.get("ready") is not True
        or plan.get("calendarId") != "primary"
        or plan.get("operation") != request["operation"]
        or plan.get("blockKey") != request["block_key"]
        or plan.get("blockers") != []
        or not isinstance(plan.get("checksum"), str)
        or SHA256.fullmatch(plan["checksum"]) is None
    ):
        raise CalendarWorkerError("Calendar plan preview is invalid")
    linear = plan.get("linearIssue")
    event = plan.get("event")
    expected_linear = request["linear_url"]
    if expected_linear:
        linear_matches = isinstance(linear, dict) and linear.get("url") == expected_linear
    else:
        linear_matches = linear is None
    if not linear_matches or not isinstance(event, dict):
        raise CalendarWorkerError("Calendar plan preview does not match the request")
    operation = request["operation"]
    if operation == "delete":
        if event != {}:
            raise CalendarWorkerError("Calendar delete preview is invalid")
        summary = details = start = end = ""
    else:
        start_value = event.get("start")
        end_value = event.get("end")
        expected_description = request["details"].strip()
        if request["linear_url"]:
            marker = f"Linear: {request['linear_url']}"
            expected_description = (
                f"{expected_description}\n\n{marker}" if expected_description else marker
            )
        if (
            event.get("summary") != request["summary"].strip()
            or event.get("description") != expected_description
            or not isinstance(start_value, dict)
            or not isinstance(end_value, dict)
            or start_value.get("timeZone") != "Europe/Kyiv"
            or end_value.get("timeZone") != "Europe/Kyiv"
            or not isinstance(start_value.get("dateTime"), str)
            or not isinstance(end_value.get("dateTime"), str)
        ):
            raise CalendarWorkerError("Calendar event preview does not match the request")
        summary = event["summary"]
        details = request["details"].strip()
        start = start_value["dateTime"]
        end = end_value["dateTime"]
    return {
        "operation": operation,
        "block_key": request["block_key"],
        "summary": summary,
        "details": details,
        "start": start,
        "end": end,
        "timezone": "Europe/Kyiv",
        "linear_url": request["linear_url"],
    }


def _safe_read_data(operation: str, data: Any) -> dict[str, Any]:
    common = {"operation", "status", "timezone", "window", "bounds"}
    extras = {
        "inventory": {"calendar_count", "writable_calendar_count"},
        "events": {"calendar_count", "writable_calendar_count", "event_count", "all_day_events", "recurring_events"},
        "freebusy": {"calendar_count", "writable_calendar_count", "busy_intervals"},
    }
    allowed = common | extras[operation]
    if (
        not isinstance(data, dict)
        or data.get("operation") != operation
        or data.get("status") != "ok"
        or not set(data).issubset(allowed)
        or not common.issubset(data)
        or data.get("timezone") != "Europe/Kyiv"
    ):
        raise CalendarWorkerError("Calendar read workflow did not return verified bounded output")
    bounds = data.get("bounds")
    if not isinstance(bounds, dict) or set(bounds) != {"start", "end"}:
        raise CalendarWorkerError("Calendar read bounds are invalid")
    try:
        start = datetime.fromisoformat(bounds["start"])
        end = datetime.fromisoformat(bounds["end"])
    except (TypeError, ValueError) as exc:
        raise CalendarWorkerError("Calendar read bounds are invalid") from exc
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise CalendarWorkerError("Calendar read bounds are invalid")
    for field in extras[operation]:
        if field in data and (
            isinstance(data[field], bool)
            or not isinstance(data[field], int)
            or data[field] < 0
            or data[field] > 1_000_000
        ):
            raise CalendarWorkerError("Calendar read count is invalid")
    serialized = json.dumps(data, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 8_192 or any(
        pattern.search(serialized) for pattern in CREDENTIAL_PATTERNS
    ):
        raise CalendarWorkerError("Calendar read workflow returned unsafe data")
    return dict(data)


def execute_calendar_command(
    command: dict[str, Any], *, session_id: str, db_path: Path,
    workflows: Any, approval_loader: Callable[[str, Path], dict[str, Any]] = _load_approval_plan,
    reservation_check: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    operation = command["operation"]
    request = command["request"]
    base = _base_result(command)
    if operation in {"inventory", "events", "freebusy"}:
        reservation_check()
        data = _safe_read_data(operation, workflows.read(operation, request.get("window")))
        return {**base, "phase": "completed", "outcome": "read", "data": data}
    if operation == "plan_write":
        reservation_check()
        planned = workflows.plan(request)
        if not isinstance(planned, dict) or set(planned) != {"run_id", "artifact_version", "preview"}:
            raise CalendarWorkerError("Calendar plan workflow result is invalid")
        raw_plan = planned["preview"]
        preview = _public_plan_preview(request, raw_plan)
        reservation_check()
        snapshot = workflows.snapshot(request)
        expected_identifier_match = PUBLIC_ISSUE_URL.fullmatch(request["linear_url"])
        expected_identifier = (
            expected_identifier_match.group(1) if expected_identifier_match else None
        )
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {
                "operation", "status", "linearIssue", "blockKey", "beforeStateHash"
            }
            or snapshot.get("operation") != "snapshot"
            or snapshot.get("status") != "ok"
            or snapshot.get("linearIssue") != expected_identifier
            or snapshot.get("blockKey") != request["block_key"]
            or not isinstance(snapshot.get("beforeStateHash"), str)
            or SHA256.fullmatch(snapshot["beforeStateHash"]) is None
        ):
            raise CalendarWorkerError("Calendar before-state snapshot is invalid")
        plan = _plan_reference({
            "run_id": planned["run_id"],
            "artifact_version": planned["artifact_version"],
            "checksum": raw_plan["checksum"],
            "before_state_hash": snapshot["beforeStateHash"],
        }, require_before_state=True)
        return {
            **base,
            "phase": "awaiting_approval",
            "outcome": "planned",
            "preview": preview,
            "approval_reference": _approval_token(command, plan, session_id),
            "plan_reference": plan,
        }
    reference = request.get("approval_reference")
    if not isinstance(reference, str) or APPROVAL_REFERENCE.fullmatch(reference) is None:
        raise CalendarWorkerError("Calendar approval reference is invalid")
    approved_plan = approval_loader(reference, db_path)
    if (
        not isinstance(approved_plan, dict)
        or approved_plan.get("source_profile") != command["source_profile"]
        or approved_plan.get("session_id") != session_id
        or approved_plan.get("approval_reference") != reference
    ):
        raise CalendarWorkerError("Calendar approval must come from the same exact source session")
    plan = _plan_reference(
        approved_plan.get("plan_reference"), require_before_state=True
    )
    approved_request = approved_plan.get("request")
    if not isinstance(approved_request, dict):
        raise CalendarWorkerError("Calendar approval plan request binding is invalid")
    reservation_check()
    started = workflows.start_approval(plan)
    if not isinstance(started, dict) or started.get("status") != "suspended":
        raise CalendarWorkerError("Calendar approval workflow did not suspend")
    approval_run_id = started.get("run_id")
    if not isinstance(approval_run_id, str) or UUID.fullmatch(approval_run_id) is None:
        raise CalendarWorkerError("Calendar approval workflow run is invalid")
    reservation_check()
    workflows.approve(approval_run_id)
    reservation_check()
    resumed = workflows.resume_approval(approval_run_id)
    if (
        not isinstance(resumed, dict)
        or resumed.get("run_id") != approval_run_id
        or resumed.get("status") != "succeeded"
    ):
        raise CalendarWorkerError("Calendar approval workflow did not resume successfully")
    approval = _plan_reference({
        "run_id": approval_run_id,
        "artifact_version": resumed.get("artifact_version"),
        "checksum": resumed.get("checksum"),
    })
    reservation_check()
    live_snapshot = workflows.snapshot(approved_request)
    expected_identifier_match = PUBLIC_ISSUE_URL.fullmatch(
        approved_request.get("linear_url", "")
    )
    expected_identifier = (
        expected_identifier_match.group(1) if expected_identifier_match else None
    )
    if (
        not isinstance(live_snapshot, dict)
        or set(live_snapshot) != {
            "operation", "status", "linearIssue", "blockKey", "beforeStateHash"
        }
        or live_snapshot.get("operation") != "snapshot"
        or live_snapshot.get("status") != "ok"
        or live_snapshot.get("linearIssue") != expected_identifier
        or live_snapshot.get("blockKey") != approved_request.get("block_key")
        or live_snapshot.get("beforeStateHash") != plan["before_state_hash"]
    ):
        raise CalendarWorkerError("Calendar target changed after owner preview")
    reservation_check()
    data = workflows.apply(plan, approval)
    required_keys = {"operation", "status", "reused", "blockKey"}
    allowed_keys = required_keys | {"linearIssue"}
    if (
        not isinstance(data, dict)
        or not required_keys.issubset(data)
        or not set(data).issubset(allowed_keys)
        or data.get("status") != "verified"
        or data.get("operation") != approved_request.get("operation")
        or not isinstance(data.get("reused"), bool)
        or data.get("linearIssue") != expected_identifier
        or data.get("blockKey") != approved_request.get("block_key")
    ):
        raise CalendarWorkerError("Calendar apply workflow lacks verified read-back")
    return {
        **base,
        "phase": "completed",
        "outcome": "no_op" if data["reused"] else "applied",
        "data": data,
    }


class SwampCalendarWorkflows:
    """Fixed-command adapter for the reviewed Calendar Swamp workflows."""

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        revision_file: Path | None = None,
    ) -> None:
        self.workspace = workspace or RUNTIME_WORKSPACE
        self.revision_file = revision_file or RUNTIME_REVISION_FILE

    def _json(self, argv: list[str]) -> dict[str, Any]:
        _verify_runtime_workspace(self.workspace, self.revision_file)
        completed = subprocess.run(
            argv, cwd=self.workspace, capture_output=True, text=True,
            check=False, shell=False, timeout=600,
        )
        if completed.returncode != 0:
            raise CalendarWorkerError("Calendar workflow command failed")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CalendarWorkerError("Calendar workflow returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise CalendarWorkerError("Calendar workflow returned non-object JSON")
        return value

    @staticmethod
    def _artifact_version(run: dict[str, Any]) -> int:
        candidates = [run.get("artifactVersion"), run.get("artifact_version")]
        outputs = run.get("outputs")
        if isinstance(outputs, dict) and isinstance(outputs.get("result"), dict):
            candidates.append(outputs["result"].get("version"))
        for job in run.get("jobs", []):
            if not isinstance(job, dict):
                continue
            for step in job.get("steps", []):
                if not isinstance(step, dict):
                    continue
                for artifact in step.get("dataArtifacts", []):
                    if isinstance(artifact, dict) and artifact.get("name") == "result":
                        candidates.append(artifact.get("version"))
        valid = {
            candidate
            for candidate in candidates
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0
        }
        if len(valid) == 1:
            return valid.pop()
        if len(valid) > 1:
            raise CalendarWorkerError("Calendar workflow returned ambiguous result artifacts")
        raise CalendarWorkerError("Calendar workflow omitted result artifact version")

    def _result_artifact(
        self, model: str, workflow: str, run: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        run_id = run.get("id")
        if (
            not isinstance(run_id, str)
            or UUID.fullmatch(run_id) is None
            or run.get("status") != "succeeded"
            or run.get("workflowName") != workflow
        ):
            raise CalendarWorkerError("Calendar workflow did not succeed")
        version = self._artifact_version(run)
        artifact = self._json(["swamp", "data", "get", model, "result", "--version", str(version), "--json"])
        owner = artifact.get("ownerDefinition")
        content = artifact.get("content")
        if (
            artifact.get("modelName") != model
            or artifact.get("name") != "result"
            or artifact.get("version") != version
            or not isinstance(owner, dict)
            or owner.get("workflowRunId") != run_id
            or (
                owner.get("workflowName") is not None
                and owner.get("workflowName") != workflow
            )
            or not isinstance(content, dict)
            or content.get("exitCode") != 0
            or not isinstance(content.get("stdout"), str)
        ):
            raise CalendarWorkerError("Calendar workflow artifact provenance is invalid")
        try:
            result = json.loads(content["stdout"])
        except json.JSONDecodeError as exc:
            raise CalendarWorkerError("Calendar workflow artifact content is invalid") from exc
        if not isinstance(result, dict):
            raise CalendarWorkerError("Calendar workflow artifact result is invalid")
        return version, result

    def read(self, operation: str, window: str) -> dict[str, Any]:
        run = self._json([
            "swamp", "workflow", "run", "google-calendar-read",
            "--input", f"operation={operation}", "--input", f"window={window}", "--json",
        ])
        _version, result = self._result_artifact(
            "google-calendar-read", "google-calendar-read", run
        )
        return result

    def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        mapping = {
            "operation": "operation", "block_key": "blockKey", "summary": "summary",
            "start": "start", "end": "end", "linear_url": "linearUrl", "details": "details",
        }
        argv = ["swamp", "workflow", "run", "google-calendar-write-plan"]
        for source, target in mapping.items():
            argv.extend(["--input", f"{target}={request[source]}"])
        argv.append("--json")
        run = self._json(argv)
        version, preview = self._result_artifact(
            "google-calendar-write", "google-calendar-write-plan", run
        )
        return {"run_id": run["id"], "artifact_version": version, "preview": preview}

    def snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        run = self._json([
            "swamp", "workflow", "run", "google-calendar-write-snapshot",
            "--input", f"blockKey={request['block_key']}",
            "--input", f"linearUrl={request['linear_url']}", "--json",
        ])
        _version, result = self._result_artifact(
            "google-calendar-write", "google-calendar-write-snapshot", run
        )
        return result

    def start_approval(self, plan: dict[str, Any]) -> dict[str, Any]:
        value = self._json([
            "swamp", "workflow", "run", "google-calendar-write-approval",
            "--input", f"planRunId={plan['run_id']}",
            "--input", f"planArtifactVersion={plan['artifact_version']}",
            "--input", f"planChecksum={plan['checksum']}", "--json",
        ])
        return {"run_id": value.get("id"), "status": value.get("status")}

    def approve(self, run_id: str) -> None:
        value = self._json([
            "swamp", "workflow", "approve", "google-calendar-write-approval",
            "approve-calendar-write", "--run", run_id, "--json",
        ])
        if value.get("runId") != run_id:
            raise CalendarWorkerError("Calendar approval command returned the wrong run")

    def resume_approval(self, run_id: str) -> dict[str, Any]:
        run = self._json([
            "swamp", "workflow", "resume", "google-calendar-write-approval",
            "--run", run_id, "--json",
        ])
        version, result = self._result_artifact(
            "google-calendar-write-approval", "google-calendar-write-approval", run
        )
        return {
            "run_id": run.get("id"), "status": run.get("status"),
            "artifact_version": version, "checksum": result.get("checksum"),
        }

    def apply(self, plan: dict[str, Any], approval: dict[str, Any]) -> dict[str, Any]:
        run = self._json([
            "swamp", "workflow", "run", "google-calendar-write-apply",
            "--input", f"planRunId={plan['run_id']}",
            "--input", f"planArtifactVersion={plan['artifact_version']}",
            "--input", f"planChecksum={plan['checksum']}",
            "--input", f"approvalRunId={approval['run_id']}",
            "--input", f"approvalArtifactVersion={approval['artifact_version']}",
            "--input", f"approvalChecksum={approval['checksum']}",
            "--input", f"beforeStateHash={plan['before_state_hash']}", "--json",
        ])
        _version, result = self._result_artifact(
            "google-calendar-write", "google-calendar-write-apply", run
        )
        return result


class HermesKanbanLifecycle:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id

    @staticmethod
    def _require_ok(raw: str, action: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CalendarWorkerError(f"Kanban {action} returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise CalendarWorkerError(f"Kanban {action} failed")

    def complete(self, *, summary: str, result: str) -> None:
        from tools.kanban_tools import _handle_complete
        self._require_ok(_handle_complete({"task_id": self.task_id, "summary": summary, "result": result}), "complete")

    def block(self, *, reason: str, kind: str) -> None:
        from tools.kanban_tools import _handle_block
        self._require_ok(_handle_block({"task_id": self.task_id, "reason": reason, "kind": kind}), "block")


def _completed_result_path(command: dict[str, Any], environ: Any) -> Path:
    home = Path(
        str(environ.get("HERMES_HOME") or "/Users/hermes/.hermes/profiles/personal-assistant")
    )
    journal_identity = (
        command["command_id"] + "\0" + command["idempotency_key"]
    ).encode("ascii")
    digest = hashlib.sha256(journal_identity).hexdigest()
    return home / "calendar-command-lane" / "completed" / f"{digest}.json"


def _validate_completed_result(
    command: dict[str, Any], result: Any, session_id: str
) -> dict[str, Any]:
    common = {
        "schema_version", "command_id", "idempotency_key", "source_profile",
        "operation", "verified", "phase", "outcome",
    }
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != "calendar-result.v1"
        or result.get("command_id") != command["command_id"]
        or result.get("idempotency_key") != command["idempotency_key"]
        or result.get("source_profile") != command["source_profile"]
        or result.get("operation") != command["operation"]
        or result.get("verified") is not True
    ):
        raise CalendarWorkerError("completed Calendar journal binding is invalid")
    operation = command["operation"]
    if operation in {"inventory", "events", "freebusy"}:
        if (
            set(result) != common | {"data"}
            or result.get("phase") != "completed"
            or result.get("outcome") != "read"
        ):
            raise CalendarWorkerError("completed Calendar read journal is invalid")
        data = _safe_read_data(operation, result.get("data"))
        if data.get("window") != command["request"]["window"]:
            raise CalendarWorkerError("completed Calendar read journal scope drifted")
    elif operation == "plan_write":
        if set(result) != common | {
            "preview", "approval_reference", "plan_reference"
        }:
            raise CalendarWorkerError("completed Calendar plan journal is invalid")
        plan = _plan_reference(
            result.get("plan_reference"), require_before_state=True
        )
        if (
            result.get("phase") != "awaiting_approval"
            or result.get("outcome") != "planned"
            or not isinstance(result.get("preview"), dict)
            or result.get("approval_reference")
            != _approval_token(command, plan, session_id)
        ):
            raise CalendarWorkerError("completed Calendar plan journal binding is invalid")
    elif operation == "approve_write":
        data = result.get("data")
        required_data = {"operation", "status", "reused", "blockKey"}
        allowed_data = required_data | {"linearIssue"}
        if (
            set(result) != common | {"data"}
            or result.get("phase") != "completed"
            or result.get("outcome") not in {"applied", "no_op"}
            or not isinstance(data, dict)
            or not required_data.issubset(data)
            or not set(data).issubset(allowed_data)
            or data.get("status") != "verified"
            or not isinstance(data.get("reused"), bool)
        ):
            raise CalendarWorkerError("completed Calendar apply journal is invalid")
    else:
        raise CalendarWorkerError("completed Calendar journal operation is invalid")
    return dict(result)


def _load_completed_result(
    command: dict[str, Any], environ: Any, session_id: str
) -> dict[str, Any] | None:
    path = _completed_result_path(command, environ)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalendarWorkerError("completed Calendar journal is unreadable") from exc
    return _validate_completed_result(command, value, session_id)


def _write_completed_result(
    command: dict[str, Any], result: dict[str, Any], environ: Any
) -> None:
    path = _completed_result_path(command, environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def _execution_guard(command: dict[str, Any], environ: Any):
    """Serialize one persisted command across load, execution, and journaling."""
    journal = _completed_result_path(command, environ)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.parent.chmod(0o700)
    lock_path = journal.with_suffix(".execute.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def handle_pa_calendar_execute(args: dict[str, Any], **kwargs: Any) -> str:
    environ = kwargs.get("environ") or os.environ
    profile = str(environ.get("HERMES_PROFILE") or "")
    task_id = str(environ.get("HERMES_KANBAN_TASK") or "")
    raw_run_id = str(environ.get("HERMES_KANBAN_RUN_ID") or "")
    if profile != "personal-assistant":
        return json.dumps({"status": "rejected", "error": "tool requires personal-assistant profile"}, sort_keys=True)
    if not TASK_ID.fullmatch(task_id):
        return json.dumps({"status": "rejected", "error": "tool requires a current Kanban task"}, sort_keys=True)
    if not raw_run_id.isdigit() or int(raw_run_id) < 1:
        return json.dumps({"status": "rejected", "error": "tool requires a current Kanban run"}, sort_keys=True)
    if args != {}:
        return json.dumps({"status": "rejected", "error": "tool accepts no model-supplied command"}, sort_keys=True)
    db_path = Path(str(environ.get("HERMES_KANBAN_DB") or ""))
    if not db_path.is_absolute() or db_path.name != "kanban.db":
        return json.dumps({"status": "rejected", "error": "tool requires a pinned Kanban database"}, sort_keys=True)
    claim_lock = str(environ.get("HERMES_KANBAN_CLAIM_LOCK") or "").strip()
    if not claim_lock or len(claim_lock) > 256:
        return json.dumps({"status": "rejected", "error": "tool requires the current claim lock"}, sort_keys=True)

    loader = kwargs.get("task_loader") or _load_current_task
    try:
        task = loader(task_id, db_path)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return json.dumps({
            "status": "rejected", "error": "current Kanban task could not be loaded",
            "error_class": type(exc).__name__,
        }, sort_keys=True)
    if (
        _task_value(task, "id") != task_id
        or _task_value(task, "assignee") != "personal-assistant"
        or _task_value(task, "status") != "running"
        or _task_value(task, "current_run_id") != int(raw_run_id)
        or not isinstance(_task_value(task, "session_id"), str)
    ):
        return json.dumps({"status": "rejected", "error": "current Kanban task/run binding is invalid"}, sort_keys=True)
    try:
        command = _command_from_task(task)
    except Exception as exc:
        command_error: Exception | None = exc
        command = {}
    else:
        command_error = None
    reserver = kwargs.get("run_reserver") or _reserve_current_run
    try:
        reserved = reserver(task_id, int(raw_run_id), db_path, claim_lock)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return json.dumps({
            "status": "rejected", "error": "current Kanban run could not be reserved",
            "error_class": type(exc).__name__,
        }, sort_keys=True)
    if not reserved:
        return json.dumps({"status": "rejected", "error": "current Kanban run was superseded"}, sort_keys=True)

    lifecycle = (kwargs.get("lifecycle_factory") or HermesKanbanLifecycle)(task_id)
    session_id = _task_value(task, "session_id")
    result_loader = kwargs.get("result_loader") or _load_completed_result
    result_writer = kwargs.get("result_writer") or _write_completed_result
    execution_guard = kwargs.get("execution_guard") or _execution_guard
    try:
        if command_error is not None:
            raise command_error
        def reservation_check() -> None:
            if not reserver(task_id, int(raw_run_id), db_path, claim_lock):
                raise CalendarRunSuperseded("current Kanban run was superseded")
        with execution_guard(command, environ):
            reservation_check()
            result = result_loader(command, environ, session_id)
            if result is None:
                workflows = (kwargs.get("workflow_runner_factory") or SwampCalendarWorkflows)()
                result = execute_calendar_command(
                    command,
                    session_id=session_id,
                    db_path=db_path,
                    workflows=workflows,
                    approval_loader=kwargs.get("approval_loader") or _load_approval_plan,
                    reservation_check=reservation_check,
                )
                result = _validate_completed_result(command, result, session_id)
                result_writer(command, result, environ)
    except CalendarRunSuperseded:
        return json.dumps(
            {"status": "rejected", "error": "current Kanban run was superseded"},
            sort_keys=True,
        )
    except Exception as exc:
        reason = _sanitize_error(exc)
        try:
            lifecycle.block(reason=f"Calendar command failed: {reason}", kind="capability")
        except Exception as block_exc:
            return json.dumps({
                "status": "rejected",
                "error": "Calendar command failed and blocker could not be recorded",
                "block_error": type(block_exc).__name__,
            }, sort_keys=True)
        return json.dumps({"status": "blocked", "task_id": task_id, "error": reason}, ensure_ascii=False, sort_keys=True)

    result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
    summary = (
        "Calendar preview is ready for explicit owner approval."
        if result["phase"] == "awaiting_approval"
        else "Calendar request completed with verified read-back."
    )
    try:
        lifecycle.complete(summary=summary, result=result_json)
    except Exception:
        # The verified result is already durable. Do not convert external success
        # into a blocker; a later run replays the journal and retries completion.
        return json.dumps(
            {"status": "rejected", "error": "Calendar completion persistence is unavailable"},
            sort_keys=True,
        )
    return json.dumps(
        {"status": "completed", "task_id": task_id, "verified": True}, sort_keys=True
    )


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="pa_calendar_execute",
        toolset="personal-assistant-calendar",
        schema=PA_CALENDAR_EXECUTE_SCHEMA,
        handler=handle_pa_calendar_execute,
        description=PA_CALENDAR_EXECUTE_SCHEMA["description"],
        emoji="📅",
    )
