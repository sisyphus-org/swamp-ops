"""Bounded create-only convergence for one SIS project hierarchy."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

MAX_NAME = 200
MAX_DESCRIPTION = 10_000
MAX_COMMAND_BYTES = 24_576


def _load_validation() -> Any:
    """Load shared bundled policy in package and standalone contexts."""
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
SAFE_STATES = _VALIDATION.SAFE_STATES
CREDENTIAL_SHAPES = _VALIDATION.CREDENTIAL_SHAPES
RESERVED_MARKER = _VALIDATION.RESERVED_MARKER


@dataclass(frozen=True)
class LiveHierarchy:
    team: dict[str, Any]
    project: dict[str, Any] | None
    milestone: dict[str, Any] | None
    issue: dict[str, Any] | None
    state: dict[str, Any] | None


def _fail(error_cls: type[Exception], message: str) -> NoReturn:
    raise error_cls(message)


def _deterministic_uuid4(domain: str, semantic_key: str) -> str:
    """Derive one canonical UUIDv4-shaped entity ID inside the trusted PM lane."""
    raw = bytearray(hashlib.sha256(f"{domain}:{semantic_key}".encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _with_internal_ids(change: dict[str, Any], semantic_key: str) -> dict[str, Any]:
    result = {kind: dict(spec) for kind, spec in change.items()}
    domains = {
        "project": "linear-command:hierarchy:project:v2",
        "milestone": "linear-command:hierarchy:milestone:v2",
        "issue": "linear-command:hierarchy:issue:v2",
    }
    for kind, domain in domains.items():
        result[kind]["id"] = _deterministic_uuid4(domain, semantic_key)
    return result


def _text(
    value: Any,
    path: str,
    *,
    maximum: int,
    required: bool,
    error_cls: type[Exception],
) -> None:
    if not isinstance(value, str) or len(value) > maximum:
        _fail(error_cls, f"{path} must be a string of at most {maximum} characters")
    if required and not value.strip():
        _fail(error_cls, f"{path} must be non-empty")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        _fail(error_cls, f"{path} contains control characters")
    if RESERVED_MARKER in value:
        _fail(error_cls, f"{path} contains the reserved marker")
    if any(pattern.search(value) for pattern in CREDENTIAL_SHAPES):
        _fail(error_cls, f"{path} contains credential-shaped data")


def validate_change(change: Any, error_cls: type[Exception]) -> dict[str, Any]:
    """Validate the exact one-project/one-milestone/one-issue contract."""
    if not isinstance(change, dict) or set(change) != {"project", "milestone", "issue"}:
        _fail(error_cls, "converge_hierarchy change must contain project, milestone, and issue")
    if len(json.dumps(change, ensure_ascii=False).encode("utf-8")) > MAX_COMMAND_BYTES:
        _fail(error_cls, "converge_hierarchy change exceeds the bounded size limit")
    specs = (
        ("project", {"name", "description"}, "name"),
        ("milestone", {"name", "description"}, "name"),
        ("issue", {"title", "description", "state"}, "title"),
    )
    for kind, allowed, name_field in specs:
        spec = change.get(kind)
        if not isinstance(spec, dict) or name_field not in spec:
            _fail(error_cls, f"{kind} must contain {name_field}")
        if not set(spec).issubset(allowed):
            _fail(error_cls, f"{kind} contains unsupported fields")
        _text(
            spec[name_field],
            f"{kind}.{name_field}",
            maximum=MAX_NAME,
            required=True,
            error_cls=error_cls,
        )
        if "description" in spec:
            _text(
                spec["description"],
                f"{kind}.description",
                maximum=MAX_DESCRIPTION,
                required=False,
                error_cls=error_cls,
            )
        if kind == "issue" and "state" in spec and spec["state"] not in SAFE_STATES:
            _fail(error_cls, "issue.state is not in the safe-state allowlist")
    return change


def _bounded_nodes(nodes: Any, label: str, error_cls: type[Exception]) -> list[dict[str, Any]]:
    if not isinstance(nodes, list) or len(nodes) > 100:
        _fail(error_cls, f"{label} exceeds the supported 100-item limit")
    if any(not isinstance(node, dict) for node in nodes):
        _fail(error_cls, f"{label} contains a malformed node")
    return nodes


def _one(items: list[dict[str, Any]], label: str, error_cls: type[Exception]) -> dict[str, Any] | None:
    if len(items) > 1:
        _fail(error_cls, f"ambiguous scoped Linear match for {label}")
    return items[0] if items else None


def _require_id_name(
    nodes: list[dict[str, Any]], name_field: str, label: str, error_cls: type[Exception]
) -> None:
    for node in nodes:
        if not isinstance(node.get("id"), str) or not node["id"].strip():
            _fail(error_cls, f"{label} contains a malformed id")
        if not isinstance(node.get(name_field), str) or not node[name_field].strip():
            _fail(error_cls, f"{label} contains a malformed {name_field}")


def _verify_optional(spec: dict[str, Any], live: dict[str, Any], error_cls: type[Exception], label: str) -> None:
    if "description" in spec and live.get("description") != spec["description"]:
        _fail(error_cls, f"{label} supplied description conflicts with live state")


def preflight(client: Any, change: dict[str, Any], error_cls: type[Exception]) -> LiveHierarchy:
    """Read every currently reachable bounded scope before any write."""
    teams = _bounded_nodes(client.list_teams(), "workspace teams", error_cls)
    _require_id_name(teams, "key", "workspace teams", error_cls)
    sis = _one([item for item in teams if item["key"] == "SIS"], "team SIS", error_cls)
    if sis is None:
        _fail(error_cls, "exact SIS team was not found")

    projects = _bounded_nodes(
        client.list_team_projects(sis["id"]), "SIS team projects", error_cls
    )
    _require_id_name(projects, "name", "SIS team projects", error_cls)
    project_spec = change["project"]
    project = _one(
        [item for item in projects if item["id"] == project_spec["id"]],
        "project id",
        error_cls,
    )
    name_collision = [
        item for item in projects if item["name"] == project_spec["name"] and item["id"] != project_spec["id"]
    ]
    if name_collision:
        _fail(error_cls, "project name already exists with a different deterministic id")
    state = None
    if "state" in change["issue"]:
        states = _bounded_nodes(client.list_states(sis["id"]), "SIS team states", error_cls)
        _require_id_name(states, "name", "SIS team states", error_cls)
        state = _one(
            [item for item in states if item["name"] == change["issue"]["state"]],
            "workflow state",
            error_cls,
        )
        if state is None:
            _fail(error_cls, f"exact workflow state not found: {change['issue']['state']}")
    if project is None:
        return LiveHierarchy(sis, None, None, None, state)
    teams_connection = project.get("teams")
    team_nodes = teams_connection.get("nodes") if isinstance(teams_connection, dict) else None
    if (
        project.get("name") != project_spec["name"]
        or not isinstance(team_nodes, list)
        or {item.get("id") for item in team_nodes if isinstance(item, dict)} != {sis["id"]}
    ):
        _fail(error_cls, "project deterministic id conflicts with live scope or name")
    _verify_optional(project_spec, project, error_cls, "project")

    milestones = _bounded_nodes(
        client.list_project_milestones(project["id"]), "project milestones", error_cls
    )
    _require_id_name(milestones, "name", "project milestones", error_cls)
    milestone_spec = change["milestone"]
    milestone = _one(
        [item for item in milestones if item["id"] == milestone_spec["id"]],
        "milestone id",
        error_cls,
    )
    if any(
        item["name"] == milestone_spec["name"] and item["id"] != milestone_spec["id"]
        for item in milestones
    ):
        _fail(error_cls, "milestone name already exists with a different deterministic id")
    if milestone is not None:
        milestone_project = milestone.get("project")
        if (
            milestone.get("name") != milestone_spec["name"]
            or not isinstance(milestone_project, dict)
            or milestone_project.get("id") != project["id"]
        ):
            _fail(error_cls, "milestone deterministic id conflicts with live scope or name")
        _verify_optional(milestone_spec, milestone, error_cls, "milestone")

    issues = _bounded_nodes(
        client.list_project_issues(project["id"]), "project issues", error_cls
    )
    _require_id_name(issues, "title", "project issues", error_cls)
    issue_spec = change["issue"]
    issue = _one(
        [item for item in issues if item["id"] == issue_spec["id"]], "issue id", error_cls
    )
    if any(
        item["title"] == issue_spec["title"] and item["id"] != issue_spec["id"]
        for item in issues
    ):
        _fail(error_cls, "issue title already exists with a different deterministic id")
    if issue is not None:
        issue_team = issue.get("team")
        issue_project = issue.get("project")
        issue_milestone = issue.get("projectMilestone")
        if (
            milestone is None
            or issue.get("title") != issue_spec["title"]
            or issue.get("parent") is not None
            or not isinstance(issue_team, dict)
            or issue_team.get("id") != sis["id"]
            or issue_team.get("key") != "SIS"
            or not isinstance(issue_project, dict)
            or issue_project.get("id") != project["id"]
            or not isinstance(issue_milestone, dict)
            or issue_milestone.get("id") != milestone["id"]
        ):
            _fail(error_cls, "issue deterministic id conflicts with live hierarchy")
    return LiveHierarchy(sis, project, milestone, issue, state)


def _description_matches(desired: str, live: Any) -> bool:
    """Match exact bytes or Linear's sole bare-URL read-back serialization.

    Mutation payloads remain unchanged; this only recognizes the deterministic
    Markdown representation that Linear returns after storing one pure HTTP(S)
    URL.
    """
    if live == desired:
        return True
    if re.fullmatch(r"https?://[^\s\[\]<>]+", desired) is None:
        return False
    return live == f"[{desired}](<{desired}>)"


def _issue_drift(change: dict[str, Any], live: LiveHierarchy) -> list[str]:
    """Return managed issue fields that differ from the exact desired state."""
    if live.issue is None:
        return []
    spec = change["issue"]
    fields: list[str] = []
    if "description" in spec and not _description_matches(
        spec["description"], live.issue.get("description")
    ):
        fields.append("description")
    if "state" in spec:
        state = live.issue.get("state")
        if not isinstance(state, dict) or state.get("name") != spec["state"]:
            fields.append("state")
    return fields


def build_plan(change: dict[str, Any], live: LiveHierarchy) -> list[dict[str, Any]]:
    def action(kind: str, name_field: str) -> dict[str, Any]:
        spec = change[kind]
        item: dict[str, Any] = {"action": f"create_{kind}", name_field: spec[name_field]}
        if "description" in spec:
            description = spec["description"]
            item["description_sha256"] = hashlib.sha256(description.encode()).hexdigest()
            item["description_length"] = len(description)
        return item

    actions: list[dict[str, Any]] = []
    if live.project is None:
        actions.append(action("project", "name"))
    if live.milestone is None:
        actions.append(action("milestone", "name"))
    if live.issue is None:
        actions.append(action("issue", "title"))
    else:
        fields = _issue_drift(change, live)
        if fields:
            actions.append({"action": "update_issue", "fields": fields})
    return actions


def _optional_live_string(
    live: dict[str, Any], field: str, error_cls: type[Exception]
) -> str | None:
    value = live.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(error_cls, f"live issue {field} is malformed")
    return value


def _snapshot(
    change: dict[str, Any],
    live: LiveHierarchy,
    *,
    desired: bool,
    error_cls: type[Exception],
) -> dict[str, Any]:
    project_spec = change["project"]
    milestone_spec = change["milestone"]
    issue_spec = change["issue"]

    def scalar(spec: dict[str, Any], name_field: str) -> dict[str, Any]:
        item = {"id": spec["id"], name_field: spec[name_field]}
        if "description" in spec:
            item["description"] = spec["description"]
        return item

    project = None
    if desired or live.project is not None:
        project = scalar(project_spec, "name")
    milestone = None
    if desired or live.milestone is not None:
        milestone = scalar(milestone_spec, "name")
        milestone["project_id"] = project_spec["id"]
    issue = None
    if desired or live.issue is not None:
        issue = scalar(issue_spec, "title")
        issue.update(
            {
                "team_key": "SIS",
                "project_id": project_spec["id"],
                "milestone_id": milestone_spec["id"],
                "parent_id": None,
            }
        )
        if "state" in issue_spec:
            issue["state"] = issue_spec["state"]
        if live.issue is not None:
            if not desired and "description" in issue_spec:
                issue["description"] = live.issue.get("description")
            if not desired and "state" in issue_spec:
                live_state = live.issue.get("state")
                issue["state"] = (
                    live_state.get("name") if isinstance(live_state, dict) else None
                )
            for field in ("identifier", "url"):
                value = _optional_live_string(live.issue, field, error_cls)
                if value is not None:
                    issue[field] = value
    return {"project": project, "milestone": milestone, "issue": issue}


def _result(
    command: dict[str, Any],
    change: dict[str, Any],
    mode: str,
    result: str,
    plan: list[dict[str, Any]],
    verified: bool,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    issue = change["issue"]
    target: dict[str, Any] = {
        "type": "project",
        "identifier": command["change"]["project"]["name"],
    }
    return {
        "schema_version": "linear-result.v2",
        "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "operation": command["operation"],
        "mode": mode,
        "target": target,
        "result": result,
        "plan": plan,
        "no_op": result == "no_op",
        "verified": verified,
        "before": before,
        "after": after,
        "hierarchy": {
            "project": change["project"]["name"],
            "milestone": change["milestone"]["name"],
            "issue": issue["title"],
        },
    }


def execute(
    client: Any,
    command: dict[str, Any],
    *,
    mode: str,
    error_cls: type[Exception],
) -> dict[str, Any]:
    """Plan or apply one create-only hierarchy after complete scoped preflight."""
    validated = validate_change(command["change"], error_cls)
    change = _with_internal_ids(validated, command["idempotency_key"])
    live = preflight(client, change, error_cls)
    before = _snapshot(change, live, desired=False, error_cls=error_cls)
    plan = build_plan(change, live)
    if mode == "plan":
        after = _snapshot(change, live, desired=True, error_cls=error_cls)
        return _result(
            command, change, mode, "no_op" if not plan else "planned", plan, not plan, before, after
        )
    if not plan:
        return _result(command, change, mode, "no_op", [], True, before, before)

    project = live.project
    if project is None:
        kwargs = {
            "project_id": change["project"]["id"],
            "team_id": live.team["id"],
            "name": change["project"]["name"],
        }
        if "description" in change["project"]:
            kwargs["description"] = change["project"]["description"]
        client.create_project(**kwargs)
        live = preflight(client, change, error_cls)
        project = live.project
        if project is None:
            _fail(error_cls, "project exact read-back verification failed")

    milestone = live.milestone
    if milestone is None:
        kwargs = {
            "milestone_id": change["milestone"]["id"],
            "project_id": project["id"],
            "name": change["milestone"]["name"],
        }
        if "description" in change["milestone"]:
            kwargs["description"] = change["milestone"]["description"]
        client.create_project_milestone(**kwargs)
        live = preflight(client, change, error_cls)
        milestone = live.milestone
        if milestone is None:
            _fail(error_cls, "milestone exact read-back verification failed")

    if live.issue is not None:
        fields = _issue_drift(change, live)
        if fields:
            kwargs = {}
            if "description" in fields:
                kwargs["description"] = change["issue"]["description"]
            if "state" in fields:
                if live.state is None:
                    _fail(error_cls, "desired issue state disappeared before update")
                kwargs["state_id"] = live.state["id"]
            client.update_project_issue(live.issue["id"], **kwargs)
            live = preflight(client, change, error_cls)

    if live.issue is None:
        kwargs = {
            "issue_id": change["issue"]["id"],
            "team_id": live.team["id"],
            "project_id": project["id"],
            "milestone_id": milestone["id"],
            "title": change["issue"]["title"],
        }
        if live.state is not None:
            kwargs["state_id"] = live.state["id"]
        if "description" in change["issue"]:
            kwargs["description"] = change["issue"]["description"]
        client.create_project_issue(**kwargs)
        issue = _one(
            [
                item
                for item in _bounded_nodes(
                    client.list_project_issues(project["id"]), "project issues", error_cls
                )
                if item.get("id") == change["issue"]["id"]
            ],
            "created issue read-back",
            error_cls,
        )
        if issue is None:
            _fail(error_cls, "issue exact read-back verification failed")

    verified = preflight(client, change, error_cls)
    remaining = build_plan(change, verified)
    if remaining:
        _fail(error_cls, "hierarchy exact read-back verification failed")
    after = _snapshot(change, verified, desired=False, error_cls=error_cls)
    return _result(command, change, mode, "applied", plan, True, before, after)
