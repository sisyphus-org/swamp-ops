#!/usr/bin/env python3
"""Policy-bounded Linear command lane for the project-manager profile."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any


ISSUE_IDENTIFIER = re.compile(r"^SIS-[1-9][0-9]*$")
PROFILE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{7,199}$")
OPERATIONS = {
    "read_issue",
    "change_state",
    "update_issue",
    "inventory_sub_issues",
    "update_sub_issues",
    "add_comment",
    "create_issue",
    "converge_hierarchy",
    "create_standalone_issue",
    "converge_issue_tree",
    "create_issue_relation",
}
OWNER_CONTROLLED_STATES = {"Done", "Canceled", "Duplicate"}
OWNER_APPROVAL_PARENT_BLOCKER = (
    "owner approval required: clearing or replacing an issue parent"
)
ISSUE_RELATION_TYPES = {"blocks", "blocked_by", "related"}
PRIORITIES = {"High": 2, "Medium": 3, "Low": 4}
MAX_COMMENT_LENGTH = 4000
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 10000
API_URL = "https://api.linear.app/graphql"
COMMAND_ROOT = Path(__file__).parents[2] / "commands" / "linear"
ISSUE_QUERY = """
query LaneIssue($id: String!) {
  issue(id: $id) {
    id identifier title url description priority dueDate estimate
    state { id name type }
    assignee { id name email }
    labels { nodes { id name } }
    team { id key }
    parent { id identifier }
    project { id }
    projectMilestone { id }
  }
}
"""
TEAM_STATES_QUERY = """
query LaneStates($teamId: String!) {
  team(id: $teamId) {
    states(first: 100) { nodes { id name type } pageInfo { hasNextPage } }
  }
}
"""
WORKSPACE_USERS_QUERY = """
query LaneUsers($after: String) {
  users(first: 100, after: $after) {
    nodes { id name email }
    pageInfo { hasNextPage endCursor }
  }
}
"""
ISSUE_LABELS_QUERY = """
query LaneIssueLabels($teamId: ID!, $after: String) {
  issueLabels(
    first: 100
    after: $after
    filter: { team: { id: { eq: $teamId } } }
  ) {
    nodes { id name }
    pageInfo { hasNextPage endCursor }
  }
}
"""
COMMENTS_QUERY = """
query LaneComments($issueId: String!) {
  issue(id: $issueId) {
    comments(first: 100) { nodes { id body } pageInfo { hasNextPage } }
  }
}
"""
COMMENT_QUERY = """
query LaneComment($id: String!) {
  comment(id: $id) { id body issueId }
}
"""
ISSUE_UPDATE = """
mutation LaneState($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success }
}
"""
COMMENT_CREATE = """
mutation LaneComment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id body issueId } }
}
"""
ISSUE_RELATIONS_QUERY = """
query LaneIssueRelations($id: String!) {
  issue(id: $id) {
    id identifier
    relations(first: 100) {
      nodes { id type issue { id identifier } relatedIssue { id identifier } }
      pageInfo { hasNextPage }
    }
    inverseRelations(first: 100) {
      nodes { id type issue { id identifier } relatedIssue { id identifier } }
      pageInfo { hasNextPage }
    }
  }
}
"""
ISSUE_RELATION_QUERY = """
query LaneIssueRelation($id: String!) {
  issueRelation(id: $id) {
    id type issue { id identifier } relatedIssue { id identifier }
  }
}
"""
ISSUE_RELATION_CREATE = """
mutation LaneIssueRelationCreate($input: IssueRelationCreateInput!) {
  issueRelationCreate(input: $input) {
    success
    issueRelation {
      id type issue { id identifier } relatedIssue { id identifier }
    }
  }
}
"""
PARENT_CHILDREN_QUERY = """
query LaneChildren($id: String!) {
  issue(id: $id) {
    children(first: 100) {
      nodes {
        id identifier title url description priority
        state { id name type }
        team { id key }
        parent { id identifier }
        project { id }
        projectMilestone { id }
      }
      pageInfo { hasNextPage }
    }
  }
}
"""
ISSUE_CREATE = """
mutation LaneCreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier } }
}
"""
TEAMS_QUERY = """
query LaneTeams {
  teams(first: 100) {
    nodes { id key name }
    pageInfo { hasNextPage }
  }
}
"""
TEAM_PROJECTS_QUERY = """
query LaneTeamProjects($teamId: String!) {
  team(id: $teamId) {
    projects(first: 100, includeArchived: false) {
      nodes { id name description teams { nodes { id } } }
      pageInfo { hasNextPage }
    }
  }
}
"""
PROJECT_MILESTONES_QUERY = """
query LaneProjectMilestones($projectId: String!) {
  project(id: $projectId) {
    projectMilestones(first: 100) {
      nodes { id name description project { id } }
      pageInfo { hasNextPage }
    }
  }
}
"""
PROJECT_ISSUES_QUERY = """
query LaneProjectIssues($projectId: ID!) {
  issues(first: 100, filter: { project: { id: { eq: $projectId } } }) {
    nodes {
      id identifier title url description priority
      team { id key }
      project { id }
      projectMilestone { id }
      state { id name }
      parent { id identifier }
    }
    pageInfo { hasNextPage }
  }
}
"""
TEAM_ISSUES_BY_TITLE_QUERY = """
query LaneTeamIssuesByTitle($teamId: ID!, $title: String!) {
  issues(
    first: 100
    filter: { team: { id: { eq: $teamId } }, title: { eq: $title } }
  ) {
    nodes {
      id identifier title url description priority
      team { id key }
      project { id }
      projectMilestone { id }
      state { id name }
      parent { id identifier }
    }
    pageInfo { hasNextPage }
  }
}
"""
PROJECT_CREATE = """
mutation LaneCreateProject($input: ProjectCreateInput!) {
  projectCreate(input: $input) { success project { id } }
}
"""
PROJECT_MILESTONE_CREATE = """
mutation LaneCreateProjectMilestone($input: ProjectMilestoneCreateInput!) {
  projectMilestoneCreate(input: $input) { success projectMilestone { id } }
}
"""
PROJECT_ISSUE_CREATE = """
mutation LaneCreateProjectIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier } }
}
"""
ROOT_FIELDS = {
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


class ContractError(RuntimeError):
    """The command or live state violates the bounded lane contract."""


def _load_bundled_module(filename: str, name: str) -> Any:
    """Load one bundled module consistently in package and standalone contexts."""
    import sys

    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(filename)
    )
    if spec is None or spec.loader is None:
        raise ContractError(f"bundled {filename} module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_validation() -> Any:
    return _load_bundled_module("validation.py", "project_manager_linear_validation")


def _load_hierarchy() -> Any:
    """Load the bundled hierarchy module in package and standalone contexts."""
    return _load_bundled_module("hierarchy.py", "project_manager_linear_hierarchy")


def _load_comparison() -> Any:
    """Load shared Linear comparison rules in every execution context."""
    return _load_bundled_module("comparison.py", "project_manager_linear_comparison")


def _load_issue_tree() -> Any:
    """Load standalone/tree convergence in package and standalone contexts."""
    return _load_bundled_module("issue_tree.py", "project_manager_linear_issue_tree")


_VALIDATION = _load_validation()
_COMPARISON = _load_comparison()
SAFE_STATES = _VALIDATION.SAFE_STATES
CREDENTIAL_SHAPES = _VALIDATION.CREDENTIAL_SHAPES
RESERVED_COMMENT_MARKER = _VALIDATION.RESERVED_COMMENT_MARKER
RESERVED_CREATE_MARKER = _VALIDATION.RESERVED_CREATE_MARKER


class LinearClient:
    """Minimal fixed-query Linear GraphQL client for the MVP command lane."""

    def __init__(self, token: str, endpoint: str = API_URL) -> None:
        token = token.strip()
        if not token:
            raise ContractError("LINEAR_TOKEN is empty")
        self.authorization = token
        self.endpoint = endpoint

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a fixed query with bounded timeout and normalized errors."""
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
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
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContractError("Linear API response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ContractError("Linear API response root was not an object")
        if payload.get("errors"):
            messages = [item.get("message", "unknown GraphQL error") for item in payload["errors"]]
            raise ContractError("Linear GraphQL error: " + "; ".join(messages))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ContractError("Linear API response did not contain data")
        return data

    def get_issue(self, identifier: str) -> dict[str, Any] | None:
        """Resolve one issue by its exact identifier."""
        return self.execute(ISSUE_QUERY, {"id": identifier}).get("issue")

    def list_states(self, team_id: str) -> list[dict[str, Any]]:
        """Return all supported workflow states or fail on pagination."""
        connection = self.execute(TEAM_STATES_QUERY, {"teamId": team_id})["team"]["states"]
        if connection["pageInfo"]["hasNextPage"]:
            raise ContractError("team exceeds the supported 100-state limit")
        return connection["nodes"]

    def list_users(self) -> list[dict[str, Any]]:
        """Return the complete workspace user inventory with cursor pagination."""
        users: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            connection = self.execute(WORKSPACE_USERS_QUERY, {"after": after}).get("users")
            if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
                raise ContractError("workspace users payload is invalid")
            users.extend(connection["nodes"])
            page = connection.get("pageInfo")
            if not isinstance(page, dict):
                raise ContractError("workspace users pagination is invalid")
            if not page.get("hasNextPage"):
                return users
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise ContractError("workspace users pagination cursor is invalid")
            after = cursor

    def list_issue_labels(self, team_id: str) -> list[dict[str, Any]]:
        """Return every issue label in the exact team scope."""
        labels: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            connection = self.execute(
                ISSUE_LABELS_QUERY,
                {"teamId": team_id, "after": after},
            ).get("issueLabels")
            if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
                raise ContractError("issue labels payload is invalid")
            labels.extend(connection["nodes"])
            page = connection.get("pageInfo")
            if not isinstance(page, dict):
                raise ContractError("issue labels pagination is invalid")
            if not page.get("hasNextPage"):
                return labels
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise ContractError("issue labels pagination cursor is invalid")
            after = cursor

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        """Return bounded comments for before/after count evidence."""
        connection = self.execute(COMMENTS_QUERY, {"issueId": issue_id})["issue"]["comments"]
        if connection["pageInfo"]["hasNextPage"]:
            raise ContractError("issue exceeds the supported 100-comment evidence limit")
        return connection["nodes"]

    def get_comment(self, comment_id: str) -> dict[str, Any] | None:
        """Read one deterministic comment ID for invisible replay detection."""
        return self.execute(COMMENT_QUERY, {"id": comment_id}).get("comment")

    def update_issue_state(self, issue_id: str, state_id: str) -> None:
        """Apply only an issue stateId mutation."""
        result = self.execute(ISSUE_UPDATE, {"id": issue_id, "input": {"stateId": state_id}})["issueUpdate"]
        if result.get("success") is not True:
            raise ContractError("Linear state mutation did not succeed")

    def create_comment(self, issue_id: str, comment_id: str, body: str) -> None:
        """Create one clean comment at a deterministic caller-supplied ID."""
        result = self.execute(
            COMMENT_CREATE,
            {"input": {"id": comment_id, "issueId": issue_id, "body": body}},
        )["commentCreate"]
        if result.get("success") is not True:
            raise ContractError("Linear comment mutation did not succeed")

    def list_issue_relations(self, identifier: str) -> list[dict[str, Any]]:
        """Inventory both relation directions for one exact issue."""
        issue = self.execute(ISSUE_RELATIONS_QUERY, {"id": identifier}).get("issue")
        if not isinstance(issue, dict) or issue.get("identifier") != identifier:
            raise ContractError(f"exact Linear issue not found: {identifier}")
        relations: dict[str, dict[str, Any]] = {}
        for field in ("relations", "inverseRelations"):
            nodes = self._bounded_connection(
                issue.get(field), f"issue {field}"
            )
            for node in nodes:
                relation_id = node.get("id") if isinstance(node, dict) else None
                if not isinstance(relation_id, str) or not relation_id:
                    raise ContractError("issue relation inventory payload is invalid")
                previous = relations.get(relation_id)
                if previous is not None and previous != node:
                    raise ContractError("issue relation inventory contains conflicting duplicates")
                relations[relation_id] = node
        return list(relations.values())

    def get_issue_relation(self, relation_id: str) -> dict[str, Any] | None:
        """Read one deterministic relation for exact post-create verification."""
        return self.execute(ISSUE_RELATION_QUERY, {"id": relation_id}).get(
            "issueRelation"
        )

    def create_issue_relation(
        self,
        *,
        relation_id: str,
        issue_id: str,
        related_issue_id: str,
        relation_type: str,
    ) -> None:
        """Create one relation through the fixed caller-ID mutation."""
        result = self.execute(
            ISSUE_RELATION_CREATE,
            {
                "input": {
                    "id": relation_id,
                    "issueId": issue_id,
                    "relatedIssueId": related_issue_id,
                    "type": relation_type,
                }
            },
        )["issueRelationCreate"]
        if result.get("success") is not True:
            raise ContractError("Linear issue relation creation did not succeed")

    def list_child_issues(self, parent_identifier: str) -> list[dict[str, Any]]:
        """Return bounded children of one exact parent for create replay detection."""
        parent = self.execute(PARENT_CHILDREN_QUERY, {"id": parent_identifier}).get("issue")
        if not isinstance(parent, dict):
            raise ContractError(f"exact Linear parent not found: {parent_identifier}")
        connection = parent.get("children")
        if not isinstance(connection, dict) or connection.get("pageInfo", {}).get("hasNextPage"):
            raise ContractError("parent exceeds the supported 100-child idempotency limit")
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise ContractError("Linear children payload is invalid")
        return nodes

    def create_issue(
        self,
        *,
        issue_id: str,
        team_id: str,
        state_id: str,
        parent_id: str,
        title: str,
        description: str,
        priority: int,
    ) -> None:
        """Create one bounded SIS issue under an exact parent."""
        payload = {
            "id": issue_id,
            "teamId": team_id,
            "stateId": state_id,
            "parentId": parent_id,
            "title": title,
            "description": description,
            "priority": priority,
        }
        result = self.execute(ISSUE_CREATE, {"input": payload})["issueCreate"]
        if result.get("success") is not True:
            raise ContractError("Linear issue creation did not succeed")

    @staticmethod
    def _bounded_connection(connection: Any, label: str) -> list[dict[str, Any]]:
        if not isinstance(connection, dict):
            raise ContractError(f"{label} payload is invalid")
        if connection.get("pageInfo", {}).get("hasNextPage"):
            raise ContractError(f"{label} exceeds the supported 100-item limit")
        nodes = connection.get("nodes")
        if not isinstance(nodes, list) or len(nodes) > 100:
            raise ContractError(f"{label} payload is invalid")
        return nodes

    def list_teams(self) -> list[dict[str, Any]]:
        return self._bounded_connection(self.execute(TEAMS_QUERY).get("teams"), "workspace teams")

    def list_team_projects(self, team_id: str) -> list[dict[str, Any]]:
        team = self.execute(TEAM_PROJECTS_QUERY, {"teamId": team_id}).get("team")
        if not isinstance(team, dict):
            raise ContractError("exact preflighted SIS team disappeared")
        return self._bounded_connection(team.get("projects"), "SIS team projects")

    def list_project_milestones(self, project_id: str) -> list[dict[str, Any]]:
        project = self.execute(PROJECT_MILESTONES_QUERY, {"projectId": project_id}).get("project")
        if not isinstance(project, dict):
            raise ContractError("exact preflighted project disappeared")
        return self._bounded_connection(project.get("projectMilestones"), "project milestones")

    def list_project_issues(self, project_id: str) -> list[dict[str, Any]]:
        return self._bounded_connection(
            self.execute(PROJECT_ISSUES_QUERY, {"projectId": project_id}).get("issues"),
            "project issues",
        )

    def list_team_issues_by_title(
        self, team_id: str, title: str
    ) -> list[dict[str, Any]]:
        """Return bounded exact-title candidates for legacy partial-write recovery."""
        return self._bounded_connection(
            self.execute(
                TEAM_ISSUES_BY_TITLE_QUERY,
                {"teamId": team_id, "title": title},
            ).get("issues"),
            "SIS exact-title issues",
        )

    def create_project(self, *, project_id: str, team_id: str, name: str, **optional: Any) -> None:
        payload = {"id": project_id, "teamIds": [team_id], "name": name, **optional}
        result = self.execute(PROJECT_CREATE, {"input": payload})["projectCreate"]
        if result.get("success") is not True:
            raise ContractError("Linear project creation did not succeed")

    def create_project_milestone(
        self, *, milestone_id: str, project_id: str, name: str, **optional: Any
    ) -> None:
        payload = {"id": milestone_id, "projectId": project_id, "name": name, **optional}
        result = self.execute(PROJECT_MILESTONE_CREATE, {"input": payload})[
            "projectMilestoneCreate"
        ]
        if result.get("success") is not True:
            raise ContractError("Linear milestone creation did not succeed")

    def create_project_issue(
        self,
        *,
        issue_id: str,
        team_id: str,
        project_id: str,
        milestone_id: str,
        title: str,
        state_id: str | None = None,
        priority: int | None = None,
        **optional: Any,
    ) -> None:
        payload = {
            "id": issue_id,
            "teamId": team_id,
            "projectId": project_id,
            "projectMilestoneId": milestone_id,
            "title": title,
            **optional,
        }
        if state_id is not None:
            payload["stateId"] = state_id
        if priority is not None:
            payload["priority"] = priority
        result = self.execute(PROJECT_ISSUE_CREATE, {"input": payload})["issueCreate"]
        if result.get("success") is not True:
            raise ContractError("Linear hierarchy issue creation did not succeed")

    def update_scoped_issue(
        self,
        issue_id: str,
        *,
        description: str | None = None,
        state_id: str | None = None,
        priority: int | None = None,
        parent_id: str | None | object = ...,
        project_id: str | None = None,
        milestone_id: str | None = None,
    ) -> None:
        """Reconcile only allowlisted issue fields and structural links."""
        payload: dict[str, Any] = {}
        if description is not None:
            payload["description"] = description
        if state_id is not None:
            payload["stateId"] = state_id
        if priority is not None:
            payload["priority"] = priority
        if parent_id is not ...:
            payload["parentId"] = parent_id
        if project_id is not None:
            payload["projectId"] = project_id
        if milestone_id is not None:
            payload["projectMilestoneId"] = milestone_id
        if not payload:
            raise ContractError("Linear scoped issue update has no managed fields")
        result = self.execute(ISSUE_UPDATE, {"id": issue_id, "input": payload})[
            "issueUpdate"
        ]
        if result.get("success") is not True:
            raise ContractError("Linear scoped issue update did not succeed")

    def update_issue_fields(self, issue_id: str, **fields: Any) -> None:
        """Apply only allowlisted issue fields through the fixed mutation."""
        allowed = {
            "title",
            "description",
            "state_id",
            "priority",
            "assignee_id",
            "label_ids",
            "due_date",
            "estimate",
            "parent_id",
            "project_id",
            "milestone_id",
        }
        if not fields or not set(fields).issubset(allowed):
            raise ContractError("issue update has invalid managed fields")
        payload: dict[str, Any] = {}
        if "title" in fields:
            payload["title"] = fields["title"]
        if "assignee_id" in fields:
            payload["assigneeId"] = fields["assignee_id"]
        if "label_ids" in fields:
            payload["labelIds"] = fields["label_ids"]
        if "description" in fields:
            payload["description"] = fields["description"]
        if "state_id" in fields:
            payload["stateId"] = fields["state_id"]
        if "priority" in fields:
            payload["priority"] = fields["priority"]
        if "due_date" in fields:
            payload["dueDate"] = fields["due_date"]
        if "estimate" in fields:
            payload["estimate"] = fields["estimate"]
        if "parent_id" in fields:
            payload["parentId"] = fields["parent_id"]
        if "project_id" in fields:
            payload["projectId"] = fields["project_id"]
        if "milestone_id" in fields:
            payload["projectMilestoneId"] = fields["milestone_id"]
        result = self.execute(ISSUE_UPDATE, {"id": issue_id, "input": payload})[
            "issueUpdate"
        ]
        if result.get("success") is not True:
            raise ContractError("Linear issue update was not successful")

    def update_project_issue(
        self,
        issue_id: str,
        *,
        description: str | None = None,
        state_id: str | None = None,
    ) -> None:
        """Reconcile bounded managed fields on one deterministic hierarchy issue."""
        payload: dict[str, Any] = {}
        if description is not None:
            payload["description"] = description
        if state_id is not None:
            payload["stateId"] = state_id
        if not payload:
            raise ContractError("Linear hierarchy issue update has no managed fields")
        result = self.execute(ISSUE_UPDATE, {"id": issue_id, "input": payload})[
            "issueUpdate"
        ]
        if result.get("success") is not True:
            raise ContractError("Linear hierarchy issue update did not succeed")


def validate_command(raw: Any) -> dict[str, Any]:
    """Validate the exact linear-command.v2 envelope and return it unchanged."""
    if not isinstance(raw, dict) or set(raw) != ROOT_FIELDS:
        raise ContractError("command must contain exactly the linear-command.v2 fields")
    if raw["schema_version"] != "linear-command.v2":
        raise ContractError("schema_version must equal linear-command.v2")
    for field in ("command_id", "correlation_id"):
        try:
            value = uuid.UUID(str(raw[field]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContractError(f"{field} must be a UUID") from exc
        if value.version != 4:
            raise ContractError(f"{field} must be a UUIDv4")
    key = raw["idempotency_key"]
    if not isinstance(key, str) or not IDEMPOTENCY_KEY.fullmatch(key):
        raise ContractError(
            "idempotency_key must be 8-200 allowlisted printable characters"
        )
    if not PROFILE_NAME.fullmatch(str(raw["source_profile"])):
        raise ContractError("source_profile is invalid")
    operation = raw["operation"]
    if operation not in OPERATIONS:
        raise ContractError("operation is not allowed")
    target = raw["target"]
    if not isinstance(target, dict) or set(target) != {"type", "identifier"}:
        raise ContractError("target must contain exactly type and identifier")
    if operation in {
        "create_issue",
        "converge_hierarchy",
        "create_standalone_issue",
        "converge_issue_tree",
    }:
        if target != {"type": "team", "identifier": "SIS"}:
            raise ContractError(f"{operation} target must be the exact SIS team")
    elif target["type"] != "issue" or not ISSUE_IDENTIFIER.fullmatch(
        str(target["identifier"])
    ):
        raise ContractError("target must be an exact SIS-N issue identifier")
    change = raw["change"]
    if not isinstance(change, dict):
        raise ContractError("change must be an object")
    if operation == "read_issue":
        if change:
            raise ContractError("read_issue change must be empty")
    elif operation == "change_state":
        if set(change) != {"state"} or not isinstance(change.get("state"), str):
            raise ContractError("change_state supports exactly one state field")
        state = change["state"]
        if state in OWNER_CONTROLLED_STATES:
            raise ContractError(f"state {state} is owner-controlled and unavailable in MVP")
        if state not in SAFE_STATES:
            raise ContractError("requested state is not in the exact safe-state allowlist")
    elif operation == "update_issue":
        allowed = {
            "title",
            "description",
            "state",
            "priority",
            "assignee",
            "labels",
            "due_date",
            "estimate",
            "parent_identifier",
            "project",
            "milestone",
        }
        if not change or not set(change).issubset(allowed):
            raise ContractError(
                "update_issue supports title, description, state, priority, assignee, labels, due_date, estimate, parent_identifier, project, and milestone"
            )
        if "title" in change:
            title = change["title"]
            if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LENGTH:
                raise ContractError("update_issue title must be 1-200 characters")
            if RESERVED_CREATE_MARKER in title or RESERVED_COMMENT_MARKER in title:
                raise ContractError("update_issue title contains the reserved marker")
            if any(pattern.search(title) for pattern in CREDENTIAL_SHAPES):
                raise ContractError("update_issue title contains credential-shaped data")
        if "description" in change:
            description = change["description"]
            if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
                raise ContractError("update_issue description must be 0-10000 characters")
            if RESERVED_CREATE_MARKER in description or RESERVED_COMMENT_MARKER in description:
                raise ContractError("update_issue description contains the reserved marker")
            if any(pattern.search(description) for pattern in CREDENTIAL_SHAPES):
                raise ContractError("update_issue description contains credential-shaped data")
        if "state" in change:
            if change["state"] in OWNER_CONTROLLED_STATES:
                raise ContractError("update_issue state is owner-controlled")
            if change["state"] not in SAFE_STATES:
                raise ContractError("update_issue state is not in the safe-state allowlist")
        if "priority" in change and change["priority"] not in PRIORITIES:
            raise ContractError("update_issue priority is not in the bounded allowlist")
        if "assignee" in change and change["assignee"] is not None:
            assignee = change["assignee"]
            if not isinstance(assignee, str) or not assignee.strip() or len(assignee) > 200:
                raise ContractError("update_issue assignee must be null or 1-200 characters")
        if "labels" in change:
            labels = change["labels"]
            if (
                not isinstance(labels, list)
                or len(labels) > 100
                or len(set(labels)) != len(labels)
                or any(
                    not isinstance(label, str) or not label.strip() or len(label) > 200
                    for label in labels
                )
            ):
                raise ContractError("update_issue labels must be 0-100 unique exact names")
        if "due_date" in change and change["due_date"] is not None:
            due_date = change["due_date"]
            if not isinstance(due_date, str) or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}", due_date
            ):
                raise ContractError("update_issue due_date must be ISO YYYY-MM-DD or null")
            try:
                date.fromisoformat(due_date)
            except ValueError as exc:
                raise ContractError(
                    "update_issue due_date must be a valid calendar date"
                ) from exc
        if "estimate" in change:
            estimate = change["estimate"]
            if estimate is not None and (
                isinstance(estimate, bool)
                or not isinstance(estimate, int)
                or estimate < 0
            ):
                raise ContractError(
                    "update_issue estimate must be a non-negative integer or null"
                )
        if "parent_identifier" in change:
            parent_identifier = change["parent_identifier"]
            if parent_identifier is not None and (
                not isinstance(parent_identifier, str)
                or not ISSUE_IDENTIFIER.fullmatch(parent_identifier)
            ):
                raise ContractError(
                    "update_issue parent_identifier must be an exact SIS-N identifier or null"
                )
        if ("project" in change) != ("milestone" in change):
            raise ContractError(
                "update_issue project and milestone must be supplied together"
            )
        if "project" in change:
            project = change["project"]
            milestone = change["milestone"]
            if (project is None) != (milestone is None):
                raise ContractError(
                    "update_issue project and milestone must both be exact names or null"
                )
            if project is not None:
                values = (project, milestone)
                if any(
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > MAX_TITLE_LENGTH
                    for value in values
                ):
                    raise ContractError(
                        "update_issue project and milestone must both be exact 1-200 character names or null"
                    )
                if any(
                    any(ord(char) < 32 for char in value)
                    or _VALIDATION.RESERVED_MARKER in value
                    or any(pattern.search(value) for pattern in CREDENTIAL_SHAPES)
                    for value in values
                ):
                    raise ContractError(
                        "update_issue project or milestone name contains unsafe data"
                    )
    elif operation == "inventory_sub_issues":
        if change:
            raise ContractError("inventory_sub_issues change must be empty")
    elif operation == "create_issue_relation":
        if set(change) != {"related_identifier", "relation_type"}:
            raise ContractError(
                "create_issue_relation supports exactly related_identifier and relation_type"
            )
        related_identifier = change.get("related_identifier")
        if not isinstance(related_identifier, str) or not ISSUE_IDENTIFIER.fullmatch(
            related_identifier
        ):
            raise ContractError(
                "create_issue_relation related_identifier must be an exact SIS-N identifier"
            )
        if change.get("relation_type") not in ISSUE_RELATION_TYPES:
            raise ContractError(
                "create_issue_relation relation_type is not in the bounded allowlist"
            )
        if related_identifier == target["identifier"]:
            raise ContractError("create_issue_relation cannot relate an issue to itself")
    elif operation == "update_sub_issues":
        if set(change) != {"description"}:
            raise ContractError("update_sub_issues supports exactly description")
        description = change["description"]
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
            raise ContractError("update_sub_issues description must be 0-10000 characters")
        if RESERVED_CREATE_MARKER in description or RESERVED_COMMENT_MARKER in description:
            raise ContractError("update_sub_issues description contains the reserved marker")
        if any(pattern.search(description) for pattern in CREDENTIAL_SHAPES):
            raise ContractError("update_sub_issues description contains credential-shaped data")
    elif operation == "add_comment":
        if set(change) != {"body"} or not isinstance(change.get("body"), str):
            raise ContractError("add_comment supports exactly one comment body")
        body = change["body"]
        if not body.strip() or len(body) > MAX_COMMENT_LENGTH:
            raise ContractError("comment body must be 1-4000 characters")
        if RESERVED_COMMENT_MARKER in body:
            raise ContractError("comment body contains the reserved marker")
        if any(pattern.search(body) for pattern in CREDENTIAL_SHAPES):
            raise ContractError("comment body contains credential-shaped data")
    elif operation == "create_issue":
        expected = {
            "title",
            "description",
            "parent_identifier",
            "state",
            "priority",
        }
        if set(change) != expected:
            raise ContractError("create_issue change has invalid fields")
        title = change.get("title")
        description = change.get("description")
        parent = change.get("parent_identifier")
        state = change.get("state")
        priority = change.get("priority")
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LENGTH:
            raise ContractError("create_issue title must be 1-200 characters")
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
            raise ContractError("create_issue description must be 0-10000 characters")
        if not isinstance(parent, str) or not ISSUE_IDENTIFIER.fullmatch(parent):
            raise ContractError("create_issue parent must be an exact SIS-N identifier")
        if state not in SAFE_STATES:
            raise ContractError("create_issue state is not in the safe-state allowlist")
        if priority not in PRIORITIES:
            raise ContractError("create_issue priority is not in the bounded allowlist")
        if any(
            marker in value
            for marker in (RESERVED_COMMENT_MARKER, RESERVED_CREATE_MARKER)
            for value in (title, description)
        ):
            raise ContractError("create_issue fields contain the reserved marker")
        if any(
            pattern.search(title + "\n" + description)
            for pattern in CREDENTIAL_SHAPES
        ):
            raise ContractError("create_issue fields contain credential-shaped data")
    elif operation == "converge_hierarchy":
        _load_hierarchy().validate_change(change, ContractError)
    elif operation in {"create_standalone_issue", "converge_issue_tree"}:
        _load_issue_tree().validate_change(change, operation, ContractError)
    if raw["policy"] != {"mode": "standard"}:
        raise ContractError("policy must be the standard fail-closed lane")
    return raw


def issue_snapshot(issue: dict[str, Any]) -> dict[str, Any]:
    """Return validated bounded issue fields allowed in a typed result."""
    state = issue.get("state")
    required_strings = {
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "state": state.get("name") if isinstance(state, dict) else None,
    }
    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in required_strings.values()
    ):
        raise ContractError("Linear issue payload is missing required bounded fields")
    return required_strings


