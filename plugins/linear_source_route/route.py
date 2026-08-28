#!/usr/bin/env python3
"""Bounded SWE ingress for exact Linear commands over Hermes Kanban."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable


COMMENT_REQUEST = re.compile(
    r"^Добавь к (SIS-[1-9][0-9]*) комментарий:\s*(\S(?:.*\S)?)$",
    re.DOTALL,
)
SESSION_ID = re.compile(r"^[0-9]{8}_[0-9]{6}_[a-f0-9]{8}$")
PROFILE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
SPECIAL_PROFILES = {"broker", "project-manager"}
NUMERIC_ID = re.compile(r"^[1-9][0-9]*$")
TERMINAL_IN_FLIGHT = {"todo", "ready", "running", "review"}
CREDENTIAL_SHAPES = (
    re.compile(r"Authorization:\s*(?:Bearer|Basic)\s+\S+", re.IGNORECASE),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[bap]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class RouteError(RuntimeError):
    """The user command or source route violates the bounded contract."""


@dataclass(frozen=True)
class SourceContext:
    """Exact source identity required for a session-thread wake route."""

    session_id: str
    profile: str
    platform: str
    chat_id: str
    user_id: str
    chat_type: str
    thread_id: str = ""


@dataclass(frozen=True)
class ParsedRequest:
    """Validated command plus its stable semantic idempotency key."""

    command: dict[str, Any]


UUIDFactory = Callable[[], str]


def _uuid4() -> str:
    return str(uuid.uuid4())


def _semantic_key(command: dict[str, Any]) -> str:
    semantic = {
        key: command[key]
        for key in ("source_profile", "operation", "target", "change", "policy")
    }
    digest = hashlib.sha256(
        json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"linear:v1:{digest[:32]}"


def parse_linear_request(
    request: Any,
    *,
    source_profile: str = "swe",
    uuid_factory: UUIDFactory = _uuid4,
) -> ParsedRequest:
    """Parse one bounded source request into linear-command.v1."""
    if not PROFILE_NAME.fullmatch(source_profile):
        raise RouteError("source profile is invalid")
    if isinstance(request, str):
        match = COMMENT_REQUEST.fullmatch(request.strip())
        if match is None:
            raise RouteError(
                "unsupported Linear request; expected exact SIS-N comment command"
            )
        identifier, body = match.groups()
        body = body.strip()
        if len(body) > 4000:
            raise RouteError("comment body must be 1-4000 characters")
        if any(pattern.search(body) for pattern in CREDENTIAL_SHAPES):
            raise RouteError("comment body contains credential-shaped data")
        operation = "add_comment"
        target = {"type": "issue", "identifier": identifier}
        change = {"body": body}
    elif isinstance(request, dict):
        operation = request.get("operation")
        if operation == "change_state":
            if set(request) != {"operation", "identifier", "state"}:
                raise RouteError("structured state request has invalid fields")
            identifier = request.get("identifier")
            state = request.get("state")
            if not isinstance(identifier, str) or not re.fullmatch(
                r"SIS-[1-9][0-9]*", identifier
            ):
                raise RouteError("target must be an exact SIS-N identifier")
            if state not in {"Backlog", "Todo", "Research", "In Progress", "In Review"}:
                raise RouteError("state is not in the safe-state allowlist")
            target = {"type": "issue", "identifier": identifier}
            change = {"state": state}
        elif operation == "create_issue":
            expected = {
                "operation",
                "title",
                "description",
                "parent_identifier",
                "state",
                "priority",
            }
            if set(request) != expected:
                raise RouteError("structured create request has invalid fields")
            title = request.get("title")
            description = request.get("description")
            parent = request.get("parent_identifier")
            state = request.get("state")
            priority = request.get("priority")
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise RouteError("title must be 1-200 characters")
            if not isinstance(description, str) or len(description) > 10000:
                raise RouteError("description must be 0-10000 characters")
            if any(pattern.search(title + "\n" + description) for pattern in CREDENTIAL_SHAPES):
                raise RouteError("create request contains credential-shaped data")
            if not isinstance(parent, str) or not re.fullmatch(r"SIS-[1-9][0-9]*", parent):
                raise RouteError("parent must be an exact SIS-N identifier")
            if state not in {"Backlog", "Todo", "Research", "In Progress", "In Review"}:
                raise RouteError("state is not in the safe-state allowlist")
            if priority not in {"High", "Medium", "Low"}:
                raise RouteError("priority is not in the bounded allowlist")
            target = {"type": "team", "identifier": "SIS"}
            change = {
                "title": title.strip(),
                "description": description.strip(),
                "parent_identifier": parent,
                "state": state,
                "priority": priority,
            }
        else:
            raise RouteError("structured operation is not allowed")
    else:
        raise RouteError("request must be text or a structured request")
    command: dict[str, Any] = {
        "schema_version": "linear-command.v1",
        "command_id": uuid_factory(),
        "correlation_id": uuid_factory(),
        "idempotency_key": "pending",
        "source_profile": source_profile,
        "operation": operation,
        "target": target,
        "change": change,
        "policy": {"mode": "standard"},
    }
    command["idempotency_key"] = _semantic_key(command)
    return ParsedRequest(command=command)


def is_source_profile(profile: str) -> bool:
    return bool(PROFILE_NAME.fullmatch(profile)) and profile not in SPECIAL_PROFILES


def validate_source_context(source: SourceContext) -> None:
    """Require an exact user-facing Telegram DM session thread."""
    if not is_source_profile(source.profile):
        raise RouteError("source profile is not an allowed user-facing profile")
    if source.platform != "telegram":
        raise RouteError("source platform must be telegram")
    if source.chat_type != "dm":
        raise RouteError("source chat must be a DM")
    if not NUMERIC_ID.fullmatch(source.thread_id):
        raise RouteError("source thread id must be a positive numeric id")
    if not SESSION_ID.fullmatch(source.session_id):
        raise RouteError("source session id is invalid")
    if not NUMERIC_ID.fullmatch(source.chat_id):
        raise RouteError("source chat id must be a positive numeric id")
    if not NUMERIC_ID.fullmatch(source.user_id):
        raise RouteError("source user id must be a positive numeric id")


def build_task_body(command: dict[str, Any]) -> str:
    """Build a machine-readable PM worker envelope without credentials."""
    envelope = {
        "schema_version": "linear-kanban-task.v1",
        "command": command,
        "worker_contract": {
            "profile": "project-manager",
            "tool": "pm_linear_execute",
            "mode": "plan_apply_read_back",
            "completion": "tool_completes_current_kanban_task",
        },
    }
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


def _verified_replay(task: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    raw_result = task.get("result")
    try:
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except json.JSONDecodeError as exc:
        raise RouteError("completed replay has invalid structured result") from exc
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != "linear-result.v1"
        or result.get("verified") is not True
    ):
        raise RouteError("completed replay lacks a verified linear-result.v1")
    return {
        "status": "verified_no_op",
        "task_id": task["id"],
        "idempotency_key": idempotency_key,
        "replayed": True,
        "linear_result": result,
    }


def route_request(
    request: Any,
    *,
    source: SourceContext,
    board: Any,
    uuid_factory: UUIDFactory = _uuid4,
) -> dict[str, Any]:
    """Create or replay one audited PM task and promote only after route pass."""
    validate_source_context(source)
    parsed = parse_linear_request(
        request,
        source_profile=source.profile,
        uuid_factory=uuid_factory,
    )
    command = parsed.command
    idempotency_key = command["idempotency_key"]
    task = board.find_task(idempotency_key)
    replayed = task is not None

    if task is not None:
        if task.get("session_id") != source.session_id:
            raise RouteError("idempotent task belongs to a different source session")
        status = task.get("status")
        if status == "done":
            return _verified_replay(task, idempotency_key)
        if status == "blocked":
            return {
                "status": "blocked",
                "task_id": task["id"],
                "idempotency_key": idempotency_key,
                "replayed": True,
            }
        if status in TERMINAL_IN_FLIGHT:
            return {
                "status": "already_in_flight",
                "task_id": task["id"],
                "idempotency_key": idempotency_key,
                "replayed": True,
            }
        if status != "triage":
            raise RouteError(f"idempotent task has unsupported status: {status}")
    else:
        task = board.create_task(
            title=f"Linear {command['operation']} {command['target']['identifier']}",
            body=build_task_body(command),
            assignee="project-manager",
            skills=["project-manager-linear-worker"],
            triage=True,
            idempotency_key=idempotency_key,
            session_id=source.session_id,
            max_runtime_seconds=300,
        )
        if task.get("status") != "triage":
            raise RouteError("new Linear task did not remain in triage")
        if task.get("session_id") != source.session_id:
            raise RouteError("new Linear task did not persist the exact source session")

    task_id = task["id"]
    board.set_wake_route(task_id, source)
    audit = board.audit_route(task_id, source)
    if audit.get("result") != "pass":
        raise RouteError("route audit failed; task remains in triage")
    board.release(task_id, "exact source session-thread wake route verified")
    return {
        "status": "queued",
        "task_id": task_id,
        "idempotency_key": idempotency_key,
        "replayed": replayed,
        "route_audit": audit,
    }
