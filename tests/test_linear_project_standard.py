import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "linear_project_standard.py"
SPEC = importlib.util.spec_from_file_location("linear_project_standard", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import standard reconciler: {SCRIPT}")
standard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = standard
SPEC.loader.exec_module(standard)


def manifest():
    return {
        "schemaVersion": 1,
        "team": "ENG",
        "project": {"name": "Example"},
        "milestones": [
            {
                "name": "Discovery",
                "issues": [
                    {
                        "title": "Research",
                        "state": "Todo",
                        "subIssues": [{"title": "Interview users", "state": "Todo"}],
                    }
                ],
            }
        ],
    }


def team():
    return {
        "id": "team-1",
        "name": "Engineering",
        "key": "ENG",
        "states": {"nodes": [{"id": "todo-1", "name": "Todo", "type": "unstarted"}]},
    }


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_loads_from_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(manifest()))
            loaded = standard.load_manifest(path)
        self.assertEqual(loaded["project"]["name"], "Example")

    def test_rejects_duplicate_milestones(self):
        raw = manifest()
        raw["milestones"].append(raw["milestones"][0].copy())
        with self.assertRaisesRegex(standard.ContractError, "duplicate milestone"):
            standard.validate_manifest(raw)

    def test_rejects_duplicate_titles_across_milestones(self):
        raw = manifest()
        raw["milestones"].append(
            {
                "name": "Delivery",
                "issues": [{"title": "Research", "subIssues": []}],
            }
        )
        with self.assertRaisesRegex(standard.ContractError, "across manifest hierarchy"):
            standard.validate_manifest(raw)

    def test_rejects_duplicate_sub_issues(self):
        raw = manifest()
        issue = raw["milestones"][0]["issues"][0]
        issue["subIssues"].append(issue["subIssues"][0].copy())
        with self.assertRaisesRegex(standard.ContractError, "duplicate sub-issue"):
            standard.validate_manifest(raw)

    def test_rejects_invalid_milestone_description(self):
        raw = manifest()
        raw["milestones"][0]["description"] = {"not": "text"}
        with self.assertRaisesRegex(standard.ContractError, r"milestones\[0\].description"):
            standard.validate_manifest(raw)

    def test_rejects_unknown_fields(self):
        raw = manifest()
        raw["project"]["surprise"] = True
        with self.assertRaisesRegex(standard.ContractError, "supports only"):
            standard.validate_manifest(raw)


