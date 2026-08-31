import concurrent.futures
import contextlib
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
    }


class FakeClient:
    def __init__(self, state="In Progress"):
        self.current = issue(state)
        self.comments = []
        self.children = []
        self.writes = []

    def get_issue(self, identifier):
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
        for state in ("Done", "Canceled", "Duplicate"):
            with self.assertRaisesRegex(lane.ContractError, "owner-controlled"):
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
            {"title": "No"},
            {"priority": "Urgent"},
            {"description": "New", "description_transform": "remove_links"},
            {"description_transform": "unknown"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(lane.ContractError):
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
                return {"issue": {"children": {"nodes": [{**issue(), "description": "marker"}], "pageInfo": {"hasNextPage": False}}}}
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


if __name__ == "__main__":
    unittest.main()
