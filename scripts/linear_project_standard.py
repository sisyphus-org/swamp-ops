#!/usr/bin/env python3
"""Reconcile a Linear project to the standard hierarchy.

Contract: Project -> Milestones -> Issues -> Sub-issues.
Milestones are assigned to top-level issues. Sub-issues inherit the grouping
through their parent and intentionally receive no direct milestone assignment.

Plan mode performs live reads only. Apply mode creates missing objects and never
updates, moves, archives, or deletes existing Linear objects. Ambiguous matches
fail closed rather than producing duplicates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_URL = "https://api.linear.app/graphql"
MAX_ITEMS = 100


class ContractError(RuntimeError):
    """A manifest or live Linear state violates the reconciliation contract."""


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{path} must be a non-empty string")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ContractError(f"{path} contains control characters")
    return value.strip()


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"manifest does not exist: {path}")
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text())
    elif path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on runtime
            raise ContractError("PyYAML is required for YAML manifests") from exc
        raw = yaml.safe_load(path.read_text())
    else:
        raise ContractError("manifest must use .json, .yaml, or .yml")
    if not isinstance(raw, dict):
        raise ContractError("manifest root must be an object")
    validate_manifest(raw)
    return raw


def validate_manifest(raw: dict[str, Any]) -> None:
    allowed_root = {"schemaVersion", "team", "project", "milestones"}
    unknown = set(raw) - allowed_root
    if unknown:
        raise ContractError(f"unknown manifest fields: {sorted(unknown)}")
    if raw.get("schemaVersion") != 1:
        raise ContractError("schemaVersion must equal 1")
    require_string(raw.get("team"), "team")
    project = raw.get("project")
    if not isinstance(project, dict):
        raise ContractError("project must be an object")
    if set(project) - {"name", "description"}:
        raise ContractError("project supports only name and description")
    require_string(project.get("name"), "project.name")
    if "description" in project:
        require_string(project["description"], "project.description")
    milestones = raw.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        raise ContractError("milestones must be a non-empty array")
    seen_milestones: set[str] = set()
    seen_titles: set[str] = set()
    for mi, milestone in enumerate(milestones):
        if not isinstance(milestone, dict):
            raise ContractError(f"milestones[{mi}] must be an object")
        if set(milestone) - {"name", "description", "issues"}:
            raise ContractError(f"milestones[{mi}] contains unsupported fields")
        milestone_name = require_string(milestone.get("name"), f"milestones[{mi}].name")
        if "description" in milestone:
            require_string(milestone["description"], f"milestones[{mi}].description")
        if milestone_name in seen_milestones:
            raise ContractError(f"duplicate milestone name: {milestone_name}")
        seen_milestones.add(milestone_name)
        issues = milestone.get("issues")
        if not isinstance(issues, list) or not issues:
            raise ContractError(f"milestones[{mi}].issues must be a non-empty array")
        seen_issues: set[str] = set()
        for ii, issue in enumerate(issues):
            validate_issue(issue, f"milestones[{mi}].issues[{ii}]", allow_children=True)
            title = issue["title"].strip()
            if title in seen_issues:
                raise ContractError(f"duplicate issue title in {milestone_name}: {title}")
            if title in seen_titles:
                raise ContractError(f"duplicate title across manifest hierarchy: {title}")
            seen_issues.add(title)
            seen_titles.add(title)
            for child in issue.get("subIssues", []):
                child_title = child["title"].strip()
                if child_title in seen_titles:
                    raise ContractError(
                        f"duplicate title across manifest hierarchy: {child_title}"
                    )
                seen_titles.add(child_title)


def validate_issue(raw: Any, path: str, *, allow_children: bool) -> None:
    if not isinstance(raw, dict):
        raise ContractError(f"{path} must be an object")
    allowed = {"title", "description", "state"}
    if allow_children:
        allowed.add("subIssues")
    if set(raw) - allowed:
        raise ContractError(f"{path} contains unsupported fields")
    require_string(raw.get("title"), f"{path}.title")
    if "description" in raw:
        require_string(raw["description"], f"{path}.description")
    if "state" in raw:
        require_string(raw["state"], f"{path}.state")
    if allow_children:
        children = raw.get("subIssues", [])
        if not isinstance(children, list):
            raise ContractError(f"{path}.subIssues must be an array")
        seen: set[str] = set()
        for index, child in enumerate(children):
            validate_issue(child, f"{path}.subIssues[{index}]", allow_children=False)
            title = child["title"].strip()
            if title in seen:
                raise ContractError(f"duplicate sub-issue title under {raw['title']}: {title}")
            seen.add(title)


class LinearClient:
    def __init__(self, token: str, endpoint: str = API_URL) -> None:
        token = token.strip()
        if not token:
            raise ContractError("LINEAR_TOKEN is empty")
        self.endpoint = endpoint
        self.authorization = token

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": self.authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            raise ContractError(f"Linear API HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ContractError(f"Linear API request failed: {exc}") from exc
        if payload.get("errors"):
            messages = [item.get("message", "unknown GraphQL error") for item in payload["errors"]]
            raise ContractError("Linear GraphQL error: " + "; ".join(messages))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ContractError("Linear API response did not contain data")
        return data


TEAMS_QUERY = """
query StandardTeams {
  teams(first: 100) {
    nodes { id name key }
    pageInfo { hasNextPage }
  }
}
"""

TEAM_STATES_QUERY = """
query TeamStates($teamId: String!) {
  team(id: $teamId) {
    states(first: 100) {
      nodes { id name type }
      pageInfo { hasNextPage }
    }
  }
}
"""

PROJECTS_QUERY = """
query StandardProjects {
  projects(first: 100, includeArchived: false) {
    nodes {
      id
      name
      teams { nodes { id } }
    }
    pageInfo { hasNextPage }
  }
}
"""

MILESTONES_QUERY = """
query ProjectMilestones($projectId: String!) {
  project(id: $projectId) {
    projectMilestones(first: 100) {
      nodes { id name }
      pageInfo { hasNextPage }
    }
  }
}
"""

ISSUES_QUERY = """
query ProjectIssues($projectId: ID!) {
  issues(first: 100, filter: { project: { id: { eq: $projectId } } }) {
    nodes {
      id
      identifier
      title
      parent { id }
      projectMilestone { id name }
    }
    pageInfo { hasNextPage }
  }
}
"""

PROJECT_CREATE = """
mutation CreateProject($input: ProjectCreateInput!) {
  projectCreate(input: $input) {
    success
    project { id name }
  }
}
"""

MILESTONE_CREATE = """
mutation CreateMilestone($input: ProjectMilestoneCreateInput!) {
  projectMilestoneCreate(input: $input) {
    success
    projectMilestone { id name }
  }
}
"""

ISSUE_CREATE = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier title }
  }
}
"""


