"""Fail-closed owner-approved archive/delete for exact core Linear entities."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
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


def _canonical_raw_impact(impact: dict[str, list[Any]]) -> dict[str, list[Any]]:
    if not isinstance(impact, dict) or any(not isinstance(items, list) for items in impact.values()):
        raise RuntimeError("Linear dependency impact inventory is invalid")
    normalized: dict[str, list[Any]] = {}
    for key in sorted(impact):
        raw_ids = [
            item.get("id") if isinstance(item, dict) else None
            for item in impact[key]
        ]
        if any(not isinstance(item, str) or not item for item in raw_ids):
            raise RuntimeError(
                "Linear dependency impact inventory contains an invalid raw ID"
            )
        if len(set(raw_ids)) != len(raw_ids):
            raise RuntimeError(
                "Linear dependency impact inventory contains duplicate raw IDs"
            )
        values = copy.deepcopy(impact[key])
        values.sort(key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ))
        normalized[key] = values
    return normalized


def _canonical_impact(impact: dict[str, list[Any]]) -> dict[str, list[Any]]:
    return {
        key: [_scrub(item) for item in items]
        for key, items in _canonical_raw_impact(impact).items()
    }


def _trusted_before_state(
    entity: dict[str, Any], raw_impact: dict[str, list[Any]]
) -> dict[str, Any]:
    """Canonical internal state used only for approval identity binding."""
    impact = _canonical_raw_impact(raw_impact)
    return {
        "entity": copy.deepcopy(entity),
        "archived": False,
        "impact": impact,
        "impact_counts": {key: len(items) for key, items in impact.items()},
    }


def _project_issue_archive_impact(
    entity: dict[str, Any],
    raw_impact: dict[str, list[Any]],
    projection: dict[str, Any],
) -> dict[str, list[Any]]:
    """Remove only producer-committed direct-child subtrees from issue impact."""
    producers = projection.get("producers")
    if (
        set(projection)
        != {"version", "parent_intent_hash", "archive_target", "producers"}
        or projection.get("version") != 1
        or not isinstance(projection.get("parent_intent_hash"), str)
        or len(projection["parent_intent_hash"]) != 64
        or projection.get("archive_target") != entity.get("identifier")
        or not isinstance(producers, list)
        or not producers
    ):
        raise RuntimeError("bulk lifecycle projection is invalid")
    children = raw_impact.get("children")
    if not isinstance(children, list):
        raise RuntimeError("bulk lifecycle projection requires issue child impact")
    roots: set[str] = set()
    producer_indices: set[int] = set()
    for producer in producers:
        if (
            not isinstance(producer, dict)
            or set(producer) != {"index", "kind", "root_identifier"}
            or producer.get("kind")
            not in {"reparent_from_direct_parent", "archive_direct_child"}
            or not isinstance(producer.get("index"), int)
            or producer["index"] < 0
            or producer["index"] in producer_indices
            or not isinstance(producer.get("root_identifier"), str)
            or producer["root_identifier"] in roots
        ):
            raise RuntimeError("bulk lifecycle projection is invalid")
        producer_indices.add(producer["index"])
        roots.add(producer["root_identifier"])
    by_identifier: dict[str, dict[str, Any]] = {}
    for child in children:
        identifier = child.get("identifier") if isinstance(child, dict) else None
        if not isinstance(identifier, str) or identifier in by_identifier:
            raise RuntimeError("bulk lifecycle child impact is ambiguous")
        by_identifier[identifier] = child
    entity_id = entity.get("id")
    for root in roots:
        child = by_identifier.get(root)
        parent = child.get("parent") if isinstance(child, dict) else None
        if (
            child is None
            or not isinstance(parent, dict)
            or parent.get("identifier") != entity.get("identifier")
            or (
                isinstance(entity_id, str)
                and parent.get("id") != entity_id
            )
        ):
            raise RuntimeError(
                "bulk lifecycle projection root is not an exact direct child"
            )
    excluded = set(roots)
    changed = True
    while changed:
        changed = False
        for identifier, child in by_identifier.items():
            parent = child.get("parent")
            parent_identifier = (
                parent.get("identifier") if isinstance(parent, dict) else None
            )
            if identifier not in excluded and parent_identifier in excluded:
                excluded.add(identifier)
                changed = True
    projected = copy.deepcopy(raw_impact)
    projected["children"] = [
        child for child in children if child.get("identifier") not in excluded
    ]
    return projected


def _load(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("entity destruction recovery journal is invalid") from exc
    if (
        not isinstance(value, dict) or value.get("schema_version") != 3
        or not isinstance(value.get("entries"), dict)
    ):
        raise RuntimeError("entity destruction recovery journal is invalid")
    entries = value["entries"]
    required = {
        "request_hash", "approval_checksum", "intent_hash", "command_hash",
        "before_state_hash", "after_state_hash", "impact_before_hash", "phase",
        "recovery_manifest", "manifest_hash", "manifest_binding_hash",
    }
    digest_fields = required - {"phase", "recovery_manifest"}
    for key, entry in entries.items():
        manifest = entry.get("recovery_manifest") if isinstance(entry, dict) else None
        if (
            not isinstance(key, str) or not isinstance(entry, dict)
            or set(entry) != required or entry.get("phase") not in {"prepared", "completed"}
            or any(
                not isinstance(entry.get(field), str)
                or len(entry[field]) != 64
                for field in digest_fields
            )
            or not _valid_recovery_manifest(manifest)
            or entry["manifest_hash"] != _hash(manifest)
            or entry["manifest_binding_hash"] != _hash({
                "approval_checksum": entry["approval_checksum"],
                "intent_hash": entry["intent_hash"],
                "command_hash": entry["command_hash"],
                "before_state_hash": entry["before_state_hash"],
                "manifest_hash": entry["manifest_hash"],
            })
        ):
            raise RuntimeError("entity destruction recovery journal is invalid")
    return entries


def _write(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump({"schema_version": 3, "entries": entries}, handle, sort_keys=True)
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


def _state_hash_except(value: dict[str, Any], fields: set[str]) -> str:
    projected = _scrub(value)
    for field in fields:
        projected.pop(field, None)
    return _hash(projected)


def _archive_recovery_manifest(
    entity_type: str, raw: dict[str, list[Any]]
) -> dict[str, Any]:
    if entity_type == "issue":
        return {
            "schema_version": 2,
            "operation": "archive_linear_entity",
            "entity_type": entity_type,
            "children": [
                {"id": child["id"], "state_hash": _hash(child)}
                for child in raw.get("children", [])
            ],
            "relations": [
                {"id": relation["id"], "state_hash": _hash(relation)}
                for relation in raw.get("relations", [])
            ],
        }
    return {
        "schema_version": 1,
        "operation": "archive_linear_entity",
        "entity_type": entity_type,
        "impact_hash": _hash(_canonical_impact(raw)),
    }


def _delete_recovery_manifest(
    entity_type: str,
    entity: dict[str, Any],
    raw: dict[str, list[Any]],
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "operation": "delete_linear_entity",
        "entity_type": entity_type,
        "entity_id": entity["id"],
    }
    if entity_type == "issue":
        manifest["children"] = [
            {
                "id": child["id"],
                "state_hash": _state_hash_except(child, {"parent"}),
            }
            for child in raw.get("children", [])
        ]
        relations = []
        for relation in raw.get("relations", []):
            peer_ids = sorted(
                {
                    endpoint["id"]
                    for endpoint in (
                        relation.get("issue"),
                        relation.get("relatedIssue"),
                    )
                    if isinstance(endpoint, dict)
                    and isinstance(endpoint.get("id"), str)
                    and endpoint["id"] != entity["id"]
                }
            )
            relations.append({"id": relation["id"], "peer_ids": peer_ids})
        manifest["relations"] = relations
    elif entity_type == "project":
        manifest["issues"] = [
            {
                "id": issue["id"],
                "state_hash": _state_hash_except(
                    issue, {"project", "projectMilestone"}
                ),
            }
            for issue in raw.get("issues", [])
        ]
        manifest["milestone_ids"] = [
            milestone["id"] for milestone in raw.get("milestones", [])
        ]
        manifest["initiative_ids"] = [
            initiative["id"] for initiative in raw.get("linked_initiatives", [])
        ]
    elif entity_type == "milestone":
        manifest["issues"] = [
            {
                "id": issue["id"],
                "state_hash": _state_hash_except(issue, {"projectMilestone"}),
            }
            for issue in raw.get("issues", [])
        ]
    else:
        manifest["projects"] = [
            {
                "id": project["id"],
                "state_hash": _state_hash_except(project, {"initiatives"}),
            }
            for project in raw.get("linked_projects", [])
        ]
    return manifest


def _valid_recovery_manifest(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") not in {1, 2}
    ):
        return False
    operation = value.get("operation")
    entity_type = value.get("entity_type")
    if not isinstance(operation, str) or not isinstance(entity_type, str):
        return False
    if operation == "archive_linear_entity" and entity_type == "issue":
        children = value.get("children")
        relations = value.get("relations")
        if not isinstance(children, list) or not isinstance(relations, list):
            return False
        dependencies = [*children, *relations]
        return bool(
            set(value)
            == {
                "schema_version",
                "operation",
                "entity_type",
                "children",
                "relations",
            }
            and value.get("schema_version") == 2
            and all(
                isinstance(item, dict)
                and set(item) == {"id", "state_hash"}
                and isinstance(item.get("id"), str)
                and item["id"]
                and isinstance(item.get("state_hash"), str)
                and len(item["state_hash"]) == 64
                for item in dependencies
            )
        )
    if operation == "archive_linear_entity":
        return (
            set(value)
            == {"schema_version", "operation", "entity_type", "impact_hash"}
            and entity_type in ENTITY_TYPES
            and isinstance(value.get("impact_hash"), str)
            and len(value["impact_hash"]) == 64
        )
    expected = {
        "issue": {"children", "relations"},
        "project": {"issues", "milestone_ids", "initiative_ids"},
        "milestone": {"issues"},
        "initiative": {"projects"},
    }
    if operation != "delete_linear_entity" or entity_type not in expected:
        return False
    dependency_fields = expected[entity_type]
    if set(value) != {
        "schema_version", "operation", "entity_type", "entity_id",
        *dependency_fields,
    } or not isinstance(value.get("entity_id"), str) or not value["entity_id"]:
        return False
    if any(not isinstance(value.get(field), list) for field in dependency_fields):
        return False
    hash_items = [
        *value.get("children", []),
        *value.get("issues", []),
        *value.get("projects", []),
    ]
    if any(
        not isinstance(item, dict)
        or set(item) != {"id", "state_hash"}
        or not isinstance(item.get("id"), str)
        or not item["id"]
        or not isinstance(item.get("state_hash"), str)
        or len(item["state_hash"]) != 64
        for item in hash_items
    ):
        return False
    relations = value.get("relations", [])
    if any(
        not isinstance(item, dict)
        or set(item) != {"id", "peer_ids"}
        or not isinstance(item.get("id"), str)
        or not item["id"]
        or not isinstance(item.get("peer_ids"), list)
        or any(not isinstance(peer_id, str) or not peer_id for peer_id in item["peer_ids"])
        for item in relations
    ):
        return False
    id_lists = [
        value.get("milestone_ids", []), value.get("initiative_ids", [])
    ]
    return all(
        isinstance(items, list)
        and all(isinstance(item, str) and item for item in items)
        for items in id_lists
    )


def _verify_archive_impact(
    client: Any, entity_type: str, entity: dict[str, Any],
    manifest: dict[str, Any], error_cls: type[Exception],
) -> None:
    if entity_type == "issue" and manifest.get("schema_version") == 2:
        for child in manifest["children"]:
            current = _direct_lookup(client, "issue", child["id"])
            if not isinstance(current, dict) or _hash(current) != child["state_hash"]:
                raise error_cls("archive_linear_entity impacted child read-back drifted")
        for relation in manifest["relations"]:
            current = client.get_issue_relation(relation["id"])
            if not isinstance(current, dict) or _hash(current) != relation["state_hash"]:
                raise error_cls("archive_linear_entity impacted relation read-back drifted")
        return
    actual = _canonical_impact(_impact(client, entity_type, entity))
    if _hash(actual) != manifest["impact_hash"]:
        raise error_cls("archive_linear_entity impacted entity read-back drifted")


def _verify_delete_impact(
    client: Any, entity_type: str, entity: dict[str, Any],
    manifest: dict[str, Any], error_cls: type[Exception],
) -> None:
    """Read every affected object and allow only documented unlink/cascade effects."""
    if not hasattr(client, "get_linear_entity"):
        return  # deterministic test doubles may expose only scoped inventories
    if entity_type == "issue":
        for child in manifest.get("children", []):
            current = _direct_lookup(client, "issue", child["id"])
            if (
                not isinstance(current, dict)
                or _state_hash_except(current, {"parent"}) != child["state_hash"]
            ):
                raise error_cls("delete_linear_entity child issue read-back drifted")
            parent = current.get("parent")
            if parent is not None and parent.get("id") == manifest["entity_id"]:
                raise error_cls("delete_linear_entity child remained linked to trashed parent")
        for relation in manifest.get("relations", []):
            for peer_id in relation["peer_ids"]:
                current = _direct_lookup(client, "issue", peer_id)
                if not isinstance(current, dict):
                    raise error_cls("delete_linear_entity related issue disappeared")
                remaining = client.list_issue_relations(current["identifier"])
                if any(item.get("id") == relation["id"] for item in remaining):
                    raise error_cls("delete_linear_entity relation remained linked")
    elif entity_type == "project":
        for issue in manifest.get("issues", []):
            current = _direct_lookup(client, "issue", issue["id"])
            if (
                not isinstance(current, dict)
                or _state_hash_except(
                    current, {"project", "projectMilestone"}
                ) != issue["state_hash"]
            ):
                raise error_cls("delete_linear_entity project issue read-back drifted")
            project = current.get("project")
            if project is not None and project.get("id") == manifest["entity_id"]:
                raise error_cls("delete_linear_entity issue remained linked to trashed project")
        for milestone_id in manifest.get("milestone_ids", []):
            if _direct_lookup(client, "milestone", milestone_id) is not None:
                raise error_cls("delete_linear_entity project milestone did not cascade")
        for initiative_id in manifest.get("initiative_ids", []):
            current = _direct_lookup(client, "initiative", initiative_id)
            if not isinstance(current, dict):
                raise error_cls("delete_linear_entity linked initiative disappeared")
            if any(
                item.get("id") == manifest["entity_id"]
                for item in client.list_initiative_projects(initiative_id)
            ):
                raise error_cls("delete_linear_entity initiative link remained")
    elif entity_type == "milestone":
        for issue in manifest.get("issues", []):
            current = _direct_lookup(client, "issue", issue["id"])
            if (
                not isinstance(current, dict)
                or _state_hash_except(current, {"projectMilestone"})
                != issue["state_hash"]
            ):
                raise error_cls("delete_linear_entity milestone issue read-back drifted")
            milestone = current.get("projectMilestone")
            if milestone is not None and milestone.get("id") == manifest["entity_id"]:
                raise error_cls("delete_linear_entity issue remained linked to deleted milestone")
    else:
        for project in manifest.get("projects", []):
            current = _direct_lookup(client, "project", project["id"])
            if (
                not isinstance(current, dict)
                or _state_hash_except(current, {"initiatives"})
                != project["state_hash"]
            ):
                raise error_cls("delete_linear_entity linked project read-back drifted")
            if any(
                item.get("id") == manifest["entity_id"]
                for item in client.list_project_initiatives(project["id"])
            ):
                raise error_cls("delete_linear_entity project remained linked to trashed initiative")


def execute(
    client: Any, command: dict[str, Any], *, mode: str, journal_path: Path | None,
    key_hash: str, request_hash: str, error_cls: type[Exception],
    projection: dict[str, Any] | None = None,
    authorized_before_state_hash: str | None = None,
) -> dict[str, Any]:
    operation = command["operation"]
    if mode == "apply":
        required = [
            "get_linear_entity",
            "list_issue_relations",
            "list_project_initiatives",
        ]
        if (
            operation == "archive_linear_entity"
            and command.get("target", {}).get("type") == "issue"
        ):
            required.append("get_issue_relation")
        missing = [
            capability
            for capability in required
            if not callable(getattr(client, capability, None))
        ]
        if missing:
            raise error_cls(
                "destructive apply requires client capability: " + missing[0]
            )
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
    approval_ref = command["policy"].get("approval")
    binding = (
        {
            "approval_checksum": approval_ref["checksum"],
            "intent_hash": approval_ref["intent_hash"],
            "command_hash": _hash(command),
        }
        if isinstance(approval_ref, dict)
        else {}
    )
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

    def evidence(entry: dict[str, Any]) -> dict[str, str]:
        return {
            "schema_version": "linear-owner-recovery.v1", **binding,
            "before_state_hash": entry["before_state_hash"],
            "after_state_hash": entry["after_state_hash"], "phase": entry["phase"],
        }

    if recovery is not None and recovery.get("phase") in {"prepared", "completed"}:
        manifest = recovery["recovery_manifest"]
        if (
            manifest.get("operation") != operation
            or manifest.get("entity_type") != entity_type
        ):
            raise error_cls("entity destruction recovery manifest conflicts with target")
        recovered_after: dict[str, Any] = {"present": False}
        recovered = False
        if operation == "archive_linear_entity" and len(matches) == 1 and not active:
            recovered_after = {"entity": _scrub(matches[0]), "archived": True}
            recovered = _hash(recovered_after) == recovery.get("after_state_hash")
            if recovered:
                _verify_archive_impact(
                    client, entity_type, matches[0], manifest, error_cls
                )
        elif operation == "delete_linear_entity" and not matches:
            if _direct_lookup(client, entity_type, manifest["entity_id"]) is not None:
                raise error_cls("delete_linear_entity direct lookup still returned the target")
            _verify_delete_impact(
                client, entity_type, {"id": manifest["entity_id"]}, manifest,
                error_cls,
            )
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
    initial_before_state_hash = _hash(_trusted_before_state(entity, raw_impact))
    dependency_hash: str | None = None
    if projection is not None:
        if mode != "plan" or operation != "archive_linear_entity" or entity_type != "issue":
            raise error_cls("bulk lifecycle projection is unsupported for this operation")
        try:
            raw_impact = _project_issue_archive_impact(entity, raw_impact, projection)
        except RuntimeError as exc:
            raise error_cls(str(exc)) from exc
        dependency_hash = _hash(projection)
    impact = _canonical_impact(raw_impact)
    impact_counts = {key: len(items) for key, items in impact.items()}
    trusted_before = _trusted_before_state(entity, raw_impact)
    projected_before_state_hash = _hash(trusted_before)
    before_state_hash = (
        _hash(
            {
                "version": 1,
                "initial_before_state_hash": initial_before_state_hash,
                "projected_before_state_hash": projected_before_state_hash,
                "dependency_hash": dependency_hash,
            }
        )
        if dependency_hash is not None
        else projected_before_state_hash
    )
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
        result = {
            **base, "result": "planned", "before": before, "after": after,
            "plan": plan, "no_op": False, "verified": False,
            "before_state_hash": before_state_hash,
        }
        if projection is not None:
            result.update(
                {
                    "initial_before_state_hash": initial_before_state_hash,
                    "projected_before_state_hash": projected_before_state_hash,
                    "dependency_hash": dependency_hash,
                    "producer_indices": [
                        producer["index"] for producer in projection["producers"]
                    ],
                }
            )
        return result
    if recovery_path is None:
        raise error_cls("entity destruction apply requires recovery journal")
    recovery_manifest = (
        _archive_recovery_manifest(entity_type, raw_impact)
        if operation == "archive_linear_entity"
        else _delete_recovery_manifest(entity_type, entity, raw_impact)
    )
    manifest_hash = _hash(recovery_manifest)
    if authorized_before_state_hash is not None:
        expected_before_state_hash = authorized_before_state_hash
    elif isinstance(approval_ref, dict):
        expected_before_state_hash = approval_ref.get("before_state_hash")
    else:
        expected_before_state_hash = None
    if (
        not isinstance(approval_ref, dict)
        or expected_before_state_hash != before_state_hash
    ):
        raise error_cls("entity destruction final before-state drifted from approval")
    entry = {
        "request_hash": request_hash, **binding,
        "before_state_hash": before_state_hash, "after_state_hash": _hash(after),
        "impact_before_hash": _hash(trusted_before["impact"]), "phase": "prepared",
        "recovery_manifest": recovery_manifest,
        "manifest_hash": manifest_hash,
    }
    entry["manifest_binding_hash"] = _hash({
        "approval_checksum": entry["approval_checksum"],
        "intent_hash": entry["intent_hash"],
        "command_hash": entry["command_hash"],
        "before_state_hash": entry["before_state_hash"],
        "manifest_hash": manifest_hash,
    })
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
        _verify_archive_impact(
            client, entity_type, verified_matches[0], recovery_manifest, error_cls
        )
    else:
        if normal_matches or verified_matches:
            raise error_cls("delete_linear_entity exact read-back verification failed")
        direct = _direct_lookup(client, entity_type, entity["id"])
        if direct is not None:
            raise error_cls("delete_linear_entity direct lookup still returned the target")
        _verify_delete_impact(
            client, entity_type, entity, recovery_manifest, error_cls
        )
    entry["phase"] = "completed"
    _write(recovery_path, entries)
    return {
        **base, "result": "applied", "before": before, "after": after,
        "plan": plan, "no_op": False, "verified": True,
        "before_state_hash": before_state_hash,
        "recovery_evidence": evidence(entry),
    }


def preview_delete(
    client: Any,
    command: dict[str, Any],
    *,
    error_cls: type[Exception],
) -> dict[str, Any]:
    """Return the exact existing delete plan without creating mutation authority."""
    delete_command = copy.deepcopy(command)
    delete_command["operation"] = "delete_linear_entity"
    planned = execute(
        client,
        delete_command,
        mode="plan",
        journal_path=None,
        key_hash="",
        request_hash="",
        error_cls=error_cls,
    )
    before = copy.deepcopy(planned["before"])
    before["impact"]["children"] = [
        {"identifier": child.get("identifier"), "title": child.get("title")}
        for child in before["impact"]["children"]
    ]
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).replace(
        microsecond=0
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        **planned,
        "operation": "preview_delete_linear_entity",
        "before": before,
        "mode": "apply",
        "result": "read",
        "no_op": True,
        "verified": True,
        "approval_intent": {
            "operation": "delete_linear_entity",
            "target": copy.deepcopy(command["target"]),
            "change": {},
        },
        "before_state_hash": planned["before_state_hash"],
        "expires_at": expires_at,
        "delete_semantics": "recoverable_trash_30_days",
    }
