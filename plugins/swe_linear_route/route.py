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
    """Exact source identity required for a root-DM wake route."""

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
    text: str,
    *,
    uuid_factory: UUIDFactory = _uuid4,
) -> ParsedRequest:
    """Parse the exact supported Russian comment request into linear-command.v1."""
    if not isinstance(text, str):
        raise RouteError("request must be text")
    match = COMMENT_REQUEST.fullmatch(text.strip())
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
    command: dict[str, Any] = {
        "schema_version": "linear-command.v1",
        "command_id": uuid_factory(),
        "correlation_id": uuid_factory(),
        "idempotency_key": "pending",
        "source_profile": "swe",
        "operation": "add_comment",
        "target": {"type": "issue", "identifier": identifier},
        "change": {"body": body},
        "policy": {"mode": "standard"},
    }
    command["idempotency_key"] = _semantic_key(command)
    return ParsedRequest(command=command)


def validate_source_context(source: SourceContext) -> None:
    """Require an exact SWE Telegram root-DM source session."""
    if source.profile != "swe":
        raise RouteError("source profile must be swe")
    if source.platform != "telegram":
        raise RouteError("source platform must be telegram")
    if source.chat_type != "dm":
        raise RouteError("source chat must be a root DM")
    if source.thread_id:
        raise RouteError("source root DM must not carry a thread/topic id")
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
    text: str,
    *,
    source: SourceContext,
    board: Any,
    uuid_factory: UUIDFactory = _uuid4,
) -> dict[str, Any]:
    """Create or replay one audited PM task and promote only after route pass."""
    validate_source_context(source)
    parsed = parse_linear_request(text, uuid_factory=uuid_factory)
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
    board.release(task_id, "exact source root-DM wake route verified")
    return {
        "status": "queued",
        "task_id": task_id,
        "idempotency_key": idempotency_key,
        "replayed": replayed,
        "route_audit": audit,
    }
