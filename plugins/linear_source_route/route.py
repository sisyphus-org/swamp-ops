#!/usr/bin/env python3
"""Bounded SWE ingress for exact Linear commands over Hermes Kanban."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
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
    re.compile(r"\blin_api_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[bap]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
SAFE_STATES = {"Backlog", "Todo", "Research", "In Progress", "In Review"}
RESERVED_MARKER = "<!-- linear-command"
MAX_HIERARCHY_BYTES = 24_576
MAX_ISSUE_TREE_BYTES = 65_536
PRIORITIES = {"High", "Medium", "Low"}
ISSUE_RELATION_TYPES = {"blocks", "blocked_by", "related"}


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
        key: command[key] for key in ("operation", "target", "change", "policy")
    }
    digest = hashlib.sha256(
        json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"linear:v2:{digest[:32]}"


def _delivery_key(mutation_key: str, source: SourceContext) -> str:
    delivery = {
        "mutation_key": mutation_key,
        "source_profile": source.profile,
        "platform": source.platform,
        "chat_id": source.chat_id,
        "user_id": source.user_id,
        "thread_id": source.thread_id,
        "session_id": source.session_id,
    }
    digest = hashlib.sha256(
        json.dumps(
            delivery,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"linear-delivery:v2:{digest[:32]}"


def _validate_clean_text(
    value: Any, path: str, *, maximum: int, required: bool
) -> None:
    if not isinstance(value, str) or len(value) > maximum:
        raise RouteError(f"{path} must be a string of at most {maximum} characters")
    if required and not value.strip():
        raise RouteError(f"{path} must be non-empty")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise RouteError(f"{path} contains control characters")
    if RESERVED_MARKER in value:
        raise RouteError(f"{path} contains the reserved marker")
    if any(pattern.search(value) for pattern in CREDENTIAL_SHAPES):
        raise RouteError(f"{path} contains credential-shaped data")


def _validate_hierarchy_request(request: dict[str, Any]) -> dict[str, Any]:
    if set(request) != {"operation", "project", "milestone", "issue"}:
        raise RouteError("structured hierarchy request has invalid fields")
    if len(json.dumps(request, ensure_ascii=False).encode("utf-8")) > MAX_HIERARCHY_BYTES:
        raise RouteError("structured hierarchy request exceeds the bounded size limit")
    specs = (
        ("project", {"name", "description"}, "name"),
        ("milestone", {"name", "description"}, "name"),
        ("issue", {"title", "description", "state"}, "title"),
    )
    change: dict[str, Any] = {}
    for kind, allowed, name_field in specs:
        spec = request.get(kind)
        if not isinstance(spec, dict) or name_field not in spec:
            raise RouteError(f"{kind} must contain {name_field}")
        if not set(spec).issubset(allowed):
            raise RouteError(f"{kind} contains unsupported fields")
        _validate_clean_text(
            spec[name_field], f"{kind}.{name_field}", maximum=200, required=True
        )
        if "description" in spec:
            _validate_clean_text(
                spec["description"],
                f"{kind}.description",
                maximum=10_000,
                required=False,
            )
        if kind == "issue" and "state" in spec and spec["state"] not in SAFE_STATES:
            raise RouteError("issue.state is not in the safe-state allowlist")
        change[kind] = dict(spec)
    return change


def _validate_project_management_request(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    milestone = operation in {"create_milestone", "update_milestone"}
    updating = operation in {"update_project", "update_milestone"}
    allowed = {"operation", "name", "description", "target_date"}
    required = {"operation", "name"}
    if milestone:
        allowed.add("project")
        required.add("project")
    if updating:
        allowed.add("new_name")
        if not (set(request) & {"new_name", "description", "target_date"}):
            raise RouteError(f"{operation} requires at least one managed field")
    if not required.issubset(request) or not set(request).issubset(allowed):
        raise RouteError("structured project management request has invalid fields")
    for field in ("project", "name", "new_name"):
        if field in request:
            _validate_clean_text(request[field], field, maximum=200, required=True)
    if "description" in request:
        _validate_clean_text(request["description"], "description", maximum=10_000, required=False)
    if "target_date" in request and request["target_date"] is not None:
        target_date = request["target_date"]
        if not isinstance(target_date, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", target_date
        ):
            raise RouteError("target_date must be ISO YYYY-MM-DD or null")
        try:
            date.fromisoformat(target_date)
        except ValueError as exc:
            raise RouteError("target_date must be a valid calendar date") from exc
    return {key: value for key, value in request.items() if key != "operation"}


def _validate_exact_issue_spec(spec: Any, path: str) -> dict[str, Any]:
    if not isinstance(spec, dict) or set(spec) != {
        "title",
        "description",
        "state",
        "priority",
    }:
        raise RouteError(
            f"{path} must contain exactly title, description, state, and priority"
        )
    _validate_clean_text(spec["title"], f"{path}.title", maximum=200, required=True)
    _validate_clean_text(
        spec["description"],
        f"{path}.description",
        maximum=10_000,
        required=False,
    )
    if spec["state"] not in SAFE_STATES:
        raise RouteError(f"{path}.state is not in the safe-state allowlist")
    if spec["priority"] not in PRIORITIES:
        raise RouteError(f"{path}.priority is not in the bounded allowlist")
    return dict(spec)


def _validate_scoped_issue_request(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    composite = operation == "converge_issue_tree"
    expected = {
        "operation",
        "project",
        "milestone",
        "issue",
        *(["sub_issues"] if composite else []),
    }
    if set(request) != expected:
        raise RouteError("structured scoped issue request has invalid fields")
    if len(json.dumps(request, ensure_ascii=False).encode()) > MAX_ISSUE_TREE_BYTES:
        raise RouteError("structured scoped issue request exceeds the bounded size limit")
    change: dict[str, Any] = {}
    for kind in ("project", "milestone"):
        spec = request.get(kind)
        if not isinstance(spec, dict) or "name" not in spec or not set(spec).issubset(
            {"name", "description"}
        ):
            raise RouteError(f"{kind} must contain name and optional description")
        _validate_clean_text(
            spec["name"], f"{kind}.name", maximum=200, required=True
        )
        if "description" in spec:
            _validate_clean_text(
                spec["description"],
                f"{kind}.description",
                maximum=10_000,
                required=False,
            )
        change[kind] = dict(spec)
    change["issue"] = _validate_exact_issue_spec(request.get("issue"), "issue")
    if composite:
        children = request.get("sub_issues")
        if not isinstance(children, list) or not 1 <= len(children) <= 10:
            raise RouteError("sub_issues must contain 1-10 issues")
        validated_children = [
            _validate_exact_issue_spec(item, f"sub_issues[{index}]")
            for index, item in enumerate(children)
        ]
        titles = [change["issue"]["title"], *(item["title"] for item in validated_children)]
        if len(set(titles)) != len(titles):
            raise RouteError("issue tree titles must be unique")
        change["sub_issues"] = validated_children
    return change


def parse_linear_request(
    request: Any,
    *,
    source_profile: str = "swe",
    uuid_factory: UUIDFactory = _uuid4,
) -> ParsedRequest:
    """Parse one bounded source request into linear-command.v2."""
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
            if state not in SAFE_STATES:
                raise RouteError("state is not in the safe-state allowlist")
            target = {"type": "issue", "identifier": identifier}
            change = {"state": state}
        elif operation == "update_issue":
            allowed = {
                "operation",
                "identifier",
                "title",
                "description",
                "state",
                "priority",
                "assignee",
                "labels",
                "due_date",
                "estimate",
                "parent_identifier",
                "project",
                "milestone",
            }
            if (
                not set(request).issubset(allowed)
                or "identifier" not in request
                or len(request) < 3
            ):
                raise RouteError("structured issue update has invalid fields")
            identifier = request.get("identifier")
            if not isinstance(identifier, str) or not re.fullmatch(
                r"SIS-[1-9][0-9]*", identifier
            ):
                raise RouteError("target must be an exact SIS-N identifier")
            change = {
                key: request[key]
                for key in (
                    "title",
                    "description",
                    "state",
                    "priority",
                    "assignee",
                    "labels",
                    "due_date",
                    "estimate",
                    "parent_identifier",
                    "project",
                    "milestone",
                )
                if key in request
            }
            if "title" in change:
                _validate_clean_text(
                    change["title"],
                    "title",
                    maximum=200,
                    required=True,
                )
            if "description" in change:
                _validate_clean_text(
                    change["description"],
                    "description",
                    maximum=10_000,
                    required=False,
                )
            if "state" in change and change["state"] not in SAFE_STATES:
                raise RouteError("state is not in the safe-state allowlist")
            if "priority" in change and change["priority"] not in PRIORITIES:
                raise RouteError("priority is not in the bounded allowlist")
            if "assignee" in change and change["assignee"] is not None:
                _validate_clean_text(
                    change["assignee"],
                    "assignee",
                    maximum=200,
                    required=True,
                )
            if "labels" in change:
                labels = change["labels"]
                if not isinstance(labels, list) or len(labels) > 100:
                    raise RouteError("labels must be an array of at most 100 exact names")
                for index, label in enumerate(labels):
                    _validate_clean_text(
                        label,
                        f"labels[{index}]",
                        maximum=200,
                        required=True,
                    )
                if len(set(labels)) != len(labels):
                    raise RouteError("labels must contain unique exact names")
            if "due_date" in change and change["due_date"] is not None:
                due_date = change["due_date"]
                if not isinstance(due_date, str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}", due_date
                ):
                    raise RouteError("due_date must be ISO YYYY-MM-DD or null")
                try:
                    date.fromisoformat(due_date)
                except ValueError as exc:
                    raise RouteError("due_date must be a valid calendar date") from exc
            if "estimate" in change:
                estimate = change["estimate"]
                if estimate is not None and (
                    isinstance(estimate, bool)
                    or not isinstance(estimate, int)
                    or estimate < 0
                ):
                    raise RouteError("estimate must be a non-negative integer or null")
            if "parent_identifier" in change:
                parent_identifier = change["parent_identifier"]
                if parent_identifier is not None and (
                    not isinstance(parent_identifier, str)
                    or not re.fullmatch(r"SIS-[1-9][0-9]*", parent_identifier)
                ):
                    raise RouteError(
                        "parent_identifier must be an exact SIS-N identifier or null"
                    )
            if ("project" in change) != ("milestone" in change):
                raise RouteError("project and milestone must be supplied together")
            if "project" in change:
                project = change["project"]
                milestone = change["milestone"]
                if (project is None) != (milestone is None):
                    raise RouteError("project and milestone must both be exact names or null")
                if project is not None:
                    _validate_clean_text(
                        project, "project", maximum=200, required=True
                    )
                    _validate_clean_text(
                        milestone, "milestone", maximum=200, required=True
                    )
            target = {"type": "issue", "identifier": identifier}
        elif operation == "inventory_sub_issues":
            if set(request) != {"operation", "identifier"}:
                raise RouteError("sub-issue inventory request has invalid fields")
            identifier = request.get("identifier")
            if not isinstance(identifier, str) or not re.fullmatch(
                r"SIS-[1-9][0-9]*", identifier
            ):
                raise RouteError("target must be an exact SIS-N identifier")
            target = {"type": "issue", "identifier": identifier}
            change = {}
        elif operation == "create_issue_relation":
            if set(request) != {
                "operation",
                "identifier",
                "related_identifier",
                "relation_type",
            }:
                raise RouteError("issue relation request has invalid fields")
            identifier = request.get("identifier")
            related_identifier = request.get("related_identifier")
            if not isinstance(identifier, str) or not re.fullmatch(
                r"SIS-[1-9][0-9]*", identifier
            ):
                raise RouteError("target must be an exact SIS-N identifier")
            if not isinstance(related_identifier, str) or not re.fullmatch(
                r"SIS-[1-9][0-9]*", related_identifier
            ):
                raise RouteError("related target must be an exact SIS-N identifier")
            if related_identifier == identifier:
                raise RouteError("an issue cannot be related to itself")
            relation_type = request.get("relation_type")
            if relation_type not in ISSUE_RELATION_TYPES:
                raise RouteError("relation_type is not in the bounded allowlist")
            target = {"type": "issue", "identifier": identifier}
            change = {
                "related_identifier": related_identifier,
                "relation_type": relation_type,
            }
        elif operation == "update_sub_issues":
            if set(request) != {"operation", "identifier", "description"}:
                raise RouteError("sub-issue update request has invalid fields")
            identifier = request.get("identifier")
            if not isinstance(identifier, str) or not re.fullmatch(
                r"SIS-[1-9][0-9]*", identifier
            ):
                raise RouteError("target must be an exact SIS-N identifier")
            _validate_clean_text(
                request.get("description"),
                "description",
                maximum=10_000,
                required=False,
            )
            target = {"type": "issue", "identifier": identifier}
            change = {"description": request["description"]}
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
            if state not in SAFE_STATES:
                raise RouteError("state is not in the safe-state allowlist")
            if priority not in {"High", "Medium", "Low"}:
                raise RouteError("priority is not in the bounded allowlist")
            target = {"type": "team", "identifier": "SIS"}
            change = {
                "title": title,
                "description": description,
                "parent_identifier": parent,
                "state": state,
                "priority": priority,
            }
        elif operation == "converge_hierarchy":
            target = {"type": "team", "identifier": "SIS"}
            change = _validate_hierarchy_request(request)
        elif operation in {"create_standalone_issue", "converge_issue_tree"}:
            target = {"type": "team", "identifier": "SIS"}
            change = _validate_scoped_issue_request(request)
        elif operation in {
            "create_project",
            "create_milestone",
            "update_project",
            "update_milestone",
        }:
            target = {"type": "team", "identifier": "SIS"}
            change = _validate_project_management_request(request)
        else:
            raise RouteError("structured operation is not allowed")
    else:
        raise RouteError("request must be text or a structured request")
    command: dict[str, Any] = {
        "schema_version": "linear-command.v2",
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
        "schema_version": "linear-kanban-task.v2",
        "command": command,
        "worker_contract": {
            "profile": "project-manager",
            "tool": "pm_linear_execute",
            "mode": "plan_apply_read_back",
            "completion": "tool_completes_current_kanban_task",
        },
    }
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


def _verified_replay(
    task: dict[str, Any], command: dict[str, Any], delivery_key: str
) -> dict[str, Any]:
    raw_result = task.get("result")
    try:
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except json.JSONDecodeError as exc:
        raise RouteError("completed replay has invalid structured result") from exc
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != "linear-result.v2"
        or result.get("verified") is not True
    ):
        raise RouteError("completed replay lacks a verified linear-result.v2")

    raw_body = task.get("body")
    try:
        envelope = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
    except json.JSONDecodeError as exc:
        raise RouteError("completed replay has an invalid persisted task envelope") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schema_version", "command", "worker_contract"}
        or envelope.get("schema_version") != "linear-kanban-task.v2"
        or not isinstance(envelope.get("command"), dict)
        or envelope.get("worker_contract")
        != {
            "profile": "project-manager",
            "tool": "pm_linear_execute",
            "mode": "plan_apply_read_back",
            "completion": "tool_completes_current_kanban_task",
        }
    ):
        raise RouteError("completed replay lacks a linear-kanban-task.v2 envelope")
    persisted = envelope["command"]
    expected_command_fields = {
        "schema_version",
        "command_id",
        "correlation_id",
        "idempotency_key",
        "source_profile",
        "operation",
        "target",
        "change",
        "policy",
    }
    id_fields = ("command_id", "correlation_id")
    try:
        command_ids = {
            field: uuid.UUID(str(persisted[field]))
            for field in id_fields
        }
    except (KeyError, ValueError, AttributeError, TypeError) as exc:
        raise RouteError("completed replay has an invalid persisted command") from exc
    if (
        set(persisted) != expected_command_fields
        or any(value.version != 4 for value in command_ids.values())
        or any(str(value) != persisted[field] for field, value in command_ids.items())
    ):
        raise RouteError("completed replay has an invalid persisted command")
    semantic_fields = ("schema_version", "idempotency_key", "source_profile", "operation", "target", "change", "policy")
    if any(persisted.get(field) != command.get(field) for field in semantic_fields):
        raise RouteError("completed replay does not match persisted command")

    required_result_fields = {
        "schema_version",
        "command_id",
        "correlation_id",
        "idempotency_key",
        "source_profile",
        "operation",
        "mode",
        "target",
        "result",
        "before",
        "after",
        "plan",
        "no_op",
        "verified",
    }
    if not required_result_fields.issubset(result):
        raise RouteError("completed replay has an incomplete linear-result.v2")
    binding_fields = (
        "command_id",
        "correlation_id",
        "idempotency_key",
        "source_profile",
        "operation",
    )
    if any(result.get(field) != persisted.get(field) for field in binding_fields):
        raise RouteError("completed replay does not match persisted command")
    outcome = result.get("result")
    if (
        result.get("mode") != "apply"
        or outcome not in {"applied", "no_op", "read"}
        or not isinstance(result.get("plan"), list)
        or not isinstance(result.get("no_op"), bool)
        or result["no_op"] != (outcome in {"no_op", "read"})
    ):
        raise RouteError("completed replay has an invalid verified outcome")

    target = result.get("target")
    if not isinstance(target, dict):
        raise RouteError("completed replay has an invalid verified target")
    operation = persisted["operation"]
    if operation == "converge_hierarchy":
        expected_target = {
            "type": "project",
            "identifier": persisted["change"]["project"]["name"],
        }
        if target != expected_target:
            raise RouteError("completed replay target does not match persisted command")
    elif operation in {
        "create_project",
        "create_milestone",
        "update_project",
        "update_milestone",
    }:
        change = persisted["change"]
        milestone = operation.endswith("milestone")
        expected_target = {
            "type": "milestone" if milestone else "project",
            "identifier": change.get("new_name", change["name"]),
        }
        if milestone:
            expected_target["project"] = change["project"]
        if target != expected_target:
            raise RouteError("completed replay target does not match persisted command")
    elif operation in {
        "create_issue",
        "create_standalone_issue",
        "converge_issue_tree",
    }:
        after = result.get("after")
        identifier = target.get("identifier")
        url = target.get("url")
        after_issue = (
            after.get("issue")
            if operation in {"create_standalone_issue", "converge_issue_tree"}
            and isinstance(after, dict)
            else after
        )
        if (
            not isinstance(after_issue, dict)
            or target.get("type") != "issue"
            or not isinstance(identifier, str)
            or not identifier.strip()
            or not isinstance(url, str)
            or not url.strip()
            or identifier != after_issue.get("identifier")
            or url != after_issue.get("url")
        ):
            raise RouteError("completed replay has an invalid verified target")
    elif (
        target.get("type") != persisted["target"].get("type")
        or target.get("identifier") != persisted["target"].get("identifier")
        or not isinstance(target.get("url"), str)
        or not target["url"].strip()
    ):
        raise RouteError("completed replay target does not match persisted command")
    return {
        "status": "verified_no_op",
        "task_id": task["id"],
        "idempotency_key": command["idempotency_key"],
        "delivery_key": delivery_key,
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
    delivery_key = _delivery_key(idempotency_key, source)
    task, created = board.get_or_create_task(
        delivery_key,
        title=f"Linear {command['operation']} {command['target']['identifier']}",
        body=build_task_body(command),
        assignee="project-manager",
        skills=["project-manager-linear-worker"],
        triage=True,
        idempotency_key=delivery_key,
        session_id=source.session_id,
        max_runtime_seconds=300,
    )
    replayed = not created

    if not created:
        if task.get("idempotency_key") != delivery_key:
            raise RouteError("idempotent task delivery key mismatch")
        if task.get("session_id") != source.session_id:
            raise RouteError("idempotent task belongs to a different source session")
        status = task.get("status")
        if status == "done":
            return _verified_replay(task, command, delivery_key)
        if status == "blocked":
            return {
                "status": "blocked",
                "task_id": task["id"],
                "idempotency_key": idempotency_key,
                "delivery_key": delivery_key,
                "replayed": True,
                "operation": command["operation"],
                "reason": board.block_reason(task["id"]),
            }
        if status in TERMINAL_IN_FLIGHT:
            return {
                "status": "already_in_flight",
                "task_id": task["id"],
                "idempotency_key": idempotency_key,
                "delivery_key": delivery_key,
                "replayed": True,
            }
        if status != "triage":
            raise RouteError(f"idempotent task has unsupported status: {status}")
    else:
        if task.get("status") != "triage":
            raise RouteError("new Linear task did not remain in triage")
        if task.get("session_id") != source.session_id:
            raise RouteError("new Linear task did not persist the exact source session")
        if task.get("idempotency_key") != delivery_key:
            raise RouteError("new Linear task did not persist the delivery key")

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
        "delivery_key": delivery_key,
        "replayed": replayed,
        "route_audit": audit,
    }
