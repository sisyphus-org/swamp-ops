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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Required live-network acknowledgement")
    parser.add_argument(
        "--operation",
        required=True,
        choices=("search_linear", "inventory_linear"),
    )
    parser.add_argument(
        "--entity-type",
        action="append",
        dest="entity_types",
        required=True,
        choices=ENTITY_TYPES,
    )
    parser.add_argument("--query")
    archive = parser.add_mutually_exclusive_group(required=True)
    archive.add_argument("--include-archived", action="store_true")
    archive.add_argument("--exclude-archived", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required")
    if (args.operation == "search_linear") != (args.query is not None):
        parser.error("search_linear requires --query; inventory_linear forbids it")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
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
