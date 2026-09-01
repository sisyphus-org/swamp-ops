"""Bounded standalone issue and top-level issue-tree convergence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

MAX_NAME = 200
MAX_DESCRIPTION = 10_000
MAX_SUB_ISSUES = 10
MAX_COMMAND_BYTES = 65_536
PRIORITIES = {"High": 2, "Medium": 3, "Low": 4}


def _load_bundled(filename: str, name: str) -> Any:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"bundled {filename} module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_VALIDATION = _load_bundled("validation.py", "project_manager_linear_validation")
_COMPARISON = _load_bundled("comparison.py", "project_manager_linear_comparison")
SAFE_STATES = _VALIDATION.SAFE_STATES
CREDENTIAL_SHAPES = _VALIDATION.CREDENTIAL_SHAPES
RESERVED_MARKER = _VALIDATION.RESERVED_MARKER


@dataclass(frozen=True)
class ExactScope:
    team: dict[str, Any]
    project: dict[str, Any]
    milestone: dict[str, Any]
    states: dict[str, dict[str, Any]]
    issues: list[dict[str, Any]]


def _fail(error_cls: type[Exception], message: str) -> NoReturn:
    raise error_cls(message)


def _deterministic_uuid4(domain: str, semantic_key: str) -> str:
    raw = bytearray(hashlib.sha256(f"{domain}:{semantic_key}".encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


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


def _validate_scope_spec(
    spec: Any, path: str, error_cls: type[Exception]
) -> dict[str, Any]:
    if not isinstance(spec, dict) or "name" not in spec or not set(spec).issubset(
        {"name", "description"}
    ):
        _fail(error_cls, f"{path} must contain name and optional description")
    _text(spec["name"], f"{path}.name", maximum=MAX_NAME, required=True, error_cls=error_cls)
    if "description" in spec:
        _text(
            spec["description"],
            f"{path}.description",
            maximum=MAX_DESCRIPTION,
            required=False,
            error_cls=error_cls,
        )
    return spec


def _validate_issue_spec(
    spec: Any, path: str, error_cls: type[Exception]
) -> dict[str, Any]:
    required = {"title", "description", "state", "priority"}
    if not isinstance(spec, dict) or set(spec) != required:
        _fail(error_cls, f"{path} must contain exactly title, description, state, and priority")
    _text(spec["title"], f"{path}.title", maximum=MAX_NAME, required=True, error_cls=error_cls)
    _text(
        spec["description"],
        f"{path}.description",
        maximum=MAX_DESCRIPTION,
        required=False,
        error_cls=error_cls,
    )
    if spec["state"] not in SAFE_STATES:
        _fail(error_cls, f"{path}.state is not in the safe-state allowlist")
    if spec["priority"] not in PRIORITIES:
        _fail(error_cls, f"{path}.priority is not in the bounded allowlist")
    return spec


def validate_change(
    change: Any, operation: str, error_cls: type[Exception]
) -> dict[str, Any]:
    """Validate one exact existing scope plus one issue or bounded issue tree."""
    composite = operation == "converge_issue_tree"
    expected = {
        "project",
        "milestone",
        "issue",
        *(["sub_issues"] if composite else []),
    }
    if not isinstance(change, dict) or set(change) != expected:
        _fail(error_cls, f"{operation} change has invalid fields")
    if len(json.dumps(change, ensure_ascii=False).encode()) > MAX_COMMAND_BYTES:
        _fail(error_cls, f"{operation} change exceeds the bounded size limit")
    _validate_scope_spec(change["project"], "project", error_cls)
    _validate_scope_spec(change["milestone"], "milestone", error_cls)
    _validate_issue_spec(change["issue"], "issue", error_cls)
    if composite:
        children = change["sub_issues"]
        if not isinstance(children, list) or not 1 <= len(children) <= MAX_SUB_ISSUES:
            _fail(error_cls, "sub_issues must contain 1-10 issues")
        titles: set[str] = set()
        for index, child in enumerate(children):
            _validate_issue_spec(child, f"sub_issues[{index}]", error_cls)
            if child["title"] in titles or child["title"] == change["issue"]["title"]:
                _fail(error_cls, "issue tree titles must be unique")
            titles.add(child["title"])
    return change


def _bounded(nodes: Any, label: str, error_cls: type[Exception]) -> list[dict[str, Any]]:
    if not isinstance(nodes, list) or len(nodes) > 100 or any(
        not isinstance(item, dict) for item in nodes
    ):
        _fail(error_cls, f"{label} payload is invalid or exceeds 100 items")
    return nodes


def _one(items: list[dict[str, Any]], label: str, error_cls: type[Exception]) -> dict[str, Any] | None:
    if len(items) > 1:
        _fail(error_cls, f"ambiguous scoped Linear match for {label}")
    return items[0] if items else None


def _compatible_projection(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return true when two partial GraphQL projections agree where both speak."""
    for key in set(left).intersection(right):
        left_value = left[key]
        right_value = right[key]
        if isinstance(left_value, dict) and isinstance(right_value, dict):
            if not _compatible_projection(left_value, right_value):
                return False
        elif left_value != right_value:
            return False
    return True


