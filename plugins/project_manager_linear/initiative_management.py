"""Bounded non-destructive Linear initiative management."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

MAX_NAME = 200
MAX_DESCRIPTION = 10_000
CREATE_OPERATIONS = {"create_initiative"}
UPDATE_OPERATIONS = {"update_initiative"}
LINK_OPERATIONS = {"link_project_to_initiative"}
OPERATIONS = CREATE_OPERATIONS | UPDATE_OPERATIONS | LINK_OPERATIONS


def _load_validation() -> Any:
    name = "project_manager_linear_validation"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name("validation.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("bundled validation module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_VALIDATION = _load_validation()


def _fail(error_cls: type[Exception], message: str) -> NoReturn:
    raise error_cls(message)


def _text(
    value: Any,
    path: str,
    *,
    required: bool,
    maximum: int,
    error_cls: type[Exception],
) -> None:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (required and not value.strip())
    ):
        qualifier = "non-empty " if required else ""
        _fail(
            error_cls,
            f"{path} must be a {qualifier}string of at most {maximum} characters",
        )
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        _fail(error_cls, f"{path} contains control characters")
    if _VALIDATION.RESERVED_MARKER in value:
        _fail(error_cls, f"{path} contains the reserved marker")
    if any(pattern.search(value) for pattern in _VALIDATION.CREDENTIAL_SHAPES):
        _fail(error_cls, f"{path} contains credential-shaped data")


def _target_date(value: Any, error_cls: type[Exception]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
    ):
        _fail(error_cls, "target_date must be ISO YYYY-MM-DD or null")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(error_cls, "target_date must be a valid calendar date")


def validate_change(
    change: Any, operation: str, error_cls: type[Exception]
) -> dict[str, Any]:
    if operation not in OPERATIONS or not isinstance(change, dict):
        _fail(error_cls, "initiative management operation is invalid")
    if operation in LINK_OPERATIONS:
        if set(change) != {"project", "initiative"}:
            _fail(error_cls, "initiative project link change has invalid fields")
        for field in ("project", "initiative"):
            _text(
                change[field],
                field,
                required=True,
                maximum=MAX_NAME,
                error_cls=error_cls,
            )
        return change
    allowed = {"name", "description", "target_date"}
    if operation in UPDATE_OPERATIONS:
        allowed.add("new_name")
        if not (set(change) & {"new_name", "description", "target_date"}):
            _fail(error_cls, "update_initiative requires at least one managed field")
    if "name" not in change or not set(change).issubset(allowed):
        _fail(error_cls, f"{operation} change has invalid fields")
    for field in ("name", "new_name"):
        if field in change:
            _text(
                change[field],
                field,
                required=True,
                maximum=MAX_NAME,
                error_cls=error_cls,
            )
    if "description" in change:
        _text(
            change["description"],
            "description",
            required=False,
            maximum=MAX_DESCRIPTION,
            error_cls=error_cls,
        )
    if "target_date" in change:
        _target_date(change["target_date"], error_cls)
    return change


def _bounded(nodes: Any, error_cls: type[Exception]) -> list[dict[str, Any]]:
    if (
        not isinstance(nodes, list)
        or len(nodes) > 100
        or any(not isinstance(item, dict) for item in nodes)
    ):
        _fail(
            error_cls,
            "workspace initiatives exceed the supported 100-item limit or are malformed",
        )
    return nodes


def _one(
    nodes: list[dict[str, Any]], label: str, error_cls: type[Exception]
) -> dict[str, Any] | None:
    if len(nodes) > 1:
        _fail(error_cls, f"ambiguous scoped Linear match for {label}")
    return nodes[0] if nodes else None


def _named(
    nodes: list[dict[str, Any]], name: str, error_cls: type[Exception]
) -> dict[str, Any] | None:
    for item in nodes:
        if (
            not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(item.get("name"), str)
        ):
            _fail(error_cls, "initiative inventory is malformed")
    return _one(
        [item for item in nodes if item["name"] == name],
        "initiative name",
        error_cls,
    )


def _deterministic_uuid4(domain: str, semantic_key: str) -> str:
    raw = bytearray(hashlib.sha256(f"{domain}:{semantic_key}".encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _sis(client: Any, error_cls: type[Exception]) -> dict[str, Any]:
    teams = _bounded(client.list_teams(), error_cls)
    for team in teams:
        if (
            not isinstance(team.get("id"), str)
            or not team["id"]
            or not isinstance(team.get("key"), str)
        ):
            _fail(error_cls, "workspace teams inventory is malformed")
    team = _one([item for item in teams if item["key"] == "SIS"], "team SIS", error_cls)
    if team is None:
        _fail(error_cls, "exact SIS team was not found")
    return team


def _project_scope(
    project: dict[str, Any], team_id: str, error_cls: type[Exception]
) -> None:
    teams = project.get("teams")
    nodes = teams.get("nodes") if isinstance(teams, dict) else None
    if (
        not isinstance(nodes, list)
        or team_id
        not in {item.get("id") for item in nodes if isinstance(item, dict)}
    ):
        _fail(error_cls, "exact project match conflicts with SIS team scope")


def _execute_link(
    client: Any,
    command: dict[str, Any],
    *,
    mode: str,
    error_cls: type[Exception],
) -> dict[str, Any]:
    change = command["change"]
    team = _sis(client, error_cls)
    projects = _bounded(client.list_team_projects(team["id"]), error_cls)
    for item in projects:
        if not isinstance(item.get("id"), str) or not item["id"]:
            _fail(error_cls, "project inventory is malformed")
    project = _one(
        [item for item in projects if item.get("name") == change["project"]],
        "project name",
        error_cls,
    )
    if project is None:
        _fail(error_cls, f"exact Linear project not found: {change['project']}")
    _project_scope(project, team["id"], error_cls)

    initiative = _named(
        _bounded(client.list_initiatives(), error_cls),
        change["initiative"],
        error_cls,
    )
    if initiative is None:
        _fail(error_cls, f"exact Linear initiative not found: {change['initiative']}")
    linked_projects = _bounded(
        client.list_initiative_projects(initiative["id"]), error_cls
    )
    for linked in linked_projects:
        if not isinstance(linked.get("id"), str) or not linked["id"]:
            _fail(error_cls, "initiative projects inventory is malformed")
    exact_links = [item for item in linked_projects if item["id"] == project["id"]]
    if len(exact_links) > 1:
        _fail(error_cls, "exact initiative project link exists more than once")

    desired = {
        "initiative": change["initiative"],
        "project": change["project"],
    }
    base = {
        "schema_version": "linear-result.v2",
        "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "operation": command["operation"],
        "mode": mode,
        "target": {"type": "initiative_project", **desired},
    }
    if exact_links:
        return {
            **base,
            "result": "no_op",
            "before": desired,
            "after": desired,
            "plan": [],
            "no_op": True,
            "verified": True,
        }
    plan = [{"action": command["operation"], **desired}]
    if mode == "plan":
        return {
            **base,
            "result": "planned",
            "before": None,
            "after": desired,
            "plan": plan,
            "no_op": False,
            "verified": False,
        }

    link_id = _deterministic_uuid4(
        "linear-command:initiative-project:v2", command["idempotency_key"]
    )
    client.create_initiative_project_link(
        link_id=link_id,
        initiative_id=initiative["id"],
        project_id=project["id"],
    )
    refreshed = _named(
        _bounded(client.list_initiatives(), error_cls),
        change["initiative"],
        error_cls,
    )
    if refreshed is None or refreshed.get("id") != initiative["id"]:
        _fail(error_cls, "initiative project link exact read-back verification failed")
    refreshed_links = _bounded(
        client.list_initiative_projects(refreshed["id"]), error_cls
    )
    if len([item for item in refreshed_links if item.get("id") == project["id"]]) != 1:
        _fail(error_cls, "initiative project link exact read-back verification failed")
    return {
        **base,
        "result": "applied",
        "before": None,
        "after": desired,
        "plan": plan,
        "no_op": False,
        "verified": True,
    }


def _desired_name(change: dict[str, Any]) -> str:
    return change.get("new_name", change["name"])


def _matches(entity: dict[str, Any], change: dict[str, Any]) -> bool:
    if entity.get("name") != _desired_name(change):
        return False
    if "description" in change and entity.get("description") != change["description"]:
        return False
    if "target_date" in change and entity.get("targetDate") != change["target_date"]:
        return False
    return True


def _snapshot(
    change: dict[str, Any], live: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = {
        "name": live.get("name") if live is not None else _desired_name(change)
    }
    if "target_date" in change:
        result["target_date"] = (
            live.get("targetDate") if live is not None else change["target_date"]
        )
    return result


def _base(command: dict[str, Any], mode: str) -> dict[str, Any]:
    return {
        "schema_version": "linear-result.v2",
        "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "operation": command["operation"],
        "mode": mode,
        "target": {
            "type": "initiative",
            "identifier": _desired_name(command["change"]),
        },
    }


def execute(
    client: Any,
    command: dict[str, Any],
    *,
    mode: str,
    error_cls: type[Exception],
) -> dict[str, Any]:
    if command["operation"] in LINK_OPERATIONS:
        return _execute_link(client, command, mode=mode, error_cls=error_cls)
    change = command["change"]
    operation = command["operation"]
    creating = operation in CREATE_OPERATIONS
    inventory = _bounded(client.list_initiatives(), error_cls)
    entity = _named(inventory, change["name"], error_cls)
    desired_name = _desired_name(change)
    desired_match = _named(inventory, desired_name, error_cls)
    if (
        entity is not None
        and desired_match is not None
        and entity["id"] != desired_match["id"]
    ):
        _fail(error_cls, "ambiguous scoped Linear match for initiative name")
    if (
        not creating
        and entity is None
        and desired_name != change["name"]
        and desired_match is not None
        and _matches(desired_match, change)
    ):
        entity = desired_match
    if not creating and entity is None:
        _fail(error_cls, f"exact Linear initiative not found: {change['name']}")
    candidate_id = _deterministic_uuid4(
        "linear-command:initiative:v2", command["idempotency_key"]
    )
    if creating:
        by_id = _one(
            [item for item in inventory if item.get("id") == candidate_id],
            "initiative id",
            error_cls,
        )
        if by_id is not None:
            if entity is not None and entity["id"] != by_id["id"]:
                _fail(error_cls, "ambiguous scoped Linear match for initiative name")
            entity = by_id
    if creating and entity is not None and not _matches(entity, change):
        _fail(error_cls, "exact existing initiative conflicts with managed fields")

    before = _snapshot(change, entity) if entity is not None else None
    base = _base(command, mode)
    drift = entity is None or not _matches(entity, change)
    if not drift:
        return {
            **base,
            "result": "no_op",
            "before": before,
            "after": before,
            "plan": [],
            "no_op": True,
            "verified": True,
        }

    plan_item: dict[str, Any] = {
        "action": operation,
        "name": desired_name,
    }
    if "target_date" in change:
        plan_item["target_date"] = change["target_date"]
    if not creating:
        plan_item["fields"] = [
            field
            for field in ("new_name", "description", "target_date")
            if field in change
        ]
    plan = [plan_item]
    desired = _snapshot(change)
    if mode == "plan":
        return {
            **base,
            "result": "planned",
            "before": None,
            "after": desired,
            "plan": plan,
            "no_op": False,
            "verified": False,
        }

    optional = {
        field: change[field]
        for field in ("description", "target_date")
        if field in change
    }
    if creating:
        client.create_initiative(
            initiative_id=candidate_id,
            name=change["name"],
            **optional,
        )
    else:
        if entity is None:
            _fail(error_cls, f"exact Linear initiative not found: {change['name']}")
        fields = {
            field: change[field]
            for field in ("new_name", "description", "target_date")
            if field in change
        }
        client.update_initiative(entity["id"], **fields)
    refreshed = _named(
        _bounded(client.list_initiatives(), error_cls), desired_name, error_cls
    )
    if (
        refreshed is None
        or (creating and refreshed.get("id") != candidate_id)
        or (not creating and entity is not None and refreshed.get("id") != entity["id"])
        or not _matches(refreshed, change)
    ):
        _fail(error_cls, f"{operation} exact read-back verification failed")
    return {
        **base,
        "result": "applied",
        "before": None,
        "after": _snapshot(change, refreshed),
        "plan": plan,
        "no_op": False,
        "verified": True,
    }