class LiveContext:
    def __init__(
        self,
        *,
        team: dict[str, Any],
        project: dict[str, Any] | None,
        milestones: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> None:
        self.team = team
        self.project = project
        self.milestones = milestones
        self.issues = issues


def unique(items: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    if len(items) > 1:
        raise ContractError(f"ambiguous live Linear match for {label}")
    return items[0] if items else None


def fetch_context(client: LinearClient, manifest: dict[str, Any]) -> LiveContext:
    teams = client.execute(TEAMS_QUERY)["teams"]
    projects = client.execute(PROJECTS_QUERY)["projects"]
    if teams["pageInfo"]["hasNextPage"] or projects["pageInfo"]["hasNextPage"]:
        raise ContractError(f"workspace exceeds the supported {MAX_ITEMS}-item discovery limit")
    team_ref = manifest["team"].casefold()
    team = unique(
        [
            item
            for item in teams["nodes"]
            if item["name"].casefold() == team_ref or item["key"].casefold() == team_ref
        ],
        f"team {manifest['team']}",
    )
    if team is None:
        raise ContractError(f"Linear team not found: {manifest['team']}")
    state_data = client.execute(TEAM_STATES_QUERY, {"teamId": team["id"]})["team"][
        "states"
    ]
    if state_data["pageInfo"]["hasNextPage"]:
        raise ContractError(f"team exceeds the supported {MAX_ITEMS}-state discovery limit")
    team = {**team, "states": {"nodes": state_data["nodes"]}}
    project_name = manifest["project"]["name"]
    project = unique(
        [
            item
            for item in projects["nodes"]
            if item["name"] == project_name
            and team["id"] in {candidate["id"] for candidate in item["teams"]["nodes"]}
        ],
        f"project {project_name} in team {team['name']}",
    )
    if project is None:
        return LiveContext(team=team, project=None, milestones=[], issues=[])
    milestone_data = client.execute(MILESTONES_QUERY, {"projectId": project["id"]})[
        "project"
    ]["projectMilestones"]
    if milestone_data["pageInfo"]["hasNextPage"]:
        raise ContractError(f"project exceeds the supported {MAX_ITEMS}-milestone discovery limit")
    issue_data = client.execute(ISSUES_QUERY, {"projectId": project["id"]})["issues"]
    if issue_data["pageInfo"]["hasNextPage"]:
        raise ContractError(f"project exceeds the supported {MAX_ITEMS}-issue reconciliation limit")
    return LiveContext(
        team=team,
        project=project,
        milestones=milestone_data["nodes"],
        issues=issue_data["nodes"],
    )


def resolve_state(team: dict[str, Any], requested: str | None) -> str:
    name = requested or "Todo"
    matches = [item for item in team["states"]["nodes"] if item["name"] == name]
    state = unique(matches, f"state {name}")
    if state is None:
        raise ContractError(f"workflow state not found in {team['name']}: {name}")
    return state["id"]


def validate_live_references(manifest: dict[str, Any], live: LiveContext) -> None:
    """Resolve every external reference before apply can perform its first write."""
    for milestone in manifest["milestones"]:
        for issue in milestone["issues"]:
            resolve_state(live.team, issue.get("state"))
            for child in issue.get("subIssues", []):
                resolve_state(live.team, child.get("state"))


def build_plan(manifest: dict[str, Any], live: LiveContext) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if live.project is None:
        actions.append({"action": "create_project", "name": manifest["project"]["name"]})
        for milestone in manifest["milestones"]:
            actions.append({"action": "create_milestone", "name": milestone["name"]})
            for issue in milestone["issues"]:
                actions.append({"action": "create_issue", "milestone": milestone["name"], "title": issue["title"]})
                for child in issue.get("subIssues", []):
                    actions.append({"action": "create_sub_issue", "parent": issue["title"], "title": child["title"]})
        return actions

    milestone_by_name = {item["name"]: item for item in live.milestones}
    if len(milestone_by_name) != len(live.milestones):
        raise ContractError("project contains duplicate milestone names")
    for milestone in manifest["milestones"]:
        live_milestone = milestone_by_name.get(milestone["name"])
        if live_milestone is None:
            actions.append({"action": "create_milestone", "name": milestone["name"]})
            milestone_id = None
        else:
            milestone_id = live_milestone["id"]
        for issue in milestone["issues"]:
            same_title = [item for item in live.issues if item["title"] == issue["title"]]
            if len(same_title) > 1:
                raise ContractError(
                    f"duplicate live issue title in project: {issue['title']}"
                )
            correct = [
                item
                for item in same_title
                if item["parent"] is None
                and milestone_id is not None
                and item["projectMilestone"] is not None
                and item["projectMilestone"]["id"] == milestone_id
            ]
            parent = unique(correct, f"top-level issue {issue['title']}")
            if parent is None and same_title:
                raise ContractError(
                    f"issue title already exists in a different hierarchy: {issue['title']}"
                )
            if parent is None:
                for child in issue.get("subIssues", []):
                    collisions = [
                        item for item in live.issues if item["title"] == child["title"]
                    ]
                    if collisions:
                        raise ContractError(
                            f"sub-issue title already exists in a different hierarchy: {child['title']}"
                        )
                actions.append({"action": "create_issue", "milestone": milestone["name"], "title": issue["title"]})
                for child in issue.get("subIssues", []):
                    actions.append({"action": "create_sub_issue", "parent": issue["title"], "title": child["title"]})
                continue
            for child in issue.get("subIssues", []):
                same_child_title = [
                    item for item in live.issues if item["title"] == child["title"]
                ]
                if len(same_child_title) > 1:
                    raise ContractError(
                        f"duplicate live issue title in project: {child['title']}"
                    )
                child_matches = [
                    item
                    for item in same_child_title
                    if item["parent"] is not None
                    and item["parent"]["id"] == parent["id"]
                    and item["projectMilestone"] is None
                ]
                found = unique(child_matches, f"sub-issue {child['title']} under {issue['title']}")
                if found is None:
                    collisions = [item for item in live.issues if item["title"] == child["title"]]
                    if collisions:
                        raise ContractError(
                            f"sub-issue title already exists in a different hierarchy: {child['title']}"
                        )
                    actions.append({"action": "create_sub_issue", "parent": issue["title"], "title": child["title"]})
    return actions


def create_project(client: LinearClient, manifest: dict[str, Any], team_id: str) -> dict[str, Any]:
    project_input: dict[str, Any] = {
        "name": manifest["project"]["name"],
        "teamIds": [team_id],
    }
    if manifest["project"].get("description"):
        project_input["description"] = manifest["project"]["description"]
    payload = client.execute(PROJECT_CREATE, {"input": project_input})["projectCreate"]
    if not payload["success"] or payload["project"] is None:
        raise ContractError("Linear did not create the project")
    return payload["project"]


def create_milestone(client: LinearClient, project_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    milestone_input: dict[str, Any] = {"name": spec["name"], "projectId": project_id}
    if spec.get("description"):
        milestone_input["description"] = spec["description"]
    payload = client.execute(MILESTONE_CREATE, {"input": milestone_input})["projectMilestoneCreate"]
    if not payload["success"] or payload["projectMilestone"] is None:
        raise ContractError(f"Linear did not create milestone {spec['name']}")
    return payload["projectMilestone"]


def create_issue(
    client: LinearClient,
    *,
    team: dict[str, Any],
    project_id: str,
    milestone_id: str,
    spec: dict[str, Any],
    parent_id: str | None = None,
) -> dict[str, Any]:
    issue_input: dict[str, Any] = {
        "title": spec["title"],
        "teamId": team["id"],
        "projectId": project_id,
        "stateId": resolve_state(team, spec.get("state")),
    }
    if parent_id is None:
        issue_input["projectMilestoneId"] = milestone_id
    if spec.get("description"):
        issue_input["description"] = spec["description"]
    if parent_id is not None:
        issue_input["parentId"] = parent_id
    payload = client.execute(ISSUE_CREATE, {"input": issue_input})["issueCreate"]
    if not payload["success"] or payload["issue"] is None:
        raise ContractError(f"Linear did not create issue {spec['title']}")
    return payload["issue"]


def apply_manifest(client: LinearClient, manifest: dict[str, Any], live: LiveContext) -> list[dict[str, str]]:
    applied: list[dict[str, str]] = []
    declared_titles = {
        spec["title"]
        for milestone in manifest["milestones"]
        for issue in milestone["issues"]
        for spec in [issue, *issue.get("subIssues", [])]
    }
    for title in declared_titles:
        if sum(1 for item in live.issues if item["title"] == title) > 1:
            raise ContractError(f"duplicate live issue title in project: {title}")
    project = live.project
    if project is None:
        project = create_project(client, manifest, live.team["id"])
        applied.append({"action": "created_project", "name": project["name"], "id": project["id"]})
    milestone_by_name = {item["name"]: item for item in live.milestones}
    parent_by_scope: dict[tuple[str, str], dict[str, Any]] = {}
    for item in live.issues:
        if item["parent"] is not None or item["projectMilestone"] is None:
            continue
        key = (item["projectMilestone"]["id"], item["title"])
        if key in parent_by_scope:
            raise ContractError(
                f"ambiguous top-level issue in milestone {item['projectMilestone']['name']}: {item['title']}"
            )
        parent_by_scope[key] = item
    for milestone_spec in manifest["milestones"]:
        milestone = milestone_by_name.get(milestone_spec["name"])
        if milestone is None:
            milestone = create_milestone(client, project["id"], milestone_spec)
            milestone_by_name[milestone["name"]] = milestone
            applied.append({"action": "created_milestone", "name": milestone["name"], "id": milestone["id"]})
        for issue_spec in milestone_spec["issues"]:
            parent_key = (milestone["id"], issue_spec["title"])
            parent = parent_by_scope.get(parent_key)
            if parent is None:
                parent = create_issue(
                    client,
                    team=live.team,
                    project_id=project["id"],
                    milestone_id=milestone["id"],
                    spec=issue_spec,
                )
                parent_by_scope[parent_key] = parent
                applied.append({"action": "created_issue", "title": parent["title"], "id": parent["identifier"]})
            for child_spec in issue_spec.get("subIssues", []):
                existing_child = next(
                    (
                        item
                        for item in live.issues
                        if item["title"] == child_spec["title"]
                        and item["parent"] is not None
                        and item["parent"]["id"] == parent["id"]
                        and item["projectMilestone"] is None
                    ),
                    None,
                )
                if existing_child is not None:
                    continue
                collisions = [
                    item for item in live.issues if item["title"] == child_spec["title"]
                ]
                if collisions:
                    raise ContractError(
                        f"sub-issue title already exists in a different hierarchy: {child_spec['title']}"
                    )
                child = create_issue(
                    client,
                    team=live.team,
                    project_id=project["id"],
                    milestone_id=milestone["id"],
                    spec=child_spec,
                    parent_id=parent["id"],
                )
                applied.append({"action": "created_sub_issue", "title": child["title"], "id": child["identifier"], "parent": parent["title"]})
    return applied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--mode", choices=["plan", "apply"], default="plan")
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest.resolve())
        token = os.environ.get("LINEAR_TOKEN", "")
        client = LinearClient(token)
        live = fetch_context(client, manifest)
        validate_live_references(manifest, live)
        plan = build_plan(manifest, live)
        if args.mode == "plan":
            emit({
                "mode": "plan",
                "result": "converged" if not plan else "changes_required",
                "contract": "Project -> Milestones -> Issues -> Sub-issues",
                "project": manifest["project"]["name"],
                "actions": plan,
            })
            return 0
        if not plan:
            emit({"mode": "apply", "result": "converged", "project": manifest["project"]["name"], "applied": []})
            return 0
        applied = apply_manifest(client, manifest, live)
        verified = fetch_context(client, manifest)
        remaining = build_plan(manifest, verified)
        if remaining:
            raise ContractError(f"verification failed; remaining actions: {remaining}")
        emit({
            "mode": "apply",
            "result": "applied",
            "contract": "Project -> Milestones -> Issues -> Sub-issues",
            "project": manifest["project"]["name"],
            "applied": applied,
            "verified": True,
        })
        return 0
    except (ContractError, json.JSONDecodeError) as exc:
        emit({"mode": args.mode, "result": "error", "issues": [str(exc)]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