class PlanningTests(unittest.TestCase):
    def test_missing_project_plans_full_hierarchy(self):
        live = standard.LiveContext(team=team(), project=None, milestones=[], issues=[])
        actions = standard.build_plan(manifest(), live)
        self.assertEqual(
            [item["action"] for item in actions],
            ["create_project", "create_milestone", "create_issue", "create_sub_issue"],
        )

    def test_existing_hierarchy_is_converged(self):
        live = standard.LiveContext(
            team=team(),
            project={"id": "project-1", "name": "Example"},
            milestones=[{"id": "milestone-1", "name": "Discovery"}],
            issues=[
                {
                    "id": "issue-1",
                    "identifier": "ENG-1",
                    "title": "Research",
                    "parent": None,
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                },
                {
                    "id": "issue-2",
                    "identifier": "ENG-2",
                    "title": "Interview users",
                    "parent": {"id": "issue-1"},
                    "projectMilestone": None,
                },
            ],
        )
        self.assertEqual(standard.build_plan(manifest(), live), [])

    def test_duplicate_live_parent_title_fails_even_when_one_is_correct(self):
        live = standard.LiveContext(
            team=team(),
            project={"id": "project-1", "name": "Example"},
            milestones=[{"id": "milestone-1", "name": "Discovery"}],
            issues=[
                {
                    "id": "correct-parent",
                    "identifier": "ENG-1",
                    "title": "Research",
                    "parent": None,
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                },
                {
                    "id": "duplicate-parent",
                    "identifier": "ENG-2",
                    "title": "Research",
                    "parent": None,
                    "projectMilestone": {"id": "other-milestone", "name": "Other"},
                },
            ],
        )
        with self.assertRaisesRegex(standard.ContractError, "duplicate live issue title"):
            standard.build_plan(manifest(), live)

    def test_duplicate_live_child_title_fails_even_when_one_is_correct(self):
        live = standard.LiveContext(
            team=team(),
            project={"id": "project-1", "name": "Example"},
            milestones=[{"id": "milestone-1", "name": "Discovery"}],
            issues=[
                {
                    "id": "parent",
                    "identifier": "ENG-1",
                    "title": "Research",
                    "parent": None,
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                },
                {
                    "id": "correct-child",
                    "identifier": "ENG-2",
                    "title": "Interview users",
                    "parent": {"id": "parent"},
                    "projectMilestone": None,
                },
                {
                    "id": "duplicate-child",
                    "identifier": "ENG-3",
                    "title": "Interview users",
                    "parent": {"id": "other-parent"},
                    "projectMilestone": None,
                },
            ],
        )
        with self.assertRaisesRegex(standard.ContractError, "duplicate live issue title"):
            standard.build_plan(manifest(), live)

    def test_direct_milestone_on_child_fails_closed(self):
        live = standard.LiveContext(
            team=team(),
            project={"id": "project-1", "name": "Example"},
            milestones=[{"id": "milestone-1", "name": "Discovery"}],
            issues=[
                {
                    "id": "parent",
                    "identifier": "ENG-1",
                    "title": "Research",
                    "parent": None,
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                },
                {
                    "id": "child",
                    "identifier": "ENG-2",
                    "title": "Interview users",
                    "parent": {"id": "parent"},
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                },
            ],
        )
        with self.assertRaisesRegex(standard.ContractError, "different hierarchy"):
            standard.build_plan(manifest(), live)

    def test_title_in_wrong_hierarchy_fails_closed(self):
        live = standard.LiveContext(
            team=team(),
            project={"id": "project-1", "name": "Example"},
            milestones=[{"id": "milestone-1", "name": "Discovery"}],
            issues=[
                {
                    "id": "issue-1",
                    "identifier": "ENG-1",
                    "title": "Research",
                    "parent": {"id": "other-parent"},
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                }
            ],
        )
        with self.assertRaisesRegex(standard.ContractError, "different hierarchy"):
            standard.build_plan(manifest(), live)

    def test_missing_parent_with_colliding_child_title_fails_closed(self):
        live = standard.LiveContext(
            team=team(),
            project={"id": "project-1", "name": "Example"},
            milestones=[{"id": "milestone-1", "name": "Discovery"}],
            issues=[
                {
                    "id": "existing-child",
                    "identifier": "ENG-7",
                    "title": "Interview users",
                    "parent": {"id": "other-parent"},
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                }
            ],
        )
        with self.assertRaisesRegex(standard.ContractError, "different hierarchy"):
            standard.build_plan(manifest(), live)

    def test_live_reference_validation_checks_every_state_before_apply(self):
        raw = manifest()
        raw["milestones"][0]["issues"][0]["subIssues"][0]["state"] = "Missing"
        live = standard.LiveContext(team=team(), project=None, milestones=[], issues=[])
        with self.assertRaisesRegex(standard.ContractError, "workflow state not found"):
            standard.validate_live_references(raw, live)

    def test_state_resolution_is_exact_and_fail_closed(self):
        self.assertEqual(standard.resolve_state(team(), "Todo"), "todo-1")
        with self.assertRaisesRegex(standard.ContractError, "workflow state not found"):
            standard.resolve_state(team(), "In Progress")


