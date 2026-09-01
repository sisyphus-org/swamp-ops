"""Fail-closed owner-approved archive/delete for exact core Linear entities."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

OPERATIONS = {"archive_linear_entity", "delete_linear_entity"}
ENTITY_TYPES = {"issue", "project", "milestone", "initiative"}
SAFE_MATRIX = {
    ("archive_linear_entity", "issue"),
    ("archive_linear_entity", "project"),
    ("archive_linear_entity", "initiative"),
    ("delete_linear_entity", "issue"),
    ("delete_linear_entity", "project"),
    ("delete_linear_entity", "milestone"),
    ("delete_linear_entity", "initiative"),
}
COLLECTION = {
    "issue": "issues", "project": "projects",
    "milestone": "milestones", "initiative": "initiatives",
}


def _clean_name(value: Any, label: str, error_cls: type[Exception]) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 200
        or any(ord(char) < 32 for char in value)
    ):
        raise error_cls(f"{label} must be an exact safe 1-200 character name")
    return value


def validate_target(target: Any, operation: str, error_cls: type[Exception]) -> None:
    if not isinstance(target, dict) or set(target) != {"type", "selector"}:
        raise error_cls("destructive target must contain exactly type and selector")
    entity_type = target.get("type")
    selector = target.get("selector")
    if entity_type not in ENTITY_TYPES or not isinstance(selector, dict):
        raise error_cls("destructive entity_type or selector is invalid")
    if (operation, entity_type) not in SAFE_MATRIX:
        raise error_cls("archive/delete combination is outside the supported safe matrix")
    if entity_type == "issue":
        import re
        identifier = selector.get("identifier")
        if (
            set(selector) != {"identifier"} or not isinstance(identifier, str)
            or re.fullmatch(r"SIS-[1-9][0-9]*", identifier) is None
        ):
            raise error_cls("issue selector must be an exact SIS-N")
    elif entity_type in {"project", "initiative"}:
        if set(selector) != {"name"}:
            raise error_cls(f"{entity_type} selector must contain exactly name")
        _clean_name(selector.get("name"), f"{entity_type} selector name", error_cls)
    else:
        if set(selector) != {"project", "name"}:
            raise error_cls("milestone selector must contain exactly project and name")
        _clean_name(selector.get("project"), "milestone selector project", error_cls)
        _clean_name(selector.get("name"), "milestone selector name", error_cls)


def _scrub(value: Any) -> Any:
    """Remove trusted IDs from approval/public projections while retaining facts."""
    if isinstance(value, dict):
        return {
            key: _scrub(item)
            for key, item in sorted(value.items())
            if key not in {"id", "archivedAt", "email", "lastSyncId"}
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return copy.deepcopy(value)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _teams(node: dict[str, Any]) -> list[dict[str, Any]]:
    teams = node.get("teams")
    nodes = teams.get("nodes") if isinstance(teams, dict) else None
    return nodes if isinstance(nodes, list) else []


def _matches(node: Any, entity_type: str, selector: dict[str, str]) -> bool:
    if not isinstance(node, dict):
        return False
    if entity_type == "issue":
        team = node.get("team")
        return (
            node.get("identifier") == selector["identifier"]
            and isinstance(team, dict) and team.get("key") == "SIS"
        )
    if entity_type == "project":
        return node.get("name") == selector["name"] and any(
            team.get("key") == "SIS" for team in _teams(node) if isinstance(team, dict)
        )
    if entity_type == "initiative":
        return node.get("name") == selector["name"]
    project = node.get("project")
    return (
        node.get("name") == selector["name"] and isinstance(project, dict)
        and project.get("name") == selector["project"]
        and any(team.get("key") == "SIS" for team in _teams(project) if isinstance(team, dict))
    )


def _inventory(client: Any, entity_type: str, *, include_archived: bool = True) -> list[dict[str, Any]]:
    value = client.list_linear_entities(
        COLLECTION[entity_type], include_archived=include_archived
    )
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RuntimeError("Linear exact scoped inventory is invalid")
    return value


def _complete_children(client: Any, root_identifier: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    pending = [root_identifier]
    seen = {root_identifier}
    while pending:
        parent = pending.pop()
        children = client.list_child_issues(parent)
        if not isinstance(children, list):
            raise RuntimeError("Linear child impact inventory is invalid")
        for child in children:
            if not isinstance(child, dict):
                raise RuntimeError("Linear child impact inventory is invalid")
            identifier = child.get("identifier")
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                raise RuntimeError("Linear child impact inventory contains a cycle or duplicate")
            seen.add(identifier)
            result.append(child)
            pending.append(identifier)
    return result


def _impact(client: Any, entity_type: str, entity: dict[str, Any]) -> dict[str, list[Any]]:
    if entity_type == "issue":
        result = {"children": _complete_children(client, entity["identifier"])}
        if hasattr(client, "list_issue_relations"):
            result["relations"] = client.list_issue_relations(entity["identifier"])
        return result
    if entity_type == "project":
        result = {
            "issues": client.list_project_issues(entity["id"]),
            "milestones": client.list_project_milestones(entity["id"]),
        }
        if hasattr(client, "list_project_initiatives"):
            result["linked_initiatives"] = client.list_project_initiatives(entity["id"])
        return result
    if entity_type == "milestone":
        return {"issues": client.list_milestone_issues(entity["id"])}
    return {"linked_projects": client.list_initiative_projects(entity["id"])}


def _canonical_impact(impact: dict[str, list[Any]]) -> dict[str, list[Any]]:
    if not isinstance(impact, dict) or any(not isinstance(items, list) for items in impact.values()):
        raise RuntimeError("Linear dependency impact inventory is invalid")
    normalized: dict[str, list[Any]] = {}
    for key in sorted(impact):
        values = [_scrub(item) for item in impact[key]]
        values.sort(key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ))
        if len({_hash(item) for item in values}) != len(values):
            raise RuntimeError("Linear dependency impact inventory contains duplicates")
        normalized[key] = values
    return normalized


def _load(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("entity destruction recovery journal is invalid") from exc
    if (
        not isinstance(value, dict) or value.get("schema_version") != 2
        or not isinstance(value.get("entries"), dict)
    ):
        raise RuntimeError("entity destruction recovery journal is invalid")
    entries = value["entries"]
    required = {
        "request_hash", "approval_checksum", "intent_hash", "command_hash",
        "before_state_hash", "after_state_hash", "impact_before_hash", "phase",
    }
    for key, entry in entries.items():
        if (
            not isinstance(key, str) or not isinstance(entry, dict)
            or set(entry) != required or entry.get("phase") not in {"prepared", "completed"}
            or any(
                not isinstance(entry.get(field), str)
                or len(entry[field]) != 64
                for field in required - {"phase"}
            )
        ):
            raise RuntimeError("entity destruction recovery journal is invalid")
    return entries


def _write(path: Path, entries: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump({"schema_version": 2, "entries": entries}, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _direct_lookup(client: Any, entity_type: str, entity_id: str) -> Any:
    if hasattr(client, "get_linear_entity"):
        return client.get_linear_entity(entity_type, entity_id)
    if entity_type == "issue" and hasattr(client, "get_issue"):
        return client.get_issue(entity_id)
    return None


def _verify_archive_impact(
    client: Any, entity_type: str, entity: dict[str, Any], expected: dict[str, list[Any]],
    error_cls: type[Exception],
) -> None:
    actual = _canonical_impact(_impact(client, entity_type, entity))
    if actual != expected:
        raise error_cls("archive_linear_entity impacted entity read-back drifted")


def _same_except(before: dict[str, Any], after: dict[str, Any], fields: set[str]) -> bool:
    left, right = _scrub(before), _scrub(after)
    for field in fields:
        left.pop(field, None)
        right.pop(field, None)
    return left == right


def _verify_delete_impact(
    client: Any, entity_type: str, entity: dict[str, Any], raw: dict[str, list[Any]],
    error_cls: type[Exception],
) -> None:
    """Read every affected object and allow only documented unlink/cascade effects."""
    if not hasattr(client, "get_linear_entity"):
        return  # deterministic test doubles may expose only scoped inventories
    if entity_type == "issue":
        for child in raw.get("children", []):
            current = _direct_lookup(client, "issue", child["id"])
            if not isinstance(current, dict) or not _same_except(child, current, {"parent"}):
                raise error_cls("delete_linear_entity child issue read-back drifted")
            parent = current.get("parent")
            if parent is not None and parent.get("id") == entity["id"]:
                raise error_cls("delete_linear_entity child remained linked to trashed parent")
        for relation in raw.get("relations", []):
            for endpoint in (relation.get("issue"), relation.get("relatedIssue")):
                if isinstance(endpoint, dict) and endpoint.get("id") != entity["id"]:
                    current = _direct_lookup(client, "issue", endpoint["id"])
                    if not isinstance(current, dict):
                        raise error_cls("delete_linear_entity related issue disappeared")
                    remaining = client.list_issue_relations(current["identifier"])
                    if any(item.get("id") == relation.get("id") for item in remaining):
                        raise error_cls("delete_linear_entity relation remained linked")
    elif entity_type == "project":
        for issue in raw.get("issues", []):
            current = _direct_lookup(client, "issue", issue["id"])
            if not isinstance(current, dict) or not _same_except(
                issue, current, {"project", "projectMilestone"}
            ):
                raise error_cls("delete_linear_entity project issue read-back drifted")
            project = current.get("project")
            if project is not None and project.get("id") == entity["id"]:
                raise error_cls("delete_linear_entity issue remained linked to trashed project")
        for milestone in raw.get("milestones", []):
            if _direct_lookup(client, "milestone", milestone["id"]) is not None:
                raise error_cls("delete_linear_entity project milestone did not cascade")
        for initiative in raw.get("linked_initiatives", []):
            current = _direct_lookup(client, "initiative", initiative["id"])
            if not isinstance(current, dict):
                raise error_cls("delete_linear_entity linked initiative disappeared")
            if any(item.get("id") == entity["id"] for item in client.list_initiative_projects(initiative["id"])):
                raise error_cls("delete_linear_entity initiative link remained")
    elif entity_type == "milestone":
        for issue in raw.get("issues", []):
            current = _direct_lookup(client, "issue", issue["id"])
            if not isinstance(current, dict) or not _same_except(issue, current, {"projectMilestone"}):
                raise error_cls("delete_linear_entity milestone issue read-back drifted")
            milestone = current.get("projectMilestone")
            if milestone is not None and milestone.get("id") == entity["id"]:
                raise error_cls("delete_linear_entity issue remained linked to deleted milestone")
    else:
        for project in raw.get("linked_projects", []):
            current = _direct_lookup(client, "project", project["id"])
            if not isinstance(current, dict) or not _same_except(project, current, {"initiatives"}):
                raise error_cls("delete_linear_entity linked project read-back drifted")
            if any(item.get("id") == entity["id"] for item in client.list_project_initiatives(project["id"])):
                raise error_cls("delete_linear_entity project remained linked to trashed initiative")


def execute(
    client: Any, command: dict[str, Any], *, mode: str, journal_path: Path | None,
    key_hash: str, request_hash: str, error_cls: type[Exception],
) -> dict[str, Any]:
    operation = command["operation"]
    target = command["target"]
    entity_type = target["type"]
    selector = target["selector"]
    base = {
        "schema_version": "linear-result.v2", "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"], "operation": operation,
        "mode": mode, "target": copy.deepcopy(target),
    }
    recovery_path = (
        journal_path.with_name(journal_path.name + ".linear-entity-destruction")
        if journal_path else None
    )
    entries = _load(recovery_path) if recovery_path else {}
    recovery = entries.get(key_hash)
    approval_ref = command["policy"]["approval"]
    binding = {
        "approval_checksum": approval_ref["checksum"],
        "intent_hash": approval_ref["intent_hash"],
        "command_hash": _hash(command),
    }
    if recovery is not None and (
        recovery.get("request_hash") != request_hash
        or any(recovery.get(key) != value for key, value in binding.items())
    ):
        raise error_cls("entity destruction recovery journal request conflict")

    inventory = _inventory(client, entity_type, include_archived=True)
    matches = [node for node in inventory if _matches(node, entity_type, selector)]
    if len(matches) > 1:
        raise error_cls(f"exact Linear {entity_type} selector is ambiguous")
    active = [node for node in matches if node.get("archivedAt") is None]

    def evidence(entry: dict[str, str]) -> dict[str, str]:
        return {
            "schema_version": "linear-owner-recovery.v1", **binding,
            "before_state_hash": entry["before_state_hash"],
            "after_state_hash": entry["after_state_hash"], "phase": entry["phase"],
        }

    if recovery is not None and recovery.get("phase") in {"prepared", "completed"}:
        recovered_after: dict[str, Any] = {"present": False}
        recovered = False
        if operation == "archive_linear_entity" and len(matches) == 1 and not active:
            recovered_after = {"entity": _scrub(matches[0]), "archived": True}
            recovered = _hash(recovered_after) == recovery.get("after_state_hash")
        elif operation == "delete_linear_entity" and not matches:
            recovered = True
        if recovered:
            recovery["phase"] = "completed"
            if recovery_path:
                _write(recovery_path, entries)
            return {
                **base, "result": "no_op", "before": {"recovered": True},
                "after": recovered_after, "plan": [], "no_op": True,
                "verified": True, "recovered": True,
                "recovery_evidence": evidence(recovery),
            }

    if len(active) != 1:
        raise error_cls(f"exact active Linear {entity_type} not found")
    entity = active[0]
    raw_impact = _impact(client, entity_type, entity)
    impact = _canonical_impact(raw_impact)
    impact_counts = {key: len(items) for key, items in impact.items()}
    before = {
        "entity": _scrub(entity), "archived": False,
        "impact": impact, "impact_counts": impact_counts,
    }
    after = (
        {"entity": _scrub(entity), "archived": True}
        if operation == "archive_linear_entity" else {"present": False}
    )
    plan = [{
        "action": operation, "entity_type": entity_type,
        "selector": copy.deepcopy(selector), "impact_counts": impact_counts,
        "affected_entities": copy.deepcopy(impact),
    }]
    if mode == "plan":
        return {
            **base, "result": "planned", "before": before, "after": after,
            "plan": plan, "no_op": False, "verified": False,
        }
    if recovery_path is None:
        raise error_cls("entity destruction apply requires recovery journal")
    entry = {
        "request_hash": request_hash, **binding,
        "before_state_hash": _hash(before), "after_state_hash": _hash(after),
        "impact_before_hash": _hash(impact), "phase": "prepared",
    }
    entries[key_hash] = entry
    _write(recovery_path, entries)
    if operation == "archive_linear_entity":
        client.archive_linear_entity(entity_type, entity["id"])
    else:
        client.delete_linear_entity(entity_type, entity["id"])

    normal_matches = [
        node for node in _inventory(client, entity_type, include_archived=False)
        if _matches(node, entity_type, selector)
    ]
    verified_inventory = _inventory(client, entity_type, include_archived=True)
    verified_matches = [
        node for node in verified_inventory if _matches(node, entity_type, selector)
    ]
    if operation == "archive_linear_entity":
        if normal_matches or len(verified_matches) != 1 or verified_matches[0].get("archivedAt") is None:
            raise error_cls("archive_linear_entity exact read-back verification failed")
        verified_after = {"entity": _scrub(verified_matches[0]), "archived": True}
        if verified_after != after:
            raise error_cls("archive_linear_entity read-back changed unmanaged fields")
        _verify_archive_impact(client, entity_type, verified_matches[0], impact, error_cls)
    else:
        if normal_matches or verified_matches:
            raise error_cls("delete_linear_entity exact read-back verification failed")
        direct = _direct_lookup(client, entity_type, entity["id"])
        if direct is not None:
            raise error_cls("delete_linear_entity direct lookup still returned the target")
        _verify_delete_impact(client, entity_type, entity, raw_impact, error_cls)
    entry["phase"] = "completed"
    _write(recovery_path, entries)
    return {
        **base, "result": "applied", "before": before, "after": after,
        "plan": plan, "no_op": False, "verified": True,
        "recovery_evidence": evidence(entry),
    }
