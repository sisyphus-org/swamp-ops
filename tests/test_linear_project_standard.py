import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "linear_project_standard.py"
MANIFEST_DIR = Path(__file__).parents[1] / "manifests" / "linear"
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


def identified_manifest(identifier="ENG-1", title="Research"):
    return {
        "schemaVersion": 1,
        "team": "ENG",
        "project": {"name": "Example"},
        "milestones": [
            {
                "name": "Discovery",
                "issues": [{"identifier": identifier, "title": title}],
            }
        ],
    }


def identified_live(*, title="Research", parent=None, milestone=None):
    return standard.LiveContext(
        team=team(),
        project={"id": "project-1", "name": "Example"},
        milestones=[{"id": "milestone-1", "name": "Discovery"}],
        issues=[
            {
                "id": "issue-1",
                "identifier": "ENG-1",
                "title": title,
                "parent": parent,
                "projectMilestone": milestone,
            }
        ],
    )


def team():
    return {
        "id": "team-1",
        "name": "Engineering",
        "key": "ENG",
        "states": {"nodes": [{"id": "todo-1", "name": "Todo", "type": "unstarted"}]},
    }


class ManifestTests(unittest.TestCase):
    def test_cli_apply_rejects_before_manifest_token_client_or_network_access(self):
        output = io.StringIO()
        argv = ["linear_project_standard.py", "--manifest", "missing.json", "--mode", "apply"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            standard, "load_manifest"
        ) as load_manifest, mock.patch.object(
            standard, "LinearClient"
        ) as client_cls, contextlib.redirect_stdout(output):
            code = standard.main()

        self.assertEqual(code, 1)
        load_manifest.assert_not_called()
        client_cls.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "apply")
        self.assertEqual(payload["result"], "error")
        self.assertIn("Project Manager", payload["issues"][0])

    def test_swamp_workflow_exposes_only_a_fixed_plan_entry_point(self):
        workflow = (
            Path(__file__).parents[1]
            / "workflows"
            / "workflow-linear-project-standard.yaml"
        ).read_text()
        self.assertNotIn("inputs.mode", workflow)
        self.assertNotIn("  mode:\n", workflow)
        self.assertNotIn("--mode apply", workflow)
        self.assertIn("--mode plan", workflow)

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

    def test_rejects_invalid_or_duplicate_identifiers(self):
        raw = identified_manifest(identifier="not-an-id")
        with self.assertRaisesRegex(standard.ContractError, "explicit Linear issue identifier"):
            standard.validate_manifest(raw)
        raw = identified_manifest()
        raw["milestones"].append(
            {
                "name": "Delivery",
                "issues": [{"identifier": "ENG-1", "title": "Ship"}],
            }
        )
        with self.assertRaisesRegex(standard.ContractError, "duplicate issue identifier"):
            standard.validate_manifest(raw)
    def test_committed_manifests_cover_current_projects_but_not_unprojected_issues(self):
        paths = sorted(MANIFEST_DIR.glob("*.json"))
        loaded = [standard.load_manifest(path) for path in paths]
        self.assertEqual(
            {item["project"]["name"] for item in loaded},
            {
                "Hermes Foundation",
                "Hermes Experience",
                "Home Infrastructure",
                "Knowledge System",
                "Crypto X Daily Intelligence Digest",
                "Книги",
            },
        )
        identifiers = {
            issue["identifier"]
            for item in loaded
            for milestone in item["milestones"]
            for issue in milestone["issues"]
            if "identifier" in issue
        }
        self.assertEqual(
            identifiers,
            {
                "SIS-6", "SIS-7", "SIS-8", "SIS-9", "SIS-10", "SIS-11",
                "SIS-12", "SIS-13", "SIS-14", "SIS-15", "SIS-16", "SIS-17",
                "SIS-18", "SIS-19", "SIS-20", "SIS-21", "SIS-22", "SIS-25",
                "SIS-28", "SIS-44", "SIS-45", "SIS-46", "SIS-47", "SIS-48",
                "SIS-51",
            },
        )


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

    def test_identified_issue_without_milestone_plans_assignment(self):
        actions = standard.build_plan(identified_manifest(), identified_live())
        self.assertEqual(
            actions,
            [
                {
                    "action": "assign_issue_milestone",
                    "id": "ENG-1",
                    "title": "Research",
                    "milestone": "Discovery",
                }
            ],
        )

    def test_identified_issue_in_wrong_milestone_plans_reassignment(self):
        actions = standard.build_plan(
            identified_manifest(),
            identified_live(milestone={"id": "milestone-old", "name": "Old"}),
        )
        self.assertEqual(actions[0]["action"], "reassign_issue_milestone")
        self.assertEqual(actions[0]["from"], "Old")

    def test_identified_issue_wrong_id_or_title_fails_closed(self):
        with self.assertRaisesRegex(standard.ContractError, "identified issue not found"):
            standard.build_plan(identified_manifest(identifier="ENG-2"), identified_live())
        with self.assertRaisesRegex(standard.ContractError, "title mismatch"):
            standard.build_plan(
                identified_manifest(title="Expected title"), identified_live()
            )

    def test_identified_sub_issue_cannot_receive_direct_milestone(self):
        with self.assertRaisesRegex(standard.ContractError, "must be top-level"):
            standard.build_plan(
                identified_manifest(), identified_live(parent={"id": "parent-1"})
            )

    def test_identified_issue_is_converged_only_at_exact_milestone(self):
        live = identified_live(
            milestone={"id": "milestone-1", "name": "Discovery"}
        )
        self.assertEqual(standard.build_plan(identified_manifest(), live), [])

    def test_identified_issue_requires_existing_declared_project(self):
        live = standard.LiveContext(team=team(), project=None, milestones=[], issues=[])
        with self.assertRaisesRegex(standard.ContractError, "declared project does not exist"):
            standard.build_plan(identified_manifest(), live)

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


class ClientTests(unittest.TestCase):
    def test_module_exports_no_legacy_mutation_surface(self):
        forbidden = {
            "PROJECT_CREATE",
            "MILESTONE_CREATE",
            "ISSUE_CREATE",
            "ISSUE_UPDATE",
            "create_project",
            "create_milestone",
            "create_issue",
            "update_issue_milestone",
            "apply_manifest",
        }
        self.assertEqual(forbidden.intersection(vars(standard)), set())

    def test_client_rejects_arbitrary_mutation_before_request_or_network(self):
        client = standard.LinearClient("lin_api_fixture", endpoint="https://example.invalid")
        mutation = "mutation Arbitrary { issueDelete(id: \"fixture\") { success } }"
        with mock.patch.object(standard.urllib.request, "Request") as request_cls, mock.patch.object(
            standard.urllib.request, "urlopen"
        ) as urlopen:
            with self.assertRaisesRegex(standard.ContractError, "fixed read query"):
                client.execute(mutation)
        request_cls.assert_not_called()
        urlopen.assert_not_called()

    def test_client_read_allowlist_is_exact_and_fixed(self):
        self.assertEqual(
            standard.READ_QUERIES,
            frozenset(
                {
                    standard.TEAMS_QUERY,
                    standard.TEAM_STATES_QUERY,
                    standard.PROJECTS_QUERY,
                    standard.MILESTONES_QUERY,
                    standard.ISSUES_QUERY,
                }
            ),
        )

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