class ApplyTests(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.inputs = []
            self.issue_number = 0

        def execute(self, query, variables=None):
            variables = variables or {}
            self.inputs.append((query, variables))
            if query == standard.PROJECT_CREATE:
                return {
                    "projectCreate": {
                        "success": True,
                        "project": {"id": "project-1", "name": "Example"},
                    }
                }
            if query == standard.MILESTONE_CREATE:
                return {
                    "projectMilestoneCreate": {
                        "success": True,
                        "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                    }
                }
            if query == standard.ISSUE_CREATE:
                self.issue_number += 1
                issue_input = variables["input"]
                return {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": f"issue-{self.issue_number}",
                            "identifier": f"ENG-{self.issue_number}",
                            "title": issue_input["title"],
                        },
                    }
                }
            raise AssertionError("unexpected query")

    def test_apply_creates_project_milestone_issue_and_linked_sub_issue(self):
        client = self.FakeClient()
        live = standard.LiveContext(team=team(), project=None, milestones=[], issues=[])
        applied = standard.apply_manifest(client, manifest(), live)
        self.assertEqual(
            [item["action"] for item in applied],
            ["created_project", "created_milestone", "created_issue", "created_sub_issue"],
        )
        issue_inputs = [
            variables["input"]
            for query, variables in client.inputs
            if query == standard.ISSUE_CREATE
        ]
        self.assertNotIn("parentId", issue_inputs[0])
        self.assertEqual(issue_inputs[1]["parentId"], "issue-1")
        self.assertEqual(issue_inputs[0]["projectMilestoneId"], "milestone-1")
        self.assertNotIn("projectMilestoneId", issue_inputs[1])


    def test_apply_uses_parent_in_declared_milestone(self):
        client = self.FakeClient()
        live = standard.LiveContext(
            team=team(),
            project={"id": "project-1", "name": "Example"},
            milestones=[{"id": "milestone-1", "name": "Discovery"}],
            issues=[
                {
                    "id": "wrong-parent",
                    "identifier": "ENG-8",
                    "title": "Other research",
                    "parent": None,
                    "projectMilestone": {"id": "other-milestone", "name": "Other"},
                },
                {
                    "id": "correct-parent",
                    "identifier": "ENG-9",
                    "title": "Research",
                    "parent": None,
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                },
            ],
        )
        applied = standard.apply_manifest(client, manifest(), live)
        self.assertEqual([item["action"] for item in applied], ["created_sub_issue"])
        issue_input = next(
            variables["input"]
            for query, variables in client.inputs
            if query == standard.ISSUE_CREATE
        )
        self.assertEqual(issue_input["parentId"], "correct-parent")

    def test_apply_rejects_direct_milestone_on_existing_child(self):
        client = self.FakeClient()
        live = standard.LiveContext(
            team=team(),
            project={"id": "project-1", "name": "Example"},
            milestones=[{"id": "milestone-1", "name": "Discovery"}],
            issues=[
                {
                    "id": "parent",
                    "identifier": "ENG-1",
                    "title": "Research",
                    "parent": None,
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                },
                {
                    "id": "child",
                    "identifier": "ENG-2",
                    "title": "Interview users",
                    "parent": {"id": "parent"},
                    "projectMilestone": {"id": "milestone-1", "name": "Discovery"},
                },
            ],
        )
        with self.assertRaisesRegex(standard.ContractError, "different hierarchy"):
            standard.apply_manifest(client, manifest(), live)


class ClientTests(unittest.TestCase):
    def test_authorization_header_is_preserved_for_api_keys_and_oauth(self):
        raw = standard.LinearClient("lin_api_fixture", endpoint="https://example.invalid")
        bearer = standard.LinearClient("Bearer fixture", endpoint="https://example.invalid")
        self.assertEqual(raw.authorization, "lin_api_fixture")
        self.assertEqual(bearer.authorization, "Bearer fixture")

    def test_empty_token_is_rejected(self):
        with self.assertRaisesRegex(standard.ContractError, "LINEAR_TOKEN is empty"):
            standard.LinearClient("")


if __name__ == "__main__":
    unittest.main()
