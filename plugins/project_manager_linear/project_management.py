"""Standalone bounded SIS project and milestone convergence."""

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
CREATE_OPERATIONS = {"create_project", "create_milestone"}
UPDATE_OPERATIONS = {"update_project", "update_milestone"}
OPERATIONS = CREATE_OPERATIONS | UPDATE_OPERATIONS


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


def _text(value: Any, path: str, *, required: bool, maximum: int, error_cls: type[Exception]) -> None:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        qualifier = "non-empty " if required else ""
        _fail(error_cls, f"{path} must be a {qualifier}string of at most {maximum} characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        _fail(error_cls, f"{path} contains control characters")
    if _VALIDATION.RESERVED_MARKER in value:
        _fail(error_cls, f"{path} contains the reserved marker")
    if any(pattern.search(value) for pattern in _VALIDATION.CREDENTIAL_SHAPES):
        _fail(error_cls, f"{path} contains credential-shaped data")


def _target_date(value: Any, path: str, error_cls: type[Exception]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        _fail(error_cls, f"{path} must be ISO YYYY-MM-DD or null")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(error_cls, f"{path} must be a valid calendar date")


def validate_change(change: Any, operation: str, error_cls: type[Exception]) -> dict[str, Any]:
    """Validate one exact standalone management shape."""
    if operation not in OPERATIONS or not isinstance(change, dict):
        _fail(error_cls, "project management operation is invalid")
    milestone = operation.endswith("milestone")
    allowed = {"name", "description", "target_date"}
    required = {"name"}
    if milestone:
        allowed.add("project")
        required.add("project")
    if operation in UPDATE_OPERATIONS:
        allowed.add("new_name")
        if not (set(change) & {"new_name", "description", "target_date"}):
            _fail(error_cls, f"{operation} requires at least one managed field")
    if not required.issubset(change) or not set(change).issubset(allowed):
        _fail(error_cls, f"{operation} change has invalid fields")
    for field in ("project", "name", "new_name"):
        if field in change:
            _text(change[field], field, required=True, maximum=MAX_NAME, error_cls=error_cls)
    if "description" in change:
        _text(change["description"], "description", required=False, maximum=MAX_DESCRIPTION, error_cls=error_cls)
    if "target_date" in change:
        _target_date(change["target_date"], "target_date", error_cls)
    return change


def _bounded(nodes: Any, label: str, error_cls: type[Exception]) -> list[dict[str, Any]]:
    if not isinstance(nodes, list) or len(nodes) > 100 or any(not isinstance(item, dict) for item in nodes):
        _fail(error_cls, f"{label} exceeds the supported 100-item limit or is malformed")
    return nodes


def _one(nodes: list[dict[str, Any]], label: str, error_cls: type[Exception]) -> dict[str, Any] | None:
    if len(nodes) > 1:
        _fail(error_cls, f"ambiguous scoped Linear match for {label}")
    return nodes[0] if nodes else None


def _named(nodes: list[dict[str, Any]], name: str, label: str, error_cls: type[Exception]) -> dict[str, Any] | None:
    for item in nodes:
        if not isinstance(item.get("id"), str) or not item["id"] or not isinstance(item.get("name"), str):
            _fail(error_cls, f"{label} inventory is malformed")
    return _one([item for item in nodes if item["name"] == name], f"{label} name", error_cls)


def _sis(client: Any, error_cls: type[Exception]) -> dict[str, Any]:
    teams = _bounded(client.list_teams(), "workspace teams", error_cls)
    for team in teams:
        if not isinstance(team.get("id"), str) or not team["id"] or not isinstance(team.get("key"), str):
            _fail(error_cls, "workspace teams inventory is malformed")
    team = _one([item for item in teams if item["key"] == "SIS"], "team SIS", error_cls)
    if team is None:
        _fail(error_cls, "exact SIS team was not found")
    return team


def _project_scope(project: dict[str, Any], team_id: str, error_cls: type[Exception]) -> None:
    teams = project.get("teams")
    nodes = teams.get("nodes") if isinstance(teams, dict) else None
    if (
        not isinstance(nodes, list)
        or team_id
        not in {item.get("id") for item in nodes if isinstance(item, dict)}
    ):
        _fail(error_cls, "exact project match conflicts with SIS team scope")


def _milestone_scope(milestone: dict[str, Any], project_id: str, error_cls: type[Exception]) -> None:
    project = milestone.get("project")
    if not isinstance(project, dict) or project.get("id") != project_id:
        _fail(error_cls, "exact milestone match conflicts with project scope")


def _deterministic_uuid4(domain: str, semantic_key: str) -> str:
    raw = bytearray(hashlib.sha256(f"{domain}:{semantic_key}".encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


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


def _snapshot(change: dict[str, Any], *, milestone: bool, live: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"name": live.get("name") if live is not None else _desired_name(change)}
    if milestone:
        result = {"project": change["project"], **result}
    if "target_date" in change:
        result["target_date"] = live.get("targetDate") if live is not None else change["target_date"]
    return result


def _base(command: dict[str, Any], mode: str, *, milestone: bool, name: str) -> dict[str, Any]:
    target: dict[str, Any] = {"type": "milestone" if milestone else "project", "identifier": name}
    if milestone:
        target["project"] = command["change"]["project"]
    return {
        "schema_version": "linear-result.v2",
        "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "operation": command["operation"],
        "mode": mode,
        "target": target,
    }


def execute(client: Any, command: dict[str, Any], *, mode: str, error_cls: type[Exception]) -> dict[str, Any]:
    """Plan/apply one operation, then verify through the exact bounded list query."""
    operation = command["operation"]
    change = command["change"]
    milestone = operation.endswith("milestone")
    creating = operation in CREATE_OPERATIONS
    team = _sis(client, error_cls)
    projects = _bounded(client.list_team_projects(team["id"]), "SIS team projects", error_cls)

    project: dict[str, Any] | None
    entity: dict[str, Any] | None
    if milestone:
        project = _named(projects, change["project"], "project", error_cls)
        if project is None:
            _fail(error_cls, f"exact Linear project not found: {change['project']}")
        _project_scope(project, team["id"], error_cls)
        inventory = _bounded(client.list_project_milestones(project["id"]), "project milestones", error_cls)
        entity = _named(inventory, change["name"], "milestone", error_cls)
    else:
        project = None
        inventory = projects
        entity = _named(inventory, change["name"], "project", error_cls)

    desired_name = _desired_name(change)
    desired_match = _named(inventory, desired_name, "milestone" if milestone else "project", error_cls)
    if entity is not None and desired_match is not None and entity["id"] != desired_match["id"]:
        _fail(error_cls, f"ambiguous scoped Linear match for {'milestone' if milestone else 'project'} name")
    if not creating and entity is None and desired_name != change["name"] and desired_match is not None and _matches(desired_match, change):
        entity = desired_match
    if not creating and entity is None:
        _fail(
            error_cls,
            f"exact Linear {'milestone' if milestone else 'project'} not found: {change['name']}",
        )
    if entity is not None:
        if milestone:
            _milestone_scope(entity, project["id"], error_cls)
        else:
            _project_scope(entity, team["id"], error_cls)

    candidate_id = _deterministic_uuid4(
        f"linear-command:{'milestone' if milestone else 'project'}:v2",
        command["idempotency_key"],
    )
    if creating:
        by_id = _one([item for item in inventory if item.get("id") == candidate_id], f"{'milestone' if milestone else 'project'} id", error_cls)
        if by_id is not None:
            if entity is not None and entity["id"] != by_id["id"]:
                _fail(error_cls, f"ambiguous scoped Linear match for {'milestone' if milestone else 'project'} name")
            entity = by_id
            if milestone:
                _milestone_scope(entity, project["id"], error_cls)
            else:
                _project_scope(entity, team["id"], error_cls)
        if entity is not None and not _matches(entity, change):
            _fail(error_cls, f"exact existing {'milestone' if milestone else 'project'} conflicts with managed fields")

    before = _snapshot(change, milestone=milestone, live=entity) if entity is not None else None
    drift = entity is None or not _matches(entity, change)
    if creating:
        action = "create_milestone" if milestone else "create_project"
    else:
        action = "update_milestone" if milestone else "update_project"
    plan = []
    if drift:
        item: dict[str, Any] = {"action": action, "name": desired_name}
        if milestone:
            item["project"] = change["project"]
        if "target_date" in change:
            item["target_date"] = change["target_date"]
        if not creating:
            item["fields"] = [field for field in ("new_name", "description", "target_date") if field in change]
        plan = [item]
    after_desired = _snapshot(change, milestone=milestone)
    base = _base(command, mode, milestone=milestone, name=desired_name)
    if not drift:
        return {**base, "result": "no_op", "before": before, "after": before, "plan": [], "no_op": True, "verified": True}
    if mode == "plan":
        return {**base, "result": "planned", "before": before, "after": after_desired, "plan": plan, "no_op": False, "verified": False}

    optional = {field: change[field] for field in ("description", "target_date") if field in change}
    if creating:
        if milestone:
            client.create_project_milestone(milestone_id=candidate_id, project_id=project["id"], name=change["name"], **optional)
        else:
            client.create_project(project_id=candidate_id, team_id=team["id"], name=change["name"], **optional)
    else:
        fields = {field: change[field] for field in ("new_name", "description", "target_date") if field in change}
        if entity is None:
            _fail(error_cls, f"exact Linear {'milestone' if milestone else 'project'} not found: {change['name']}")
        if milestone:
            client.update_project_milestone(entity["id"], **fields)
        else:
            client.update_project(entity["id"], **fields)

    refreshed_projects = _bounded(client.list_team_projects(team["id"]), "SIS team projects", error_cls)
    if milestone:
        refreshed_project = _named(refreshed_projects, change["project"], "project", error_cls)
        if refreshed_project is None or refreshed_project["id"] != project["id"]:
            _fail(error_cls, "milestone exact read-back verification failed")
        refreshed = _named(
            _bounded(client.list_project_milestones(project["id"]), "project milestones", error_cls),
            desired_name,
            "milestone",
            error_cls,
        )
        if refreshed is not None:
            _milestone_scope(refreshed, project["id"], error_cls)
    else:
        refreshed = _named(refreshed_projects, desired_name, "project", error_cls)
        if refreshed is not None:
            _project_scope(refreshed, team["id"], error_cls)
    if refreshed is None or not _matches(refreshed, change):
        _fail(error_cls, f"{operation} exact read-back verification failed")
    after = _snapshot(change, milestone=milestone, live=refreshed)
    return {**base, "result": "applied", "before": before, "after": after, "plan": plan, "no_op": False, "verified": True}
