"""Typed Calendar ingress over the existing exact-session Kanban route."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .route import SourceContext, validate_source_context


READ_OPERATIONS = {"inventory", "events", "freebusy"}
WRITE_OPERATIONS = {"create", "update", "delete"}
WINDOWS = {"today", "next-7-days", "next-30-days"}
WRITE_FIELDS = {
    "operation", "block_key", "summary", "start", "end", "linear_url", "details"
}
PUBLIC_ISSUE_URL = re.compile(
    r"^https://linear\.app/[A-Za-z0-9_-]+/issue/(SIS-[1-9][0-9]*)/"
    r"[A-Za-z0-9][A-Za-z0-9_-]*$"
)
BLOCK_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LOCAL_DATETIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?$"
)
APPROVAL_REFERENCE = re.compile(r"^calendar-approval:v1:[a-f0-9]{64}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CREDENTIAL_SHAPES = (
    re.compile(r"Authorization:\s*\S+(?:\s+\S+)?", re.IGNORECASE),
    re.compile(r"\b(?:ya29\.|1//)[A-Za-z0-9._-]{12,}\b"),
    re.compile(r'"(?:client_secret|refresh_token|access_token)"\s*:', re.IGNORECASE),
)


class CalendarRouteError(RuntimeError):
    """A Calendar request violates the bounded source contract."""


class CalendarRequestError(CalendarRouteError):
    """A safe owner-visible validation error in the submitted request."""


@dataclass(frozen=True)
class ParsedCalendarRequest:
    command: dict[str, Any]


UUIDFactory = Callable[[], str]


def _uuid4() -> str:
    return str(uuid.uuid4())


def _semantic_key(command: dict[str, Any]) -> str:
    semantic = {
        "operation": command["operation"],
        "request": command["request"],
    }
    digest = hashlib.sha256(
        json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"calendar:v1:{digest[:32]}"


def _validate_text(value: Any, field: str, maximum: int, *, required: bool) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise CalendarRouteError(f"{field} must be a string of at most {maximum} characters")
    if required and not value.strip():
        raise CalendarRouteError(f"{field} must be non-empty")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise CalendarRouteError(f"{field} contains control characters")
    if field in {"summary", "details"} and any(char in value for char in ("'", "\r", "\n")):
        raise CalendarRouteError(f"{field} is not representable by the fixed Calendar workflow")
    if any(pattern.search(value) for pattern in CREDENTIAL_SHAPES):
        raise CalendarRouteError(f"{field} contains credential-shaped data")
    return value


def _write_request(request: dict[str, Any]) -> dict[str, Any]:
    if set(request) not in (WRITE_FIELDS, WRITE_FIELDS - {"linear_url"}):
        raise CalendarRouteError("Calendar write request has invalid fields")
    operation = request.get("operation")
    if operation not in WRITE_OPERATIONS:
        raise CalendarRouteError("Calendar write operation is invalid")
    block_key = request.get("block_key")
    if not isinstance(block_key, str) or len(block_key) > 64 or BLOCK_KEY.fullmatch(block_key) is None:
        raise CalendarRouteError("block_key must be a bounded safe slug")
    summary = _validate_text(request.get("summary"), "summary", 200, required=operation != "delete")
    details = _validate_text(request.get("details"), "details", 4000, required=False)
    start = _validate_text(request.get("start"), "start", 19, required=operation != "delete")
    end = _validate_text(request.get("end"), "end", 19, required=operation != "delete")
    linear_url = request.get("linear_url", "")
    if not isinstance(linear_url, str) or (
        linear_url and PUBLIC_ISSUE_URL.fullmatch(linear_url) is None
    ):
        raise CalendarRouteError("linear_url must be empty or one canonical public SIS issue URL")
    if operation == "delete":
        if any((summary, start, end, details)):
            raise CalendarRouteError("delete requires empty event fields")
    else:
        parsed_boundaries: dict[str, datetime] = {}
        for field, value in (("start", start), ("end", end)):
            if LOCAL_DATETIME.fullmatch(value) is None:
                raise CalendarRouteError(f"{field} must be a local ISO datetime")
            try:
                parsed_boundaries[field] = datetime.fromisoformat(value)
            except ValueError as exc:
                raise CalendarRouteError(f"{field} must be a valid datetime") from exc
        if parsed_boundaries["end"] <= parsed_boundaries["start"]:
            raise CalendarRouteError("end must be after start")
    return {**request, "linear_url": linear_url}


def _parse_calendar_request(
    request: Any,
    *,
    source_profile: str,
    uuid_factory: UUIDFactory = _uuid4,
) -> ParsedCalendarRequest:
    if not isinstance(request, dict):
        raise CalendarRouteError("Calendar request must be an object")
    operation = request.get("operation")
    if operation in READ_OPERATIONS:
        if set(request) != {"operation", "window"} or request.get("window") not in WINDOWS:
            raise CalendarRouteError("Calendar read request is outside the bounded allowlist")
        command_operation = operation
        canonical_request = {"window": request["window"]}
    elif operation in WRITE_OPERATIONS:
        command_operation = "plan_write"
        canonical_request = _write_request(request)
    elif operation == "approve":
        if set(request) != {"operation", "approval_reference"}:
            raise CalendarRouteError("Calendar approval request has invalid fields")
        reference = request.get("approval_reference")
        if not isinstance(reference, str) or APPROVAL_REFERENCE.fullmatch(reference) is None:
            raise CalendarRouteError("approval_reference is invalid")
        command_operation = "approve_write"
        canonical_request = {"approval_reference": reference}
    else:
        raise CalendarRouteError("Calendar operation is outside the bounded allowlist")
    command = {
        "schema_version": "calendar-command.v1",
        "command_id": uuid_factory(),
        "source_profile": source_profile,
        "operation": command_operation,
        "request": canonical_request,
        "idempotency_key": "pending",
    }
    command["idempotency_key"] = _semantic_key(command)
    return ParsedCalendarRequest(command=command)


def parse_calendar_request(
    request: Any,
    *,
    source_profile: str,
    uuid_factory: UUIDFactory = _uuid4,
) -> ParsedCalendarRequest:
    try:
        return _parse_calendar_request(
            request, source_profile=source_profile, uuid_factory=uuid_factory
        )
    except CalendarRouteError as exc:
        raise CalendarRequestError(str(exc)) from exc


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
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"calendar-delivery:v1:{digest[:32]}"


def build_calendar_task_body(command: dict[str, Any]) -> str:
    return json.dumps(
        {
            "schema_version": "calendar-kanban-task.v1",
            "command": command,
            "worker_contract": {
                "profile": "personal-assistant",
                "tool": "pa_calendar_execute",
                "mode": "workflow_plan_approval_apply_read_back",
                "completion": "tool_completes_current_kanban_task",
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _expected_approval_reference(
    command: dict[str, Any], plan: dict[str, Any], session_id: str
) -> str:
    binding = {
        "command_id": command["command_id"],
        "idempotency_key": command["idempotency_key"],
        "plan": plan,
        "session_id": session_id,
    }
    digest = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"calendar-approval:v1:{digest}"


def _load_completed(task: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    try:
        envelope = json.loads(task.get("body", ""))
        result = json.loads(task.get("result", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CalendarRouteError("completed Calendar task is malformed") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schema_version", "command", "worker_contract"}
        or envelope.get("schema_version") != "calendar-kanban-task.v1"
        or envelope.get("worker_contract")
        != {
            "profile": "personal-assistant",
            "tool": "pa_calendar_execute",
            "mode": "workflow_plan_approval_apply_read_back",
            "completion": "tool_completes_current_kanban_task",
        }
        or not isinstance(envelope.get("command"), dict)
    ):
        raise CalendarRouteError("completed Calendar task has an invalid envelope")
    persisted = envelope["command"]
    semantic_fields = ("schema_version", "idempotency_key", "source_profile", "operation", "request")
    if any(persisted.get(field) != command.get(field) for field in semantic_fields):
        raise CalendarRouteError("completed Calendar replay does not match its command")
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != "calendar-result.v1"
        or result.get("verified") is not True
        or any(
            result.get(field) != persisted.get(field)
            for field in ("command_id", "idempotency_key", "source_profile", "operation")
        )
    ):
        raise CalendarRouteError("completed Calendar result is not bound to its command")
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 65_536 or any(
        pattern.search(serialized) for pattern in CREDENTIAL_SHAPES
    ):
        raise CalendarRouteError("completed Calendar result contains unsafe data")
    if result["operation"] == "plan_write":
        reference = result.get("approval_reference")
        preview = result.get("preview")
        plan = result.get("plan_reference")
        request = persisted["request"]
        session_id = task.get("session_id")
        expected_preview = {
            "operation", "block_key", "summary", "details", "start", "end",
            "timezone", "linear_url",
        }
        if (
            set(result)
            != {
                "schema_version", "command_id", "idempotency_key", "source_profile",
                "operation", "phase", "outcome", "preview", "approval_reference",
                "plan_reference", "verified",
            }
            or result.get("phase") != "awaiting_approval"
            or result.get("outcome") != "planned"
            or not isinstance(reference, str)
            or APPROVAL_REFERENCE.fullmatch(reference) is None
            or not isinstance(preview, dict)
            or set(preview) != expected_preview
            or preview.get("operation") != request.get("operation")
            or preview.get("block_key") != request.get("block_key")
            or preview.get("linear_url") != request.get("linear_url")
            or preview.get("timezone") != "Europe/Kyiv"
            or not isinstance(plan, dict)
            or set(plan) != {
                "run_id", "artifact_version", "checksum", "before_state_hash"
            }
            or not isinstance(plan.get("run_id"), str)
            or UUID.fullmatch(plan["run_id"]) is None
            or not isinstance(plan.get("artifact_version"), int)
            or isinstance(plan.get("artifact_version"), bool)
            or plan["artifact_version"] < 1
            or not isinstance(plan.get("checksum"), str)
            or SHA256.fullmatch(plan["checksum"]) is None
            or not isinstance(plan.get("before_state_hash"), str)
            or SHA256.fullmatch(plan["before_state_hash"]) is None
            or not isinstance(session_id, str)
            or reference
            != _expected_approval_reference(persisted, plan, session_id)
        ):
            raise CalendarRouteError("Calendar plan completion is invalid")
        return {
            "status": "completed",
            "phase": "awaiting_approval",
            "preview": preview,
            "approval_reference": reference,
        }
    data = result.get("data")
    if result["operation"] in READ_OPERATIONS:
        common = {"operation", "status", "timezone", "window", "bounds"}
        extras = {
            "inventory": {"calendar_count", "writable_calendar_count"},
            "events": {"calendar_count", "writable_calendar_count", "event_count", "all_day_events", "recurring_events"},
            "freebusy": {"calendar_count", "writable_calendar_count", "busy_intervals"},
        }
        allowed = common | extras[result["operation"]]
        bounds = data.get("bounds") if isinstance(data, dict) else None
        if (
            result.get("phase") != "completed"
            or result.get("outcome") != "read"
            or not isinstance(data, dict)
            or not set(data).issubset(allowed)
            or not common.issubset(data)
            or data.get("operation") != result["operation"]
            or data.get("status") != "ok"
            or data.get("timezone") != "Europe/Kyiv"
            or data.get("window") != persisted["request"].get("window")
            or not isinstance(bounds, dict)
            or set(bounds) != {"start", "end"}
        ):
            raise CalendarRouteError("Calendar read completion is invalid")
        try:
            start = datetime.fromisoformat(bounds["start"])
            end = datetime.fromisoformat(bounds["end"])
        except (TypeError, ValueError) as exc:
            raise CalendarRouteError("Calendar read completion is invalid") from exc
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise CalendarRouteError("Calendar read completion is invalid")
        for field in extras[result["operation"]]:
            if field in data and (
                isinstance(data[field], bool)
                or not isinstance(data[field], int)
                or data[field] < 0
                or data[field] > 1_000_000
            ):
                raise CalendarRouteError("Calendar read completion is invalid")
        return {"status": "completed", "phase": "completed", "data": data}
    if result["operation"] == "approve_write":
        allowed_apply = {"operation", "status", "reused", "linearIssue", "blockKey"}
        if (
            result.get("phase") != "completed"
            or result.get("outcome") not in {"applied", "no_op"}
            or not isinstance(data, dict)
            or set(data) != allowed_apply
            or data.get("operation") not in WRITE_OPERATIONS
            or data.get("status") != "verified"
            or not isinstance(data.get("reused"), bool)
            or (
                data.get("linearIssue") is not None
                and (
                    not isinstance(data.get("linearIssue"), str)
                    or re.fullmatch(r"SIS-[1-9][0-9]*", data["linearIssue"]) is None
                )
            )
            or not isinstance(data.get("blockKey"), str)
            or BLOCK_KEY.fullmatch(data["blockKey"]) is None
        ):
            raise CalendarRouteError("Calendar apply completion is invalid")
        return {
            "status": "completed",
            "phase": "completed",
            "changed": result["outcome"] == "applied",
            "data": data,
        }
    raise CalendarRouteError("Calendar result operation is invalid")


def route_calendar_request(
    request: Any,
    *,
    source: SourceContext,
    board: Any,
    uuid_factory: UUIDFactory = _uuid4,
) -> dict[str, Any]:
    validate_source_context(source)
    command = parse_calendar_request(
        request, source_profile=source.profile, uuid_factory=uuid_factory
    ).command
    key = delivery_key(command["idempotency_key"], source)
    task, created = board.get_or_create_task(
        key,
        title=f"Calendar {command['operation']}",
        body=build_calendar_task_body(command),
        assignee="personal-assistant",
        skills=["personal-assistant-calendar-worker"],
        triage=True,
        idempotency_key=key,
        session_id=source.session_id,
        max_runtime_seconds=300,
    )
    if task.get("idempotency_key") != key or task.get("session_id") != source.session_id:
        raise CalendarRouteError("Calendar task is not bound to the exact source session")
    if not created:
        status = task.get("status")
        if status == "done":
            return _load_completed(task, command)
        if status == "blocked":
            return {"status": "blocked", "message": "Calendar routing or execution failed safely."}
        if status in {"todo", "ready", "running", "review"}:
            return {"status": "queued"}
        if status != "triage":
            raise CalendarRouteError("Calendar task has an unsupported state")
    if task.get("status") != "triage":
        raise CalendarRouteError("new Calendar task did not remain in triage")
    board.set_wake_route(task["id"], source)
    if board.audit_route(task["id"], source).get("result") != "pass":
        raise CalendarRouteError("Calendar route audit failed; task remains in triage")
    board.release(task["id"], "exact Calendar source-session wake route verified")
    return {"status": "queued"}
