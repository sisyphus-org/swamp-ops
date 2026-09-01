import concurrent.futures
import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "project_manager_linear"
    / "lane.py"
)
SPEC = importlib.util.spec_from_file_location("linear_command_lane", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import Linear command lane: {SCRIPT}")
lane = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lane
SPEC.loader.exec_module(lane)


def command(operation="read_issue", change=None, key="linear:SIS-59:read:fixture"):
    return {
        "schema_version": "linear-command.v2",
        "command_id": "11111111-1111-4111-8111-111111111111",
        "correlation_id": "22222222-2222-4222-8222-222222222222",
        "idempotency_key": key,
        "source_profile": "swe",
        "operation": operation,
        "target": {"type": "issue", "identifier": "SIS-59"},
        "change": change or {},
        "policy": {"mode": "standard"},
    }


def owner_policy():
    return {
        "mode": "owner_approved",
        "approval": {
            "workflow": "linear-destructive-owner-approval-attest",
            "model": "linear-destructive-owner-approval-attest",
            "run_id": "55555555-5555-4555-8555-555555555555",
            "artifact_version": 7,
            "checksum": "a" * 64,
            "intent_hash": "b" * 64,
            "before_state_hash": "c" * 64,
            "expires_at": "2026-08-31T23:00:00Z",
        },
    }


def create_command(key="linear:SIS:create:fixture"):
    raw = command("read_issue", {}, key)
    raw["operation"] = "create_issue"
    raw["target"] = {"type": "team", "identifier": "SIS"}
    raw["change"] = {
        "title": "Universal routing tracer bullet",
        "description": "Bounded verification issue.",
        "parent_identifier": "SIS-56",
        "state": "Todo",
        "priority": "High",
    }
    return raw


def relation_command(
    relation_type="blocked_by", key="linear:SIS:relation:fixture"
):
    raw = command("create_issue_relation", {}, key)
    raw["target"] = {"type": "issue", "identifier": "SIS-59"}
    raw["change"] = {
        "related_identifier": "SIS-56",
        "relation_type": relation_type,
    }
    return raw


def relation_change_command(
    operation="remove_issue_relation",
    key="linear:SIS:relation-change:fixture",
):
    raw = command(operation, {}, key)
    raw["target"] = {"type": "issue", "identifier": "SIS-59"}
    raw["policy"] = owner_policy()
    raw["change"] = {
        "related_identifier": "SIS-56",
        "relation_type": "blocked_by",
    }
    if operation == "replace_issue_relation":
        raw["change"] = {
            "old_related_identifier": "SIS-56",
            "old_relation_type": "blocked_by",
            "new_related_identifier": "SIS-57",
            "new_relation_type": "related",
        }
    return raw


def consumed_owner_authorization(raw):
    approval_module = lane._load_approval()
    verified = approval_module.VerifiedOwnerApproval(
        {},
        {
            "operation": raw["operation"],
            "target": raw["target"],
            "change": raw["change"],
        },
        "c" * 64,
        "a" * 64,
        _marker=approval_module._VERIFIED_MARKER,
    )
    return approval_module.ConsumedOwnerApproval(
        verified, _marker=approval_module._CONSUMED_MARKER
    )


def hierarchy_command(key="linear:SIS:hierarchy:health"):
    raw = command("read_issue", {}, key)
    raw["operation"] = "converge_hierarchy"
    raw["target"] = {"type": "team", "identifier": "SIS"}
    raw["change"] = {
        "project": {
            "name": "health",
        },
        "milestone": {
            "name": "Подолог",
        },
        "issue": {
            "title": "Сходить в Solomia и записаться",
            "description": "https://solomia.in.ua",
        },
    }
    return raw


def standalone_command(key="linear:SIS:standalone:wardrobe"):
    raw = command("read_issue", {}, key)
    raw["operation"] = "create_standalone_issue"
    raw["target"] = {"type": "team", "identifier": "SIS"}
    raw["change"] = {
        "project": {"name": "Wardrobe & Style", "description": "Wardrobe project"},
        "milestone": {"name": "Autumn 2026", "description": "Autumn milestone"},
        "issue": {
            "title": "Выбрать и купить костюм",
            "description": "Варианты: https://example.com/suits",
            "state": "Todo",
            "priority": "High",
        },
    }
    return raw


def issue_tree_command(key="linear:SIS:tree:shakespeare"):
    raw = standalone_command(key)
    raw["operation"] = "converge_issue_tree"
    raw["change"] = {
        "project": {"name": "Книги", "description": "Reading project"},
        "milestone": {
            "name": "Английская литература",
            "description": "English literature",
        },
        "issue": {
            "title": "Уильям Шекспир — великие трагедии",
            "description": "Прочитать основную четвёрку.",
            "state": "Todo",
            "priority": "Medium",
        },
        "sub_issues": [
            {
                "title": title,
                "description": f"Прочитать «{title}».",
                "state": "Todo",
                "priority": "Medium",
            }
            for title in ("Король Лир", "Макбет", "Гамлет", "Отелло")
        ],
    }
    return raw


def initiative_link_command(key="linear:SIS:initiative-link:fixture"):
    raw = command("link_project_to_initiative", {}, key)
    raw["target"] = {"type": "team", "identifier": "SIS"}
    raw["change"] = {
        "project": "Hermes Experience",
        "initiative": "Personal operating system",
    }
    return raw


def workspace_read_command(
    operation="inventory_linear",
    *,
    entity_types=None,
    include_archived=False,
    query="STRASSE",
    key="linear:workspace:read:fixture",
):
    raw = command(operation, {}, key)
    raw["target"] = {"type": "workspace", "identifier": "current"}
    raw["change"] = {
        "entity_types": entity_types
        if entity_types is not None
        else ["issues", "projects", "milestones", "initiatives"],
        "include_archived": include_archived,
    }
    if operation == "search_linear":
        raw["change"] = {"query": query, **raw["change"]}
    return raw


def initiative_command(
    operation="create_initiative", key="linear:workspace:initiative:fixture"
):
    raw = command(operation, {}, key)
    raw["target"] = {"type": "workspace", "identifier": "current"}
    raw["change"] = {
        "name": "Personal operating system",
        "description": "Connected personal systems",
        "target_date": "2026-12-31",
    }
    if operation == "update_initiative":
        raw["change"] = {
            "name": "Personal operating system",
            "new_name": "Personal systems",
            "description": "Unified personal systems",
            "target_date": None,
        }
    return raw


def project_command(operation="create_project", key="linear:SIS:project:fixture"):
    raw = command(operation, {}, key)
    raw["target"] = {"type": "team", "identifier": "SIS"}
    raw["change"] = {
        "name": "Hermes Experience",
        "description": "User-facing integrations",
        "target_date": "2026-12-31",
    }
    if operation == "update_project":
        raw["change"] = {
            "name": "Hermes Experience",
            "new_name": "Hermes Personal Experience",
            "description": "Personal integrations",
            "target_date": None,
        }
    return raw


def milestone_command(operation="create_milestone", key="linear:SIS:milestone:fixture"):
    raw = command(operation, {}, key)
    raw["target"] = {"type": "team", "identifier": "SIS"}
    raw["change"] = {
        "project": "Hermes Experience",
        "name": "Calendar integration",
        "description": "Calendar milestone",
        "target_date": "2026-10-01",
    }
    if operation == "update_milestone":
        raw["change"] = {
            "project": "Hermes Experience",
            "name": "Calendar integration",
            "new_name": "Calendar and reminders",
            "description": "Calendar and reminders milestone",
            "target_date": None,
        }
    return raw


def issue(state="In Progress"):
    return {
        "id": "issue-uuid",
        "identifier": "SIS-59",
        "title": "Implement lane",
        "url": "https://linear.app/example/issue/SIS-59",
        "state": {"id": f"state-{state}", "name": state, "type": "started"},
        "team": {"id": "team-uuid", "key": "SIS"},
        "description": "Old description",
        "priority": lane.PRIORITIES["Low"],
        "dueDate": None,
        "estimate": None,
        "assignee": None,
        "labels": {"nodes": []},
        "parent": {"id": "parent-uuid", "identifier": "SIS-1"},
        "project": {"id": "project-uuid"},
        "projectMilestone": {"id": "milestone-uuid"},
    }


class FakeClient:
    def __init__(self, state="In Progress"):
        self.current = issue(state)
        self.related = {}
        self.comments = []
        self.children = []
        self.issue_relations = []
        self.writes = []
        self.projects = [
            {
                "id": "project-uuid",
                "name": "Current Project",
                "teams": {"nodes": [{"id": "team-uuid"}]},
            },
            {
                "id": "project-two",
                "name": "Project Two",
                "teams": {"nodes": [{"id": "team-uuid"}]},
            },
        ]
        self.milestones = {
            "project-uuid": [
                {
                    "id": "milestone-uuid",
                    "name": "Current Milestone",
                    "project": {"id": "project-uuid"},
                }
            ],
            "project-two": [
                {
                    "id": "milestone-two",
                    "name": "Milestone Two",
                    "project": {"id": "project-two"},
                }
            ],
        }

    def get_issue(self, identifier):
        related = next(
            (
                item
                for item in self.related.values()
                if item.get("identifier") == identifier or item.get("id") == identifier
            ),
            None,
        )
        if related is not None:
            return json.loads(json.dumps(related))
        if identifier == "SIS-56":
            parent = issue("In Progress")
            parent["id"] = "parent-uuid"
            parent["identifier"] = "SIS-56"
            parent["title"] = "Parent"
            parent["url"] = "https://linear.app/example/issue/SIS-56"
            return parent
        if identifier != "SIS-59":
            return next(
                (
                    json.loads(json.dumps(item))
                    for item in self.children
                    if item["identifier"] == identifier or item["id"] == identifier
                ),
                None,
            )
        return json.loads(json.dumps(self.current))

    def list_states(self, team_id):
        return [
            {"id": "state-Todo", "name": "Todo", "type": "unstarted"},
            {"id": "state-In Review", "name": "In Review", "type": "started"},
        ]

    def update_issue_state(self, issue_id, state_id):
        self.writes.append(("state", issue_id, state_id))
        name = state_id.removeprefix("state-")
        self.current["state"] = {"id": state_id, "name": name, "type": "started"}

    def update_issue_fields(self, issue_id, **fields):
        self.writes.append(("fields", issue_id, fields))
        if "title" in fields:
            self.current["title"] = fields["title"]
        if "assignee_id" in fields:
            assignee_id = fields["assignee_id"]
            self.current["assignee"] = (
                None
                if assignee_id is None
                else next(user for user in self.list_users() if user["id"] == assignee_id)
            )
        if "label_ids" in fields:
            by_id = {label["id"]: label for label in self.list_issue_labels("team-uuid")}
            self.current["labels"] = {
                "nodes": [by_id[label_id] for label_id in fields["label_ids"]]
            }
        if "description" in fields:
            self.current["description"] = fields["description"]
        if "priority" in fields:
            self.current["priority"] = fields["priority"]
        if "state_id" in fields:
            state_id = fields["state_id"]
            self.current["state"] = {
                "id": state_id,
                "name": state_id.removeprefix("state-"),
                "type": "started",
            }
        if "due_date" in fields:
            self.current["dueDate"] = fields["due_date"]
        if "estimate" in fields:
            self.current["estimate"] = fields["estimate"]
        if "parent_id" in fields:
            parent_id = fields["parent_id"]
            parent = self.related.get(parent_id)
            self.current["parent"] = (
                None
                if parent is None
                else {"id": parent["id"], "identifier": parent["identifier"]}
            )
        if "project_id" in fields:
            self.current["project"] = (
                None if fields["project_id"] is None else {"id": fields["project_id"]}
            )
        if "milestone_id" in fields:
            self.current["projectMilestone"] = (
                None
                if fields["milestone_id"] is None
                else {"id": fields["milestone_id"]}
            )

    def list_team_projects(self, team_id):
        return json.loads(json.dumps(self.projects))

    def list_project_milestones(self, project_id):
        return json.loads(json.dumps(self.milestones.get(project_id, [])))

    def list_users(self):
        return [
            {
                "id": "user-alexey",
                "name": "Alexey Petrov",
                "email": "alexey@example.com",
            }
        ]

    def list_issue_labels(self, team_id):
        return [
            {"id": "label-linear", "name": "area:linear"},
            {"id": "label-owner", "name": "priority:owner"},
        ]

    def list_comments(self, issue_id):
        return list(self.comments)

    def get_comment(self, comment_id):
        return next((item for item in self.comments if item["id"] == comment_id), None)

    def create_comment(self, issue_id, comment_id, body):
        self.writes.append(("comment", issue_id, comment_id, body))
        self.comments.append({"id": comment_id, "issueId": issue_id, "body": body})

    def list_child_issues(self, parent_id):
        self.assert_parent_id = parent_id
        return json.loads(json.dumps(self.children))

    def create_issue(self, *, issue_id, team_id, state_id, parent_id, title, description, priority):
        self.writes.append(
            (
                "create_issue",
                issue_id,
                team_id,
                state_id,
                parent_id,
                title,
                description,
                priority,
            )
        )
        created = issue(state_id.removeprefix("state-"))
        created.update(
            {
                "id": issue_id,
                "identifier": "SIS-99",
                "title": title,
                "url": "https://linear.app/example/issue/SIS-99",
                "description": description,
                "priority": priority,
                "parent": {"id": parent_id, "identifier": "SIS-56"},
            }
        )
        self.children.append(created)

    def list_issue_relations(self, identifier):
        return json.loads(json.dumps(self.issue_relations))

    def get_issue_relation(self, relation_id):
        return next(
            (
                json.loads(json.dumps(item))
                for item in self.issue_relations
                if item["id"] == relation_id
            ),
            None,
        )

    def create_issue_relation(
        self, *, relation_id, issue_id, related_issue_id, relation_type
    ):
        self.writes.append(
            (
                "create_issue_relation",
                relation_id,
                issue_id,
                related_issue_id,
                relation_type,
            )
        )
        by_id = {
            item["id"]: item
            for item in (self.get_issue("SIS-59"), self.get_issue("SIS-56"))
        }
        self.issue_relations.append(
            {
                "id": relation_id,
                "type": relation_type,
                "issue": {
                    "id": issue_id,
                    "identifier": by_id[issue_id]["identifier"],
                },
                "relatedIssue": {
                    "id": related_issue_id,
                    "identifier": by_id[related_issue_id]["identifier"],
                },
            }
        )

    def delete_issue_relation(self, relation_id):
        self.writes.append(("delete_issue_relation", relation_id))
        self.issue_relations = [
            item for item in self.issue_relations if item["id"] != relation_id
        ]


class FakeHierarchyClient:
    def __init__(self):
        self.projects = []
        self.milestones = []
        self.issues = []
        self.calls = []

    def list_teams(self):
        self.calls.append(("list_teams",))
        return [{"id": "team-sis", "key": "SIS", "name": "Sisyphus"}]

    def list_team_projects(self, team_id):
        self.calls.append(("list_team_projects", team_id))
        return json.loads(json.dumps(self.projects))

    def list_states(self, team_id):
        self.calls.append(("list_states", team_id))
        return [{"id": "state-Todo", "name": "Todo", "type": "unstarted"}]

    def list_project_milestones(self, project_id):
        self.calls.append(("list_project_milestones", project_id))
        return json.loads(json.dumps(self.milestones))

    def list_project_issues(self, project_id):
        self.calls.append(("list_project_issues", project_id))
        return json.loads(json.dumps(self.issues))

    def create_project(self, *, project_id, team_id, name, description=mock.ANY):
        self.calls.append(("create_project", project_id, team_id, name, description))
        project = {"id": project_id, "name": name, "teams": {"nodes": [{"id": team_id}]}}
        if description is not mock.ANY:
            project["description"] = description
        self.projects.append(project)

    def create_project_milestone(self, *, milestone_id, project_id, name, description=mock.ANY):
        self.calls.append(
            ("create_project_milestone", milestone_id, project_id, name, description)
        )
        milestone = {"id": milestone_id, "name": name, "project": {"id": project_id}}
        if description is not mock.ANY:
            milestone["description"] = description
        self.milestones.append(milestone)

    def create_project_issue(
        self,
        *,
        issue_id,
        team_id,
        project_id,
        milestone_id,
        title,
        state_id=None,
        priority=None,
        description=mock.ANY,
    ):
        self.calls.append(("create_project_issue", issue_id))
        created = {
            "id": issue_id,
            "identifier": "SIS-100",
            "title": title,
            "url": "https://linear.app/example/issue/SIS-100",
            "team": {"id": team_id, "key": "SIS"},
            "project": {"id": project_id},
            "projectMilestone": {"id": milestone_id},
            "state": {"id": state_id or "state-Todo", "name": "Todo"},
            "parent": None,
            "priority": priority,
        }
        if description is not mock.ANY:
            created["description"] = description
        self.issues.append(created)


class FakeIssueTreeClient(FakeHierarchyClient):
    def __init__(self, *, project_name="Wardrobe & Style", milestone_name="Autumn 2026"):
        super().__init__()
        self.projects = [
            {
                "id": "project-existing",
                "name": project_name,
                "description": (
                    "Wardrobe project"
                    if project_name == "Wardrobe & Style"
                    else "Reading project"
                ),
                "teams": {"nodes": [{"id": "team-sis"}]},
            }
        ]
        self.milestones = [
            {
                "id": "milestone-existing",
                "name": milestone_name,
                "description": (
                    "Autumn milestone"
                    if milestone_name == "Autumn 2026"
                    else "English literature"
                ),
                "project": {"id": "project-existing"},
            }
        ]
        self.child_counter = 0

    def list_child_issues(self, parent_identifier):
        self.calls.append(("list_child_issues", parent_identifier))
        return json.loads(
            json.dumps(
                [
                    item
                    for item in self.issues
                    if isinstance(item.get("parent"), dict)
                    and item["parent"].get("identifier") == parent_identifier
                ]
            )
        )

    def list_team_issues_by_title(self, team_id, title):
        self.calls.append(("list_team_issues_by_title", team_id, title))
        return json.loads(
            json.dumps([item for item in self.issues if item.get("title") == title])
        )

    def create_issue(
        self,
        *,
        issue_id,
        team_id,
        state_id,
        parent_id,
        title,
        description,
        priority,
    ):
        self.child_counter += 1
        parent = next(item for item in self.issues if item["id"] == parent_id)
        self.calls.append(("create_issue", issue_id, parent_id))
        identifier = f"SIS-{200 + self.child_counter}"
        self.issues.append(
            {
                "id": issue_id,
                "identifier": identifier,
                "title": title,
                "url": f"https://linear.app/example/issue/{identifier}",
                "description": description,
                "priority": priority,
                "state": {"id": state_id, "name": state_id.removeprefix("state-")},
                "team": {"id": team_id, "key": "SIS"},
                "project": {"id": parent["project"]["id"]},
                "projectMilestone": {"id": parent["projectMilestone"]["id"]},
                "parent": {"id": parent_id, "identifier": parent["identifier"]},
            }
        )

    def update_scoped_issue(self, issue_id, **changes):
        self.calls.append(("update_scoped_issue", issue_id, dict(changes)))
        current = next(item for item in self.issues if item["id"] == issue_id)
        if "description" in changes:
            current["description"] = changes["description"]
        if "state_id" in changes:
            current["state"] = {
                "id": changes["state_id"],
                "name": changes["state_id"].removeprefix("state-"),
            }
        if "priority" in changes:
            current["priority"] = changes["priority"]
        if "parent_id" in changes:
            parent_id = changes["parent_id"]
            if parent_id is None:
                current["parent"] = None
            else:
                parent = next(item for item in self.issues if item["id"] == parent_id)
                current["parent"] = {
                    "id": parent_id,
                    "identifier": parent["identifier"],
                }
        if "project_id" in changes:
            current["project"] = {"id": changes["project_id"]}
        if "milestone_id" in changes:
            current["projectMilestone"] = {"id": changes["milestone_id"]}


class ContractTests(unittest.TestCase):
    def test_accepts_exact_workspace_read_contracts(self):
        inventory = lane.validate_command(
            workspace_read_command(entity_types=["issues", "initiatives"])
        )
        search = lane.validate_command(
            workspace_read_command(
                "search_linear",
                entity_types=["projects", "milestones"],
                query="Straße",
            )
        )
        self.assertEqual(
            inventory["target"], {"type": "workspace", "identifier": "current"}
        )
        self.assertEqual(search["change"]["query"], "Straße")

    def test_workspace_read_contracts_reject_unbounded_or_implicit_inputs(self):
        cases = []
        for entity_types in (
            [],
            ["issues", "issues"],
            [["issues"]],
            [None],
            ["users"],
        ):
            cases.append(workspace_read_command(entity_types=entity_types))
        raw = workspace_read_command(entity_types=["issues"])
        raw["change"]["include_archived"] = 1
        cases.append(raw)
        for query in ("", "   "):
            cases.append(
                workspace_read_command(
                    "search_linear", entity_types=["issues"], query=query
                )
            )
        raw = workspace_read_command("search_linear", entity_types=["issues"])
        raw["change"]["graphql"] = "query Workspace"
        cases.append(raw)
        for raw in cases:
            with self.subTest(change=raw["change"]), self.assertRaises(
                lane.ContractError
            ):
                lane.validate_command(raw)

    def test_accepts_exact_bounded_issue_relation_command(self):
        validated = lane.validate_command(relation_command())

        self.assertEqual(validated["operation"], "create_issue_relation")
        self.assertEqual(
            validated["change"],
            {"related_identifier": "SIS-56", "relation_type": "blocked_by"},
        )

    def test_relation_removal_and_replacement_require_owner_policy_and_exact_changes(self):
        for operation in ("remove_issue_relation", "replace_issue_relation"):
            with self.subTest(operation=operation):
                approved = relation_change_command(operation)
                self.assertEqual(
                    lane.validate_command(approved)["policy"]["mode"],
                    "owner_approved",
                )
                standard = json.loads(json.dumps(approved))
                standard["policy"] = {"mode": "standard"}
                with self.assertRaisesRegex(lane.ContractError, "owner_approved"):
                    lane.validate_command(standard)

        forged = relation_change_command("replace_issue_relation")
        forged["change"]["relation_id"] = "raw-id"
        with self.assertRaises(lane.ContractError):
            lane.validate_command(forged)

    def test_hierarchy_malformed_payloads_fail_before_any_preflight_or_write(self):
        malformed = hierarchy_command()
        malformed["change"]["issue"]["id"] = "caller-controlled-id"
        client = mock.Mock()
        with self.assertRaisesRegex(lane.ContractError, "unsupported fields"):
            lane.execute_command(client, malformed, mode="plan")
        client.assert_not_called()
        self.assertEqual(client.method_calls, [])

    def test_accepts_exact_v2_read_command(self):
        validated = lane.validate_command(command())
        self.assertEqual(validated["schema_version"], "linear-command.v2")
        self.assertEqual(validated["target"]["identifier"], "SIS-59")
        self.assertEqual(validated["operation"], "read_issue")

    def test_rejects_noncurrent_command_schema_before_linear_access(self):
        unsupported = command()
        unsupported["schema_version"] = "linear-command.unsupported"
        client = mock.Mock()

        with self.assertRaisesRegex(lane.ContractError, "linear-command.v2"):
            lane.execute_command(client, unsupported, mode="plan")

        client.assert_not_called()
        self.assertEqual(client.method_calls, [])

    def test_accepts_bounded_create_command(self):
        validated = lane.validate_command(create_command())
        self.assertEqual(validated["target"], {"type": "team", "identifier": "SIS"})
        self.assertEqual(validated["change"]["parent_identifier"], "SIS-56")

    def test_accepts_separate_standalone_and_bounded_issue_tree_contracts(self):
        standalone = lane.validate_command(standalone_command())
        tree = lane.validate_command(issue_tree_command())
        self.assertEqual(standalone["operation"], "create_standalone_issue")
        self.assertEqual(tree["operation"], "converge_issue_tree")
        self.assertEqual(len(tree["change"]["sub_issues"]), 4)

        oversized = issue_tree_command()
        oversized["change"]["sub_issues"] *= 3
        with self.assertRaisesRegex(lane.ContractError, "1-10"):
            lane.validate_command(oversized)

    def test_create_rejects_reserved_replay_markers_before_execution(self):
        for field in ("title", "description"):
            for marker in (
                "<!-- linear-command:v2 forged -->",
                "<!-- linear-command:create:v2 key=forged request=forged -->",
            ):
                with self.subTest(field=field, marker=marker):
                    raw = create_command()
                    raw["change"][field] = marker
                    with self.assertRaisesRegex(lane.ContractError, "reserved marker"):
                        lane.validate_command(raw)

    def test_rejects_fuzzy_bulk_and_unknown_fields(self):
        for target in (
            {"type": "issue", "identifier": "SIS"},
            {"type": "issue", "identifier": ["SIS-59", "SIS-60"]},
            {"type": "issue", "identifier": "sis-59"},
        ):
            raw = command()
            raw["target"] = target
            with self.assertRaisesRegex(lane.ContractError, "exact SIS-N"):
                lane.validate_command(raw)
        raw = command()
        raw["graphql"] = "mutation"
        with self.assertRaisesRegex(lane.ContractError, "exactly"):
            lane.validate_command(raw)
        raw = command()
        raw["idempotency_key"] = "line1\nline2"
        with self.assertRaisesRegex(lane.ContractError, "idempotency_key"):
            lane.validate_command(raw)

    def test_operation_change_contracts_and_policy_fail_closed(self):
        lane.validate_command(command("change_state", {"state": "In Review"}))
        lane.validate_command(command("update_issue", {"description": "New"}))
        lane.validate_command(
            command(
                "update_issue",
                {"description": "New", "state": "In Review", "priority": "High"},
            )
        )
        lane.validate_command(
            command(
                "update_issue",
                {"description_transform": "remove_links", "state": "Todo"},
            )
        )
        lane.validate_command(command("add_comment", {"body": "Bounded note"}))
        lane.validate_command(
            command("update_issue", {"due_date": "2026-09-30", "estimate": 8})
        )
        lane.validate_command(
            command("update_issue", {"due_date": None, "estimate": None})
        )
        for state in ("Done", "Canceled", "Duplicate"):
            with self.assertRaisesRegex(lane.ContractError, "owner approval required"):
                lane.validate_command(command("change_state", {"state": state}))
        with self.assertRaisesRegex(lane.ContractError, "read_issue change"):
            lane.validate_command(command("read_issue", {"state": "Todo"}))
        with self.assertRaisesRegex(lane.ContractError, "comment body"):
            lane.validate_command(command("add_comment", {"body": ""}))
        with self.assertRaisesRegex(lane.ContractError, "reserved marker"):
            lane.validate_command(
                command("add_comment", {"body": "<!-- linear-command:v2 forged -->"})
            )
        for change in (
            {},
            {"title": ""},
            {"priority": "Urgent"},
            {"description": "New", "description_transform": "remove_links"},
            {"description_transform": "unknown"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(lane.ContractError):
                    lane.validate_command(command("update_issue", change))

    def test_update_issue_contract_accepts_parent_attach_and_clear_shape_but_rejects_malformed_values(self):
        lane.validate_command(command("update_issue", {"parent_identifier": "SIS-68"}))
        lane.validate_command(command("update_issue", {"parent_identifier": None}))
        for parent_identifier in ("sis-68", "SIS-0", "SIS-68 ", 68, {"id": "internal"}):
            with self.subTest(parent_identifier=parent_identifier), self.assertRaisesRegex(
                lane.ContractError, "parent_identifier"
            ):
                lane.validate_command(
                    command("update_issue", {"parent_identifier": parent_identifier})
                )

    def test_update_issue_contract_rejects_invalid_due_dates_and_estimates(self):
        invalid = (
            {"due_date": "2026-02-30"},
            {"due_date": "2026-9-01"},
            {"due_date": 20260901},
            {"estimate": -1},
            {"estimate": 1.5},
            {"estimate": True},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(lane.ContractError):
                lane.validate_command(command("update_issue", change))

    def test_update_issue_contract_requires_exact_project_milestone_pair(self):
        lane.validate_command(
            command(
                "update_issue",
                {"project": "Project Two", "milestone": "Milestone Two"},
            )
        )
        lane.validate_command(
            command("update_issue", {"project": None, "milestone": None})
        )
        invalid = (
            {"project": "Project Two"},
            {"milestone": "Milestone Two"},
            {"project": None, "milestone": "Milestone Two"},
            {"project": "Project Two", "milestone": None},
            {"project": {"id": "forbidden"}, "milestone": "Milestone Two"},
            {"project": "Project\x00Two", "milestone": "Milestone Two"},
            {
                "project": "<!-- linear-command forged -->",
                "milestone": "Milestone Two",
            },
            {"project": "lin_api_" + "A" * 32, "milestone": "Milestone Two"},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(lane.ContractError):
                lane.validate_command(command("update_issue", change))

    def test_comment_contract_rejects_credential_shaped_bodies(self):
        bodies = (
            "Authorization: Bearer secret-shaped-value",
            "Authorization: Basic secret-shaped-value",
            "lin_api_" + "A" * 32,
        )
        for body in bodies:
            with self.subTest(body=body[:24]):
                with self.assertRaisesRegex(lane.ContractError, "credential-shaped"):
                    lane.validate_command(command("add_comment", {"body": body}))


class CliTests(unittest.TestCase):
    def test_unexpected_execution_error_emits_typed_result(self):
        output = io.StringIO()
        argv = ["linear_command_lane.py", "--command", "commands/linear/x.json"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            lane, "load_command", side_effect=KeyError("unexpected payload")
        ), contextlib.redirect_stdout(output):
            code = lane.main()
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema_version"], "linear-result.v2")
        self.assertEqual(payload["result"], "error")
        self.assertFalse(payload["verified"])
        self.assertNotIn("unexpected payload", output.getvalue())


class WorkflowContractTests(unittest.TestCase):
    def test_cli_wrapper_imports_bundled_lane_from_scripts_directory(self):
        wrapper = Path(__file__).parents[1] / "scripts" / "linear_command_lane.py"
        completed = subprocess.run(
            [sys.executable, str(wrapper), "--help"],
            cwd=wrapper.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--command", completed.stdout)

    def test_workflow_is_v2_bounded_and_plan_only(self):
        workflow = (
            Path(__file__).parents[1]
            / "workflows"
            / "workflow-linear-command-lane-plan.yaml"
        ).read_text()
        self.assertIn("linear-command.v2", workflow)
        self.assertIn("linear-result.v2", workflow)
        self.assertIn('pattern: "^[a-z0-9][a-z0-9-]{0,62}$"', workflow)
        self.assertIn("commands/linear/${{ inputs.command }}.json", workflow)
        self.assertIn("--mode plan", workflow)
        self.assertNotIn("inputs.mode", workflow)
        self.assertNotIn("--mode apply", workflow)

    def test_active_protocol_artifacts_contain_only_current_contract(self):
        root = Path(__file__).parents[1]
        paths = [
            root / "README.md",
            root / "docs" / "linear-command-lane.md",
            root / "docs" / "universal-linear-routing-e2e.md",
            root / "skills" / "linear-source-request-routing" / "SKILL.md",
            root / "skills" / "project-manager-linear-worker" / "SKILL.md",
            root / "plugins" / "linear_source_route" / "__init__.py",
            root / "plugins" / "linear_source_route" / "route.py",
            root / "plugins" / "project_manager_linear" / "__init__.py",
        ]
        forbidden = (
            f"linear-command.v{1}",
            f"linear-kanban-task.v{1}",
            f"linear-result.v{1}",
            f"linear:v{1}:",
            f"linear-command:v{1}",
            f"linear-command:create:v{1}",
        )
        contents = []
        for path in paths:
            with self.subTest(path=path.name):
                content = path.read_text()
                contents.append(content)
                for token in forbidden:
                    self.assertNotIn(token, content)
        self.assertTrue(any("linear-command.v2" in content for content in contents))
        self.assertTrue(any("linear-result.v2" in content for content in contents))

    def test_command_loader_rejects_paths_outside_allowlisted_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "commands" / "linear"
            root.mkdir(parents=True)
            inside = root / "valid.json"
            inside.write_text(json.dumps(command()))
            outside = Path(tmp) / "outside.json"
            outside.write_text(json.dumps(command()))
            self.assertEqual(
                lane.load_command(inside, allowed_root=root)["operation"],
                "read_issue",
            )
            with self.assertRaisesRegex(lane.ContractError, "allowlisted command root"):
                lane.load_command(outside, allowed_root=root)


class ClientTests(unittest.TestCase):
    def test_issue_relation_delete_uses_only_fixed_graphql_document_and_exact_id(self):
        client = object.__new__(lane.LinearClient)
        calls = []

        def execute(query, variables=None):
            calls.append((query, variables))
            return {"issueRelationDelete": {"success": True}}

        client.execute = execute
        client.delete_issue_relation("relation-exact")
        self.assertEqual(
            calls,
            [(lane.ISSUE_RELATION_DELETE, {"id": "relation-exact"})],
        )
        self.assertIn("issueRelationDelete(id: $id)", lane.ISSUE_RELATION_DELETE)

    def test_workspace_core_reads_paginate_to_exhaustion_with_fixed_queries(self):
        client = object.__new__(lane.LinearClient)
        calls = []
        query_by_type = {
            "issues": lane.WORKSPACE_ISSUES_QUERY,
            "projects": lane.WORKSPACE_PROJECTS_QUERY,
            "milestones": lane.WORKSPACE_MILESTONES_QUERY,
            "initiatives": lane.WORKSPACE_INITIATIVES_QUERY,
        }

        def execute(query, variables=None):
            variables = variables or {}
            calls.append((query, dict(variables)))
            entity_type = next(
                kind for kind, fixed_query in query_by_type.items() if query == fixed_query
            )
            after = variables["after"]
            nodes = (
                [{"id": f"{entity_type}-1", "name": "first"}]
                if after is None
                else [{"id": f"{entity_type}-2", "name": "second"}]
            )
            return {
                lane.WORKSPACE_CONNECTION_FIELDS[entity_type]: {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": after is None,
                        "endCursor": "cursor-1" if after is None else None,
                    },
                }
            }

        client.execute = execute
        for entity_type in lane.LINEAR_ENTITY_TYPES:
            with self.subTest(entity_type=entity_type):
                result = client.list_linear_entities(entity_type, include_archived=True)
                self.assertEqual(len(result), 2)
                entity_calls = [item for item in calls if item[0] == query_by_type[entity_type]]
                self.assertEqual(
                    [item[1] for item in entity_calls],
                    [
                        {"after": None, "includeArchived": True},
                        {"after": "cursor-1", "includeArchived": True},
                    ],
                )
                self.assertIn("first: 100", query_by_type[entity_type])
                self.assertIn("after: $after", query_by_type[entity_type])

    def test_workspace_pagination_rejects_duplicate_nodes_and_repeated_cursors(self):
        for duplicate in (True, False):
            client = object.__new__(lane.LinearClient)

            def execute(_query, variables=None, *, duplicate=duplicate):
                after = (variables or {}).get("after")
                return {
                    "issues": {
                        "nodes": [
                            {
                                "id": "same" if duplicate or after is None else "second",
                                "identifier": "SIS-1" if after is None else "SIS-2",
                            }
                        ],
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "cursor-1",
                        },
                    }
                }

            client.execute = execute
            expected = "duplicate" if duplicate else "cursor"
            with self.subTest(expected=expected), self.assertRaisesRegex(
                lane.ContractError, expected
            ):
                client.list_linear_entities("issues", include_archived=False)

    def test_direct_child_reader_uses_complete_cursor_pagination(self):
        client = object.__new__(lane.LinearClient)
        calls = []

        def execute(query, variables=None):
            self.assertEqual(query, lane.PARENT_CHILDREN_QUERY)
            calls.append(dict(variables or {}))
            after = variables["after"]
            start = 0 if after is None else 100
            size = 100 if after is None else 1
            return {
                "issue": {
                    "identifier": "SIS-1",
                    "children": {
                        "nodes": [
                            {"id": f"child-{index}", "identifier": f"SIS-{index + 2}"}
                            for index in range(start, start + size)
                        ],
                        "pageInfo": {
                            "hasNextPage": after is None,
                            "endCursor": "children-100" if after is None else None,
                        },
                    },
                }
            }

        client.execute = execute
        children = client.list_child_issues("SIS-1")
        self.assertEqual(len(children), 101)
        self.assertEqual(
            calls,
            [
                {"id": "SIS-1", "after": None},
                {"id": "SIS-1", "after": "children-100"},
            ],
        )

    class StubClient(lane.LinearClient):
        def __init__(self):
            self.authorization = "fixture"
            self.endpoint = "fixture"
            self.calls = []

        def execute(self, query, variables=None):
            self.calls.append((query, variables or {}))
            if query == lane.ISSUE_QUERY:
                return {"issue": issue()}
            if query == lane.TEAM_STATES_QUERY:
                return {"team": {"states": {"nodes": [{"id": "s", "name": "Todo", "type": "unstarted"}], "pageInfo": {"hasNextPage": False}}}}
            if query == lane.COMMENTS_QUERY:
                return {"issue": {"comments": {"nodes": [{"id": "c", "body": "body"}], "pageInfo": {"hasNextPage": False}}}}
            if query == lane.COMMENT_QUERY:
                return {"comment": {"id": variables["id"], "issueId": "issue-uuid", "body": "body"}}
            if query == lane.PARENT_CHILDREN_QUERY:
                return {
                    "issue": {
                        "identifier": variables["id"],
                        "children": {
                            "nodes": [{**issue(), "description": "marker"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            if query == lane.ISSUE_UPDATE:
                return {"issueUpdate": {"success": True}}
            if query == lane.COMMENT_CREATE:
                return {"commentCreate": {"success": True, "comment": variables["input"]}}
            if query == lane.ISSUE_CREATE:
                return {"issueCreate": {"success": True, "issue": {"id": "new", "identifier": "SIS-99"}}}
            if query == lane.PROJECT_ISSUE_CREATE:
                return {"issueCreate": {"success": True, "issue": {"id": "new", "identifier": "SIS-100"}}}
            raise AssertionError("unexpected query")

    def test_client_methods_use_fixed_graphql_shapes(self):
        client = self.StubClient()
        self.assertEqual(client.get_issue("SIS-59")["identifier"], "SIS-59")
        self.assertEqual(client.list_states("team-uuid")[0]["name"], "Todo")
        self.assertEqual(client.list_comments("issue-uuid")[0]["id"], "c")
        self.assertEqual(client.get_comment("comment-uuid")["id"], "comment-uuid")
        client.update_issue_state("issue-uuid", "state-uuid")
        client.create_comment("issue-uuid", "comment-uuid", "body")
        self.assertEqual(client.list_child_issues("SIS-56")[0]["description"], "marker")
        client.create_issue(
            issue_id="created-uuid",
            team_id="team-uuid",
            state_id="state-uuid",
            parent_id="parent-uuid",
            title="Created",
            description="Description",
            priority=2,
        )
        client.create_project_issue(
            issue_id="hierarchy-uuid",
            team_id="team-uuid",
            project_id="project-uuid",
            milestone_id="milestone-uuid",
            title="Hierarchy issue",
            state_id="state-uuid",
            description="Description",
        )
        calls = {query: variables for query, variables in client.calls}
        self.assertEqual(
            calls[lane.ISSUE_UPDATE],
            {"id": "issue-uuid", "input": {"stateId": "state-uuid"}},
        )
        self.assertEqual(
            calls[lane.COMMENT_CREATE],
            {"input": {"id": "comment-uuid", "issueId": "issue-uuid", "body": "body"}},
        )
        self.assertEqual(
            calls[lane.ISSUE_CREATE],
            {
                "input": {
                    "id": "created-uuid",
                    "teamId": "team-uuid",
                    "stateId": "state-uuid",
                    "parentId": "parent-uuid",
                    "title": "Created",
                    "description": "Description",
                    "priority": 2,
                }
            },
        )
        hierarchy_input = calls[lane.PROJECT_ISSUE_CREATE]["input"]
        self.assertEqual(hierarchy_input["stateId"], "state-uuid")
        self.assertNotIn("state_id", hierarchy_input)

    def test_client_issue_query_and_mutation_map_due_date_and_estimate(self):
        client = self.StubClient()
        client.get_issue("SIS-59")
        client.update_issue_fields(
            "issue-uuid",
            due_date=None,
            estimate=8,
        )
        self.assertIn("dueDate estimate", lane.ISSUE_QUERY)
        self.assertIn("project { id }", lane.ISSUE_QUERY)
        self.assertIn("projectMilestone { id }", lane.ISSUE_QUERY)
        self.assertEqual(
            client.calls[-1],
            (
                lane.ISSUE_UPDATE,
                {
                    "id": "issue-uuid",
                    "input": {"dueDate": None, "estimate": 8},
                },
            ),
        )

    def test_client_issue_reparent_emits_only_parent_id_including_null_clear(self):
        client = self.StubClient()
        client.update_issue_fields("issue-uuid", parent_id="parent-uuid")
        client.update_issue_fields("issue-uuid", parent_id=None)
        self.assertEqual(
            client.calls,
            [
                (
                    lane.ISSUE_UPDATE,
                    {"id": "issue-uuid", "input": {"parentId": "parent-uuid"}},
                ),
                (
                    lane.ISSUE_UPDATE,
                    {"id": "issue-uuid", "input": {"parentId": None}},
                ),
            ],
        )

    def test_initiative_inventory_avoids_nested_connection_complexity(self):
        self.assertNotIn("projects(", lane.INITIATIVES_QUERY)
        self.assertIn("initiative(id: $initiativeId)", lane.INITIATIVE_PROJECTS_QUERY)
        client = object.__new__(lane.LinearClient)
        calls = []

        def execute(query, variables=None):
            calls.append((query, variables))
            if query == lane.INITIATIVES_QUERY:
                return {
                    "initiatives": {
                        "nodes": [{"id": "initiative", "name": "Exact"}],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            return {
                "initiative": {
                    "projects": {
                        "nodes": [{"id": "project", "name": "Exact"}],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }

        client.execute = execute
        self.assertEqual(client.list_initiatives()[0]["id"], "initiative")
        self.assertEqual(
            client.list_initiative_projects("initiative")[0]["id"], "project"
        )
        self.assertEqual(calls[1][1], {"initiativeId": "initiative", "after": None})

    def test_client_issue_move_emits_only_exact_structural_ids(self):
        client = self.StubClient()
        client.update_issue_fields(
            "issue-uuid",
            project_id="project-two",
            milestone_id="milestone-two",
        )
        self.assertEqual(
            client.calls,
            [
                (
                    lane.ISSUE_UPDATE,
                    {
                        "id": "issue-uuid",
                        "input": {
                            "projectId": "project-two",
                            "projectMilestoneId": "milestone-two",
                        },
                    },
                )
            ],
        )
        client.calls.clear()
        client.update_issue_fields(
            "issue-uuid",
            project_id=None,
            milestone_id=None,
        )
        self.assertEqual(
            client.calls[0][1]["input"],
            {"projectId": None, "projectMilestoneId": None},
        )

    def test_client_project_issue_update_uses_fixed_managed_graphql_shape(self):
        client = self.StubClient()
        client.update_project_issue(
            "hierarchy-uuid",
            description="https://solomia.in.ua",
            state_id="state-uuid",
        )
        self.assertEqual(
            client.calls,
            [
                (
                    lane.ISSUE_UPDATE,
                    {
                        "id": "hierarchy-uuid",
                        "input": {
                            "description": "https://solomia.in.ua",
                            "stateId": "state-uuid",
                        },
                    },
                )
            ],
        )
        with self.assertRaisesRegex(lane.ContractError, "no managed fields"):
            client.update_project_issue("hierarchy-uuid")

    def test_client_issue_tree_queries_and_updates_exact_structural_fields(self):
        for field in (
            "priority",
            "parent { id identifier }",
            "project { id }",
            "projectMilestone { id }",
        ):
            with self.subTest(field=field):
                self.assertIn(field, lane.PARENT_CHILDREN_QUERY)
        client = self.StubClient()
        client.update_scoped_issue(
            "issue-uuid",
            description="new",
            state_id="state-Todo",
            priority=2,
            parent_id=None,
            project_id="project-uuid",
            milestone_id="milestone-uuid",
        )
        query, variables = client.calls[-1]
        self.assertEqual(query, lane.ISSUE_UPDATE)
        self.assertEqual(
            variables,
            {
                "id": "issue-uuid",
                "input": {
                    "description": "new",
                    "stateId": "state-Todo",
                    "priority": 2,
                    "parentId": None,
                    "projectId": "project-uuid",
                    "projectMilestoneId": "milestone-uuid",
                },
            },
        )

    def test_hierarchy_and_lane_share_validation_policy_constants(self):
        hierarchy = lane._load_hierarchy()
        self.assertIs(lane.SAFE_STATES, hierarchy.SAFE_STATES)
        self.assertIs(lane.CREDENTIAL_SHAPES, hierarchy.CREDENTIAL_SHAPES)
        self.assertEqual(lane.RESERVED_COMMENT_MARKER, f"{hierarchy.RESERVED_MARKER}:v2")

    def test_non_json_linear_response_becomes_contract_error(self):
        client = lane.LinearClient("fixture")
        with mock.patch.object(
            lane.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b"not-json"),
        ):
            with self.assertRaisesRegex(lane.ContractError, "valid JSON"):
                client.execute(lane.ISSUE_QUERY, {"id": "SIS-59"})


class ExecutionTests(unittest.TestCase):
    class WorkspaceReadClient:
        def __init__(self):
            self.calls = []
            self.data = {
                "issues": [
                    {
                        "id": "issue-internal-1",
                        "identifier": "SIS-9",
                        "title": "Straße rollout",
                        "description": "secret description",
                        "url": "https://linear.app/secret",
                        "archivedAt": None,
                        "state": {"name": "In Progress"},
                        "team": {"key": "SIS"},
                        "parent": {"identifier": "SIS-1"},
                        "project": {"name": "Hermes"},
                        "projectMilestone": {"name": "Read lane"},
                        "assignee": {"name": "Private", "email": "private@example.com"},
                    },
                    {
                        "id": "issue-internal-2",
                        "identifier": "SIS-10",
                        "title": "Other",
                        "archivedAt": "2026-01-01T00:00:00.000Z",
                        "state": {"name": "Todo"},
                        "team": {"key": "SIS"},
                        "parent": None,
                        "project": None,
                        "projectMilestone": None,
                    },
                ],
                "projects": [
                    {
                        "id": "project-internal",
                        "name": "Straße Program",
                        "archivedAt": None,
                        "teams": {"nodes": [{"key": "SIS"}]},
                        "description": "private",
                    }
                ],
                "milestones": [
                    {
                        "id": "milestone-internal",
                        "name": "Roadmap",
                        "archivedAt": None,
                        "project": {
                            "name": "Straße Program",
                            "teams": {"nodes": [{"key": "SIS"}]},
                        },
                    }
                ],
                "initiatives": [
                    {
                        "id": "initiative-internal",
                        "name": "STRASSE Initiative",
                        "archivedAt": None,
                    }
                ],
            }

        def list_linear_entities(self, entity_type, *, include_archived):
            self.calls.append((entity_type, include_archived))
            items = self.data[entity_type]
            if not include_archived:
                items = [item for item in items if item.get("archivedAt") is None]
            return json.loads(json.dumps(items))

    def test_inventory_read_returns_safe_hierarchy_counts_without_journal(self):
        client = self.WorkspaceReadClient()
        raw = workspace_read_command(
            entity_types=["issues", "projects", "milestones", "initiatives"],
            include_archived=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            result = lane.execute_command(client, raw, mode="apply", journal_path=journal)
            self.assertFalse(journal.exists())
        self.assertEqual(result["result"], "read")
        self.assertTrue(result["verified"])
        self.assertTrue(result["no_op"])
        self.assertEqual(result["after"]["counts"], {kind: len(client.data[kind]) for kind in lane.LINEAR_ENTITY_TYPES})
        issue_item = result["after"]["entities"]["issues"][0]
        self.assertEqual(
            issue_item,
            {
                "type": "issue",
                "identifier": "SIS-9",
                "title": "Straße rollout",
                "state": "In Progress",
                "team": "SIS",
                "parent_identifier": "SIS-1",
                "project": "Hermes",
                "milestone": "Read lane",
                "archived": False,
            },
        )
        serialized = json.dumps(result, ensure_ascii=False).lower()
        for forbidden in (
            "internal",
            "description",
            "https://",
            "private@example.com",
            "assignee",
            "user",
            "email",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_search_uses_unicode_casefold_substring_over_only_allowed_names(self):
        client = self.WorkspaceReadClient()
        result = lane.execute_command(
            client,
            workspace_read_command("search_linear", query="STRASSE"),
            mode="apply",
            journal_path=Path("/must/not/be/written.json"),
        )
        after = result["after"]
        self.assertEqual(after["query"], "STRASSE")
        self.assertEqual(
            after["counts"],
            {"issues": 1, "projects": 1, "milestones": 0, "initiatives": 1},
        )
        self.assertEqual(
            after["scanned_counts"],
            {"issues": 1, "projects": 1, "milestones": 1, "initiatives": 1},
        )
        self.assertEqual(after["entities"]["issues"][0]["identifier"], "SIS-9")
        self.assertEqual(after["entities"]["projects"][0]["name"], "Straße Program")
        self.assertEqual(after["entities"]["initiatives"][0]["name"], "STRASSE Initiative")
        self.assertEqual(after["entities"]["milestones"], [])

    def test_blocks_and_equivalent_blocked_by_share_canonical_relation_identity(self):
        blocks = relation_command("blocks", "linear:SIS:relation:blocks")
        blocked_by = relation_command(
            "blocked_by", "linear:SIS:relation:blocked-by"
        )
        blocked_by["target"]["identifier"] = "SIS-56"
        blocked_by["change"]["related_identifier"] = "SIS-59"
        relation_ids = []

        with tempfile.TemporaryDirectory() as tmp:
            for index, raw in enumerate((blocks, blocked_by)):
                client = FakeClient()
                lane.execute_command(
                    client,
                    raw,
                    mode="apply",
                    journal_path=Path(tmp) / f"{index}.json",
                )
                relation_ids.append(
                    next(
                        item[1]
                        for item in client.writes
                        if item[0] == "create_issue_relation"
                    )
                )

        self.assertEqual(relation_ids[0], relation_ids[1])

    def test_blocked_by_relation_maps_canonically_and_crash_replay_is_no_op(self):
        client = FakeClient()
        raw = relation_command()

        with tempfile.TemporaryDirectory() as tmp:
            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "first.json",
            )
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "crash-replay.json",
            )

        writes = [item for item in client.writes if item[0] == "create_issue_relation"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2:], ("parent-uuid", "issue-uuid", "blocks"))
        self.assertEqual(applied["result"], "applied")
        self.assertEqual(replay["result"], "no_op")
        self.assertEqual(
            replay["after"],
            {
                "identifier": "SIS-59",
                "related_identifier": "SIS-56",
                "relation_type": "blocked_by",
            },
        )
        self.assertNotIn("id", replay["after"])

    def test_related_relation_is_canonical_and_reverse_inventory_replays_noop(self):
        client = FakeClient()
        raw = relation_command("related", "linear:SIS:relation:related")
        with tempfile.TemporaryDirectory() as tmp:
            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "first.json",
            )
            relation = client.issue_relations[0]
            relation["issue"], relation["relatedIssue"] = (
                relation["relatedIssue"],
                relation["issue"],
            )
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "replay.json",
            )
        self.assertEqual(applied["result"], "applied")
        self.assertEqual(replay["result"], "no_op")
        self.assertEqual(
            [write[0] for write in client.writes].count("create_issue_relation"),
            1,
        )
        self.assertEqual(client.writes[0][-1], "related")

    def test_relation_change_plan_canonicalizes_blocked_by_and_related_symmetry(self):
        blocked = FakeClient()
        blocked.issue_relations = [
            {
                "id": "relation-old",
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        removal = lane.execute_command(
            blocked,
            relation_change_command("remove_issue_relation"),
            mode="plan",
        )
        self.assertEqual(
            removal["plan"],
            [
                {
                    "action": "remove_issue_relation",
                    "identifier": "SIS-59",
                    "related_identifier": "SIS-56",
                    "relation_type": "blocked_by",
                }
            ],
        )
        self.assertEqual(removal["before"]["inventory"][0]["id"], "relation-old")

        symmetric = FakeClient()
        symmetric.related["related-57"] = {
            **issue("Todo"),
            "id": "related-57",
            "identifier": "SIS-57",
            "team": {"id": "team-uuid", "key": "SIS"},
        }
        symmetric.issue_relations = [
            {
                "id": "relation-related",
                "type": "related",
                "issue": {"id": "related-57", "identifier": "SIS-57"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        replace = relation_change_command("replace_issue_relation")
        replace["change"] = {
            "old_related_identifier": "SIS-57",
            "old_relation_type": "related",
            "new_related_identifier": "SIS-56",
            "new_relation_type": "blocks",
        }
        planned = lane.execute_command(symmetric, replace, mode="plan")
        self.assertEqual(
            planned["plan"],
            [
                {
                    "action": "create_issue_relation",
                    "identifier": "SIS-59",
                    "related_identifier": "SIS-56",
                    "relation_type": "blocks",
                },
                {
                    "action": "remove_issue_relation",
                    "identifier": "SIS-59",
                    "related_identifier": "SIS-57",
                    "relation_type": "related",
                },
            ],
        )

    def test_relation_change_fails_on_zero_or_ambiguous_exact_old_and_ambiguous_new(self):
        missing = FakeClient()
        duplicate = FakeClient()
        duplicate.issue_relations = [
            {
                "id": relation_id,
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
            for relation_id in ("relation-a", "relation-b")
        ]
        for client, message in (
            (missing, "not found"),
            (duplicate, "ambiguous"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                lane.ContractError, message
            ):
                lane.execute_command(
                    client,
                    relation_change_command("remove_issue_relation"),
                    mode="plan",
                )

        new_duplicate = FakeClient()
        new_duplicate.issue_relations = [
            {
                "id": "relation-old",
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            },
            *[
                {
                    "id": relation_id,
                    "type": "related",
                    "issue": {"id": "issue-uuid", "identifier": "SIS-59"},
                    "relatedIssue": {
                        "id": "parent-uuid",
                        "identifier": "SIS-56",
                    },
                }
                for relation_id in ("new-a", "new-b")
            ],
        ]
        raw = relation_change_command("replace_issue_relation")
        raw["change"] = {
            "old_related_identifier": "SIS-56",
            "old_relation_type": "blocked_by",
            "new_related_identifier": "SIS-56",
            "new_relation_type": "related",
        }
        with self.assertRaisesRegex(lane.ContractError, "new issue relation is ambiguous"):
            lane.execute_command(new_duplicate, raw, mode="plan")

    def test_exact_relation_removal_deletes_once_reads_back_and_replays_without_duplicate(self):
        client = FakeClient()
        client.issue_relations = [
            {
                "id": "relation-old",
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        raw = relation_change_command("remove_issue_relation")
        authorization = consumed_owner_authorization(raw)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=journal,
                owner_approval_authorization=authorization,
            )
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=journal,
                owner_approval_authorization=authorization,
            )
        self.assertEqual(applied["result"], "applied")
        self.assertTrue(applied["verified"])
        self.assertEqual(replay["result"], "no_op")
        self.assertEqual(
            [write for write in client.writes if write[0] == "delete_issue_relation"],
            [("delete_issue_relation", "relation-old")],
        )
        self.assertNotIn('"id":', json.dumps(applied["after"]))

    def test_relation_removal_readback_drift_restores_deleted_relation_and_fails_closed(self):
        class DriftAfterRemoval(FakeClient):
            def delete_issue_relation(self, relation_id):
                super().delete_issue_relation(relation_id)
                if relation_id == "relation-old":
                    self.issue_relations.append(
                        {
                            "id": "external-drift",
                            "type": "blocks",
                            "issue": {"id": "issue-uuid", "identifier": "SIS-59"},
                            "relatedIssue": {
                                "id": "external-uuid",
                                "identifier": "SIS-88",
                            },
                        }
                    )

        client = DriftAfterRemoval()
        client.issue_relations = [
            {
                "id": "relation-old",
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        raw = relation_change_command("remove_issue_relation")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            lane.ContractError, "compensation failed closed"
        ):
            lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
                owner_approval_authorization=consumed_owner_authorization(raw),
            )
        self.assertIn("relation-old", {item["id"] for item in client.issue_relations})

    def test_relation_removal_recovers_after_crash_between_delete_and_completion_journal(self):
        class CrashAfterDelete(FakeClient):
            crashed = False

            def delete_issue_relation(self, relation_id):
                super().delete_issue_relation(relation_id)
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process death")

        client = CrashAfterDelete()
        client.issue_relations = [
            {
                "id": "relation-old",
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        raw = relation_change_command("remove_issue_relation")
        authorization = consumed_owner_authorization(raw)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            with self.assertRaises(KeyboardInterrupt):
                lane.execute_command(
                    client,
                    raw,
                    mode="apply",
                    journal_path=journal,
                    owner_approval_authorization=authorization,
                )
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=journal,
                owner_approval_authorization=authorization,
            )
        self.assertEqual(replay["result"], "no_op")
        self.assertTrue(replay["verified"])
        self.assertEqual(
            [item[0] for item in client.writes], ["delete_issue_relation"]
        )

    def test_exact_relation_replacement_creates_then_deletes_and_literal_replay_is_noop(self):
        client = FakeClient()
        client.issue_relations = [
            {
                "id": "relation-old",
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        raw = relation_change_command("replace_issue_relation")
        raw["change"] = {
            "old_related_identifier": "SIS-56",
            "old_relation_type": "blocked_by",
            "new_related_identifier": "SIS-56",
            "new_relation_type": "related",
        }
        authorization = consumed_owner_authorization(raw)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=journal,
                owner_approval_authorization=authorization,
            )
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=journal,
                owner_approval_authorization=authorization,
            )
        writes = [item[0] for item in client.writes]
        self.assertEqual(
            writes,
            ["create_issue_relation", "delete_issue_relation"],
        )
        self.assertEqual(applied["result"], "applied")
        self.assertEqual(replay["result"], "no_op")
        self.assertEqual(len(client.issue_relations), 1)
        self.assertEqual(client.issue_relations[0]["type"], "related")
        self.assertNotIn('"id":', json.dumps(applied["after"]))

    def test_relation_replacement_recovers_after_crash_without_duplicate(self):
        class CrashAfterOldDelete(FakeClient):
            crashed = False

            def delete_issue_relation(self, relation_id):
                super().delete_issue_relation(relation_id)
                if relation_id == "relation-old" and not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process death")

        client = CrashAfterOldDelete()
        client.issue_relations = [
            {
                "id": "relation-old",
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        raw = relation_change_command("replace_issue_relation")
        raw["change"] = {
            "old_related_identifier": "SIS-56",
            "old_relation_type": "blocked_by",
            "new_related_identifier": "SIS-56",
            "new_relation_type": "related",
        }
        authorization = consumed_owner_authorization(raw)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            with self.assertRaises(KeyboardInterrupt):
                lane.execute_command(
                    client,
                    raw,
                    mode="apply",
                    journal_path=journal,
                    owner_approval_authorization=authorization,
                )
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=journal,
                owner_approval_authorization=authorization,
            )
        self.assertEqual(replay["result"], "no_op")
        self.assertEqual(
            [item[0] for item in client.writes],
            ["create_issue_relation", "delete_issue_relation"],
        )
        self.assertEqual(len(client.issue_relations), 1)

    def test_relation_replacement_delete_failure_removes_created_relation_and_restores_before_state(self):
        class RejectOldDelete(FakeClient):
            def delete_issue_relation(self, relation_id):
                if relation_id == "relation-old":
                    self.writes.append(("delete_issue_relation", relation_id))
                    raise RuntimeError("delete rejected")
                super().delete_issue_relation(relation_id)

        client = RejectOldDelete()
        original = {
            "id": "relation-old",
            "type": "blocks",
            "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
            "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
        }
        client.issue_relations = [copy.deepcopy(original)]
        raw = relation_change_command("replace_issue_relation")
        raw["change"] = {
            "old_related_identifier": "SIS-56",
            "old_relation_type": "blocked_by",
            "new_related_identifier": "SIS-56",
            "new_relation_type": "related",
        }
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            lane.ContractError, "was compensated"
        ):
            lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
                owner_approval_authorization=consumed_owner_authorization(raw),
            )
        self.assertEqual(client.issue_relations, [original])
        self.assertEqual(
            [item[0] for item in client.writes],
            [
                "create_issue_relation",
                "delete_issue_relation",
                "delete_issue_relation",
            ],
        )

    def test_relation_replacement_readback_drift_compensates_owned_mutations_and_fails_closed(self):
        class DriftAfterDelete(FakeClient):
            def delete_issue_relation(self, relation_id):
                super().delete_issue_relation(relation_id)
                if relation_id == "relation-old":
                    self.issue_relations.append(
                        {
                            "id": "external-drift",
                            "type": "blocks",
                            "issue": {"id": "issue-uuid", "identifier": "SIS-59"},
                            "relatedIssue": {
                                "id": "external-uuid",
                                "identifier": "SIS-88",
                            },
                        }
                    )

        client = DriftAfterDelete()
        client.issue_relations = [
            {
                "id": "relation-old",
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        raw = relation_change_command("replace_issue_relation")
        raw["change"] = {
            "old_related_identifier": "SIS-56",
            "old_relation_type": "blocked_by",
            "new_related_identifier": "SIS-56",
            "new_relation_type": "related",
        }
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            lane.ContractError, "compensation failed closed"
        ):
            lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
                owner_approval_authorization=consumed_owner_authorization(raw),
            )
        by_id = {item["id"]: item for item in client.issue_relations}
        self.assertIn("relation-old", by_id)
        self.assertNotIn(
            next(
                write[1]
                for write in client.writes
                if write[0] == "create_issue_relation"
            ),
            by_id,
        )

    def test_issue_relation_rejects_missing_or_wrong_team_related_issue(self):
        missing = FakeClient()
        missing_raw = relation_command("blocks", "linear:SIS:relation:missing")
        missing_raw["change"]["related_identifier"] = "SIS-999"

        wrong_team = FakeClient()
        wrong = issue("Todo")
        wrong.update(
            {
                "id": "wrong-team-uuid",
                "identifier": "SIS-99",
                "team": {"id": "other-team", "key": "OTHER"},
            }
        )
        wrong_team.related[wrong["id"]] = wrong
        wrong_raw = relation_command("blocks", "linear:SIS:relation:wrong-team")
        wrong_raw["change"]["related_identifier"] = "SIS-99"

        for client, raw, message in (
            (missing, missing_raw, "exact related Linear issue not found"),
            (wrong_team, wrong_raw, "related target is not in the SIS team"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                lane.ContractError, message
            ):
                lane.execute_command(client, raw, mode="plan")
            self.assertEqual(client.writes, [])

    def test_issue_relation_fails_closed_on_readback_drift(self):
        class DriftingRelationClient(FakeClient):
            def get_issue_relation(self, relation_id):
                relation = super().get_issue_relation(relation_id)
                if relation is not None:
                    relation["type"] = "related"
                return relation

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            lane.ContractError,
            "read-back verification failed",
        ):
            lane.execute_command(
                DriftingRelationClient(),
                relation_command("blocks", "linear:SIS:relation:drift"),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )

    def test_standalone_issue_reuses_exact_scope_and_replays_without_parent(self):
        class CanonicalizingClient(FakeIssueTreeClient):
            def create_project_issue(self, **kwargs):
                super().create_project_issue(**kwargs)
                desired = kwargs["description"]
                url = "https://example.com/suits"
                self.issues[-1]["description"] = desired.replace(
                    url, f"[{url}](<{url}>)"
                )

        with tempfile.TemporaryDirectory() as tmp:
            client = CanonicalizingClient()
            raw = standalone_command()
            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=Path(tmp) / "create.json"
            )
            self.assertEqual(applied["result"], "applied")
            self.assertIsNone(client.issues[0]["parent"])
            self.assertEqual(client.issues[0]["priority"], lane.PRIORITIES["High"])
            self.assertEqual(applied["after"]["issue"]["identifier"], "SIS-100")

            writes = [
                call
                for call in client.calls
                if call[0].startswith(("create_", "update_"))
            ]
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=Path(tmp) / "replay.json"
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(
                [
                    call
                    for call in client.calls
                    if call[0].startswith(("create_", "update_"))
                ],
                writes,
            )
            self.assertEqual(len(client.issues), 1)

    def test_standalone_reconciles_one_exact_legacy_child_without_duplicate(self):
        class LegacyOutsideDirectProjectQuery(FakeIssueTreeClient):
            def list_project_issues(self, project_id):
                self.calls.append(("list_project_issues", project_id))
                return []

        client = LegacyOutsideDirectProjectQuery()
        legacy = {
            "id": "legacy-child-id",
            "identifier": "SIS-150",
            "title": "Выбрать и купить костюм",
            "url": "https://linear.app/example/issue/SIS-150",
            "description": "Варианты: https://example.com/suits",
            "priority": lane.PRIORITIES["High"],
            "state": {"id": "state-Todo", "name": "Todo"},
            "team": {"id": "team-sis", "key": "SIS"},
            "project": {"id": "project-existing"},
            "projectMilestone": {"id": "milestone-existing"},
            "parent": {"id": "old-parent", "identifier": "SIS-81"},
        }
        client.issues.append(legacy)
        with tempfile.TemporaryDirectory() as tmp:
            result = lane.execute_command(
                client,
                standalone_command(),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
        self.assertEqual(result["result"], "applied")
        self.assertEqual(len(client.issues), 1)
        self.assertIsNone(client.issues[0]["parent"])
        self.assertEqual(result["after"]["issue"]["identifier"], "SIS-150")
        self.assertEqual(
            [call[0] for call in client.calls].count("create_project_issue"), 0
        )

    def test_standalone_reports_safe_field_level_readback_mismatch(self):
        class TamperedClient(FakeIssueTreeClient):
            def create_project_issue(self, **kwargs):
                super().create_project_issue(**kwargs)
                self.issues[-1]["priority"] = lane.PRIORITIES["Low"]

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                lane.ContractError,
                r"^create_standalone_issue read-back mismatched fields: priority$",
            ):
                lane.execute_command(
                    TamperedClient(),
                    standalone_command(),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_issue_tree_recovers_after_partial_child_write_and_literal_replay(self):
        class CrashAfterSecondChild(FakeIssueTreeClient):
            def __init__(self):
                super().__init__(
                    project_name="Книги", milestone_name="Английская литература"
                )
                self.crashed = False

            def create_issue(self, **kwargs):
                super().create_issue(**kwargs)
                if self.child_counter == 2 and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("simulated crash after second child write")

        with tempfile.TemporaryDirectory() as tmp:
            client = CrashAfterSecondChild()
            raw = issue_tree_command()
            journal = Path(tmp) / "journal.json"
            with self.assertRaisesRegex(RuntimeError, "second child write"):
                lane.execute_command(client, raw, mode="apply", journal_path=journal)
            self.assertEqual(len(client.issues), 3)
            self.assertFalse(journal.exists())

            recovered = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(recovered["result"], "applied")
            self.assertEqual(len(recovered["after"]["sub_issues"]), 4)
            self.assertEqual(len(client.issues), 5)
            self.assertEqual(
                {item["title"] for item in client.issues[1:]},
                {"Король Лир", "Макбет", "Гамлет", "Отелло"},
            )

            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.issues), 5)

    def test_issue_tree_never_adopts_non_deterministic_child_by_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeIssueTreeClient(
                project_name="Книги", milestone_name="Английская литература"
            )
            raw = issue_tree_command()
            lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "create.json",
            )
            client.issues[1]["id"] = "non-deterministic-child"
            with self.assertRaisesRegex(
                lane.ContractError,
                r"^converge_issue_tree read-back mismatched fields: id/title$",
            ):
                lane.execute_command(
                    client,
                    raw,
                    mode="apply",
                    journal_path=Path(tmp) / "replay.json",
                )

    def test_issue_tree_partial_plan_preserves_declared_child_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeIssueTreeClient(
                project_name="Книги", milestone_name="Английская литература"
            )
            raw = issue_tree_command()
            lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "create.json",
            )
            client.issues = [client.issues[0], *client.issues[2:]]
            planned = lane.execute_command(client, raw, mode="plan")
            children = planned["after"]["sub_issues"]
            self.assertEqual(len(children), 4)
            self.assertIsNone(children[0])
            self.assertEqual(children[1]["title"], "Макбет")

    def test_scoped_issue_missing_identifier_returns_typed_field_blocker(self):
        client = FakeIssueTreeClient()
        client.issues.append(
            {
                "id": "legacy",
                "title": "Выбрать и купить костюм",
                "url": "https://linear.app/example/issue/SIS-150",
                "description": "Варианты: https://example.com/suits",
                "priority": lane.PRIORITIES["High"],
                "state": {"id": "state-Todo", "name": "Todo"},
                "team": {"id": "team-sis", "key": "SIS"},
                "project": {"id": "project-existing"},
                "projectMilestone": {"id": "milestone-existing"},
                "parent": None,
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                lane.ContractError,
                r"^create_standalone_issue read-back mismatched fields: id/title$",
            ):
                lane.execute_command(
                    client,
                    standalone_command(),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_hierarchy_ids_are_internal_stable_distinct_and_not_command_uuid_bound(self):
        first = hierarchy_command("linear:SIS:hierarchy:stable")
        second = json.loads(json.dumps(first, ensure_ascii=False))
        second["command_id"] = "33333333-3333-4333-8333-333333333333"
        second["correlation_id"] = "44444444-4444-4444-8444-444444444444"

        first_after = lane.execute_command(FakeHierarchyClient(), first, mode="plan")["after"]
        second_after = lane.execute_command(FakeHierarchyClient(), second, mode="plan")["after"]
        ids = [first_after[kind]["id"] for kind in ("project", "milestone", "issue")]

        self.assertEqual(first_after, second_after)
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(uuid.UUID(value).version == 4 for value in ids))

    def test_new_issue_reuses_exact_existing_project_and_milestone(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeHierarchyClient()
            original = hierarchy_command("linear:SIS:hierarchy:original")
            original["change"]["project"].update(
                {"name": "Hermes Experience", "description": "Project description"}
            )
            original["change"]["milestone"].update(
                {
                    "name": "Personal productivity integrations",
                    "description": "Milestone description",
                }
            )
            original["change"]["issue"]["title"] = "First integration"
            lane.execute_command(
                client,
                original,
                mode="apply",
                journal_path=Path(tmp) / "original.json",
            )
            project_id = client.projects[0]["id"]
            milestone_id = client.milestones[0]["id"]

            google_calendar = hierarchy_command("linear:SIS:hierarchy:google-calendar")
            google_calendar["change"]["project"].update(
                {"name": "Hermes Experience", "description": "Project description"}
            )
            google_calendar["change"]["milestone"].update(
                {
                    "name": "Personal productivity integrations",
                    "description": "Milestone description",
                }
            )
            google_calendar["change"]["issue"]["title"] = (
                "Интегрировать Hermes с Google Calendar"
            )

            applied = lane.execute_command(
                client,
                google_calendar,
                mode="apply",
                journal_path=Path(tmp) / "google-calendar.json",
            )

            self.assertEqual(applied["result"], "applied")
            self.assertTrue(applied["verified"])
            self.assertEqual(applied["before"]["project"]["id"], project_id)
            self.assertEqual(applied["before"]["milestone"]["id"], milestone_id)
            self.assertEqual(applied["after"]["project"]["id"], project_id)
            self.assertEqual(applied["after"]["milestone"]["id"], milestone_id)
            self.assertEqual(applied["after"]["issue"]["project_id"], project_id)
            self.assertEqual(applied["after"]["issue"]["milestone_id"], milestone_id)
            self.assertEqual(
                (len(client.projects), len(client.milestones), len(client.issues)),
                (1, 1, 2),
            )

            writes = [
                call
                for call in client.calls
                if call[0]
                in {"create_project", "create_project_milestone", "create_project_issue"}
            ]
            replay = lane.execute_command(
                client,
                google_calendar,
                mode="apply",
                journal_path=Path(tmp) / "google-calendar-replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertTrue(replay["verified"])
            self.assertEqual(
                [
                    call
                    for call in client.calls
                    if call[0]
                    in {
                        "create_project",
                        "create_project_milestone",
                        "create_project_issue",
                    }
                ],
                writes,
            )

    def test_reused_project_fails_closed_on_ambiguity_scope_or_description(self):
        raw = hierarchy_command("linear:SIS:hierarchy:reuse-project-safety")
        raw["change"]["project"]["description"] = "Expected project description"
        base = {
            "id": "existing-project",
            "name": "health",
            "description": "Expected project description",
            "teams": {"nodes": [{"id": "team-sis"}]},
        }
        cases = {
            "ambiguous": [base, {**base, "id": "second-project"}],
            "wrong team": [
                {**base, "teams": {"nodes": [{"id": "team-other"}]}}
            ],
            "description": [{**base, "description": "Different description"}],
        }
        for label, projects in cases.items():
            with self.subTest(label=label):
                client = FakeHierarchyClient()
                client.projects = json.loads(json.dumps(projects))
                with self.assertRaises(lane.ContractError):
                    lane.execute_command(client, raw, mode="plan")
                self.assertFalse(any(call[0].startswith("create_") for call in client.calls))

    def test_hierarchy_accepts_sis_key_independent_of_team_display_name(self):
        class RenamedTeamClient(FakeHierarchyClient):
            def list_teams(self):
                return [{"id": "team-sis", "key": "SIS", "name": "Renamed team"}]

        planned = lane.execute_command(
            RenamedTeamClient(), hierarchy_command(), mode="plan"
        )
        self.assertEqual(
            [item["action"] for item in planned["plan"]],
            ["create_project", "create_milestone", "create_issue"],
        )

    def test_name_fallback_scope_errors_do_not_claim_deterministic_id_conflict(self):
        project_client = FakeHierarchyClient()
        project_client.projects = [
            {
                "id": "existing-project",
                "name": "health",
                "teams": {"nodes": [{"id": "team-other"}]},
            }
        ]
        with self.assertRaisesRegex(
            lane.ContractError, "project exact-name match conflicts with live scope or name"
        ):
            lane.execute_command(project_client, hierarchy_command(), mode="plan")

        milestone_client = FakeHierarchyClient()
        milestone_client.projects = [
            {
                "id": "existing-project",
                "name": "health",
                "teams": {"nodes": [{"id": "team-sis"}]},
            }
        ]
        milestone_client.milestones = [
            {
                "id": "existing-milestone",
                "name": "Подолог",
                "project": {"id": "different-project"},
            }
        ]
        with self.assertRaisesRegex(
            lane.ContractError,
            "milestone exact-name match conflicts with live scope or name",
        ):
            lane.execute_command(milestone_client, hierarchy_command(), mode="plan")

    def test_reused_milestone_fails_closed_on_ambiguity_scope_or_description(self):
        raw = hierarchy_command("linear:SIS:hierarchy:reuse-milestone-safety")
        raw["change"]["milestone"]["description"] = "Expected milestone description"
        project = {
            "id": "existing-project",
            "name": "health",
            "teams": {"nodes": [{"id": "team-sis"}]},
        }
        base = {
            "id": "existing-milestone",
            "name": "Подолог",
            "description": "Expected milestone description",
            "project": {"id": "existing-project"},
        }
        cases = {
            "ambiguous": [base, {**base, "id": "second-milestone"}],
            "wrong project": [
                {**base, "project": {"id": "different-project"}}
            ],
            "description": [{**base, "description": "Different description"}],
        }
        for label, milestones in cases.items():
            with self.subTest(label=label):
                client = FakeHierarchyClient()
                client.projects = [json.loads(json.dumps(project))]
                client.milestones = json.loads(json.dumps(milestones))
                with self.assertRaises(lane.ContractError):
                    lane.execute_command(client, raw, mode="plan")
                self.assertFalse(any(call[0].startswith("create_") for call in client.calls))

    def test_new_issue_does_not_reuse_existing_issue_by_title(self):
        client = FakeHierarchyClient()
        raw = hierarchy_command("linear:SIS:hierarchy:new-issue-id")
        client.projects = [
            {
                "id": "existing-project",
                "name": "health",
                "teams": {"nodes": [{"id": "team-sis"}]},
            }
        ]
        client.milestones = [
            {
                "id": "existing-milestone",
                "name": "Подолог",
                "project": {"id": "existing-project"},
            }
        ]
        client.issues = [
            {
                "id": "different-issue-id",
                "identifier": "SIS-50",
                "title": "Сходить в Solomia и записаться",
                "url": "https://linear.app/example/issue/SIS-50/existing",
                "team": {"id": "team-sis", "key": "SIS"},
                "project": {"id": "existing-project"},
                "projectMilestone": {"id": "existing-milestone"},
                "state": {"id": "state-Todo", "name": "Todo"},
                "parent": None,
            }
        ]

        with self.assertRaisesRegex(lane.ContractError, "issue title already exists"):
            lane.execute_command(client, raw, mode="plan")
        self.assertFalse(any(call[0].startswith("create_") for call in client.calls))

    def test_hierarchy_results_have_typed_before_after_without_description_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeHierarchyClient()
            raw = hierarchy_command()
            raw["change"]["project"]["description"] = "hidden project description"
            raw["change"]["milestone"]["description"] = "hidden milestone description"
            raw["change"]["issue"]["state"] = "Todo"

            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(planned["before"], {"project": None, "milestone": None, "issue": None})
            self.assertFalse(planned["verified"])
            self.assertEqual(planned["after"]["project"]["name"], "health")
            self.assertEqual(planned["after"]["project"]["description"], "hidden project description")
            self.assertEqual(planned["after"]["milestone"]["project_id"], planned["after"]["project"]["id"])
            self.assertEqual(planned["after"]["issue"]["project_id"], planned["after"]["project"]["id"])
            self.assertEqual(planned["after"]["issue"]["milestone_id"], planned["after"]["milestone"]["id"])
            self.assertEqual(planned["after"]["issue"]["team_key"], "SIS")
            self.assertIsNone(planned["after"]["issue"]["parent_id"])
            self.assertEqual(planned["after"]["issue"]["state"], "Todo")
            plan_log = json.dumps(planned["plan"], ensure_ascii=False)
            self.assertNotIn("hidden project description", plan_log)
            self.assertNotIn("hidden milestone description", plan_log)
            self.assertNotIn("https://solomia.in.ua", plan_log)

            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=Path(tmp) / "journal.json"
            )
            self.assertEqual(applied["before"], planned["before"])
            self.assertTrue(applied["verified"])
            self.assertEqual(applied["after"]["issue"]["identifier"], "SIS-100")
            self.assertEqual(
                applied["after"]["issue"]["url"],
                "https://linear.app/example/issue/SIS-100",
            )

            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=Path(tmp) / "journal.json"
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(replay["before"], replay["after"])
            self.assertTrue(replay["verified"])

    def test_hierarchy_reports_safe_field_level_readback_mismatch(self):
        class StaleUpdateClient(FakeHierarchyClient):
            def update_project_issue(self, issue_id, **kwargs):
                self.calls.append(("stale_update_project_issue", issue_id, kwargs))

        with tempfile.TemporaryDirectory() as tmp:
            client = StaleUpdateClient()
            raw = hierarchy_command()
            raw["change"]["issue"]["state"] = "Todo"
            lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "create.json",
            )
            client.issues[0]["description"] = "tampered"
            with self.assertRaisesRegex(
                lane.ContractError,
                r"^converge_hierarchy read-back mismatched fields: description$",
            ):
                lane.execute_command(
                    client,
                    raw,
                    mode="apply",
                    journal_path=Path(tmp) / "reconcile.json",
                )

    def test_hierarchy_reconciles_exact_issue_description_and_state_drift(self):
        class ReconcilingClient(FakeHierarchyClient):
            def update_project_issue(self, issue_id, *, description=None, state_id=None):
                self.calls.append(("update_project_issue", issue_id, description, state_id))
                current = next(item for item in self.issues if item["id"] == issue_id)
                if description is not None:
                    current["description"] = description
                if state_id is not None:
                    current["state"] = {
                        "id": state_id,
                        "name": state_id.removeprefix("state-"),
                    }

        with tempfile.TemporaryDirectory() as tmp:
            client = ReconcilingClient()
            raw = hierarchy_command()
            raw["change"]["issue"]["state"] = "Todo"
            lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "create.json",
            )
            client.issues[0]["description"] = "stale description"
            client.issues[0]["state"] = {"id": "state-Backlog", "name": "Backlog"}

            reconciled = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "reconcile.json",
            )
            self.assertEqual(reconciled["result"], "applied")
            self.assertEqual(reconciled["before"]["issue"]["description"], "stale description")
            self.assertEqual(reconciled["before"]["issue"]["state"], "Backlog")
            self.assertEqual(reconciled["after"]["issue"]["description"], "https://solomia.in.ua")
            self.assertEqual(reconciled["after"]["issue"]["state"], "Todo")
            updates = [call for call in client.calls if call[0] == "update_project_issue"]
            self.assertEqual(
                updates,
                [
                    (
                        "update_project_issue",
                        client.issues[0]["id"],
                        "https://solomia.in.ua",
                        "state-Todo",
                    )
                ],
            )

            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(
                [call for call in client.calls if call[0] == "update_project_issue"],
                updates,
            )

    def test_hierarchy_accepts_linear_canonical_bare_url_description_readback(self):
        class CanonicalizingClient(FakeHierarchyClient):
            def update_project_issue(self, issue_id, *, description=None, state_id=None):
                self.calls.append(("update_project_issue", issue_id, description, state_id))
                current = next(item for item in self.issues if item["id"] == issue_id)
                if description is not None:
                    current["description"] = f"[{description}](<{description}>)"
                if state_id is not None:
                    current["state"] = {
                        "id": state_id,
                        "name": state_id.removeprefix("state-"),
                    }

        with tempfile.TemporaryDirectory() as tmp:
            client = CanonicalizingClient()
            raw = hierarchy_command()
            raw["change"]["issue"]["state"] = "Todo"
            lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "create.json",
            )
            client.issues[0]["description"] = "stale"

            reconciled = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "reconcile.json",
            )
            self.assertEqual(reconciled["result"], "applied")
            self.assertEqual(
                reconciled["after"]["issue"]["description"],
                "[https://solomia.in.ua](<https://solomia.in.ua>)",
            )

            updates = [call for call in client.calls if call[0] == "update_project_issue"]
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(
                [call for call in client.calls if call[0] == "update_project_issue"],
                updates,
            )

    def test_hierarchy_accepts_linear_canonical_inline_url_description_readback(self):
        desired = (
            "Купить набор столовых приборов Sambonet Taste 24 предмета: "
            "https://www.sambonet.com/en-it/cutlery-set%2C-24-pieces-/"
            "52553-81_vg.html\n\n"
            "Выбрать и купить подходящую скатерть JYSK: "
            "https://jysk.ua/dlya-domu/tekstil-dlya-kukhni/skatertini-na-stil"
        )
        observed = (
            "Купить набор столовых приборов Sambonet Taste 24 предмета: "
            "[https://www.sambonet.com/en-it/cutlery-set%2C-24-pieces-/"
            "52553-81_vg.html](<https://www.sambonet.com/en-it/"
            "cutlery-set%2C-24-pieces-/52553-81_vg.html>)\n\n"
            "Выбрать и купить подходящую скатерть JYSK: "
            "[https://jysk.ua/dlya-domu/tekstil-dlya-kukhni/skatertini-na-stil]"
            "(<https://jysk.ua/dlya-domu/tekstil-dlya-kukhni/skatertini-na-stil>)"
        )

        class CanonicalizingClient(FakeHierarchyClient):
            def __init__(self):
                super().__init__()
                self.created_description = None

            def create_project_issue(self, **kwargs):
                self.created_description = kwargs.get("description")
                super().create_project_issue(**kwargs)
                self.issues[-1]["description"] = observed

        with tempfile.TemporaryDirectory() as tmp:
            client = CanonicalizingClient()
            raw = hierarchy_command()
            raw["change"]["project"]["name"] = "Home Interior"
            raw["change"]["milestone"]["name"] = "Kitchen"
            raw["change"]["issue"]["title"] = (
                "Купить Sambonet Taste и скатерть JYSK"
            )
            raw["change"]["issue"]["description"] = desired
            raw["change"]["issue"]["state"] = "Todo"

            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "create.json",
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(client.created_description, desired)
            self.assertEqual(applied["after"]["issue"]["description"], observed)

            mutations = [
                call
                for call in client.calls
                if call[0]
                in {
                    "create_project",
                    "create_project_milestone",
                    "create_project_issue",
                    "update_project_issue",
                }
            ]
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            replay_mutations = [
                call
                for call in client.calls
                if call[0]
                in {
                    "create_project",
                    "create_project_milestone",
                    "create_project_issue",
                    "update_project_issue",
                }
            ]
            self.assertEqual(replay_mutations, mutations)
            self.assertEqual(len(client.projects), 1)
            self.assertEqual(len(client.milestones), 1)
            self.assertEqual(len(client.issues), 1)
            self.assertEqual(
                sum(1 for call in client.calls if call[0] == "create_project_issue"),
                1,
            )

    def test_hierarchy_url_canonicalization_is_exact_and_narrow(self):
        hierarchy = lane._load_hierarchy()
        desired = "https://solomia.in.ua"
        self.assertTrue(hierarchy._description_matches(desired, desired))
        self.assertTrue(
            hierarchy._description_matches(
                desired, "[https://solomia.in.ua](<https://solomia.in.ua>)"
            )
        )
        for live in (
            "[https://solomia.in.ua](https://solomia.in.ua)",
            "[Solomia](<https://solomia.in.ua>)",
            "[https://solomia.in.ua/](<https://solomia.in.ua/>)",
            "https://solomia.in.ua/",
        ):
            with self.subTest(live=live):
                self.assertFalse(hierarchy._description_matches(desired, live))
        self.assertFalse(
            hierarchy._description_matches(
                "Visit https://solomia.in.ua",
                "[Visit https://solomia.in.ua](<Visit https://solomia.in.ua>)",
            )
        )
        inline = "Visit https://a.example/x\nThen https://b.example/y"
        canonical = (
            "Visit [https://a.example/x](<https://a.example/x>)\n"
            "Then [https://b.example/y](<https://b.example/y>)"
        )
        self.assertTrue(hierarchy._description_matches(inline, canonical))
        for live in (
            "Visit [https://a.example/x](<https://a.example/x>)\nThen https://b.example/y",
            "Visit [A](<https://a.example/x>)\nThen [https://b.example/y](<https://b.example/y>)",
            canonical + " ",
        ):
            with self.subTest(inline_live=live):
                self.assertFalse(hierarchy._description_matches(inline, live))
        unsafe_contexts = (
            (
                "See [A](https://a.example/x) and https://b.example/y",
                "See [A]([https://a.example/x)](<https://a.example/x)>) and "
                "[https://b.example/y](<https://b.example/y>)",
            ),
            (
                "Run `https://a.example/x` then https://b.example/y",
                "Run `[https://a.example/x](<https://a.example/x>)` then "
                "[https://b.example/y](<https://b.example/y>)",
            ),
            (
                "See *https://a.example/x* and https://b.example/y",
                "See *[https://a.example/x*](<https://a.example/x*>) and "
                "[https://b.example/y](<https://b.example/y>)",
            ),
            (
                "Visit https://a.example/x.",
                "Visit [https://a.example/x.](<https://a.example/x.>)",
            ),
            (
                "Visit https://a.example/x\"",
                "Visit [https://a.example/x\"](<https://a.example/x\">)",
            ),
            (
                "Visit https://a.example/x'",
                "Visit [https://a.example/x'](<https://a.example/x'>)",
            ),
        )
        for desired_unsafe, live_unsafe in unsafe_contexts:
            with self.subTest(desired_unsafe=desired_unsafe):
                self.assertFalse(
                    hierarchy._description_matches(desired_unsafe, live_unsafe)
                )

    def test_hierarchy_omitted_fields_are_not_sent_or_compared(self):
        class CaptureClient(FakeHierarchyClient):
            def __init__(self):
                super().__init__()
                self.create_kwargs = []

            def create_project(self, **kwargs):
                self.create_kwargs.append(("project", dict(kwargs)))
                super().create_project(**kwargs)

            def create_project_milestone(self, **kwargs):
                self.create_kwargs.append(("milestone", dict(kwargs)))
                super().create_project_milestone(**kwargs)

            def create_project_issue(self, **kwargs):
                self.create_kwargs.append(("issue", dict(kwargs)))
                super().create_project_issue(**kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            client = CaptureClient()
            lane.execute_command(
                client,
                hierarchy_command(),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            sent = dict(client.create_kwargs)
            self.assertNotIn("description", sent["project"])
            self.assertNotIn("description", sent["milestone"])
            self.assertNotIn("state_id", sent["issue"])
            client.projects[0]["description"] = "unmanaged project description"
            client.milestones[0]["description"] = "unmanaged milestone description"
            replay = lane.execute_command(
                client,
                hierarchy_command(),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(replay["result"], "no_op")

    def test_hierarchy_state_request_recovers_after_mid_composite_crash_without_journal(self):
        class CrashOnceClient(FakeHierarchyClient):
            def __init__(self):
                super().__init__()
                self.crash = True
                self.issue_state_ids = []

            def create_project_milestone(self, **kwargs):
                super().create_project_milestone(**kwargs)
                if self.crash:
                    self.crash = False
                    raise RuntimeError("simulated crash after milestone create")

            def create_project_issue(self, **kwargs):
                self.issue_state_ids.append(kwargs.get("state_id"))
                super().create_project_issue(**kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = CrashOnceClient()
            raw = hierarchy_command()
            raw["change"]["issue"]["state"] = "Todo"
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                lane.execute_command(client, raw, mode="apply", journal_path=journal)
            self.assertFalse(journal.exists())
            recovered = lane.execute_command(client, raw, mode="apply", journal_path=journal)
            self.assertEqual(recovered["result"], "applied")
            self.assertEqual(client.issue_state_ids, ["state-Todo"])
            self.assertEqual(recovered["after"]["issue"]["state"], "Todo")
            self.assertEqual(len(client.projects), 1)
            self.assertEqual(len(client.milestones), 1)
            self.assertEqual(len(client.issues), 1)

    def test_cross_profile_hierarchy_delivery_converges_under_one_global_journal_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeHierarchyClient()
            first_command = hierarchy_command()
            second_command = hierarchy_command()
            second_command.update(
                {
                    "source_profile": "ideas",
                    "command_id": "33333333-3333-4333-8333-333333333333",
                    "correlation_id": "44444444-4444-4444-8444-444444444444",
                }
            )

            first = lane.execute_command(
                client, first_command, mode="apply", journal_path=journal
            )
            writes_after_first = [call for call in client.calls if call[0].startswith("create_")]
            second = lane.execute_command(
                client, second_command, mode="apply", journal_path=journal
            )
            writes_after_second = [call for call in client.calls if call[0].startswith("create_")]

        self.assertEqual(first["result"], "applied")
        self.assertEqual(second["result"], "no_op")
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(first["source_profile"], "swe")
        self.assertEqual(second["source_profile"], "ideas")
        for entity in ("project", "milestone", "issue"):
            self.assertEqual(first["after"][entity]["id"], second["after"][entity]["id"])
            self.assertEqual(uuid.UUID(first["after"][entity]["id"]).version, 4)
        self.assertEqual(writes_after_second, writes_after_first)

    def test_concurrent_hierarchy_replay_creates_each_entity_once(self):
        class SlowClient(FakeHierarchyClient):
            def list_team_projects(self, team_id):
                snapshot = super().list_team_projects(team_id)
                if not snapshot:
                    time.sleep(0.05)
                return snapshot

        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = SlowClient()
            raw = hierarchy_command()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda _: lane.execute_command(
                            client, raw, mode="apply", journal_path=journal
                        ),
                        range(2),
                    )
                )
            self.assertEqual(
                sorted(item["result"] for item in results), ["applied", "no_op"]
            )
            self.assertEqual(len(client.projects), 1)
            self.assertEqual(len(client.milestones), 1)
            self.assertEqual(len(client.issues), 1)

    def test_hierarchy_stops_after_tampered_intermediate_read_back(self):
        class TamperedProjectClient(FakeHierarchyClient):
            def create_project(self, **kwargs):
                super().create_project(**kwargs)
                self.projects[0]["name"] = "tampered"

        with tempfile.TemporaryDirectory() as tmp:
            client = TamperedProjectClient()
            with self.assertRaisesRegex(lane.ContractError, "project"):
                lane.execute_command(
                    client,
                    hierarchy_command(),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )
            self.assertEqual(len(client.projects), 1)
            self.assertEqual(client.milestones, [])
            self.assertEqual(client.issues, [])

    def test_hierarchy_tracer_plans_applies_reads_back_and_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeHierarchyClient()
            raw = hierarchy_command()

            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(
                [item["action"] for item in planned["plan"]],
                ["create_project", "create_milestone", "create_issue"],
            )
            self.assertEqual(client.calls, [
                ("list_teams",),
                ("list_team_projects", "team-sis"),
            ])

            client.calls.clear()
            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertTrue(applied["verified"])
            self.assertEqual(applied["target"]["identifier"], "health")
            self.assertEqual(client.issues[0]["description"], "https://solomia.in.ua")
            first_write = next(
                index for index, call in enumerate(client.calls) if call[0].startswith("create_")
            )
            self.assertEqual(
                client.calls[:first_write],
                [
                    ("list_teams",),
                    ("list_team_projects", "team-sis"),
                ],
            )

            journal.unlink()
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.projects), 1)
            self.assertEqual(len(client.milestones), 1)
            self.assertEqual(len(client.issues), 1)

    def test_read_returns_linear_result_v2_with_exact_verified_target(self):
        result = lane.execute_command(FakeClient(), command(), mode="plan")
        self.assertEqual(result["schema_version"], "linear-result.v2")
        self.assertEqual(result["result"], "read")
        self.assertEqual(result["target"]["identifier"], "SIS-59")
        self.assertEqual(result["after"]["state"], "In Progress")
        self.assertTrue(result["verified"])

    def test_exact_identifier_must_resolve_inside_sis_team(self):
        client = FakeClient()
        client.current["team"]["key"] = "OTHER"
        with self.assertRaisesRegex(lane.ContractError, "SIS team"):
            lane.execute_command(client, command(), mode="plan")

    def test_malformed_issue_payload_becomes_contract_error(self):
        client = FakeClient()
        del client.current["state"]["name"]
        with self.assertRaisesRegex(lane.ContractError, "issue payload"):
            lane.execute_command(client, command(), mode="plan")

    def test_null_team_payload_becomes_contract_error(self):
        client = FakeClient()
        client.current["team"] = None
        with self.assertRaisesRegex(lane.ContractError, "SIS team"):
            lane.execute_command(client, command(), mode="plan")

    def test_state_plan_records_before_after_without_write(self):
        client = FakeClient()
        result = lane.execute_command(
            client,
            command("change_state", {"state": "In Review"}),
            mode="plan",
        )
        self.assertEqual(result["result"], "planned")
        self.assertEqual(result["before"]["state"], "In Progress")
        self.assertEqual(result["after"]["state"], "In Review")
        self.assertEqual(result["plan"], [{"action": "change_state", "from": "In Progress", "to": "In Review"}])
        self.assertEqual(client.writes, [])

    def test_state_apply_reads_back_exact_target_and_replay_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            result = lane.execute_command(
                client,
                command("change_state", {"state": "In Review"}),
                mode="apply",
                journal_path=journal,
            )
            self.assertEqual(result["result"], "applied")
            self.assertEqual(result["before"]["state"], "In Progress")
            self.assertEqual(result["after"]["state"], "In Review")
            self.assertTrue(result["verified"])
            self.assertEqual(client.writes, [("state", "issue-uuid", "state-In Review")])
            replay = lane.execute_command(
                client,
                command("change_state", {"state": "In Review"}),
                mode="apply",
                journal_path=journal,
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_update_issue_attaches_top_level_issue_with_minimal_write_and_safe_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            client.current["parent"] = None
            parent = issue("Todo")
            parent.update(
                {
                    "id": "parent-68-uuid",
                    "identifier": "SIS-68",
                    "title": "Exact parent",
                    "url": "https://linear.app/example/issue/SIS-68",
                    "parent": None,
                }
            )
            client.related[parent["id"]] = parent
            unmanaged = json.loads(json.dumps(client.current))
            raw = command(
                "update_issue",
                {"parent_identifier": "SIS-68"},
                key="linear:SIS-59:parent-attach:fixture",
            )

            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(
                planned["plan"],
                [{"action": "update_issue", "fields": ["parent_identifier"]}],
            )
            self.assertEqual(planned["after"]["parent_identifier"], "SIS-68")
            self.assertNotIn("parent-68-uuid", json.dumps(planned))
            self.assertEqual(client.writes, [])

            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(applied["after"]["parent_identifier"], "SIS-68")
            self.assertNotIn("parent-68-uuid", json.dumps(applied))
            self.assertEqual(
                client.writes,
                [("fields", "issue-uuid", {"parent_id": "parent-68-uuid"})],
            )
            self.assertEqual(
                {key: client.current[key] for key in unmanaged if key != "parent"},
                {key: unmanaged[key] for key in unmanaged if key != "parent"},
            )

            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(replay["before"], replay["after"])
            self.assertEqual(len(client.writes), 1)

    def test_update_issue_parent_clear_and_replacement_require_owner_approval(self):
        blocker = "owner approval required: clearing or replacing an issue parent"
        parent = issue("Todo")
        parent.update(
            {
                "id": "parent-68-uuid",
                "identifier": "SIS-68",
                "url": "https://linear.app/example/issue/SIS-68",
                "parent": None,
            }
        )
        for requested, current_parent in (
            (None, None),
            (
                "SIS-68",
                {"id": "different-parent-uuid", "identifier": "SIS-1"},
            ),
        ):
            client = FakeClient()
            client.current["parent"] = current_parent
            client.related[parent["id"]] = parent
            with self.subTest(requested=requested), self.assertRaisesRegex(
                lane.ContractError, rf"^{blocker}$"
            ):
                lane.execute_command(
                    client,
                    command(
                        "update_issue",
                        {"parent_identifier": requested},
                        key=f"linear:SIS-59:parent-blocker:{requested}",
                    ),
                    mode="plan",
                )
            self.assertEqual(client.writes, [])

    def test_owner_approved_parent_clear_and_replace_use_consumed_gate_and_minimal_payload(self):
        class Gate:
            class ApprovalError(RuntimeError):
                pass

            @staticmethod
            def validate_policy(policy):
                return policy

            @staticmethod
            def require_consumed_owner_approval(
                authorization, *, expected_intent, expected_command
            ):
                del expected_command
                if authorization is not consumed:
                    raise Gate.ApprovalError("apply requires consumed owner approval")
                if expected_intent["operation"] != "update_issue" or set(expected_intent["change"]) != {"parent_identifier"}:
                    raise Gate.ApprovalError("wrong intent")

        consumed = object()
        parent = issue("Todo")
        parent.update(
            {
                "id": "parent-68-uuid",
                "identifier": "SIS-68",
                "url": "https://linear.app/example/issue/SIS-68",
                "parent": None,
            }
        )
        for requested, current_parent, expected_payload in (
            (None, {"id": "old-parent-uuid", "identifier": "SIS-1"}, {"parent_id": None}),
            (
                "SIS-68",
                {"id": "old-parent-uuid", "identifier": "SIS-1"},
                {"parent_id": "parent-68-uuid"},
            ),
        ):
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as tmp:
                client = FakeClient()
                client.current["parent"] = current_parent
                client.related[parent["id"]] = parent
                unmanaged_before = json.loads(
                    json.dumps(
                        {
                            key: value
                            for key, value in client.current.items()
                            if key != "parent"
                        }
                    )
                )
                raw = command(
                    "update_issue",
                    {"parent_identifier": requested},
                    key=f"linear:SIS-59:owner-parent:{requested}",
                )
                raw["policy"] = owner_policy()
                with mock.patch.object(lane, "_load_approval", return_value=Gate):
                    planned = lane.execute_command(client, raw, mode="plan")
                    applied = lane.execute_command(
                        client,
                        raw,
                        mode="apply",
                        journal_path=Path(tmp) / "journal.json",
                        owner_approval_authorization=consumed,
                    )
                    replay = lane.execute_command(
                        client,
                        raw,
                        mode="apply",
                        journal_path=Path(tmp) / "replay.json",
                        owner_approval_authorization=consumed,
                    )
                self.assertEqual(planned["plan"], [{"action": "update_issue", "fields": ["parent_identifier"]}])
                self.assertEqual(applied["after"]["parent_identifier"], requested)
                self.assertEqual(client.writes, [("fields", "issue-uuid", expected_payload)])
                self.assertEqual(
                    {key: value for key, value in client.current.items() if key != "parent"},
                    unmanaged_before,
                )
                self.assertEqual(replay["result"], "no_op")

    def test_owner_approved_parent_apply_rejects_direct_lane_bypass_and_cycle(self):
        raw = command(
            "update_issue",
            {"parent_identifier": None},
            key="linear:SIS-59:owner-parent:bypass",
        )
        raw["policy"] = owner_policy()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            lane.ContractError, "consumed owner approval"
        ):
            lane.execute_command(
                FakeClient(), raw, mode="apply", journal_path=Path(tmp) / "journal.json"
            )

        cyclic = FakeClient()
        cyclic_parent = issue("Todo")
        cyclic_parent.update(
            {
                "id": "parent-68-uuid",
                "identifier": "SIS-68",
                "url": "https://linear.app/example/issue/SIS-68",
                "parent": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        )
        cyclic.related[cyclic_parent["id"]] = cyclic_parent
        cycle = command(
            "update_issue",
            {"parent_identifier": "SIS-68"},
            key="linear:SIS-59:owner-parent:cycle",
        )
        cycle["policy"] = owner_policy()
        with self.assertRaisesRegex(lane.ContractError, "would create a cycle"):
            lane.execute_command(cyclic, cycle, mode="plan")
        self.assertEqual(cyclic.writes, [])

    def test_owner_approved_clear_fails_exact_read_back_on_parent_drift(self):
        class Gate:
            class ApprovalError(RuntimeError):
                pass

            @staticmethod
            def validate_policy(policy):
                return policy

            @staticmethod
            def require_consumed_owner_approval(
                _authorization, *, expected_intent, expected_command
            ):
                del expected_intent, expected_command
                return None

        class DriftingClearClient(FakeClient):
            def update_issue_fields(self, issue_id, **fields):
                old_parent = self.current["parent"]
                super().update_issue_fields(issue_id, **fields)
                self.current["parent"] = old_parent

        client = DriftingClearClient()
        client.current["parent"] = {"id": "old-parent-uuid", "identifier": "SIS-1"}
        raw = command(
            "update_issue",
            {"parent_identifier": None},
            key="linear:SIS-59:owner-parent:readback-drift",
        )
        raw["policy"] = owner_policy()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            lane, "_load_approval", return_value=Gate
        ), self.assertRaisesRegex(
            lane.ContractError, r"^update_issue read-back mismatched fields: parent$"
        ):
            lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
                owner_approval_authorization=object(),
            )

    def test_update_issue_rejects_missing_wrong_team_self_and_cycle_parent(self):
        cases = []

        missing = FakeClient()
        missing.current["parent"] = None
        cases.append((missing, "SIS-68", "exact Linear parent not found"))

        wrong_team = FakeClient()
        wrong_team.current["parent"] = None
        wrong_team_parent = issue("Todo")
        wrong_team_parent.update(
            {
                "id": "parent-68-uuid",
                "identifier": "SIS-68",
                "url": "https://linear.app/example/issue/SIS-68",
                "team": {"id": "other-team", "key": "OTHER"},
                "parent": None,
            }
        )
        wrong_team.related[wrong_team_parent["id"]] = wrong_team_parent
        cases.append((wrong_team, "SIS-68", "parent is not in the SIS team"))

        self_parent = FakeClient()
        self_parent.current["parent"] = None
        cases.append((self_parent, "SIS-59", "cannot be its own parent"))

        cyclic = FakeClient()
        cyclic.current["parent"] = None
        cyclic_parent = issue("Todo")
        cyclic_parent.update(
            {
                "id": "parent-68-uuid",
                "identifier": "SIS-68",
                "url": "https://linear.app/example/issue/SIS-68",
                "parent": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        )
        cyclic.related[cyclic_parent["id"]] = cyclic_parent
        cases.append((cyclic, "SIS-68", "would create a cycle"))

        for client, parent_identifier, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                lane.ContractError, message
            ):
                lane.execute_command(
                    client,
                    command(
                        "update_issue",
                        {"parent_identifier": parent_identifier},
                        key=f"linear:SIS-59:parent-negative:{message.replace(' ', '-')}",
                    ),
                    mode="plan",
                )
            self.assertEqual(client.writes, [])

    def test_update_issue_parent_attach_fails_exact_read_back_on_parent_drift(self):
        class DriftingParentClient(FakeClient):
            def update_issue_fields(self, issue_id, **fields):
                super().update_issue_fields(issue_id, **fields)
                self.current["parent"] = None

        client = DriftingParentClient()
        client.current["parent"] = None
        parent = issue("Todo")
        parent.update(
            {
                "id": "parent-68-uuid",
                "identifier": "SIS-68",
                "url": "https://linear.app/example/issue/SIS-68",
                "parent": None,
            }
        )
        client.related[parent["id"]] = parent
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            lane.ContractError,
            r"^update_issue read-back mismatched fields: parent$",
        ):
            lane.execute_command(
                client,
                command(
                    "update_issue",
                    {"parent_identifier": "SIS-68"},
                    key="linear:SIS-59:parent-readback-drift:fixture",
                ),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )

    def test_update_issue_applies_exact_fields_and_literal_replay_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            raw = command(
                "update_issue",
                {
                    "description": "Школа на Яр валу. Сравнить расписание и пробный урок.",
                    "state": "In Review",
                    "priority": "High",
                },
                key="linear:SIS-59:update:fixture",
            )
            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(applied["after"]["description"], raw["change"]["description"])
            self.assertEqual(applied["after"]["state"], "In Review")
            self.assertEqual(applied["after"]["priority"], "High")
            self.assertEqual(
                client.writes,
                [
                    (
                        "fields",
                        "issue-uuid",
                        {
                            "description": raw["change"]["description"],
                            "state_id": "state-In Review",
                            "priority": lane.PRIORITIES["High"],
                        },
                    )
                ],
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_update_issue_moves_to_exact_project_and_milestone_with_safe_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            unmanaged = {
                key: json.loads(json.dumps(client.current[key]))
                for key in (
                    "title",
                    "description",
                    "state",
                    "priority",
                    "assignee",
                    "labels",
                    "parent",
                    "dueDate",
                    "estimate",
                    "team",
                )
            }
            raw = command(
                "update_issue",
                {"project": "Project Two", "milestone": "Milestone Two"},
                key="linear:SIS-59:move:fixture",
            )
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(
                planned["before"] | {"project": "Project Two", "milestone": "Milestone Two"},
                planned["after"],
            )
            self.assertEqual(
                planned["plan"],
                [{"action": "update_issue", "fields": ["project", "milestone"]}],
            )
            self.assertNotIn("project-two", json.dumps(planned))
            self.assertEqual(client.writes, [])

            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(applied["before"]["project"], "Current Project")
            self.assertEqual(applied["before"]["milestone"], "Current Milestone")
            self.assertEqual(applied["after"]["project"], "Project Two")
            self.assertEqual(applied["after"]["milestone"], "Milestone Two")
            self.assertEqual(
                client.writes,
                [
                    (
                        "fields",
                        "issue-uuid",
                        {"project_id": "project-two", "milestone_id": "milestone-two"},
                    )
                ],
            )
            self.assertEqual(
                {key: client.current[key] for key in unmanaged},
                unmanaged,
            )
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(replay["before"], replay["after"])
            self.assertEqual(len(client.writes), 1)

    def test_update_issue_removes_links_preserves_text_and_changes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            client.current["description"] = (
                "Куплены [все книги](https://shop.example/books).\n"
                "Список: https://example.com/list\n"
                "Заметка остаётся."
            )
            raw = command(
                "update_issue",
                {"description_transform": "remove_links", "state": "Todo"},
                key="linear:SIS-59:update:remove-links",
            )

            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )

            expected = "Куплены все книги.\nСписок:\nЗаметка остаётся."
            self.assertEqual(applied["after"]["description"], expected)
            self.assertEqual(applied["after"]["state"], "Todo")
            self.assertEqual(
                client.writes,
                [
                    (
                        "fields",
                        "issue-uuid",
                        {"description": expected, "state_id": "state-Todo"},
                    )
                ],
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_remove_links_literal_replay_preserves_url_valued_markdown_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            client.current["description"] = (
                "[https://label.example](https://destination.example)"
            )
            raw = command(
                "update_issue",
                {"description_transform": "remove_links"},
                key="linear:SIS-59:update:url-label-replay",
            )

            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )

            self.assertEqual(
                applied["after"]["description"], "https://label.example"
            )
            self.assertEqual(
                replay["after"]["description"], "https://label.example"
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_remove_links_recovers_post_write_crash_without_recomputing_after_state(self):
        class CrashAfterWrite(FakeClient):
            crashed = False

            def update_issue_fields(self, issue_id, **fields):
                super().update_issue_fields(issue_id, **fields)
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process death after update")

        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = CrashAfterWrite()
            client.current["description"] = (
                "[https://label.example](https://destination.example)"
            )
            raw = command(
                "update_issue",
                {"description_transform": "remove_links"},
                key="linear:SIS-59:update:url-label-crash",
            )

            with self.assertRaisesRegex(KeyboardInterrupt, "process death"):
                lane.execute_command(client, raw, mode="apply", journal_path=journal)
            recovered = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )

            self.assertEqual(client.current["description"], "https://label.example")
            self.assertEqual(recovered["result"], "no_op")
            self.assertTrue(recovered["recovered"])
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_remove_links_rejects_concurrent_description_edit_before_update(self):
        class ConcurrentEdit(FakeClient):
            reads = 0

            def get_issue(self, identifier):
                self.reads += 1
                if identifier == "SIS-59" and self.reads == 2:
                    self.current["description"] = "concurrent user edit"
                return super().get_issue(identifier)

        with tempfile.TemporaryDirectory() as tmp:
            client = ConcurrentEdit()
            client.current["description"] = "[kept](https://example.com)"
            with self.assertRaisesRegex(lane.ContractError, "description.*drift"):
                lane.execute_command(
                    client,
                    command(
                        "update_issue",
                        {"description_transform": "remove_links"},
                        key="linear:SIS-59:update:remove-links-toctou",
                    ),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )
            self.assertEqual(client.writes, [])
            self.assertEqual(client.current["description"], "concurrent user edit")

    def test_update_issue_clears_project_and_milestone_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            raw = command(
                "update_issue",
                {"project": None, "milestone": None},
                key="linear:SIS-59:clear-scope:fixture",
            )
            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(applied["before"]["project"], "Current Project")
            self.assertEqual(applied["before"]["milestone"], "Current Milestone")
            self.assertIsNone(applied["after"]["project"])
            self.assertIsNone(applied["after"]["milestone"])
            self.assertEqual(
                client.writes,
                [
                    (
                        "fields",
                        "issue-uuid",
                        {"project_id": None, "milestone_id": None},
                    )
                ],
            )
            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_update_issue_can_clear_project_without_a_current_milestone(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            client.current["projectMilestone"] = None
            applied = lane.execute_command(
                client,
                command(
                    "update_issue",
                    {"project": None, "milestone": None},
                    key="linear:SIS-59:clear-project-only:fixture",
                ),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(applied["before"]["project"], "Current Project")
            self.assertIsNone(applied["before"]["milestone"])
            self.assertIsNone(applied["after"]["project"])
            self.assertIsNone(applied["after"]["milestone"])

    def test_update_issue_move_rejects_missing_ambiguous_and_wrong_scope_names(self):
        cases = []

        missing_project = FakeClient()
        cases.append((missing_project, "Unknown Project", "Milestone Two", "project"))

        ambiguous_project = FakeClient()
        ambiguous_project.projects.append(
            json.loads(json.dumps(ambiguous_project.projects[1]))
        )
        ambiguous_project.projects[-1]["id"] = "project-two-duplicate"
        cases.append((ambiguous_project, "Project Two", "Milestone Two", "project"))

        missing_milestone = FakeClient()
        cases.append((missing_milestone, "Project Two", "Unknown Milestone", "milestone"))

        ambiguous_milestone = FakeClient()
        ambiguous_milestone.milestones["project-two"].append(
            {
                "id": "milestone-two-duplicate",
                "name": "Milestone Two",
                "project": {"id": "project-two"},
            }
        )
        cases.append((ambiguous_milestone, "Project Two", "Milestone Two", "milestone"))

        wrong_team = FakeClient()
        wrong_team.projects[1]["teams"] = {"nodes": [{"id": "other-team"}]}
        cases.append((wrong_team, "Project Two", "Milestone Two", "SIS team"))

        wrong_project = FakeClient()
        wrong_project.milestones["project-two"][0]["project"] = {
            "id": "project-uuid"
        }
        cases.append((wrong_project, "Project Two", "Milestone Two", "selected project"))

        for client, project, milestone, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                lane.ContractError, message
            ):
                lane.execute_command(
                    client,
                    command(
                        "update_issue",
                        {"project": project, "milestone": milestone},
                        key=f"linear:SIS-59:negative:{message.replace(' ', '-')}",
                    ),
                    mode="plan",
                )
            self.assertEqual(client.writes, [])

    def test_update_issue_move_fails_exact_read_back_on_structural_drift(self):
        class DriftingClient(FakeClient):
            def update_issue_fields(self, issue_id, **fields):
                super().update_issue_fields(issue_id, **fields)
                self.current["projectMilestone"] = {"id": "milestone-uuid"}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                lane.ContractError,
                r"^update_issue read-back mismatched fields: milestone$",
            ):
                lane.execute_command(
                    DriftingClient(),
                    command(
                        "update_issue",
                        {"project": "Project Two", "milestone": "Milestone Two"},
                        key="linear:SIS-59:move-readback-drift:fixture",
                    ),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_update_issue_title_applies_and_literal_replay_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            raw = command(
                "update_issue",
                {"title": "Ship the full Linear manager"},
                key="linear:SIS-59:title:fixture",
            )
            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(applied["before"]["title"], "Implement lane")
            self.assertEqual(applied["after"]["title"], "Ship the full Linear manager")
            self.assertEqual(
                client.writes,
                [("fields", "issue-uuid", {"title": "Ship the full Linear manager"})],
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_update_issue_assigns_exact_user_and_replays_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            client.current["assignee"] = None
            raw = command(
                "update_issue",
                {"assignee": "Alexey Petrov"},
                key="linear:SIS-59:assignee:fixture",
            )
            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(applied["after"]["assignee"], "Alexey Petrov")
            self.assertEqual(
                client.writes,
                [("fields", "issue-uuid", {"assignee_id": "user-alexey"})],
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_update_issue_unassigns_and_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            client.current["assignee"] = client.list_users()[0]
            before = json.loads(json.dumps(client.current))
            raw = command(
                "update_issue",
                {"assignee": None},
                key="linear:SIS-59:unassign:fixture",
            )
            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertIsNone(applied["after"]["assignee"])
            self.assertEqual(
                {key: client.current[key] for key in ("title", "description", "priority", "state")},
                {key: before[key] for key in ("title", "description", "priority", "state")},
            )
            self.assertEqual(
                client.writes,
                [("fields", "issue-uuid", {"assignee_id": None})],
            )

    def test_update_issue_sets_exact_label_set_and_replays_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            client.current["labels"] = {"nodes": []}
            raw = command(
                "update_issue",
                {"labels": ["area:linear", "priority:owner"]},
                key="linear:SIS-59:labels:fixture",
            )
            journal = Path(tmp) / "journal.json"
            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(
                applied["after"]["labels"],
                ["area:linear", "priority:owner"],
            )
            self.assertEqual(
                client.writes,
                [
                    (
                        "fields",
                        "issue-uuid",
                        {"label_ids": ["label-linear", "label-owner"]},
                    )
                ],
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_update_issue_plans_and_applies_due_date_and_estimate_then_replays_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            raw = command(
                "update_issue",
                {"due_date": "2026-09-30", "estimate": 8},
                key="linear:SIS-59:due-estimate:fixture",
            )
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(planned["after"]["due_date"], "2026-09-30")
            self.assertEqual(planned["after"]["estimate"], 8)
            self.assertEqual(
                planned["plan"],
                [
                    {
                        "action": "update_issue",
                        "fields": ["due_date", "estimate"],
                    }
                ],
            )
            self.assertEqual(client.writes, [])

            journal = Path(tmp) / "journal.json"
            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(applied["after"]["due_date"], "2026-09-30")
            self.assertEqual(applied["after"]["estimate"], 8)
            self.assertEqual(
                client.writes,
                [
                    (
                        "fields",
                        "issue-uuid",
                        {"due_date": "2026-09-30", "estimate": 8},
                    )
                ],
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(replay["before"], replay["after"])
            self.assertEqual(len(client.writes), 1)

    def test_update_issue_clears_due_date_and_estimate_without_changing_unmanaged_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            client.current["dueDate"] = "2026-09-30"
            client.current["estimate"] = 8
            unmanaged_before = {
                key: json.loads(json.dumps(client.current[key]))
                for key in (
                    "title",
                    "description",
                    "state",
                    "priority",
                    "assignee",
                    "labels",
                    "parent",
                    "project",
                    "projectMilestone",
                    "team",
                )
            }
            raw = command(
                "update_issue",
                {"due_date": None, "estimate": None},
                key="linear:SIS-59:clear-due-estimate:fixture",
            )
            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertIsNone(applied["after"]["due_date"])
            self.assertIsNone(applied["after"]["estimate"])
            self.assertEqual(
                {key: client.current[key] for key in unmanaged_before},
                unmanaged_before,
            )

    def test_update_issue_fails_read_back_when_due_estimate_write_changes_project(self):
        class DriftingClient(FakeClient):
            def update_issue_fields(self, issue_id, **fields):
                super().update_issue_fields(issue_id, **fields)
                self.current["project"] = {"id": "different-project"}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                lane.ContractError,
                r"update_issue read-back mismatched fields: project",
            ):
                lane.execute_command(
                    DriftingClient(),
                    command(
                        "update_issue",
                        {"estimate": 8},
                        key="linear:SIS-59:estimate-project-drift:fixture",
                    ),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_update_issue_rejects_boolean_estimate_read_back_as_mismatch(self):
        class BooleanEstimateClient(FakeClient):
            def update_issue_fields(self, issue_id, **fields):
                super().update_issue_fields(issue_id, **fields)
                self.current["estimate"] = True

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                lane.ContractError,
                r"update_issue read-back mismatched fields: estimate",
            ):
                lane.execute_command(
                    BooleanEstimateClient(),
                    command(
                        "update_issue",
                        {"estimate": 1},
                        key="linear:SIS-59:boolean-estimate-readback:fixture",
                    ),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_inventory_sub_issues_returns_complete_recursive_tree_without_writes(self):
        class RecursiveClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.current["identifier"] = "SIS-86"
                self.current["url"] = "https://linear.app/example/issue/SIS-86"
                child = issue("Todo")
                child.update(
                    {
                        "id": "child-87",
                        "identifier": "SIS-87",
                        "title": "Child",
                        "url": "https://linear.app/example/issue/SIS-87",
                        "parent": {"id": "issue-uuid", "identifier": "SIS-86"},
                    }
                )
                grandchild = issue("In Review")
                grandchild.update(
                    {
                        "id": "child-88",
                        "identifier": "SIS-88",
                        "title": "Grandchild",
                        "url": "https://linear.app/example/issue/SIS-88",
                        "parent": {"id": "child-87", "identifier": "SIS-87"},
                    }
                )
                self.by_parent = {"SIS-86": [child], "SIS-87": [grandchild], "SIS-88": []}

            def get_issue(self, identifier):
                if identifier == "SIS-86":
                    return json.loads(json.dumps(self.current))
                return None

            def list_child_issues(self, parent_id):
                return json.loads(json.dumps(self.by_parent[parent_id]))

        with tempfile.TemporaryDirectory() as tmp:
            client = RecursiveClient()
            raw = command(
                "inventory_sub_issues",
                {},
                key="linear:SIS-86:inventory:fixture",
            )
            raw["target"] = {"type": "issue", "identifier": "SIS-86"}
            result = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(result["result"], "read")
            self.assertEqual(
                [(item["identifier"], item["parent_identifier"]) for item in result["after"]],
                [("SIS-87", "SIS-86"), ("SIS-88", "SIS-87")],
            )
            self.assertEqual(client.writes, [])

    def test_recursive_inventory_has_no_python_recursion_depth_cap(self):
        depth = 1100
        root = issue("Todo")
        root.update({"identifier": "SIS-1", "id": "root"})

        class DeepClient:
            def list_child_issues(self, parent_identifier):
                number = int(parent_identifier.removeprefix("SIS-"))
                if number > depth:
                    return []
                child_number = number + 1
                child = issue("Todo")
                child.update(
                    {
                        "id": f"child-{child_number}",
                        "identifier": f"SIS-{child_number}",
                        "title": f"Child {child_number}",
                        "url": f"https://linear.app/example/issue/SIS-{child_number}",
                        "parent": {
                            "id": "ignored",
                            "identifier": parent_identifier,
                        },
                    }
                )
                return [child]

        inventory = lane.recursive_sub_issue_inventory(
            DeepClient(),
            parent=root,
            team_id="team-uuid",
        )
        self.assertEqual(len(inventory), depth)
        self.assertEqual(inventory[-1]["identifier"], f"SIS-{depth + 1}")

    def test_update_sub_issues_clears_descriptions_preserves_states_and_replays_noop(self):
        class RecursiveClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.current["identifier"] = "SIS-86"
                self.current["url"] = "https://linear.app/example/issue/SIS-86"
                child = issue("Todo")
                child.update(
                    {
                        "id": "child-87",
                        "identifier": "SIS-87",
                        "title": "Child",
                        "url": "https://linear.app/example/issue/SIS-87",
                        "description": "Remove me",
                        "parent": {"id": "issue-uuid", "identifier": "SIS-86"},
                    }
                )
                grandchild = issue("In Review")
                grandchild.update(
                    {
                        "id": "child-88",
                        "identifier": "SIS-88",
                        "title": "Grandchild",
                        "url": "https://linear.app/example/issue/SIS-88",
                        "description": "Remove me too",
                        "parent": {"id": "child-87", "identifier": "SIS-87"},
                    }
                )
                self.items = {"SIS-87": child, "SIS-88": grandchild}
                self.by_parent = {"SIS-86": ["SIS-87"], "SIS-87": ["SIS-88"], "SIS-88": []}

            def get_issue(self, identifier):
                if identifier == "SIS-86":
                    return json.loads(json.dumps(self.current))
                item = self.items.get(identifier)
                return json.loads(json.dumps(item)) if item else None

            def list_child_issues(self, parent_id):
                return [
                    json.loads(json.dumps(self.items[identifier]))
                    for identifier in self.by_parent[parent_id]
                ]

            def update_issue_fields(self, issue_id, **fields):
                self.writes.append(("fields", issue_id, fields))
                item = next(item for item in self.items.values() if item["id"] == issue_id)
                if "description" in fields:
                    item["description"] = fields["description"]

        with tempfile.TemporaryDirectory() as tmp:
            client = RecursiveClient()
            raw = command(
                "update_sub_issues",
                {"description": ""},
                key="linear:SIS-86:children-description:fixture",
            )
            raw["target"] = {"type": "issue", "identifier": "SIS-86"}
            journal = Path(tmp) / "journal.json"
            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(
                client.writes,
                [
                    ("fields", "child-87", {"description": ""}),
                    ("fields", "child-88", {"description": ""}),
                ],
            )
            self.assertEqual(
                [(item["description"], item["state"]) for item in applied["after"]],
                [("", "Todo"), ("", "In Review")],
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 2)
    def test_remove_links_transform_handles_empty_and_missing_descriptions(self):
        self.assertEqual(lane.remove_description_links(None), "")
        self.assertEqual(lane.remove_description_links("https://example.com"), "")
        self.assertEqual(
            lane.remove_description_links("Текст <https://example.com> дальше"),
            "Текст дальше",
        )

    def test_remove_links_preserves_parenthesized_markdown_and_url_labels(self):
        self.assertEqual(
            lane.remove_description_links(
                "[Wikipedia](https://example.com/Function_(mathematics))"
            ),
            "Wikipedia",
        )
        self.assertEqual(
            lane.remove_description_links(
                "[https://visible.example](https://target.example)"
            ),
            "https://visible.example",
        )
        self.assertEqual(
            lane.remove_description_links("[docs](<https://example.com/path>)"),
            "docs",
        )
        self.assertEqual(
            lane.remove_description_links("[docs](<https://example.com/a b>)"),
            "docs",
        )
        self.assertEqual(
            lane.remove_description_links(
                '[docs](<https://example.com/path> "title")'
            ),
            "docs",
        )
        self.assertIsNone(
            lane._markdown_link_at(r"[docs](<https://example.com\>)", 0)
        )
        self.assertIsNone(
            lane._markdown_link_at("[docs](<https://example.com/<x>)", 0)
        )

    def test_remove_links_preserves_unrelated_markdown_whitespace(self):
        original = (
            "- parent  \n"
            "  - [child](https://example.com)\n"
            "    code  https://target.example  tail  \n"
            "        indented block"
        )
        expected = (
            "- parent  \n"
            "  - child\n"
            "    code  tail  \n"
            "        indented block"
        )
        self.assertEqual(lane.remove_description_links(original), expected)

    def test_state_apply_fails_when_read_back_does_not_match(self):
        class StaleClient(FakeClient):
            def update_issue_state(self, issue_id, state_id):
                self.writes.append(("state", issue_id, state_id))

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "read-back verification"):
                lane.execute_command(
                    StaleClient(),
                    command("change_state", {"state": "In Review"}),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_state_apply_null_read_back_state_becomes_contract_error(self):
        class NullStateClient(FakeClient):
            def update_issue_state(self, issue_id, state_id):
                self.writes.append(("state", issue_id, state_id))
                self.current["state"] = None

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "read-back verification"):
                lane.execute_command(
                    NullStateClient(),
                    command("change_state", {"state": "In Review"}),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_comment_plan_apply_and_invisible_id_replay_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            raw = command(
                "add_comment",
                {"body": "SIS-59 command lane live verification."},
                key="linear:SIS-59:comment:fixture",
            )
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(planned["result"], "planned")
            self.assertEqual(planned["before"]["comment_count"], 0)
            self.assertEqual(planned["after"]["comment_count"], 1)
            self.assertNotIn("body", planned["plan"][0])
            self.assertEqual(client.writes, [])

            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertTrue(applied["verified"])
            self.assertEqual(client.writes[0][3], raw["change"]["body"])
            self.assertNotIn("<!-- linear-command:v2", client.writes[0][3])
            self.assertEqual(client.writes[0][2], lane.command_fingerprint(raw)[2])
            self.assertEqual(uuid.UUID(client.writes[0][2]).version, 4)
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_user_authored_whitespace_is_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            comment_client = FakeClient()
            comment = command("add_comment", {"body": "  indented\ntrailing  "})
            lane.execute_command(
                comment_client,
                comment,
                mode="apply",
                journal_path=Path(tmp) / "comment-journal.json",
            )
            self.assertEqual(comment_client.comments[0]["body"], "  indented\ntrailing  ")

            issue_client = FakeClient()
            create = create_command()
            create["change"]["description"] = "  indented\ntrailing  "
            lane.execute_command(
                issue_client,
                create,
                mode="apply",
                journal_path=Path(tmp) / "issue-journal.json",
            )
            self.assertEqual(issue_client.children[0]["description"], "  indented\ntrailing  ")
            self.assertEqual(uuid.UUID(issue_client.children[0]["id"]).version, 4)

    def test_missing_deterministic_comment_does_not_query_absent_entity(self):
        class LinearMissingLookupClient(FakeClient):
            def get_comment(self, comment_id):
                found = super().get_comment(comment_id)
                if found is None:
                    raise lane.ContractError("Linear GraphQL error: Entity not found: Comment")
                return found

        with tempfile.TemporaryDirectory() as tmp:
            client = LinearMissingLookupClient()
            result = lane.execute_command(
                client,
                command("add_comment", {"body": "clean live contract"}),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(result["result"], "applied")
            self.assertEqual(len(client.comments), 1)

    def test_comment_apply_fails_when_deterministic_id_is_not_read_back(self):
        class MissingCommentClient(FakeClient):
            def create_comment(self, issue_id, comment_id, body):
                self.writes.append(("comment", issue_id, comment_id, body))

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "comment read-back verification"):
                lane.execute_command(
                    MissingCommentClient(),
                    command("add_comment", {"body": "verify me"}),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_comment_id_rejects_same_key_for_different_request(self):
        client = FakeClient()
        first = command(
            "add_comment",
            {"body": "first"},
            key="linear:SIS-59:comment:conflict",
        )
        comment_id = lane.command_fingerprint(first)[2]
        client.comments = [{"id": comment_id, "issueId": "issue-uuid", "body": "first"}]
        second = command(
            "add_comment",
            {"body": "different"},
            key="linear:SIS-59:comment:conflict",
        )
        self.assertEqual(comment_id, lane.command_fingerprint(second)[2])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "conflicts"):
                lane.execute_command(
                    client,
                    second,
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_comment_replay_survives_lost_local_journal_without_public_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            raw = command(
                "add_comment",
                {"body": "clean crash-safe comment"},
                key="linear:SIS-59:comment:crash-window",
            )
            lane.execute_command(client, raw, mode="apply", journal_path=journal)
            journal.unlink()
            replay = lane.execute_command(client, raw, mode="apply", journal_path=journal)
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)
            self.assertEqual(client.comments[0]["body"], "clean crash-safe comment")

    def test_concurrent_comment_apply_creates_one_clean_comment(self):
        class SlowClient(FakeClient):
            def list_comments(self, issue_id):
                snapshot = super().list_comments(issue_id)
                if not snapshot:
                    time.sleep(0.05)
                return snapshot

        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = SlowClient()
            raw = command(
                "add_comment",
                {"body": "concurrent"},
                key="linear:SIS-59:comment:concurrent",
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda _: lane.execute_command(
                            client,
                            raw,
                            mode="apply",
                            journal_path=journal,
                        ),
                        range(2),
                    )
                )
            self.assertEqual(
                sorted(item["result"] for item in results),
                ["applied", "no_op"],
            )
            self.assertEqual(len(client.comments), 1)

    def test_malformed_child_nodes_fail_closed_before_issue_creation(self):
        for malformed in (None, "not-an-object", {"title": "missing id"}):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as tmp:
                class MalformedChildrenClient(FakeClient):
                    def list_child_issues(self, parent_id, value=malformed):
                        return [value]

                client = MalformedChildrenClient()
                with self.assertRaisesRegex(
                    lane.ContractError, "malformed child node"
                ):
                    lane.execute_command(
                        client,
                        create_command(),
                        mode="apply",
                        journal_path=Path(tmp) / "journal.json",
                    )
                self.assertEqual(client.writes, [])

    def test_missing_deterministic_issue_does_not_query_absent_entity(self):
        class LinearMissingIssueLookupClient(FakeClient):
            def get_issue(self, identifier):
                found = super().get_issue(identifier)
                if found is None:
                    raise lane.ContractError(
                        "Linear GraphQL error: Entity not found: Issue"
                    )
                return found

        with tempfile.TemporaryDirectory() as tmp:
            client = LinearMissingIssueLookupClient()
            result = lane.execute_command(
                client,
                create_command(),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(result["result"], "applied")
            self.assertEqual(len(client.children), 1)

    def test_create_issue_plan_apply_read_back_and_replay_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            raw = create_command()
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(planned["result"], "planned")
            self.assertEqual(planned["target"], {"type": "team", "identifier": "SIS"})
            self.assertEqual(client.writes, [])

            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(applied["target"]["identifier"], "SIS-99")
            self.assertTrue(applied["verified"])
            self.assertEqual(len(client.writes), 1)

            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(replay["target"]["identifier"], "SIS-99")
            self.assertEqual(len(client.writes), 1)
            self.assertEqual(client.children[0]["description"], raw["change"]["description"])
            self.assertNotIn("<!-- linear-command", client.children[0]["description"])

            journal.unlink()
            crash_replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(crash_replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

            conflicting = create_command()
            conflicting["idempotency_key"] = raw["idempotency_key"]
            conflicting["change"]["title"] = "Different title"
            journal.unlink()
            with self.assertRaisesRegex(
                lane.ContractError,
                r"^create_issue read-back mismatched fields: id/title$",
            ):
                lane.execute_command(
                    client, conflicting, mode="apply", journal_path=journal
                )

    def test_create_issue_accepts_shared_linear_url_canonicalization_and_replays(self):
        desired = (
            "Выбрать костюм: https://example.com/suit\n"
            "Записаться: https://example.com/appointment"
        )
        observed = (
            "Выбрать костюм: [https://example.com/suit](<https://example.com/suit>)\n"
            "Записаться: [https://example.com/appointment]"
            "(<https://example.com/appointment>)"
        )

        class CanonicalizingClient(FakeClient):
            def create_issue(self, **kwargs):
                super().create_issue(**kwargs)
                self.children[-1]["description"] = observed

        with tempfile.TemporaryDirectory() as tmp:
            client = CanonicalizingClient()
            raw = create_command()
            raw["change"]["description"] = desired
            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "create.json",
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(applied["after"]["description"], observed)

            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_create_issue_rejects_tampered_bounded_read_back_before_journal(self):
        for field in ("description", "priority"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                class TamperedClient(FakeClient):
                    def create_issue(self, test_field=field, **kwargs):
                        super().create_issue(**kwargs)
                        if test_field == "description":
                            self.children[-1]["description"] = "tampered"
                        else:
                            self.children[-1]["priority"] = 4

                journal = Path(tmp) / "journal.json"
                with self.assertRaisesRegex(
                    lane.ContractError,
                    rf"^create_issue read-back mismatched fields: {field}$",
                ):
                    lane.execute_command(
                        TamperedClient(),
                        create_command(),
                        mode="apply",
                        journal_path=journal,
                    )
                self.assertFalse(journal.exists())

    def test_create_issue_reports_only_safe_field_level_readback_mismatches(self):
        class TamperedClient(FakeClient):
            def create_issue(self, **kwargs):
                super().create_issue(**kwargs)
                self.children[-1]["description"] = "tampered"
                self.children[-1]["priority"] = 4

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                lane.ContractError,
                r"^create_issue read-back mismatched fields: description, priority$",
            ):
                lane.execute_command(
                    TamperedClient(),
                    create_command(),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_create_issue_replay_rejects_later_bounded_field_drift(self):
        for field in ("description", "priority"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                client = FakeClient()
                journal = Path(tmp) / "journal.json"
                raw = create_command()
                lane.execute_command(client, raw, mode="apply", journal_path=journal)
                if field == "description":
                    client.children[0]["description"] = "tampered"
                else:
                    client.children[0]["priority"] = 4
                with self.assertRaisesRegex(
                    lane.ContractError,
                    rf"^create_issue read-back mismatched fields: {field}$",
                ):
                    lane.execute_command(client, raw, mode="apply", journal_path=journal)
                self.assertEqual(len(client.writes), 1)

    def test_mutation_apply_requires_idempotency_journal(self):
        with self.assertRaisesRegex(lane.ContractError, "require an idempotency journal"):
            lane.execute_command(
                FakeClient(),
                command("add_comment", {"body": "bounded"}),
                mode="apply",
            )

    def test_journal_rejects_same_key_for_different_state_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            first = command(
                "change_state",
                {"state": "In Review"},
                key="linear:SIS-59:state:stable-key",
            )
            lane.execute_command(client, first, mode="apply", journal_path=journal)
            conflicting = command(
                "change_state",
                {"state": "Todo"},
                key="linear:SIS-59:state:stable-key",
            )
            with self.assertRaisesRegex(lane.ContractError, "idempotency key conflict"):
                lane.execute_command(
                    client,
                    conflicting,
                    mode="apply",
                    journal_path=journal,
                )
            stored = json.loads(journal.read_text())
            self.assertEqual(len(stored), 1)
            self.assertNotIn("In Review", journal.read_text())
            self.assertNotIn("stable-key", journal.read_text())

    def test_initiative_management_fails_closed_on_ambiguity_scope_and_conflict(self):
        class InitiativeClient:
            def __init__(self):
                self.initiatives = [
                    {
                        "id": "initiative-one",
                        "name": "Personal operating system",
                        "description": "conflict",
                        "targetDate": "2026-12-31",
                        "projects": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    }
                ]

            def list_initiatives(self):
                return json.loads(json.dumps(self.initiatives))

        with self.assertRaisesRegex(
            lane.ContractError, "conflicts with managed fields"
        ):
            lane.execute_command(InitiativeClient(), initiative_command(), mode="plan")

        ambiguous = InitiativeClient()
        ambiguous.initiatives.append(
            {**ambiguous.initiatives[0], "id": "initiative-two"}
        )
        with self.assertRaisesRegex(lane.ContractError, "ambiguous.*initiative name"):
            lane.execute_command(ambiguous, initiative_command(), mode="plan")

        class LinkClient(InitiativeClient):
            def list_teams(self):
                return [{"id": "team-sis", "key": "SIS"}]

            def list_team_projects(self, team_id):
                return [
                    {
                        "id": "project-existing",
                        "name": "Hermes Experience",
                        "teams": {"nodes": [{"id": "other-team"}]},
                    }
                ]

        with self.assertRaisesRegex(lane.ContractError, "SIS team scope"):
            lane.execute_command(LinkClient(), initiative_link_command(), mode="plan")

    def test_linear_client_initiative_writes_use_minimal_fixed_graphql_payloads(self):
        client = object.__new__(lane.LinearClient)
        calls = []

        def execute(query, variables=None):
            calls.append((query, variables))
            if "initiativeToProjectCreate" in query:
                return {"initiativeToProjectCreate": {"success": True}}
            if "initiativeUpdate" in query:
                return {"initiativeUpdate": {"success": True}}
            return {"initiativeCreate": {"success": True}}

        client.execute = execute
        client.create_initiative(
            initiative_id="11111111-1111-4111-8111-111111111111",
            name="Personal operating system",
            description="Connected systems",
            target_date="2026-12-31",
        )
        client.update_initiative(
            "initiative-existing", new_name="Personal systems", target_date=None
        )
        client.create_initiative_project_link(
            link_id="22222222-2222-4222-8222-222222222222",
            initiative_id="initiative-existing",
            project_id="project-existing",
        )
        self.assertEqual(
            calls[0][1],
            {
                "input": {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "name": "Personal operating system",
                    "description": "Connected systems",
                    "targetDate": "2026-12-31",
                }
            },
        )
        self.assertEqual(
            calls[1][1],
            {
                "id": "initiative-existing",
                "input": {"name": "Personal systems", "targetDate": None},
            },
        )
        self.assertEqual(
            calls[2][1],
            {
                "input": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "initiativeId": "initiative-existing",
                    "projectId": "project-existing",
                }
            },
        )
        for query, _variables in calls:
            self.assertNotIn("Delete", query)
            self.assertNotIn("Archive", query)

    def test_initiative_project_link_rejects_initiative_identity_drift_on_readback(self):
        class DriftingLinkClient:
            def __init__(self):
                self.initiative_reads = 0
                self.projects = []

            def list_teams(self):
                return [{"id": "team-sis", "key": "SIS"}]

            def list_team_projects(self, team_id):
                return [
                    {
                        "id": "project-existing",
                        "name": "Hermes Experience",
                        "teams": {"nodes": [{"id": "team-sis"}]},
                    }
                ]

            def list_initiatives(self):
                self.initiative_reads += 1
                return [
                    {
                        "id": (
                            "initiative-existing"
                            if self.initiative_reads == 1
                            else "initiative-replaced"
                        ),
                        "name": "Personal operating system",
                        "description": None,
                        "targetDate": None,
                    }
                ]

            def list_initiative_projects(self, initiative_id):
                return json.loads(json.dumps(self.projects))

            def create_initiative_project_link(
                self, *, link_id, initiative_id, project_id
            ):
                self.projects = [{"id": project_id, "name": "Hermes Experience"}]

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            lane.ContractError, "exact read-back"
        ):
            lane.execute_command(
                DriftingLinkClient(),
                initiative_link_command(),
                mode="apply",
                journal_path=Path(tmp) / "initiative-link-drift.json",
            )

    def test_exact_sis_project_link_to_initiative_applies_and_replays(self):
        class LinkClient:
            def __init__(self):
                self.projects = [
                    {
                        "id": "project-existing",
                        "name": "Hermes Experience",
                        "teams": {"nodes": [{"id": "team-sis"}]},
                    }
                ]
                self.initiatives = [
                    {
                        "id": "initiative-existing",
                        "name": "Personal operating system",
                        "description": None,
                        "targetDate": None,
                        "projects": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    }
                ]
                self.writes = []

            def list_teams(self):
                return [{"id": "team-sis", "key": "SIS", "name": "Sisyphus"}]

            def list_team_projects(self, team_id):
                return json.loads(json.dumps(self.projects))

            def list_initiatives(self):
                return json.loads(json.dumps(self.initiatives))

            def list_initiative_projects(self, initiative_id):
                return json.loads(
                    json.dumps(self.initiatives[0]["projects"]["nodes"])
                )

            def create_initiative_project_link(
                self, *, link_id, initiative_id, project_id
            ):
                self.writes.append(
                    {
                        "link_id": link_id,
                        "initiative_id": initiative_id,
                        "project_id": project_id,
                    }
                )
                self.initiatives[0]["projects"]["nodes"].append(
                    json.loads(json.dumps(self.projects[0]))
                )

        with tempfile.TemporaryDirectory() as tmp:
            client = LinkClient()
            raw = initiative_link_command()
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(
                planned["plan"],
                [
                    {
                        "action": "link_project_to_initiative",
                        "project": "Hermes Experience",
                        "initiative": "Personal operating system",
                    }
                ],
            )
            self.assertEqual(client.writes, [])

            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "initiative-link.json",
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(
                applied["after"],
                {
                    "initiative": "Personal operating system",
                    "project": "Hermes Experience",
                },
            )
            self.assertEqual(uuid.UUID(client.writes[0]["link_id"]).version, 4)

            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "initiative-link-replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_initiative_update_exact_selector_applies_minimal_fields_and_replays(self):
        class InitiativeClient:
            def __init__(self):
                self.initiatives = [
                    {
                        "id": "initiative-existing",
                        "name": "Personal operating system",
                        "description": "Old",
                        "targetDate": "2026-01-01",
                    }
                ]
                self.writes = []

            def list_initiatives(self):
                return json.loads(json.dumps(self.initiatives))

            def update_initiative(self, initiative_id, **fields):
                self.writes.append((initiative_id, fields))
                current = self.initiatives[0]
                current["name"] = fields.get("new_name", current["name"])
                if "description" in fields:
                    current["description"] = fields["description"]
                if "target_date" in fields:
                    current["targetDate"] = fields["target_date"]

        with tempfile.TemporaryDirectory() as tmp:
            client = InitiativeClient()
            raw = initiative_command("update_initiative")
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(
                planned["plan"],
                [
                    {
                        "action": "update_initiative",
                        "name": "Personal systems",
                        "fields": ["new_name", "description", "target_date"],
                        "target_date": None,
                    }
                ],
            )
            self.assertEqual(client.writes, [])

            journal = Path(tmp) / "initiative-update.json"
            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(
                client.writes,
                [
                    (
                        "initiative-existing",
                        {
                            "new_name": "Personal systems",
                            "description": "Unified personal systems",
                            "target_date": None,
                        },
                    )
                ],
            )
            self.assertEqual(
                applied["after"], {"name": "Personal systems", "target_date": None}
            )
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_initiative_create_plans_applies_exact_readback_and_replays(self):
        class InitiativeClient:
            def __init__(self):
                self.initiatives = []
                self.writes = []

            def list_initiatives(self):
                return json.loads(json.dumps(self.initiatives))

            def create_initiative(self, **values):
                self.writes.append(("create_initiative", values))
                self.initiatives.append(
                    {
                        "id": values["initiative_id"],
                        "name": values["name"],
                        "description": values.get("description"),
                        "targetDate": values.get("target_date"),
                    }
                )

        with tempfile.TemporaryDirectory() as tmp:
            client = InitiativeClient()
            raw = initiative_command()
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(
                planned["plan"],
                [
                    {
                        "action": "create_initiative",
                        "name": "Personal operating system",
                        "target_date": "2026-12-31",
                    }
                ],
            )
            self.assertEqual(client.writes, [])

            applied = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "initiative.json",
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(
                applied["after"],
                {
                    "name": "Personal operating system",
                    "target_date": "2026-12-31",
                },
            )
            self.assertEqual(uuid.UUID(client.writes[0][1]["initiative_id"]).version, 4)

            replay = lane.execute_command(
                client,
                raw,
                mode="apply",
                journal_path=Path(tmp) / "initiative-replay.json",
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)
            self.assertEqual(len(client.initiatives), 1)

    def test_project_management_validates_exact_bounded_shapes_and_dates(self):
        for raw in (
            project_command(), milestone_command(),
            project_command("update_project"), milestone_command("update_milestone"),
        ):
            with self.subTest(operation=raw["operation"]):
                self.assertIs(lane.validate_command(raw), raw)
        invalid = project_command()
        invalid["change"]["team_id"] = "arbitrary-team"
        with self.assertRaises(lane.ContractError):
            lane.validate_command(invalid)
        for unsafe in ("<!-- linear-command:v2 reserved -->", "lin_api_" + "A" * 32):
            invalid = project_command()
            invalid["change"]["description"] = unsafe
            with self.assertRaises(lane.ContractError):
                lane.validate_command(invalid)
        invalid = milestone_command("update_milestone")
        invalid["change"]["target_date"] = "2026-02-30"
        with self.assertRaisesRegex(lane.ContractError, "valid calendar date"):
            lane.validate_command(invalid)

    def test_project_and_milestone_create_apply_exact_readback_and_replay(self):
        class ManagementClient:
            def __init__(self):
                self.projects, self.milestones, self.writes = [], [], []
            def list_teams(self):
                return [{"id": "team-sis", "key": "SIS", "name": "Sisyphus"}]
            def list_team_projects(self, team_id):
                return json.loads(json.dumps(self.projects))
            def list_project_milestones(self, project_id):
                return json.loads(json.dumps(self.milestones))
            def create_project(self, **values):
                self.writes.append(("create_project", values))
                self.projects.append({"id": values["project_id"], "name": values["name"],
                    "description": values.get("description"), "targetDate": values.get("target_date"),
                    "teams": {"nodes": [{"id": values["team_id"]}]}})
            def create_project_milestone(self, **values):
                self.writes.append(("create_milestone", values))
                self.milestones.append({"id": values["milestone_id"], "name": values["name"],
                    "description": values.get("description"), "targetDate": values.get("target_date"),
                    "project": {"id": values["project_id"]}})

        with tempfile.TemporaryDirectory() as tmp:
            client = ManagementClient()
            raw = project_command()
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(planned["plan"], [{"action": "create_project", "name": "Hermes Experience", "target_date": "2026-12-31"}])
            self.assertEqual(planned["after"], {"name": "Hermes Experience", "target_date": "2026-12-31"})
            applied = lane.execute_command(client, raw, mode="apply", journal_path=Path(tmp) / "p.json")
            self.assertEqual(applied["after"], {"name": "Hermes Experience", "target_date": "2026-12-31"})
            candidate = client.writes[0][1]["project_id"]
            self.assertEqual(uuid.UUID(candidate).version, 4)
            second_client = ManagementClient()
            lane.execute_command(second_client, raw, mode="apply", journal_path=Path(tmp) / "p2.json")
            self.assertEqual(second_client.writes[0][1]["project_id"], candidate)
            self.assertEqual(lane.execute_command(client, raw, mode="apply", journal_path=Path(tmp) / "pr.json")["result"], "no_op")

            raw = milestone_command()
            applied = lane.execute_command(client, raw, mode="apply", journal_path=Path(tmp) / "m.json")
            self.assertEqual(applied["after"], {"project": "Hermes Experience", "name": "Calendar integration", "target_date": "2026-10-01"})
            self.assertEqual(uuid.UUID(client.writes[-1][1]["milestone_id"]).version, 4)
            self.assertEqual(lane.execute_command(client, raw, mode="apply", journal_path=Path(tmp) / "mr.json")["result"], "no_op")
            self.assertEqual(len(client.writes), 2)

    def test_project_management_accepts_sis_project_shared_with_another_team(self):
        class SharedProjectClient:
            def list_teams(self):
                return [{"id": "team-sis", "key": "SIS", "name": "Sisyphus"}]

            def list_team_projects(self, team_id):
                return [
                    {
                        "id": "shared-project",
                        "name": "Hermes Experience",
                        "description": "User-facing integrations",
                        "targetDate": "2026-12-31",
                        "teams": {
                            "nodes": [{"id": "team-sis"}, {"id": "other-team"}]
                        },
                    }
                ]

        result = lane.execute_command(
            SharedProjectClient(),
            project_command(),
            mode="plan",
        )
        self.assertEqual(result["result"], "no_op")
        self.assertEqual(result["plan"], [])

    def test_exact_project_and_milestone_edits_replay_without_duplicate_writes(self):
        class EditClient:
            def __init__(self):
                self.projects = [{"id": "project-existing", "name": "Hermes Experience", "description": "old", "targetDate": "2026-01-01", "teams": {"nodes": [{"id": "team-sis"}]}}]
                self.milestones = [{"id": "milestone-existing", "name": "Calendar integration", "description": "old", "targetDate": "2026-02-01", "project": {"id": "project-existing"}}]
                self.writes = []
            def list_teams(self):
                return [{"id": "team-sis", "key": "SIS", "name": "Sisyphus"}]
            def list_team_projects(self, team_id):
                return json.loads(json.dumps(self.projects))
            def list_project_milestones(self, project_id):
                return json.loads(json.dumps(self.milestones))
            def update_project(self, project_id, **fields):
                self.writes.append(("update_project", project_id, fields))
                item = self.projects[0]
                item["name"] = fields.get("new_name", item["name"])
                if "description" in fields:
                    item["description"] = fields["description"]
                if "target_date" in fields:
                    item["targetDate"] = fields["target_date"]
            def update_project_milestone(self, milestone_id, **fields):
                self.writes.append(("update_milestone", milestone_id, fields))
                item = self.milestones[0]
                item["name"] = fields.get("new_name", item["name"])
                if "description" in fields:
                    item["description"] = fields["description"]
                if "target_date" in fields:
                    item["targetDate"] = fields["target_date"]

        with tempfile.TemporaryDirectory() as tmp:
            client = EditClient()
            raw = project_command("update_project")
            journal = Path(tmp) / "up.json"
            applied = lane.execute_command(client, raw, mode="apply", journal_path=journal)
            self.assertEqual(applied["after"], {"name": "Hermes Personal Experience", "target_date": None})
            self.assertEqual(lane.execute_command(client, raw, mode="apply", journal_path=journal)["result"], "no_op")
            self.assertEqual(len(client.writes), 1)
            client = EditClient()
            raw = milestone_command("update_milestone")
            journal = Path(tmp) / "um.json"
            applied = lane.execute_command(client, raw, mode="apply", journal_path=journal)
            self.assertEqual(applied["after"], {"project": "Hermes Experience", "name": "Calendar and reminders", "target_date": None})
            self.assertEqual(lane.execute_command(client, raw, mode="apply", journal_path=journal)["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_project_management_fails_closed_on_exact_name_ambiguity(self):
        class AmbiguousClient:
            def list_teams(self): return [{"id": "team-sis", "key": "SIS", "name": "Sisyphus"}]
            def list_team_projects(self, team_id):
                item = {"id": "one", "name": "Hermes Experience", "teams": {"nodes": [{"id": "team-sis"}]}}
                return [item, {**item, "id": "two"}]
        with self.assertRaisesRegex(lane.ContractError, "ambiguous.*project name"):
            lane.execute_command(AmbiguousClient(), project_command(), mode="plan")

        class MissingClient(AmbiguousClient):
            def list_team_projects(self, team_id):
                return []
        with self.assertRaisesRegex(lane.ContractError, "exact Linear project not found"):
            lane.execute_command(MissingClient(), project_command("update_project"), mode="plan")

    def test_linear_client_project_updates_use_minimal_fixed_graphql_payloads(self):
        client = object.__new__(lane.LinearClient)
        calls = []
        def execute(query, variables=None):
            calls.append((query, variables))
            return {"projectUpdate": {"success": True}} if "projectUpdate" in query else {"projectMilestoneUpdate": {"success": True}}
        client.execute = execute
        client.update_project("project-id", new_name="Renamed", description="", target_date=None)
        client.update_project_milestone("milestone-id", new_name="M", target_date="2026-09-30")
        self.assertEqual(calls[0][1], {"id": "project-id", "input": {"name": "Renamed", "description": "", "targetDate": None}})
        self.assertEqual(calls[1][1], {"id": "milestone-id", "input": {"name": "M", "targetDate": "2026-09-30"}})
        with self.assertRaises(lane.ContractError):
            client.create_project(project_id="id", team_id="team", name="name", leadId="forbidden")


if __name__ == "__main__":
    unittest.main()
