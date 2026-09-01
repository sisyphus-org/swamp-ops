#!/usr/bin/env python3
"""Bounded SWE ingress for exact Linear commands over Hermes Kanban."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
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
OWNER_CONTROLLED_STATES = {"Done", "Canceled", "Duplicate"}
RESERVED_MARKER = "<!-- linear-command"
MAX_HIERARCHY_BYTES = 24_576
MAX_ISSUE_TREE_BYTES = 65_536
PRIORITIES = {"High", "Medium", "Low"}
ISSUE_RELATION_TYPES = {"blocks", "blocked_by", "related"}
LINEAR_ENTITY_TYPES = ("issues", "projects", "milestones", "initiatives")
MAX_SEARCH_QUERY = 500
MAX_BULK_ITEMS = 50
MAX_BULK_BYTES = 24_576
BULK_MUTATING_OPERATIONS = {
    "change_state", "update_issue", "update_sub_issues", "add_comment", "create_issue",
    "converge_hierarchy", "create_standalone_issue", "converge_issue_tree",
    "create_issue_relation", "remove_issue_relation", "replace_issue_relation",
    "create_project", "create_milestone", "update_project", "update_milestone",
    "create_initiative", "update_initiative", "link_project_to_initiative",
    "archive_linear_entity", "delete_linear_entity",
}
BULK_OWNER_OPERATIONS = {
    "remove_issue_relation", "replace_issue_relation",
    "archive_linear_entity", "delete_linear_entity",
}
APPROVAL_WORKFLOW = "linear-destructive-owner-approval-attest"
APPROVAL_MODEL = "linear-destructive-owner-approval-attest"
APPROVAL_FIELDS = {
    "workflow",
    "model",
    "run_id",
    "artifact_version",
    "checksum",
    "intent_hash",
    "before_state_hash",
    "expires_at",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
DESCRIPTION_TRANSFORMS = {"remove_links"}


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


def _validate_approval_reference(value: Any) -> dict[str, Any]:
    """Accept only the fixed structural Swamp attestation reference."""
    if not isinstance(value, dict) or set(value) != APPROVAL_FIELDS:
        raise RouteError("approval reference fields are invalid")
    if value.get("workflow") != APPROVAL_WORKFLOW:
        raise RouteError("approval workflow is not the fixed workflow")
    if value.get("model") != APPROVAL_MODEL:
        raise RouteError("approval model is not the fixed model")
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or UUID.fullmatch(run_id) is None:
        raise RouteError("approval run_id must be a UUID")
    version = value.get("artifact_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RouteError("approval artifact_version must be positive")
    for field in ("checksum", "intent_hash", "before_state_hash"):
        digest = value.get(field)
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise RouteError(f"approval {field} must be SHA-256")
    expires_at = value.get("expires_at")
    if not isinstance(expires_at, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        expires_at,
    ) is None:
        raise RouteError("approval expires_at must be UTC RFC3339 seconds")
    try:
        datetime.fromisoformat(expires_at[:-1] + "+00:00")
    except ValueError as exc:
        raise RouteError("approval expires_at is not a valid timestamp") from exc
    return dict(value)


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


def _validate_initiative_management_request(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    updating = operation == "update_initiative"
    allowed = {"operation", "name", "description", "target_date"}
    if updating:
        allowed.add("new_name")
        if not (set(request) & {"new_name", "description", "target_date"}):
            raise RouteError("update_initiative requires at least one managed field")
    if "name" not in request or not set(request).issubset(allowed):
        raise RouteError("structured initiative management request has invalid fields")
    for field in ("name", "new_name"):
        if field in request:
            _validate_clean_text(request[field], field, maximum=200, required=True)
    if "description" in request:
        _validate_clean_text(
            request["description"], "description", maximum=10_000, required=False
        )
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


def _validate_destructive_request(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if set(request) != {"operation", "entity_type", "selector", "approval"}:
        raise RouteError("archive/delete request must contain exact typed selector and approval")
    operation = request.get("operation")
    entity_type = request.get("entity_type")
    selector = request.get("selector")
    safe_matrix = {
        ("archive_linear_entity", "issue"),
        ("archive_linear_entity", "project"),
        ("archive_linear_entity", "initiative"),
        ("delete_linear_entity", "issue"),
        ("delete_linear_entity", "project"),
        ("delete_linear_entity", "milestone"),
        ("delete_linear_entity", "initiative"),
    }
    if (operation, entity_type) not in safe_matrix or not isinstance(selector, dict):
        raise RouteError("archive/delete combination is outside the safe matrix")
    if entity_type == "issue":
        if set(selector) != {"identifier"} or not isinstance(selector.get("identifier"), str) or re.fullmatch(r"SIS-[1-9][0-9]*", selector["identifier"]) is None:
            raise RouteError("issue selector must be exactly one SIS-N")
    elif entity_type in {"project", "initiative"}:
        if set(selector) != {"name"}:
            raise RouteError(f"{entity_type} selector must contain exactly name")
        _validate_clean_text(selector.get("name"), "selector.name", maximum=200, required=True)
    else:
        if set(selector) != {"project", "name"}:
            raise RouteError("milestone selector must contain exactly project and name")
        for field in ("project", "name"):
            _validate_clean_text(selector.get(field), f"selector.{field}", maximum=200, required=True)
    return {"type": entity_type, "selector": dict(selector)}, _validate_approval_reference(request["approval"])


def _validate_workspace_read_request(request: dict[str, Any]) -> dict[str, Any]:
    """Validate one fixed-scope read without accepting query/API passthrough."""
    operation = request.get("operation")
    expected = {"operation", "entity_types", "include_archived"}
    if operation == "search_linear":
        expected.add("query")
    if set(request) != expected:
        raise RouteError("workspace read request has invalid fields")
    entity_types = request.get("entity_types")
    if (
        not isinstance(entity_types, list)
        or not entity_types
        or len(entity_types) > len(LINEAR_ENTITY_TYPES)
        or any(not isinstance(item, str) for item in entity_types)
        or len(set(entity_types)) != len(entity_types)
        or any(item not in LINEAR_ENTITY_TYPES for item in entity_types)
    ):
        raise RouteError("entity_types must be a non-empty unique core entity subset")
    include_archived = request.get("include_archived")
    if not isinstance(include_archived, bool):
        raise RouteError("include_archived must be boolean")
    ordered_types = [item for item in LINEAR_ENTITY_TYPES if item in entity_types]
    if operation == "search_linear":
        query = request.get("query")
        _validate_clean_text(
            query,
            "query",
            maximum=MAX_SEARCH_QUERY,
            required=True,
        )
        return {
            "query": query,
            "entity_types": ordered_types,
            "include_archived": include_archived,
        }
    return {
        "entity_types": ordered_types,
        "include_archived": include_archived,
    }


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


def _validate_canonical_bulk_item(item: dict[str, Any], index: int) -> None:
    """Apply source-side lane-shape checks without importing the credentialed lane."""
    operation = item["operation"]
    target = item["target"]
    change = item["change"]
    issue_target = (
        isinstance(target, dict)
        and set(target) == {"type", "identifier"}
        and target.get("type") == "issue"
        and isinstance(target.get("identifier"), str)
        and re.fullmatch(r"SIS-[1-9][0-9]*", target["identifier"]) is not None
    )
    team_target = target == {"type": "team", "identifier": "SIS"}
    workspace_target = target == {"type": "workspace", "identifier": "current"}
    issue_ops = {
        "change_state", "update_issue", "update_sub_issues", "add_comment",
        "create_issue_relation", "remove_issue_relation", "replace_issue_relation",
    }
    team_ops = {
        "create_issue", "converge_hierarchy", "create_standalone_issue",
        "converge_issue_tree", "create_project", "create_milestone",
        "update_project", "update_milestone", "link_project_to_initiative",
    }
    workspace_ops = {"create_initiative", "update_initiative"}
    if operation in issue_ops and not issue_target:
        raise RouteError(f"bulk item {index} requires an exact issue target")
    if operation in team_ops and not team_target:
        raise RouteError(f"bulk item {index} requires the exact SIS team target")
    if operation in workspace_ops and not workspace_target:
        raise RouteError(f"bulk item {index} requires the current workspace target")
    if operation in {"archive_linear_entity", "delete_linear_entity"}:
        entity_type = target.get("type") if isinstance(target, dict) else None
        selector = target.get("selector") if isinstance(target, dict) else None
        safe = {
            ("archive_linear_entity", "issue"),
            ("archive_linear_entity", "project"),
            ("archive_linear_entity", "initiative"),
            ("delete_linear_entity", "issue"),
            ("delete_linear_entity", "project"),
            ("delete_linear_entity", "milestone"),
            ("delete_linear_entity", "initiative"),
        }
        if (operation, entity_type) not in safe or not isinstance(selector, dict) or change:
            raise RouteError(f"bulk item {index} has an invalid lifecycle shape")
        if entity_type == "issue":
            valid = set(selector) == {"identifier"} and isinstance(selector.get("identifier"), str) and re.fullmatch(r"SIS-[1-9][0-9]*", selector["identifier"]) is not None
        elif entity_type in {"project", "initiative"}:
            valid = set(selector) == {"name"}
            if valid:
                _validate_clean_text(selector.get("name"), "selector.name", maximum=200, required=True)
        else:
            valid = set(selector) == {"project", "name"}
            if valid:
                for field in ("project", "name"):
                    _validate_clean_text(selector.get(field), f"selector.{field}", maximum=200, required=True)
        if not valid:
            raise RouteError(f"bulk item {index} has an invalid lifecycle selector")
        return
    if operation == "change_state":
        if set(change) != {"state"} or change.get("state") not in SAFE_STATES | OWNER_CONTROLLED_STATES:
            raise RouteError(f"bulk item {index} has an invalid state change")
    elif operation == "update_issue":
        reconstructed = {"operation": operation, "identifier": target["identifier"], **change}
        # Source approval belongs only to the parent; validate the managed fields
        # through the normal parser and remove its parent-only approval expectation.
        if set(change) == {"parent_identifier"}:
            parent = change["parent_identifier"]
            if parent is not None and (not isinstance(parent, str) or re.fullmatch(r"SIS-[1-9][0-9]*", parent) is None):
                raise RouteError(f"bulk item {index} has an invalid parent target")
        else:
            parse_linear_request(reconstructed)
    elif operation == "update_sub_issues":
        if set(change) != {"description"}:
            raise RouteError(f"bulk item {index} has an invalid sub-issue update")
        _validate_clean_text(change.get("description"), "description", maximum=10_000, required=False)
    elif operation == "add_comment":
        if set(change) != {"body"}:
            raise RouteError(f"bulk item {index} has an invalid comment")
        _validate_clean_text(change.get("body"), "body", maximum=4_000, required=True)
    elif operation in {"create_issue_relation", "remove_issue_relation", "replace_issue_relation"}:
        if operation in {"create_issue_relation", "remove_issue_relation"}:
            expected = {"related_identifier", "relation_type"}
            endpoints = ("related_identifier",)
            types = ("relation_type",)
        else:
            expected = {"old_related_identifier", "old_relation_type", "new_related_identifier", "new_relation_type"}
            endpoints = ("old_related_identifier", "new_related_identifier")
            types = ("old_relation_type", "new_relation_type")
        if set(change) != expected:
            raise RouteError(f"bulk item {index} has invalid relation fields")
        for field in endpoints:
            if not isinstance(change.get(field), str) or re.fullmatch(r"SIS-[1-9][0-9]*", change[field]) is None or change[field] == target["identifier"]:
                raise RouteError(f"bulk item {index} has an invalid relation endpoint")
        if any(change.get(field) not in ISSUE_RELATION_TYPES for field in types):
            raise RouteError(f"bulk item {index} has an invalid relation type")
    elif operation == "create_issue":
        reconstructed = {"operation": operation, **change}
        parsed = parse_linear_request(reconstructed)
        if parsed.command["target"] != target or parsed.command["change"] != change:
            raise RouteError(f"bulk item {index} create shape drifted")
    elif operation == "converge_hierarchy":
        if _validate_hierarchy_request({"operation": operation, **change}) != change:
            raise RouteError(f"bulk item {index} hierarchy shape drifted")
    elif operation in {"create_standalone_issue", "converge_issue_tree"}:
        if _validate_scoped_issue_request({"operation": operation, **change}) != change:
            raise RouteError(f"bulk item {index} scoped issue shape drifted")
    elif operation in {"create_project", "create_milestone", "update_project", "update_milestone"}:
        if _validate_project_management_request({"operation": operation, **change}) != change:
            raise RouteError(f"bulk item {index} project shape drifted")
    elif operation in {"create_initiative", "update_initiative"}:
        if _validate_initiative_management_request({"operation": operation, **change}) != change:
            raise RouteError(f"bulk item {index} initiative shape drifted")
    elif operation == "link_project_to_initiative":
        if set(change) != {"project", "initiative"}:
            raise RouteError(f"bulk item {index} initiative link shape is invalid")
        for field in ("project", "initiative"):
            _validate_clean_text(change.get(field), field, maximum=200, required=True)


def _validate_bulk_request(
    request: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    expected = {"operation", "items"}
    if "approval" in request:
        expected.add("approval")
    if set(request) != expected:
        raise RouteError("bulk request contains unsupported parent fields")
    items = request.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BULK_ITEMS:
        raise RouteError("bulk request items must contain 1-50 entries")
    if len(json.dumps(request, ensure_ascii=False).encode("utf-8")) > MAX_BULK_BYTES:
        raise RouteError("bulk request exceeds the bounded serialized size")
    semantic: set[str] = set()
    targets: set[str] = set()
    owner_required = False
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"operation", "target", "change"}:
            raise RouteError(
                f"bulk item {index} must contain exactly operation, target, and change"
            )
        operation = item.get("operation")
        target = item.get("target")
        change = item.get("change")
        if operation not in BULK_MUTATING_OPERATIONS:
            raise RouteError(f"bulk item {index} is not an allowed mutating operation")
        if not isinstance(target, dict) or not isinstance(change, dict):
            raise RouteError(f"bulk item {index} target/change must be objects")
        _validate_canonical_bulk_item(item, index)
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        target_digest = hashlib.sha256(
            json.dumps(target, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if digest in semantic:
            raise RouteError("bulk request contains a duplicate semantic item")
        if target_digest in targets:
            raise RouteError("bulk request contains conflicting writes to the same exact target")
        semantic.add(digest)
        targets.add(target_digest)
        owner_required = owner_required or operation in BULK_OWNER_OPERATIONS or (
            operation == "change_state" and change.get("state") in OWNER_CONTROLLED_STATES
        ) or (operation == "update_issue" and set(change) == {"parent_identifier"})
        validated.append(dict(item))
    approval = (
        _validate_approval_reference(request["approval"])
        if "approval" in request
        else None
    )
    if owner_required and approval is None:
        raise RouteError("bulk request with owner-controlled items requires one owner approval")
    if not owner_required and approval is not None:
        raise RouteError("bulk owner approval is allowed only when an item requires it")
    return validated, approval
def _issue_identifier(request: dict[str, Any]) -> str:
    """Resolve one exact SIS target from an identifier or bounded issue number."""
    has_identifier = "identifier" in request
    has_number = "issue_number" in request
    if has_identifier == has_number:
        raise RouteError("request must contain exactly one issue target")
    if has_identifier:
        identifier = request["identifier"]
        if not isinstance(identifier, str) or not re.fullmatch(
            r"SIS-[1-9][0-9]*", identifier
        ):
            raise RouteError("target must be an exact SIS-N identifier")
        return identifier
    number = request["issue_number"]
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise RouteError("issue_number must be a positive integer")
    return f"SIS-{number}"


def parse_linear_request(
    request: Any,
    *,
    source_profile: str = "swe",
    uuid_factory: UUIDFactory = _uuid4,
) -> ParsedRequest:
    """Parse one bounded source request into linear-command.v2."""
    if not PROFILE_NAME.fullmatch(source_profile):
        raise RouteError("source profile is invalid")
    approval_reference: dict[str, Any] | None = None
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
        if operation == "bulk_linear_operations":
            items, approval_reference = _validate_bulk_request(request)
            target = {"type": "workspace", "identifier": "current"}
            change = {"items": items}
        elif operation == "change_state":
            state = request.get("state")
            identifier = _issue_identifier(request)
            target_field = "identifier" if "identifier" in request else "issue_number"
            expected_fields = {"operation", target_field, "state"}
            if state in OWNER_CONTROLLED_STATES:
                expected_fields.add("approval")
            if set(request) != expected_fields:
                raise RouteError("structured state request has invalid fields")
            if state not in SAFE_STATES and state not in OWNER_CONTROLLED_STATES:
                raise RouteError("state is not in the safe-state allowlist")
            if state in OWNER_CONTROLLED_STATES:
                approval_reference = _validate_approval_reference(request["approval"])
            target = {"type": "issue", "identifier": identifier}
            change = {"state": state}
        elif operation == "update_issue":
            allowed = {
                "operation",
                "identifier",
                "issue_number",
                "title",
                "description",
                "description_transform",
                "state",
                "priority",
                "assignee",
                "labels",
                "due_date",
                "estimate",
                "parent_identifier",
                "project",
                "milestone",
                "approval",
            }
            if not set(request).issubset(allowed) or len(request) < 3:
                raise RouteError("structured issue update has invalid fields")
            identifier = _issue_identifier(request)
            change = {
                key: request[key]
                for key in (
                    "title",
                    "description",
                    "description_transform",
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
            if not change:
                raise RouteError("structured issue update has no changed fields")
            if "description" in change and "description_transform" in change:
                raise RouteError(
                    "description and description_transform are mutually exclusive"
                )
            if "approval" in request:
                if set(change) != {"parent_identifier"}:
                    raise RouteError(
                        "approval is allowed only for a parent-only issue update"
                    )
                approval_reference = _validate_approval_reference(request["approval"])
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
            if (
                "description_transform" in change
                and change["description_transform"] not in DESCRIPTION_TRANSFORMS
            ):
                raise RouteError("description_transform is not in the bounded allowlist")
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
        elif operation in {"remove_issue_relation", "replace_issue_relation"}:
            if operation == "remove_issue_relation":
                expected_fields = {
                    "operation",
                    "identifier",
                    "related_identifier",
                    "relation_type",
                    "approval",
                }
                relation_fields = ("related_identifier",)
                type_fields = ("relation_type",)
            else:
                expected_fields = {
                    "operation",
                    "identifier",
                    "old_related_identifier",
                    "old_relation_type",
                    "new_related_identifier",
                    "new_relation_type",
                    "approval",
                }
                relation_fields = (
                    "old_related_identifier",
                    "new_related_identifier",
                )
                type_fields = ("old_relation_type", "new_relation_type")
            if set(request) != expected_fields:
                raise RouteError(
                    f"{operation} requires exact endpoints, relation types, and owner approval"
                )
            identifier = request.get("identifier")
            if not isinstance(identifier, str) or not re.fullmatch(
                r"SIS-[1-9][0-9]*", identifier
            ):
                raise RouteError("target must be an exact SIS-N identifier")
            for field in relation_fields:
                endpoint = request.get(field)
                if not isinstance(endpoint, str) or not re.fullmatch(
                    r"SIS-[1-9][0-9]*", endpoint
                ):
                    raise RouteError(f"{field} must be an exact SIS-N identifier")
                if endpoint == identifier:
                    raise RouteError("an issue cannot be related to itself")
            for field in type_fields:
                if request.get(field) not in ISSUE_RELATION_TYPES:
                    raise RouteError(f"{field} is not in the bounded allowlist")
            approval_reference = _validate_approval_reference(request["approval"])
            target = {"type": "issue", "identifier": identifier}
            change = {
                field: request[field]
                for field in (*relation_fields, *type_fields)
            }
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
        elif operation == "link_project_to_initiative":
            if set(request) != {"operation", "project", "initiative"}:
                raise RouteError("structured initiative project link has invalid fields")
            _validate_clean_text(
                request.get("project"), "project", maximum=200, required=True
            )
            _validate_clean_text(
                request.get("initiative"), "initiative", maximum=200, required=True
            )
            target = {"type": "team", "identifier": "SIS"}
            change = {
                "project": request["project"],
                "initiative": request["initiative"],
            }
        elif operation in {"archive_linear_entity", "delete_linear_entity"}:
            target, approval_reference = _validate_destructive_request(request)
            change = {}
        elif operation in {"create_initiative", "update_initiative"}:
            target = {"type": "workspace", "identifier": "current"}
            change = _validate_initiative_management_request(request)
        elif operation in {"search_linear", "inventory_linear"}:
            target = {"type": "workspace", "identifier": "current"}
            change = _validate_workspace_read_request(request)
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
        "policy": (
            {"mode": "owner_approved", "approval": approval_reference}
            if approval_reference is not None
            else {"mode": "standard"}
        ),
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
    if operation in {"archive_linear_entity", "delete_linear_entity"}:
        if target != persisted["target"]:
            raise RouteError("completed replay target does not match persisted command")
    elif operation == "converge_hierarchy":
        expected_target = {
            "type": "project",
            "identifier": persisted["change"]["project"]["name"],
        }
        if target != expected_target:
            raise RouteError("completed replay target does not match persisted command")
    elif operation in {"search_linear", "inventory_linear"}:
        if target != {"type": "workspace", "identifier": "current"}:
            raise RouteError("completed replay target does not match persisted command")
        after = result.get("after")
        change = persisted["change"]
        if (
            not isinstance(after, dict)
            or after.get("entity_types") != change.get("entity_types")
            or after.get("include_archived") != change.get("include_archived")
            or (
                operation == "search_linear"
                and after.get("query") != change.get("query")
            )
        ):
            raise RouteError("completed replay read scope does not match persisted command")
    elif operation == "bulk_linear_operations":
        if target != {"type": "workspace", "identifier": "current"}:
            raise RouteError("completed bulk replay target does not match persisted command")
        items = result.get("items")
        counts = result.get("counts")
        if (
            not isinstance(items, list)
            or len(items) != len(persisted["change"]["items"])
            or not isinstance(counts, dict)
            or counts.get("total") != len(items)
            or any(
                not isinstance(item, dict)
                or item.get("index") != index
                or item.get("operation") != persisted["change"]["items"][index]["operation"]
                or item.get("outcome") not in {"applied", "no_op"}
                or item.get("verified") is not True
                for index, item in enumerate(items)
            )
        ):
            raise RouteError("completed bulk replay has invalid ordered outcomes")
    elif operation in {"create_initiative", "update_initiative"}:
        change = persisted["change"]
        expected_target = {
            "type": "initiative",
            "identifier": change.get("new_name", change["name"]),
        }
        if target != expected_target:
            raise RouteError("completed replay target does not match persisted command")
    elif operation == "link_project_to_initiative":
        expected_target = {
            "type": "initiative_project",
            "initiative": persisted["change"]["initiative"],
            "project": persisted["change"]["project"],
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