def recursive_sub_issue_inventory(
    client: Any,
    *,
    parent: dict[str, Any],
    team_id: str,
) -> list[dict[str, Any]]:
    """Read and validate every descendant in stable depth-first order."""
    root_identifier = parent["identifier"]
    seen_ids = {parent["id"]}
    seen_identifiers = {root_identifier}
    inventory: list[dict[str, Any]] = []

    def visit(parent_identifier: str) -> None:
        children = client.list_child_issues(parent_identifier)
        if not isinstance(children, list):
            raise ContractError("sub-issue inventory payload is invalid")
        for child in children:
            if not isinstance(child, dict):
                raise ContractError("sub-issue inventory payload is invalid")
            child_id = child.get("id")
            child_identifier = child.get("identifier")
            child_team = child.get("team")
            child_parent = child.get("parent")
            if (
                not isinstance(child_id, str)
                or not child_id
                or not isinstance(child_identifier, str)
                or not ISSUE_IDENTIFIER.fullmatch(child_identifier)
                or not isinstance(child_team, dict)
                or child_team.get("id") != team_id
                or child_team.get("key") != "SIS"
                or not isinstance(child_parent, dict)
                or child_parent.get("identifier") != parent_identifier
            ):
                raise ContractError("sub-issue inventory relationship verification failed")
            if child_id in seen_ids or child_identifier in seen_identifiers:
                raise ContractError("sub-issue inventory contains a cycle or duplicate")
            seen_ids.add(child_id)
            seen_identifiers.add(child_identifier)
            inventory.append(
                {
                    "id": child_id,
                    **issue_snapshot(child),
                    "description": child.get("description"),
                    "parent_identifier": parent_identifier,
                }
            )
            visit(child_identifier)

    visit(root_identifier)
    return inventory