def _exact_scope(client: Any, change: dict[str, Any], error_cls: type[Exception]) -> ExactScope:
    teams = _bounded(client.list_teams(), "workspace teams", error_cls)
    team = _one([item for item in teams if item.get("key") == "SIS"], "team SIS", error_cls)
    if team is None or not isinstance(team.get("id"), str) or not team["id"].strip():
        _fail(error_cls, "exact SIS team was not found")

    projects = _bounded(client.list_team_projects(team["id"]), "SIS team projects", error_cls)
    project_spec = change["project"]
    project = _one(
        [item for item in projects if item.get("name") == project_spec["name"]],
        "project name",
        error_cls,
    )
    if project is None:
        _fail(error_cls, "exact existing project was not found")
    project_teams = project.get("teams")
    team_nodes = project_teams.get("nodes") if isinstance(project_teams, dict) else None
    if (
        not isinstance(project.get("id"), str)
        or not isinstance(team_nodes, list)
        or {item.get("id") for item in team_nodes if isinstance(item, dict)} != {team["id"]}
    ):
        _fail(error_cls, "project exact-name match conflicts with live scope or name")
    if "description" in project_spec and not _COMPARISON.description_matches(
        project_spec["description"], project.get("description")
    ):
        _fail(error_cls, "project supplied description conflicts with live state")

    milestones = _bounded(
        client.list_project_milestones(project["id"]), "project milestones", error_cls
    )
    milestone_spec = change["milestone"]
    milestone = _one(
        [item for item in milestones if item.get("name") == milestone_spec["name"]],
        "milestone name",
        error_cls,
    )
    if milestone is None:
        _fail(error_cls, "exact existing milestone was not found")
    milestone_project = milestone.get("project")
    if (
        not isinstance(milestone.get("id"), str)
        or not isinstance(milestone_project, dict)
        or milestone_project.get("id") != project["id"]
    ):
        _fail(error_cls, "milestone exact-name match conflicts with live scope or name")
    if "description" in milestone_spec and not _COMPARISON.description_matches(
        milestone_spec["description"], milestone.get("description")
    ):
        _fail(error_cls, "milestone supplied description conflicts with live state")

    state_names = {change["issue"]["state"]}
    state_names.update(item["state"] for item in change.get("sub_issues", []))
    states = _bounded(client.list_states(team["id"]), "SIS team states", error_cls)
    by_name: dict[str, dict[str, Any]] = {}
    for name in state_names:
        state = _one([item for item in states if item.get("name") == name], "workflow state", error_cls)
        if state is None or not isinstance(state.get("id"), str):
            _fail(error_cls, f"exact workflow state not found: {name}")
        by_name[name] = state
    issues = _bounded(client.list_project_issues(project["id"]), "project issues", error_cls)
    title_matches = _bounded(
        client.list_team_issues_by_title(team["id"], change["issue"]["title"]),
        "SIS exact-title issues",
        error_cls,
    )
    issues_by_id: dict[str, dict[str, Any]] = {}
    for item in [*issues, *title_matches]:
        issue_id = item.get("id")
        if not isinstance(issue_id, str) or not issue_id.strip():
            _fail(error_cls, "SIS exact-title issues contain malformed ids")
        previous = issues_by_id.get(issue_id)
        if previous is not None and not _compatible_projection(previous, item):
            _fail(error_cls, "SIS exact-title issue read-back is inconsistent")
        issues_by_id[issue_id] = item
    return ExactScope(team, project, milestone, by_name, list(issues_by_id.values()))


