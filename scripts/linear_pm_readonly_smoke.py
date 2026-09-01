#!/usr/bin/env python3
"""Live read-only smoke for PM-owned Linear workspace reads."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.project_manager_linear import lane as bundled_lane  # noqa: E402


ENTITY_TYPES = ("issues", "projects", "milestones", "initiatives")
ISSUE_IDENTIFIER = bundled_lane.ISSUE_IDENTIFIER


def build_command(
    *,
    operation: str,
    entity_types: list[str],
    include_archived: bool,
    query: str | None,
) -> dict[str, Any]:
    if operation not in {"search_linear", "inventory_linear"}:
        raise ValueError("operation must be search_linear or inventory_linear")
    if (
        not isinstance(entity_types, list)
        or not entity_types
        or any(not isinstance(item, str) for item in entity_types)
        or len(set(entity_types)) != len(entity_types)
        or any(item not in ENTITY_TYPES for item in entity_types)
    ):
        raise ValueError("entity_types must be an explicit non-empty core subset")
    ordered = [item for item in ENTITY_TYPES if item in entity_types]
    if not isinstance(include_archived, bool):
        raise ValueError("include_archived must be boolean")
    change: dict[str, Any] = {
        "entity_types": ordered,
        "include_archived": include_archived,
    }
    if operation == "search_linear":
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search_linear requires an exact non-empty query")
        change = {"query": query, **change}
    elif query is not None:
        raise ValueError("inventory_linear does not accept query")
    semantic = {
        "operation": operation,
        "target": {"type": "workspace", "identifier": "current"},
        "change": change,
        "policy": {"mode": "standard"},
    }
    digest = hashlib.sha256(
        json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "schema_version": "linear-command.v2",
        "command_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "idempotency_key": f"linear:v2:{digest[:32]}",
        "source_profile": "project-manager",
        **semantic,
    }


def run_smoke(
    *,
    operation: str,
    entity_types: list[str],
    include_archived: bool,
    query: str | None,
    environ: Mapping[str, str] = os.environ,
    lane: Any = bundled_lane,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    """Execute one live PM-owned read with no mutation plan or journal path."""
    if environ.get("HERMES_PROFILE") != "project-manager":
        raise RuntimeError("live Linear read smoke requires project-manager profile")
    token = str(environ.get("LINEAR_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("project-manager LINEAR_TOKEN is missing")
    command = build_command(
        operation=operation,
        entity_types=entity_types,
        include_archived=include_archived,
        query=query,
    )
    factory = client_factory or lane.LinearClient
    client = factory(token)
    result = lane.execute_command(
        client,
        command,
        mode="apply",
        journal_path=None,
    )
    after = result.get("after") if isinstance(result, dict) else None
    counts = after.get("counts") if isinstance(after, dict) else None
    if (
        result.get("schema_version") != "linear-result.v2"
        or result.get("operation") != operation
        or result.get("result") != "read"
        or result.get("verified") is not True
        or not isinstance(counts, dict)
        or set(counts) != set(command["change"]["entity_types"])
    ):
        raise RuntimeError("PM read smoke did not return a verified bounded result")
    return {
        "result": "pass",
        "readOnly": True,
        "operation": operation,
        "entityTypes": list(command["change"]["entity_types"]),
        "includeArchived": include_archived,
        "counts": dict(counts),
        "verified": True,
    }


DESTRUCTION_SCHEMA_QUERY = """
query LaneDestructionSchema {
  __type(name: "Mutation") { fields(includeDeprecated: true) { name isDeprecated deprecationReason } }
}
"""
DESTRUCTION_MUTATIONS = {
    ("archive_linear_entity", "issue"): "issueArchive",
    ("archive_linear_entity", "project"): "projectArchive",
    ("archive_linear_entity", "initiative"): "initiativeArchive",
    ("delete_linear_entity", "issue"): "issueDelete",
    ("delete_linear_entity", "project"): "projectDelete",
    ("delete_linear_entity", "milestone"): "projectMilestoneDelete",
    ("delete_linear_entity", "initiative"): "initiativeDelete",
}


def run_destruction_preflight_smoke(
    *,
    operation: str,
    entity_type: str,
    selector: dict[str, str],
    environ: Mapping[str, str] = os.environ,
    lane: Any = bundled_lane,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    """Read current mutation schema and exact impact without applying a mutation."""
    if environ.get("HERMES_PROFILE") != "project-manager":
        raise RuntimeError("live Linear read smoke requires project-manager profile")
    token = str(environ.get("LINEAR_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("project-manager LINEAR_TOKEN is missing")
    mutation = DESTRUCTION_MUTATIONS.get((operation, entity_type))
    if mutation is None:
        raise ValueError("archive/delete combination is outside the safe matrix")
    client = (client_factory or lane.LinearClient)(token)
    schema = client.execute(DESTRUCTION_SCHEMA_QUERY).get("__type")
    fields = schema.get("fields") if isinstance(schema, dict) else None
    names = {
        item.get("name")
        for item in fields
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    } if isinstance(fields, list) else set()
    required_mutations = set(DESTRUCTION_MUTATIONS.values())
    missing = sorted(required_mutations - names)
    if missing:
        raise RuntimeError("required fixed Linear mutations are unavailable: " + ", ".join(missing))
    target = {"type": entity_type, "selector": dict(selector)}
    destruction = lane._load_entity_destruction()
    destruction.validate_target(target, operation, lane.ContractError)
    inventory = destruction._inventory(client, entity_type)
    matches = [
        node
        for node in inventory
        if destruction._matches(node, entity_type, selector)
        and node.get("archivedAt") is None
    ]
    if len(matches) != 1:
        raise RuntimeError("destruction preflight target was missing or ambiguous")
    impact = destruction._impact(client, entity_type, matches[0])
    if not isinstance(impact, dict) or any(not isinstance(items, list) for items in impact.values()):
        raise RuntimeError("destruction preflight did not return an exact impact inventory")
    before = {
        "entity": destruction._scrub(matches[0]),
        "archived": False,
        "impact": destruction._scrub(impact),
    }
    digest = hashlib.sha256(
        json.dumps(before, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "result": "pass",
        "readOnly": True,
        "operation": operation,
        "entityType": entity_type,
        "selector": dict(selector),
        "mutation": mutation,
        "schemaMutations": sorted(required_mutations),
        "impactCounts": {key: len(items) for key, items in impact.items()},
        "beforeStateSha256": digest,
        "verified": True,
    }


def run_relation_inventory_smoke(
    *,
    identifier: str,
    environ: Mapping[str, str] = os.environ,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    """Read and hash one exact issue relation inventory without exposing raw IDs."""
    if environ.get("HERMES_PROFILE") != "project-manager":
        raise RuntimeError("live Linear read smoke requires project-manager profile")
    token = str(environ.get("LINEAR_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("project-manager LINEAR_TOKEN is missing")
    if not isinstance(identifier, str) or ISSUE_IDENTIFIER.fullmatch(identifier) is None:
        raise ValueError("identifier must be an exact SIS-N issue")
    factory = client_factory or bundled_lane.LinearClient
    client = factory(token)
    issue = client.get_issue(identifier)
    team = issue.get("team") if isinstance(issue, dict) else None
    if (
        not isinstance(issue, dict)
        or issue.get("identifier") != identifier
        or not isinstance(issue.get("id"), str)
        or not issue["id"]
        or not isinstance(team, dict)
        or team.get("key") != "SIS"
        or not isinstance(team.get("id"), str)
        or not team["id"]
    ):
        raise RuntimeError("exact relation inventory target is not a SIS issue")
    inventory = client.list_issue_relations(identifier)
    if not isinstance(inventory, list):
        raise RuntimeError("relation inventory payload is invalid")
    normalized = []
    for relation in inventory:
        source = relation.get("issue") if isinstance(relation, dict) else None
        destination = relation.get("relatedIssue") if isinstance(relation, dict) else None
        if (
            not isinstance(relation, dict)
            or set(relation) != {"id", "type", "issue", "relatedIssue"}
            or not isinstance(relation.get("id"), str)
            or not relation["id"]
            or relation.get("type") not in {"blocks", "related"}
            or not isinstance(source, dict)
            or set(source) != {"id", "identifier"}
            or not isinstance(destination, dict)
            or set(destination) != {"id", "identifier"}
            or any(
                not isinstance(endpoint.get("id"), str)
                or not endpoint["id"]
                or not isinstance(endpoint.get("identifier"), str)
                or ISSUE_IDENTIFIER.fullmatch(endpoint["identifier"]) is None
                for endpoint in (source, destination)
            )
        ):
            raise RuntimeError("relation inventory payload is invalid")
        normalized.append(relation)
    normalized.sort(key=lambda item: item["id"])
    digest = hashlib.sha256(
        json.dumps(
            {"identifier": identifier, "inventory": normalized},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "result": "pass",
        "readOnly": True,
        "operation": "inventory_issue_relations",
        "identifier": identifier,
        "relationCount": len(normalized),
        "inventorySha256": digest,
        "verified": True,
    }


def run_team_state_inventory_smoke(
    *,
    environ: Mapping[str, str] = os.environ,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    """Inventory the exact SIS team workflow schema without exposing internal IDs."""
    if environ.get("HERMES_PROFILE") != "project-manager":
        raise RuntimeError("live Linear read smoke requires project-manager profile")
    token = str(environ.get("LINEAR_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("project-manager LINEAR_TOKEN is missing")
    factory = client_factory or bundled_lane.LinearClient
    client = factory(token)
    teams = client.list_teams()
    matches = [
        team
        for team in teams
        if isinstance(team, dict) and team.get("key") == "SIS"
    ]
    if len(matches) != 1:
        raise RuntimeError("exact SIS team was not found or was ambiguous")
    team = matches[0]
    if not isinstance(team.get("id"), str) or not team["id"]:
        raise RuntimeError("exact SIS team payload is invalid")
    raw_states = client.list_states(team["id"])
    if not isinstance(raw_states, list) or len(raw_states) > 100:
        raise RuntimeError("SIS team state inventory payload is invalid")
    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for state in raw_states:
        if (
            not isinstance(state, dict)
            or set(state) != {"id", "name", "type"}
            or not isinstance(state.get("id"), str)
            or not state["id"]
            or state["id"] in seen_ids
            or not isinstance(state.get("name"), str)
            or not state["name"]
            or not isinstance(state.get("type"), str)
            or not state["type"]
        ):
            raise RuntimeError("SIS team state inventory payload is invalid")
        seen_ids.add(state["id"])
        normalized.append(
            {"id": state["id"], "name": state["name"], "type": state["type"]}
        )
    normalized.sort(key=lambda item: (item["name"], item["type"], item["id"]))
    digest = hashlib.sha256(
        json.dumps(
            {"team": "SIS", "states": normalized},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "result": "pass",
        "readOnly": True,
        "operation": "inventory_team_states",
        "team": "SIS",
        "stateCount": len(normalized),
        "states": [
            {"name": item["name"], "type": item["type"]} for item in normalized
        ],
        "inventorySha256": digest,
        "verified": True,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Required live-network acknowledgement")
    parser.add_argument(
        "--operation",
        required=True,
        choices=(
            "search_linear",
            "inventory_linear",
            "inventory_issue_relations",
            "inventory_team_states",
            "inventory_entity_destruction",
        ),
    )
    parser.add_argument(
        "--entity-type",
        action="append",
        dest="entity_types",
        required=False,
        choices=ENTITY_TYPES,
    )
    parser.add_argument("--query")
    parser.add_argument("--identifier")
    parser.add_argument(
        "--destructive-operation",
        choices=("archive_linear_entity", "delete_linear_entity"),
    )
    parser.add_argument(
        "--entity-kind", choices=("issue", "project", "milestone", "initiative")
    )
    parser.add_argument("--name")
    parser.add_argument("--project")
    archive = parser.add_mutually_exclusive_group(required=False)
    archive.add_argument("--include-archived", action="store_true")
    archive.add_argument("--exclude-archived", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required")
    if args.operation == "inventory_entity_destruction":
        if (
            args.destructive_operation is None
            or args.entity_kind is None
            or args.entity_types
            or args.query is not None
            or args.include_archived
            or args.exclude_archived
        ):
            parser.error("destruction inventory requires exact operation/entity selector only")
        if args.entity_kind == "issue":
            valid = args.identifier is not None and args.name is None and args.project is None
        elif args.entity_kind in {"project", "initiative"}:
            valid = args.identifier is None and args.name is not None and args.project is None
        else:
            valid = args.identifier is None and args.name is not None and args.project is not None
        if not valid:
            parser.error("destruction inventory selector does not match entity kind")
        return args
    if args.operation == "inventory_issue_relations":
        if (
            args.identifier is None
            or args.entity_types
            or args.query is not None
            or args.include_archived
            or args.exclude_archived
        ):
            parser.error("inventory_issue_relations requires only --identifier")
        return args
    if args.operation == "inventory_team_states":
        if (
            args.identifier is not None
            or args.entity_types
            or args.query is not None
            or args.include_archived
            or args.exclude_archived
        ):
            parser.error("inventory_team_states accepts no additional selectors")
        return args
    if not args.entity_types or args.identifier is not None:
        parser.error("workspace reads require --entity-type and forbid --identifier")
    if not (args.include_archived or args.exclude_archived):
        parser.error("workspace reads require an explicit archive mode")
    if (args.operation == "search_linear") != (args.query is not None):
        parser.error("search_linear requires --query; inventory_linear forbids it")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.operation == "inventory_entity_destruction":
            selector = (
                {"identifier": args.identifier}
                if args.entity_kind == "issue"
                else (
                    {"project": args.project, "name": args.name}
                    if args.entity_kind == "milestone"
                    else {"name": args.name}
                )
            )
            result = run_destruction_preflight_smoke(
                operation=args.destructive_operation,
                entity_type=args.entity_kind,
                selector=selector,
            )
        elif args.operation == "inventory_issue_relations":
            result = run_relation_inventory_smoke(identifier=args.identifier)
        elif args.operation == "inventory_team_states":
            result = run_team_state_inventory_smoke()
        else:
            result = run_smoke(
                operation=args.operation,
                entity_types=args.entity_types,
                include_archived=args.include_archived,
                query=args.query,
            )
    except (RuntimeError, ValueError, bundled_lane.ContractError) as exc:
        print(json.dumps({"result": "fail", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