def result_base(command: dict[str, Any], issue: dict[str, Any], mode: str) -> dict[str, Any]:
    """Build the common linear-result.v2 envelope without raw API payloads."""
    return {
        "schema_version": "linear-result.v2",
        "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "operation": command["operation"],
        "mode": mode,
        "target": {
            "type": "issue",
            "identifier": issue["identifier"],
            "url": issue["url"],
        },
    }


def command_fingerprint(command: dict[str, Any]) -> tuple[str, str, str]:
    """Return key hash, request hash, and deterministic invisible comment ID."""
    semantic = {
        field: command[field]
        for field in ("operation", "target", "change", "policy")
    }
    key_hash = hashlib.sha256(command["idempotency_key"].encode()).hexdigest()
    request_hash = hashlib.sha256(
        json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    comment_id = deterministic_uuid4("linear-command:comment:v2", key_hash)
    return key_hash, request_hash, comment_id


def deterministic_uuid4(domain: str, value: str) -> str:
    """Derive a stable RFC 4122 UUIDv4-shaped identifier from hashed input."""
    raw = bytearray(hashlib.sha256(f"{domain}:{value}".encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    result = str(uuid.UUID(bytes=bytes(raw)))
    if uuid.UUID(result).version != 4:
        raise ContractError("deterministic identifier is not UUIDv4")
    return result


def load_journal(path: Path) -> dict[str, str]:
    """Load a hash-only idempotency journal and fail closed on corruption."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"idempotency journal is unreadable: {path}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise ContractError("idempotency journal has an invalid shape")
    return raw


def write_journal(path: Path, entries: dict[str, str]) -> None:
    """Atomically persist only hashed idempotency keys and request hashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(entries, sort_keys=True) + "\n")
    temporary.replace(path)


@contextmanager
def command_lock(journal_path: Path):
    """Serialize all local mutation applies sharing one journal."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = journal_path.with_suffix(journal_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def execute_command(
    client: Any,
    raw: Any,
    *,
    mode: str,
    journal_path: Path | None = None,
    _lock_held: bool = False,
) -> dict[str, Any]:
    """Plan or execute one validated exact-target command."""
    if mode not in {"plan", "apply"}:
        raise ContractError("mode must be plan or apply")
    command = validate_command(raw)
    key_hash, request_hash, _ = command_fingerprint(command)
    mutation_apply = mode == "apply" and command["operation"] != "read_issue"
    if mutation_apply and not _lock_held:
        if journal_path is None:
            raise ContractError("apply mutations require an idempotency journal")
        with command_lock(journal_path):
            return execute_command(
                client,
                command,
                mode=mode,
                journal_path=journal_path,
                _lock_held=True,
            )
    journal: dict[str, str] = {}
    if mutation_apply and journal_path:
        journal = load_journal(journal_path)
        existing_hash = journal.get(key_hash)
        if existing_hash is not None and existing_hash != request_hash:
            raise ContractError("idempotency key conflict with a different request")

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        """Record a verified apply result without retaining request content."""
        if (
            mode == "apply"
            and command["operation"] != "read_issue"
            and journal_path is not None
            and result.get("verified") is True
        ):
            journal[key_hash] = request_hash
            write_journal(journal_path, journal)
        return result

    if command["operation"] == "converge_hierarchy":
        return finish(
            _load_hierarchy().execute(
                client,
                command,
                mode=mode,
                error_cls=ContractError,
            )
        )

    if command["operation"] in {"create_standalone_issue", "converge_issue_tree"}:
        return finish(
            _load_issue_tree().execute(
                client,
                command,
                mode=mode,
                error_cls=ContractError,
            )
        )

    if command["operation"] == "create_issue":
        change = command["change"]
        parent_identifier = change["parent_identifier"]
        parent = client.get_issue(parent_identifier)
        if not isinstance(parent, dict) or parent.get("identifier") != parent_identifier:
            raise ContractError(f"exact Linear parent not found: {parent_identifier}")
        team = parent.get("team")
        if (
            not isinstance(team, dict)
            or team.get("key") != "SIS"
            or not isinstance(team.get("id"), str)
            or not team["id"].strip()
        ):
            raise ContractError("create_issue parent is not in the SIS team")
        states = [
            item
            for item in client.list_states(team["id"])
            if item.get("name") == change["state"]
        ]
        if len(states) != 1:
            raise ContractError(f"exact workflow state not found: {change['state']}")
        key_hash, _, _ = command_fingerprint(command)
        issue_id = deterministic_uuid4("linear-command:issue:v2", key_hash)
        description = change["description"]

        def verified_create_snapshot(issue: dict[str, Any]) -> dict[str, Any]:
            """Validate every bounded create field at the deterministic issue ID."""
            issue_parent = issue.get("parent")
            issue_team = issue.get("team")
            issue_state = issue.get("state")
            fields = []
            if issue.get("id") != issue_id or issue.get("title") != change["title"]:
                fields.append("id/title")
            if not _COMPARISON.description_matches(description, issue.get("description")):
                fields.append("description")
            if not isinstance(issue_state, dict) or issue_state.get("name") != change["state"]:
                fields.append("state")
            if issue.get("priority") != PRIORITIES[change["priority"]]:
                fields.append("priority")
            if (
                not isinstance(issue_parent, dict)
                or issue_parent.get("id") != parent["id"]
                or issue_parent.get("identifier") != parent_identifier
            ):
                fields.append("parent")
            if (
                not isinstance(issue_team, dict)
                or issue_team.get("id") != team["id"]
                or issue_team.get("key") != "SIS"
            ):
                fields.append("team")
            if fields:
                raise ContractError(_COMPARISON.mismatch_message("create_issue", fields))
            snapshot = issue_snapshot(issue)
            snapshot.update(
                {
                    "description": issue.get("description"),
                    "priority": change["priority"],
                    "parent_identifier": parent_identifier,
                }
            )
            return snapshot

        children = client.list_child_issues(parent_identifier)
        for child in children:
            if (
                not isinstance(child, dict)
                or not isinstance(child.get("id"), str)
                or not child["id"].strip()
            ):
                raise ContractError("Linear child payload contains malformed child node")
        existing_child = next(
            (item for item in children if item["id"] == issue_id),
            None,
        )
        existing = (
            {
                **existing_child,
                "parent": {
                    "id": parent["id"],
                    "identifier": parent_identifier,
                },
            }
            if existing_child is not None
            else None
        )
        if existing is not None:
            snapshot = verified_create_snapshot(existing)
            created = existing
            replay_base = result_base(command, created, mode)
            return finish(
                {
                    **replay_base,
                    "result": "no_op",
                    "before": snapshot,
                    "after": snapshot,
                    "plan": [],
                    "no_op": True,
                    "verified": True,
                }
            )
        desired = {
            "title": change["title"],
            "state": change["state"],
            "priority": change["priority"],
            "parent_identifier": parent_identifier,
        }
        plan = [{"action": "create_issue", **desired}]
        team_base = {
            "schema_version": "linear-result.v2",
            "command_id": command["command_id"],
            "correlation_id": command["correlation_id"],
            "idempotency_key": command["idempotency_key"],
            "source_profile": command["source_profile"],
            "operation": "create_issue",
            "mode": mode,
            "target": {"type": "team", "identifier": "SIS"},
        }
        if mode == "plan":
            return {
                **team_base,
                "result": "planned",
                "before": None,
                "after": desired,
                "plan": plan,
                "no_op": False,
                "verified": False,
            }
        client.create_issue(
            issue_id=issue_id,
            team_id=team["id"],
            state_id=states[0]["id"],
            parent_id=parent["id"],
            title=change["title"],
            description=description,
            priority=PRIORITIES[change["priority"]],
        )
        created = client.get_issue(issue_id)
        if not isinstance(created, dict):
            raise ContractError("create_issue read-back verification failed")
        created_snapshot = verified_create_snapshot(created)
        created_base = result_base(command, created, mode)
        return finish(
            {
                **created_base,
                "result": "applied",
                "before": None,
                "after": created_snapshot,
                "plan": plan,
                "no_op": False,
                "verified": True,
            }
        )

    identifier = command["target"]["identifier"]
    issue = client.get_issue(identifier)
    if not isinstance(issue, dict) or issue.get("identifier") != identifier:
        raise ContractError(f"exact Linear issue not found: {identifier}")
    team = issue.get("team")
    if (
        not isinstance(team, dict)
        or team.get("key") != "SIS"
        or not isinstance(team.get("id"), str)
        or not team["id"].strip()
    ):
        raise ContractError(f"exact target is not in the SIS team: {identifier}")
    before = issue_snapshot(issue)
    base = result_base(command, issue, mode)
    if command["operation"] == "create_issue_relation":
        change = command["change"]
        related_identifier = change["related_identifier"]
        related = client.get_issue(related_identifier)
        if (
            not isinstance(related, dict)
            or related.get("identifier") != related_identifier
            or not isinstance(related.get("id"), str)
            or not related["id"].strip()
        ):
            raise ContractError(
                f"exact related Linear issue not found: {related_identifier}"
            )
        related_team = related.get("team")
        if (
            not isinstance(related_team, dict)
            or related_team.get("id") != team["id"]
            or related_team.get("key") != "SIS"
        ):
            raise ContractError(
                f"exact related target is not in the SIS team: {related_identifier}"
            )

        user_type = change["relation_type"]
        if user_type == "blocked_by":
            source, destination, linear_type = related, issue, "blocks"
        elif user_type == "blocks":
            source, destination, linear_type = issue, related, "blocks"
        else:
            source, destination = sorted(
                (issue, related), key=lambda item: item["identifier"]
            )
            linear_type = "related"
        desired = {
            "identifier": identifier,
            "related_identifier": related_identifier,
            "relation_type": user_type,
        }
        relation_target = {"type": "issue_relation", **desired}
        relation_base = {
            "schema_version": "linear-result.v2",
            "command_id": command["command_id"],
            "correlation_id": command["correlation_id"],
            "idempotency_key": command["idempotency_key"],
            "source_profile": command["source_profile"],
            "operation": command["operation"],
            "mode": mode,
            "target": relation_target,
        }

        def relation_matches(candidate: Any, *, expected_id: str | None = None) -> bool:
            if not isinstance(candidate, dict):
                raise ContractError("issue relation inventory payload is invalid")
            relation_id = candidate.get("id")
            candidate_source = candidate.get("issue")
            candidate_destination = candidate.get("relatedIssue")
            if (
                not isinstance(relation_id, str)
                or not relation_id
                or not isinstance(candidate_source, dict)
                or not isinstance(candidate_destination, dict)
                or not isinstance(candidate_source.get("id"), str)
                or not isinstance(candidate_destination.get("id"), str)
                or not isinstance(candidate_source.get("identifier"), str)
                or not ISSUE_IDENTIFIER.fullmatch(candidate_source["identifier"])
                or not isinstance(candidate_destination.get("identifier"), str)
                or not ISSUE_IDENTIFIER.fullmatch(candidate_destination["identifier"])
                or not isinstance(candidate.get("type"), str)
            ):
                raise ContractError("issue relation inventory payload is invalid")
            if expected_id is not None and relation_id != expected_id:
                return False
            endpoints_match = (
                candidate_source["id"] == source["id"]
                and candidate_source["identifier"] == source["identifier"]
                and candidate_destination["id"] == destination["id"]
                and candidate_destination["identifier"] == destination["identifier"]
            )
            if linear_type == "related":
                reverse_match = (
                    candidate_source["id"] == destination["id"]
                    and candidate_source["identifier"] == destination["identifier"]
                    and candidate_destination["id"] == source["id"]
                    and candidate_destination["identifier"] == source["identifier"]
                )
                endpoints_match = endpoints_match or reverse_match
            return candidate.get("type") == linear_type and endpoints_match

        inventory = client.list_issue_relations(identifier)
        if not isinstance(inventory, list):
            raise ContractError("issue relation inventory payload is invalid")
        existing = [item for item in inventory if relation_matches(item)]
        if len(existing) > 1:
            raise ContractError("exact issue relation exists more than once")
        if existing:
            return finish(
                {
                    **relation_base,
                    "result": "no_op",
                    "before": desired,
                    "after": desired,
                    "plan": [],
                    "no_op": True,
                    "verified": True,
                }
            )
        relation_semantic = json.dumps(
            {
                "issue_identifier": source["identifier"],
                "related_identifier": destination["identifier"],
                "type": linear_type,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        relation_id = deterministic_uuid4(
            "linear-command:issue-relation:v2", relation_semantic
        )
        plan = [{"action": "create_issue_relation", **desired}]
        if mode == "plan":
            return {
                **relation_base,
                "result": "planned",
                "before": None,
                "after": desired,
                "plan": plan,
                "no_op": False,
                "verified": False,
            }
        client.create_issue_relation(
            relation_id=relation_id,
            issue_id=source["id"],
            related_issue_id=destination["id"],
            relation_type=linear_type,
        )
        verified_relation = client.get_issue_relation(relation_id)
        if not relation_matches(verified_relation, expected_id=relation_id):
            raise ContractError("issue relation read-back verification failed")
        return finish(
            {
                **relation_base,
                "result": "applied",
                "before": None,
                "after": desired,
                "plan": plan,
                "no_op": False,
                "verified": True,
            }
        )
    if command["operation"] == "read_issue":
        return {
            **base,
            "result": "read",
            "before": before,
            "after": before,
            "plan": [],
            "no_op": True,
            "verified": True,
        }
    if command["operation"] == "inventory_sub_issues":
        inventory = recursive_sub_issue_inventory(
            client,
            parent=issue,
            team_id=team["id"],
        )
        return {
            **base,
            "result": "read",
            "before": inventory,
            "after": inventory,
            "plan": [],
            "no_op": True,
            "verified": True,
        }
    if command["operation"] == "update_sub_issues":
        change = command["change"]
        inventory = recursive_sub_issue_inventory(
            client,
            parent=issue,
            team_id=team["id"],
        )
        desired_description = change["description"]
        pending = [
            item
            for item in inventory
            if not _COMPARISON.description_matches(
                desired_description,
                item.get("description"),
            )
        ]
        if not pending:
            return finish(
                {
                    **base,
                    "result": "no_op",
                    "before": inventory,
                    "after": inventory,
                    "plan": [],
                    "no_op": True,
                    "verified": True,
                }
            )
        plan = [
            {
                "action": "update_sub_issue",
                "identifier": item["identifier"],
                "fields": ["description"],
            }
            for item in pending
        ]
        desired = [
            {
                **item,
                "description": desired_description,
            }
            for item in inventory
        ]
        if mode == "plan":
            return {
                **base,
                "result": "planned",
                "before": inventory,
                "after": desired,
                "plan": plan,
                "no_op": False,
                "verified": False,
            }
        for item in pending:
            client.update_issue_fields(
                item["id"],
                description=desired_description,
            )
        verified_inventory = recursive_sub_issue_inventory(
            client,
            parent=issue,
            team_id=team["id"],
        )
        immutable_fields = ("id", "identifier", "title", "url", "state", "parent_identifier")
        if len(verified_inventory) != len(inventory):
            raise ContractError("update_sub_issues read-back changed tree cardinality")
        for before_item, after_item in zip(inventory, verified_inventory, strict=True):
            if any(before_item[field] != after_item[field] for field in immutable_fields):
                raise ContractError("update_sub_issues read-back changed unmanaged fields")
            if not _COMPARISON.description_matches(
                desired_description,
                after_item.get("description"),
            ):
                raise ContractError("update_sub_issues read-back mismatched description")
        return finish(
            {
                **base,
                "result": "applied",
                "before": inventory,
                "after": verified_inventory,
                "plan": plan,
                "no_op": False,
                "verified": True,
            }
        )
    if command["operation"] == "update_issue":
        change = command["change"]
        state_id = None
        assignee_id: str | None | object = ...
        assignee_name: str | None = None
        if "assignee" in change:
            requested_assignee = change["assignee"]
            if requested_assignee is None:
                assignee_id = None
            else:
                users = [
                    user
                    for user in client.list_users()
                    if user.get("name") == requested_assignee
                    or user.get("email") == requested_assignee
                ]
                unique = {
                    user.get("id"): user
                    for user in users
                    if isinstance(user.get("id"), str)
                }
                if len(unique) != 1:
                    raise ContractError("exact Linear assignee not found or ambiguous")
                user = next(iter(unique.values()))
                assignee_id = user["id"]
                assignee_name = user.get("name")
                if not isinstance(assignee_name, str) or not assignee_name.strip():
                    raise ContractError("exact Linear assignee has no public name")
        label_ids: list[str] = []
        desired_label_names: list[str] | None = None
        if "labels" in change:
            requested_labels = change["labels"]
            available = client.list_issue_labels(team["id"])
            by_name: dict[str, list[dict[str, Any]]] = {}
            for label in available:
                if isinstance(label.get("name"), str):
                    by_name.setdefault(label["name"], []).append(label)
            resolved: list[dict[str, Any]] = []
            for name in requested_labels:
                matches = by_name.get(name, [])
                if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
                    raise ContractError("exact Linear label not found or ambiguous")
                resolved.append(matches[0])
            label_ids = [label["id"] for label in resolved]
            desired_label_names = list(requested_labels)
        if "state" in change:
            states = [
                item
                for item in client.list_states(issue["team"]["id"])
                if item.get("name") == change["state"]
            ]
            if len(states) != 1:
                raise ContractError(f"exact workflow state not found: {change['state']}")
            state_id = states[0]["id"]

        desired_parent_id: str | object = ...
        desired_parent_identifier: str | None = None
        if "parent_identifier" in change:
            requested_parent = change["parent_identifier"]
            if requested_parent is None:
                raise ContractError(OWNER_APPROVAL_PARENT_BLOCKER)
            if requested_parent == identifier:
                raise ContractError("update_issue target cannot be its own parent")
            parent = client.get_issue(requested_parent)
            if (
                not isinstance(parent, dict)
                or parent.get("identifier") != requested_parent
                or not isinstance(parent.get("id"), str)
                or not parent["id"].strip()
            ):
                raise ContractError(f"exact Linear parent not found: {requested_parent}")
            parent_team = parent.get("team")
            if (
                not isinstance(parent_team, dict)
                or parent_team.get("id") != team["id"]
                or parent_team.get("key") != "SIS"
            ):
                raise ContractError("update_issue parent is not in the SIS team")
            current_parent = issue.get("parent")
            if current_parent is not None:
                if (
                    not isinstance(current_parent, dict)
                    or not isinstance(current_parent.get("id"), str)
                    or not current_parent["id"].strip()
                    or not isinstance(current_parent.get("identifier"), str)
                    or not ISSUE_IDENTIFIER.fullmatch(current_parent["identifier"])
                ):
                    raise ContractError("current issue parent is malformed")
                if (
                    current_parent["id"] != parent["id"]
                    or current_parent["identifier"] != requested_parent
                ):
                    raise ContractError(OWNER_APPROVAL_PARENT_BLOCKER)

            ancestor = parent
            seen_ancestors: set[str] = set()
            while True:
                ancestor_identifier = ancestor.get("identifier")
                ancestor_id = ancestor.get("id")
                ancestor_team = ancestor.get("team")
                if ancestor_identifier == identifier:
                    raise ContractError("update_issue parent would create a cycle")
                if (
                    not isinstance(ancestor_identifier, str)
                    or not ISSUE_IDENTIFIER.fullmatch(ancestor_identifier)
                    or not isinstance(ancestor_id, str)
                    or not ancestor_id.strip()
                    or not isinstance(ancestor_team, dict)
                    or ancestor_team.get("id") != team["id"]
                    or ancestor_team.get("key") != "SIS"
                    or ancestor_identifier in seen_ancestors
                ):
                    raise ContractError("update_issue parent ancestry is malformed or cyclic")
                seen_ancestors.add(ancestor_identifier)
                ancestor_parent = ancestor.get("parent")
                if ancestor_parent is None:
                    break
                if (
                    not isinstance(ancestor_parent, dict)
                    or not isinstance(ancestor_parent.get("id"), str)
                    or not ancestor_parent["id"].strip()
                    or not isinstance(ancestor_parent.get("identifier"), str)
                    or not ISSUE_IDENTIFIER.fullmatch(ancestor_parent["identifier"])
                ):
                    raise ContractError("update_issue parent ancestry is malformed or cyclic")
                if ancestor_parent["identifier"] == identifier:
                    raise ContractError("update_issue parent would create a cycle")
                next_ancestor = client.get_issue(ancestor_parent["identifier"])
                if (
                    not isinstance(next_ancestor, dict)
                    or next_ancestor.get("id") != ancestor_parent["id"]
                    or next_ancestor.get("identifier") != ancestor_parent["identifier"]
                ):
                    raise ContractError("update_issue parent ancestry is malformed or missing")
                ancestor = next_ancestor
            desired_parent_id = parent["id"]
            desired_parent_identifier = requested_parent

        desired_project_id: str | None | object = ...
        desired_milestone_id: str | None | object = ...
        current_project_name: str | None = None
        current_milestone_name: str | None = None
        if "project" in change:
            projects = client.list_team_projects(team["id"])
            if not isinstance(projects, list) or len(projects) > 100:
                raise ContractError("SIS team projects payload is invalid")

            def checked_project(candidate: Any) -> dict[str, Any]:
                teams = candidate.get("teams") if isinstance(candidate, dict) else None
                team_nodes = teams.get("nodes") if isinstance(teams, dict) else None
                if (
                    not isinstance(candidate, dict)
                    or not isinstance(candidate.get("id"), str)
                    or not candidate["id"].strip()
                    or not isinstance(candidate.get("name"), str)
                    or not candidate["name"].strip()
                    or not isinstance(team_nodes, list)
                    or team["id"]
                    not in {
                        node.get("id")
                        for node in team_nodes
                        if isinstance(node, dict)
                    }
                ):
                    raise ContractError("project is not in the SIS team")
                return candidate

            current_project = issue.get("project")
            current_milestone = issue.get("projectMilestone")
            if current_project is None and current_milestone is not None:
                raise ContractError("issue project and milestone scope is malformed")
            if current_project is not None:
                current_project_id = (
                    current_project.get("id")
                    if isinstance(current_project, dict)
                    else None
                )
                current_projects = [
                    candidate
                    for candidate in projects
                    if isinstance(candidate, dict)
                    and candidate.get("id") == current_project_id
                ]
                if len(current_projects) != 1:
                    raise ContractError("current issue project is missing or ambiguous")
                current_project_node = checked_project(current_projects[0])
                current_project_name = current_project_node["name"]
                if current_milestone is not None:
                    current_milestone_id = (
                        current_milestone.get("id")
                        if isinstance(current_milestone, dict)
                        else None
                    )
                    current_milestones = client.list_project_milestones(
                        current_project_node["id"]
                    )
                    if (
                        not isinstance(current_milestones, list)
                        or len(current_milestones) > 100
                    ):
                        raise ContractError("current project milestones payload is invalid")
                    current_matches = [
                        candidate
                        for candidate in current_milestones
                        if isinstance(candidate, dict)
                        and candidate.get("id") == current_milestone_id
                    ]
                    if len(current_matches) != 1:
                        raise ContractError(
                            "current issue milestone is missing or ambiguous"
                        )
                    current_milestone_node = current_matches[0]
                    current_milestone_project = current_milestone_node.get("project")
                    if (
                        not isinstance(current_milestone_node.get("name"), str)
                        or not current_milestone_node["name"].strip()
                        or not isinstance(current_milestone_project, dict)
                        or current_milestone_project.get("id")
                        != current_project_node["id"]
                    ):
                        raise ContractError(
                            "current issue milestone has the wrong project scope"
                        )
                    current_milestone_name = current_milestone_node["name"]

            if change["project"] is None:
                desired_project_id = None
                desired_milestone_id = None
            else:
                named_projects = [
                    candidate
                    for candidate in projects
                    if isinstance(candidate, dict)
                    and candidate.get("name") == change["project"]
                ]
                if len(named_projects) != 1:
                    raise ContractError("exact Linear project not found or ambiguous")
                desired_project = checked_project(named_projects[0])
                milestones = client.list_project_milestones(desired_project["id"])
                if not isinstance(milestones, list) or len(milestones) > 100:
                    raise ContractError("project milestones payload is invalid")
                named_milestones = [
                    candidate
                    for candidate in milestones
                    if isinstance(candidate, dict)
                    and candidate.get("name") == change["milestone"]
                ]
                if len(named_milestones) != 1:
                    raise ContractError("exact Linear milestone not found or ambiguous")
                desired_milestone = named_milestones[0]
                milestone_project = desired_milestone.get("project")
                if (
                    not isinstance(desired_milestone.get("id"), str)
                    or not desired_milestone["id"].strip()
                    or not isinstance(milestone_project, dict)
                    or milestone_project.get("id") != desired_project["id"]
                ):
                    raise ContractError("milestone does not belong to the selected project")
                desired_project_id = desired_project["id"]
                desired_milestone_id = desired_milestone["id"]

        def update_mismatches(live: dict[str, Any]) -> list[str]:
            fields: list[str] = []
            if live.get("identifier") != identifier or live.get("id") != issue["id"]:
                fields.append("id/title")
            if "title" in change and live.get("title") != change["title"]:
                fields.append("id/title")
            live_team = live.get("team")
            if (
                not isinstance(live_team, dict)
                or live_team.get("id") != team["id"]
                or live_team.get("key") != "SIS"
            ):
                fields.append("team")
            if "description" in change and not _COMPARISON.description_matches(
                change["description"], live.get("description")
            ):
                fields.append("description")
            live_state = live.get("state")
            if "state" in change and (
                not isinstance(live_state, dict)
                or live_state.get("name") != change["state"]
            ):
                fields.append("state")
            if "priority" in change and live.get("priority") != PRIORITIES[change["priority"]]:
                fields.append("priority")
            if "assignee" in change:
                live_assignee = live.get("assignee")
                live_assignee_id = (
                    live_assignee.get("id") if isinstance(live_assignee, dict) else None
                )
                if live_assignee_id != assignee_id:
                    fields.append("assignee")
            if "labels" in change:
                live_labels = live.get("labels")
                live_nodes = (
                    live_labels.get("nodes") if isinstance(live_labels, dict) else None
                )
                live_label_ids = (
                    {item.get("id") for item in live_nodes if isinstance(item, dict)}
                    if isinstance(live_nodes, list)
                    else set()
                )
                if live_label_ids != set(label_ids):
                    fields.append("labels")
            if "due_date" in change and live.get("dueDate") != change["due_date"]:
                fields.append("due_date")
            if "estimate" in change:
                live_estimate = live.get("estimate")
                if change["estimate"] is None:
                    if live_estimate is not None:
                        fields.append("estimate")
                elif (
                    isinstance(live_estimate, bool)
                    or not isinstance(live_estimate, int)
                    or live_estimate < 0
                    or live_estimate != change["estimate"]
                ):
                    fields.append("estimate")

            unmanaged_scalars = (
                ("title", "title", "id/title"),
                ("description", "description", "description"),
                ("priority", "priority", "priority"),
                ("due_date", "dueDate", "due_date"),
                ("estimate", "estimate", "estimate"),
            )
            for change_name, live_name, mismatch_name in unmanaged_scalars:
                if change_name not in change and live.get(live_name) != issue.get(live_name):
                    fields.append(mismatch_name)
            if "state" not in change and live.get("state") != issue.get("state"):
                fields.append("state")
            if "assignee" not in change and live.get("assignee") != issue.get("assignee"):
                fields.append("assignee")
            if "labels" not in change and live.get("labels") != issue.get("labels"):
                fields.append("labels")
            if "parent_identifier" in change:
                live_parent = live.get("parent")
                if (
                    not isinstance(live_parent, dict)
                    or live_parent.get("id") != desired_parent_id
                    or live_parent.get("identifier") != desired_parent_identifier
                ):
                    fields.append("parent")
            elif live.get("parent") != issue.get("parent"):
                fields.append("parent")
            if "project" in change:
                live_project = live.get("project")
                live_project_id = (
                    live_project.get("id") if isinstance(live_project, dict) else None
                )
                live_milestone = live.get("projectMilestone")
                live_milestone_id = (
                    live_milestone.get("id")
                    if isinstance(live_milestone, dict)
                    else None
                )
                if live_project_id != desired_project_id:
                    fields.append("project")
                if live_milestone_id != desired_milestone_id:
                    fields.append("milestone")
            else:
                if live.get("project") != issue.get("project"):
                    fields.append("project")
                if live.get("projectMilestone") != issue.get("projectMilestone"):
                    fields.append("milestone")
            return _COMPARISON.ordered_mismatch_fields(fields)

        def update_snapshot(live: dict[str, Any]) -> dict[str, Any]:
            snapshot = issue_snapshot(live)
            if "description" in change:
                snapshot["description"] = live.get("description")
            if "priority" in change:
                snapshot["priority"] = next(
                    (name for name, value in PRIORITIES.items() if value == live.get("priority")),
                    None,
                )
            if "assignee" in change:
                live_assignee = live.get("assignee")
                snapshot["assignee"] = (
                    live_assignee.get("name")
                    if isinstance(live_assignee, dict)
                    else None
                )
            if "labels" in change:
                live_labels = live.get("labels")
                live_nodes = (
                    live_labels.get("nodes") if isinstance(live_labels, dict) else []
                )
                if not isinstance(live_nodes, list):
                    live_nodes = []
                snapshot["labels"] = sorted(
                    item["name"]
                    for item in live_nodes
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                )
            if "due_date" in change:
                snapshot["due_date"] = live.get("dueDate")
            if "estimate" in change:
                snapshot["estimate"] = live.get("estimate")
            if "parent_identifier" in change:
                live_parent = live.get("parent")
                snapshot["parent_identifier"] = (
                    live_parent.get("identifier")
                    if isinstance(live_parent, dict)
                    else None
                )
            if "project" in change:
                live_project = live.get("project")
                live_project_id = (
                    live_project.get("id") if isinstance(live_project, dict) else None
                )
                live_milestone = live.get("projectMilestone")
                live_milestone_id = (
                    live_milestone.get("id")
                    if isinstance(live_milestone, dict)
                    else None
                )
                if live_project_id is None and live_milestone_id is None:
                    snapshot["project"] = None
                    snapshot["milestone"] = None
                elif (
                    live_project_id == desired_project_id
                    and live_milestone_id == desired_milestone_id
                ):
                    snapshot["project"] = change["project"]
                    snapshot["milestone"] = change["milestone"]
                else:
                    snapshot["project"] = current_project_name
                    snapshot["milestone"] = current_milestone_name
            return snapshot

        fields = update_mismatches(issue)
        before_update = update_snapshot(issue)
        if not fields:
            return finish({
                **base,
                "result": "no_op",
                "before": before_update,
                "after": before_update,
                "plan": [],
                "no_op": True,
                "verified": True,
            })
        managed_fields = [
            field
            for field in (
                "title",
                "description",
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
            if field in change
        ]
        plan = [{"action": "update_issue", "fields": managed_fields}]
        after_update = dict(before_update)
        after_update.update(change)
        if "assignee" in change:
            after_update["assignee"] = assignee_name
        if "labels" in change:
            after_update["labels"] = sorted(desired_label_names or [])
        if mode == "plan":
            return {
                **base,
                "result": "planned",
                "before": before_update,
                "after": after_update,
                "plan": plan,
                "no_op": False,
                "verified": False,
            }
        mutation: dict[str, Any] = {}
        if "title" in change:
            mutation["title"] = change["title"]
        if "description" in change:
            mutation["description"] = change["description"]
        if state_id is not None:
            mutation["state_id"] = state_id
        if "priority" in change:
            mutation["priority"] = PRIORITIES[change["priority"]]
        if assignee_id is not ...:
            mutation["assignee_id"] = assignee_id
        if "labels" in change:
            mutation["label_ids"] = label_ids
        if "due_date" in change:
            mutation["due_date"] = change["due_date"]
        if "estimate" in change:
            mutation["estimate"] = change["estimate"]
        if desired_parent_id is not ...:
            mutation["parent_id"] = desired_parent_id
        if "project" in change:
            mutation["project_id"] = desired_project_id
            mutation["milestone_id"] = desired_milestone_id
        client.update_issue_fields(issue["id"], **mutation)
        verified_issue = client.get_issue(identifier)
        if not isinstance(verified_issue, dict):
            raise ContractError(
                _COMPARISON.mismatch_message("update_issue", ["id/title"])
            )
        fields = update_mismatches(verified_issue)
        if fields:
            raise ContractError(_COMPARISON.mismatch_message("update_issue", fields))
        return finish({
            **base,
            "result": "applied",
            "before": before_update,
            "after": update_snapshot(verified_issue),
            "plan": plan,
            "no_op": False,
            "verified": True,
        })
    if command["operation"] == "change_state":
        requested = command["change"]["state"]
        states = [item for item in client.list_states(issue["team"]["id"]) if item["name"] == requested]
        if len(states) != 1:
            raise ContractError(f"exact workflow state not found: {requested}")
        after = {**before, "state": requested}
        if before["state"] == requested:
            return finish({
                **base,
                "result": "no_op",
                "before": before,
                "after": after,
                "plan": [],
                "no_op": True,
                "verified": True,
            })
        plan = [{"action": "change_state", "from": before["state"], "to": requested}]
        if mode == "plan":
            return {
                **base,
                "result": "planned",
                "before": before,
                "after": after,
                "plan": plan,
                "no_op": False,
                "verified": False,
            }
        client.update_issue_state(issue["id"], states[0]["id"])
        verified_issue = client.get_issue(identifier)
        verified_state = (
            verified_issue.get("state")
            if isinstance(verified_issue, dict)
            else None
        )
        if (
            not isinstance(verified_issue, dict)
            or verified_issue.get("identifier") != identifier
            or not isinstance(verified_state, dict)
            or verified_state.get("name") != requested
        ):
            raise ContractError("state read-back verification failed")
        return finish({
            **base,
            "result": "applied",
            "before": before,
            "after": issue_snapshot(verified_issue),
            "plan": plan,
            "no_op": False,
            "verified": True,
        })
    if command["operation"] == "add_comment":
        body = command["change"]["body"]
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        key_hash, _, comment_id = command_fingerprint(command)
        comments = client.list_comments(issue["id"])
        existing_comment = next(
            (item for item in comments if item.get("id") == comment_id),
            None,
        )
        existing = (
            {**existing_comment, "issueId": issue["id"]}
            if existing_comment is not None
            else None
        )
        before_with_comments = {**before, "comment_count": len(comments)}
        plan = [
            {
                "action": "add_comment",
                "body_sha256": body_hash,
                "body_length": len(body),
            }
        ]
        if existing is not None and (
            existing.get("id") != comment_id
            or existing.get("issueId") != issue["id"]
            or existing.get("body") != body
        ):
            raise ContractError("idempotency key conflicts with a different request")
        if existing is not None:
            return finish({
                **base,
                "result": "no_op",
                "before": before_with_comments,
                "after": before_with_comments,
                "plan": [],
                "no_op": True,
                "verified": True,
            })
        after_with_comments = {
            **before,
            "comment_count": len(comments) + 1,
            "comment_body_sha256": body_hash,
        }
        if mode == "plan":
            return {
                **base,
                "result": "planned",
                "before": before_with_comments,
                "after": after_with_comments,
                "plan": plan,
                "no_op": False,
                "verified": False,
            }
        client.create_comment(issue["id"], comment_id, body)
        verified_comment = client.get_comment(comment_id)
        if (
            not isinstance(verified_comment, dict)
            or verified_comment.get("id") != comment_id
            or verified_comment.get("issueId") != issue["id"]
            or verified_comment.get("body") != body
        ):
            raise ContractError("comment read-back verification failed")
        verified_comments = client.list_comments(issue["id"])
        return finish({
            **base,
            "result": "applied",
            "before": before_with_comments,
            "after": {
                **after_with_comments,
                "comment_count": len(verified_comments),
            },
            "plan": plan,
            "no_op": False,
            "verified": True,
        })
    raise ContractError("validated operation has no execution path")


def load_command(
    path: Path,
    *,
    allowed_root: Path = COMMAND_ROOT,
) -> dict[str, Any]:
    """Load one JSON command confined to the repository command root."""
    resolved = path.resolve()
    root = allowed_root.resolve()
    if not resolved.is_relative_to(root):
        raise ContractError("command path is outside the allowlisted command root")
    if not resolved.is_file() or resolved.suffix.lower() != ".json":
        raise ContractError("command must be an existing .json file")
    try:
        raw = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("command file is not valid JSON") from exc
    return validate_command(raw)


def emit(payload: dict[str, Any]) -> None:
    """Emit one stable machine-readable result line."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    """Run one command in plan or apply mode and emit linear-result.v2."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True, type=Path)
    parser.add_argument("--mode", choices=["plan", "apply"], default="plan")
    default_home = Path(
        os.environ.get(
            "HERMES_HOME", "/Users/hermes/.hermes/profiles/project-manager"
        )
    )
    parser.add_argument(
        "--journal",
        type=Path,
        default=default_home / "linear-command-lane" / "journal.json",
    )
    args = parser.parse_args()
    try:
        command = load_command(args.command.resolve())
        client = LinearClient(os.environ.get("LINEAR_TOKEN", ""))
        result = execute_command(
            client,
            command,
            mode=args.mode,
            journal_path=args.journal.resolve(),
        )
        emit(result)
        return 0
    except ContractError as exc:
        emit(
            {
                "schema_version": "linear-result.v2",
                "mode": args.mode,
                "result": "error",
                "verified": False,
                "issues": [str(exc)],
            }
        )
        return 1
    except Exception as exc:
        emit(
            {
                "schema_version": "linear-result.v2",
                "mode": args.mode,
                "result": "error",
                "verified": False,
                "issues": [f"unexpected execution error: {type(exc).__name__}"],
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