def _select_issue(
    issues: list[dict[str, Any]],
    desired_id: str,
    title: str,
    error_cls: type[Exception],
    *,
    allow_legacy: bool = True,
) -> tuple[dict[str, Any] | None, bool]:
    by_id = _one([item for item in issues if item.get("id") == desired_id], "issue id", error_cls)
    titles = [item for item in issues if item.get("title") == title]
    by_title = _one(titles, "issue title", error_cls)
    if by_id is not None and by_title is not None and by_id is not by_title and by_id != by_title:
        _fail(error_cls, "ambiguous scoped Linear match for issue title")
    if by_id is not None:
        return by_id, False
    return by_title, bool(by_title is not None and allow_legacy)


def _issue_mismatches(
    live: dict[str, Any],
    spec: dict[str, Any],
    *,
    desired_id: str,
    legacy_id: bool,
    scope: ExactScope,
    parent: dict[str, Any] | None,
) -> list[str]:
    fields: list[str] = []
    if live.get("title") != spec["title"] or (not legacy_id and live.get("id") != desired_id):
        fields.append("id/title")
    if not _COMPARISON.description_matches(spec["description"], live.get("description")):
        fields.append("description")
    state = live.get("state")
    if not isinstance(state, dict) or state.get("name") != spec["state"]:
        fields.append("state")
    if live.get("priority") != PRIORITIES[spec["priority"]]:
        fields.append("priority")
    live_parent = live.get("parent")
    if parent is None:
        if live_parent is not None:
            fields.append("parent")
    elif (
        not isinstance(live_parent, dict)
        or live_parent.get("id") != parent.get("id")
        or live_parent.get("identifier") != parent.get("identifier")
    ):
        fields.append("parent")
    if parent is None:
        project = live.get("project")
        if not isinstance(project, dict) or project.get("id") != scope.project["id"]:
            fields.append("project")
        milestone = live.get("projectMilestone")
        if not isinstance(milestone, dict) or milestone.get("id") != scope.milestone["id"]:
            fields.append("milestone")
    team = live.get("team")
    if (
        not isinstance(team, dict)
        or team.get("id") != scope.team["id"]
        or team.get("key") != "SIS"
    ):
        fields.append("team")
    return _COMPARISON.ordered_mismatch_fields(fields)


def _snapshot(live: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    state = live.get("state")
    return {
        "id": live.get("id"),
        "identifier": live.get("identifier"),
        "url": live.get("url"),
        "title": live.get("title"),
        "description": live.get("description"),
        "state": state.get("name") if isinstance(state, dict) else None,
        "priority": spec["priority"],
        "parent_identifier": (
            live.get("parent", {}).get("identifier")
            if isinstance(live.get("parent"), dict)
            else None
        ),
    }


def _require_issue_identity(
    issue: dict[str, Any] | None,
    operation: str,
    error_cls: type[Exception],
) -> None:
    if issue is None:
        return
    if any(
        not isinstance(issue.get(field), str) or not issue[field].strip()
        for field in ("identifier", "url")
    ):
        _fail(error_cls, _COMPARISON.mismatch_message(operation, ["id/title"]))


def _safe_result(
    command: dict[str, Any],
    *,
    mode: str,
    outcome: str,
    plan: list[dict[str, Any]],
    scope: ExactScope,
    issue: dict[str, Any] | None,
    children: list[dict[str, Any]],
    before: dict[str, Any],
) -> dict[str, Any]:
    target = (
        {
            "type": "issue",
            "identifier": issue["identifier"],
            "url": issue["url"],
        }
        if issue is not None
        else {"type": "project", "identifier": scope.project["name"]}
    )
    after = {
        "project": {"id": scope.project["id"], "name": scope.project["name"]},
        "milestone": {
            "id": scope.milestone["id"],
            "name": scope.milestone["name"],
            "project_id": scope.project["id"],
        },
        "issue": _snapshot(issue, command["change"]["issue"]) if issue else None,
    }
    if command["operation"] == "converge_issue_tree":
        by_title = {live.get("title"): live for live in children}
        after["sub_issues"] = [
            _snapshot(by_title[spec["title"]], spec)
            if spec["title"] in by_title
            else None
            for spec in command["change"]["sub_issues"]
        ]
    return {
        "schema_version": "linear-result.v2",
        "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "operation": command["operation"],
        "mode": mode,
        "target": target,
        "result": outcome,
        "before": before,
        "after": after,
        "plan": plan,
        "no_op": outcome == "no_op",
        "verified": mode == "apply" and outcome in {"applied", "no_op"},
    }


def _plan_action(action: str, title: str, fields: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"action": action, "title": title}
    if fields:
        result["fields"] = fields
    return result


def _reconcile(
    client: Any,
    live: dict[str, Any],
    spec: dict[str, Any],
    fields: list[str],
    scope: ExactScope,
    parent: dict[str, Any] | None,
    operation: str,
    error_cls: type[Exception],
) -> None:
    blockers = [field for field in fields if field in {"id/title", "team"}]
    if blockers:
        _fail(error_cls, _COMPARISON.mismatch_message(operation, blockers))
    kwargs: dict[str, Any] = {}
    if "description" in fields:
        kwargs["description"] = spec["description"]
    if "state" in fields:
        kwargs["state_id"] = scope.states[spec["state"]]["id"]
    if "priority" in fields:
        kwargs["priority"] = PRIORITIES[spec["priority"]]
    if "parent" in fields:
        kwargs["parent_id"] = parent["id"] if parent is not None else None
    if "project" in fields:
        kwargs["project_id"] = scope.project["id"]
    if "milestone" in fields:
        kwargs["milestone_id"] = scope.milestone["id"]
    if not kwargs:
        _fail(error_cls, _COMPARISON.mismatch_message(operation, fields))
    client.update_scoped_issue(live["id"], **kwargs)


def execute(
    client: Any,
    command: dict[str, Any],
    *,
    mode: str,
    error_cls: type[Exception],
) -> dict[str, Any]:
    """Plan/apply one standalone issue or bounded parent plus sub-issues."""
    operation = command["operation"]
    change = validate_change(command["change"], operation, error_cls)
    scope = _exact_scope(client, change, error_cls)
    issue_id = _deterministic_uuid4(
        f"linear-command:{operation}:issue:v2", command["idempotency_key"]
    )
    issue, legacy = _select_issue(
        scope.issues, issue_id, change["issue"]["title"], error_cls
    )
    _require_issue_identity(issue, operation, error_cls)
    before = {
        "project": {"id": scope.project["id"], "name": scope.project["name"]},
        "milestone": {"id": scope.milestone["id"], "name": scope.milestone["name"]},
        "issue": _snapshot(issue, change["issue"]) if issue is not None else None,
    }
    plan: list[dict[str, Any]] = []
    if issue is None:
        plan.append(_plan_action("create_standalone_issue", change["issue"]["title"]))
    else:
        fields = _issue_mismatches(
            issue,
            change["issue"],
            desired_id=issue_id,
            legacy_id=legacy,
            scope=scope,
            parent=None,
        )
        if fields:
            plan.append(_plan_action("reconcile_standalone_issue", issue["title"], fields))

    children: list[dict[str, Any]] = []
    child_specs = change.get("sub_issues", [])
    if issue is None:
        plan.extend(
            _plan_action("create_sub_issue", spec["title"])
            for spec in child_specs
        )
    elif child_specs:
        current_children = _bounded(
            client.list_child_issues(issue["identifier"]), "issue children", error_cls
        )
        for index, spec in enumerate(child_specs):
            child_id = _deterministic_uuid4(
                f"linear-command:converge-issue-tree:child:{index}:v2",
                command["idempotency_key"],
            )
            child, child_legacy = _select_issue(
                current_children,
                child_id,
                spec["title"],
                error_cls,
                allow_legacy=False,
            )
            _require_issue_identity(child, operation, error_cls)
            if child is None:
                plan.append(_plan_action("create_sub_issue", spec["title"]))
            else:
                fields = _issue_mismatches(
                    child,
                    spec,
                    desired_id=child_id,
                    legacy_id=child_legacy,
                    scope=scope,
                    parent=issue,
                )
                if fields:
                    plan.append(_plan_action("reconcile_sub_issue", spec["title"], fields))
                children.append(child)

    if mode == "plan":
        return _safe_result(
            command,
            mode=mode,
            outcome="no_op" if not plan else "planned",
            plan=plan,
            scope=scope,
            issue=issue,
            children=children,
            before=before,
        )

    if issue is None:
        client.create_project_issue(
            issue_id=issue_id,
            team_id=scope.team["id"],
            project_id=scope.project["id"],
            milestone_id=scope.milestone["id"],
            title=change["issue"]["title"],
            description=change["issue"]["description"],
            state_id=scope.states[change["issue"]["state"]]["id"],
            priority=PRIORITIES[change["issue"]["priority"]],
        )
    else:
        fields = _issue_mismatches(
            issue,
            change["issue"],
            desired_id=issue_id,
            legacy_id=legacy,
            scope=scope,
            parent=None,
        )
        if fields:
            _reconcile(
                client,
                issue,
                change["issue"],
                fields,
                scope,
                None,
                operation,
                error_cls,
            )

    scope = _exact_scope(client, change, error_cls)
    issue, legacy = _select_issue(
        scope.issues, issue_id, change["issue"]["title"], error_cls
    )
    if issue is None:
        _fail(error_cls, _COMPARISON.mismatch_message(operation, ["id/title"]))
    _require_issue_identity(issue, operation, error_cls)
    fields = _issue_mismatches(
        issue,
        change["issue"],
        desired_id=issue_id,
        legacy_id=legacy,
        scope=scope,
        parent=None,
    )
    if fields:
        _fail(error_cls, _COMPARISON.mismatch_message(operation, fields))

    children = []
    for index, spec in enumerate(child_specs):
        child_id = _deterministic_uuid4(
            f"linear-command:converge-issue-tree:child:{index}:v2",
            command["idempotency_key"],
        )
        current_children = _bounded(
            client.list_child_issues(issue["identifier"]), "issue children", error_cls
        )
        child, child_legacy = _select_issue(
            current_children,
            child_id,
            spec["title"],
            error_cls,
            allow_legacy=False,
        )
        _require_issue_identity(child, operation, error_cls)
        if child is None:
            client.create_issue(
                issue_id=child_id,
                team_id=scope.team["id"],
                state_id=scope.states[spec["state"]]["id"],
                parent_id=issue["id"],
                title=spec["title"],
                description=spec["description"],
                priority=PRIORITIES[spec["priority"]],
            )
        else:
            child_fields = _issue_mismatches(
                child,
                spec,
                desired_id=child_id,
                legacy_id=child_legacy,
                scope=scope,
                parent=issue,
            )
            if child_fields:
                _reconcile(
                    client,
                    child,
                    spec,
                    child_fields,
                    scope,
                    issue,
                    operation,
                    error_cls,
                )
        current_children = _bounded(
            client.list_child_issues(issue["identifier"]), "issue children", error_cls
        )
        child, child_legacy = _select_issue(
            current_children,
            child_id,
            spec["title"],
            error_cls,
            allow_legacy=False,
        )
        _require_issue_identity(child, operation, error_cls)
        if child is None:
            _fail(error_cls, _COMPARISON.mismatch_message(operation, ["id/title"]))
        child_fields = _issue_mismatches(
            child,
            spec,
            desired_id=child_id,
            legacy_id=child_legacy,
            scope=scope,
            parent=issue,
        )
        if child_fields:
            _fail(error_cls, _COMPARISON.mismatch_message(operation, child_fields))
        children.append(child)

    return _safe_result(
        command,
        mode=mode,
        outcome="no_op" if not plan else "applied",
        plan=plan,
        scope=scope,
        issue=issue,
        children=children,
        before=before,
    )
