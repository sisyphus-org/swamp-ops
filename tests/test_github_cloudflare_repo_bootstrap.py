import importlib.util
import json
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "github_cloudflare_repo_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("github_cloudflare_repo_bootstrap", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import planner: {SCRIPT}")
planner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planner
SPEC.loader.exec_module(planner)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        response = self.responses.get(path)
        if response is None:
            raise AssertionError(f"unexpected GitHub GET: {path}")
        return response


def result(status, payload=None):
    return planner.ApiResult(status, payload if payload is not None else {})


def ready_responses(repository="example-site"):
    org = planner.STANDARD["organization"]
    template = planner.STANDARD["templateRepository"]
    responses = {
        "/user": result(200, {"login": "alexxpetrov"}),
        f"/repos/{org}/{repository}": result(404, {"message": "Not Found"}),
        f"/repos/{template}": result(
            200,
            {"full_name": template, "is_template": True, "default_branch": "main"},
        ),
        f"/repos/{template}/commits/main": result(200, {"sha": "a" * 40}),
        f"/user/memberships/orgs/{org}": result(
            200, {"state": "active", "role": "admin"}
        ),
        f"/orgs/{org}/actions/secrets/CLOUDFLARE_API_TOKEN": result(
            200, {"name": "CLOUDFLARE_API_TOKEN", "visibility": "selected"}
        ),
        f"/orgs/{org}/actions/secrets/CLOUDFLARE_ACCOUNT_ID": result(
            200, {"name": "CLOUDFLARE_ACCOUNT_ID", "visibility": "selected"}
        ),
    }
    for path in planner.STANDARD["requiredTemplateFiles"]:
        responses[planner.content_path(template, path, ref="a" * 40)] = result(
            200, {"type": "file", "path": path}
        )
    return responses


class InputTests(unittest.TestCase):
    def test_repository_name_is_the_only_owner_input(self):
        args = planner.parse_args(["--repository", "new-site"])
        self.assertEqual(args.repository, "new-site")
        with self.assertRaises(SystemExit):
            planner.parse_args(["--repository", "new-site", "--mode", "apply"])

    def test_rejects_names_outside_standard_without_normalizing(self):
        for value in ("My-Site", "my_site", "a", "1site", "x" * 56):
            with self.subTest(value=value):
                with self.assertRaises(planner.ContractError):
                    planner.validate_repository(value)

    def test_derived_worker_names_fit_workers_dev(self):
        value = "a" + "b" * 53
        self.assertEqual(planner.validate_repository(value), value)
        self.assertLessEqual(len(f"{value}-preview"), 63)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.previous_approved_revision = planner.STANDARD.get(
            "approvedTemplateRevision"
        )
        planner.STANDARD["approvedTemplateRevision"] = "a" * 40

    def tearDown(self):
        if self.previous_approved_revision is None:
            planner.STANDARD.pop("approvedTemplateRevision", None)
        else:
            planner.STANDARD["approvedTemplateRevision"] = (
                self.previous_approved_revision
            )

    def test_ready_plan_uses_versioned_defaults_and_is_read_only(self):
        client = FakeClient(ready_responses())
        plan = planner.build_plan("example-site", client)

        self.assertTrue(plan["ready"])
        self.assertTrue(plan["readOnly"])
        self.assertEqual(plan["mode"], "plan")
        self.assertEqual(plan["target"]["repository"], "sisyphus-org/example-site")
        self.assertEqual(plan["target"]["productionWorker"], "example-site")
        self.assertEqual(plan["target"]["previewWorker"], "example-site-preview")
        self.assertEqual(plan["standard"]["environment"], "branch-preview")
        self.assertEqual(plan["standard"]["requiredReviewer"], "alexxpetrov")
        self.assertEqual(plan["blockers"], [])
        self.assertNotIn("secretValues", plan["standard"])
        grant = next(
            item
            for item in plan["plannedActions"]
            if item["action"] == "ensure_repository_access_to_org_secrets"
        )
        self.assertEqual(grant["repository"], "sisyphus-org/example-site")
        self.assertEqual(grant["selectedSecretNames"], planner.STANDARD["requiredOrgSecrets"])
        self.assertEqual(plan["source"]["templateRevision"], "a" * 40)
        settings = next(
            item["settings"]
            for item in plan["plannedActions"]
            if item["action"] == "configure_repository_settings"
        )
        self.assertNotIn("cloudflareGitBuilds", settings)
        self.assertTrue(all("delete" not in item["action"] for item in plan["plannedActions"]))
        self.assertTrue(all("webhook" not in item["action"] for item in plan["plannedActions"]))

    def test_future_repository_write_uses_the_pinned_template_tree(self):
        plan = planner.build_plan("example-site", FakeClient(ready_responses()))

        actions = plan["plannedActions"]
        self.assertEqual(actions[0]["action"], "create_empty_repository")
        self.assertEqual(
            actions[1],
            {
                "order": 2,
                "action": "materialize_approved_template_revision",
                "template": planner.STANDARD["templateRepository"],
                "templateRevision": "a" * 40,
                "destinationRepository": "sisyphus-org/example-site",
            },
        )
        self.assertNotIn(
            "generate_repository_from_template",
            [action["action"] for action in actions],
        )
        initial_commit = next(
            action
            for action in actions
            if action["action"] == "create_initial_main_commit"
        )
        self.assertEqual(initial_commit["sourceTemplateRevision"], "a" * 40)

    def test_scaffold_reads_are_pinned_to_verified_template_revision(self):
        client = FakeClient(ready_responses())
        planner.build_plan("example-site", client)

        scaffold_calls = [call for call in client.calls if "/contents/" in call]
        self.assertTrue(scaffold_calls)
        self.assertTrue(all(call.endswith(f"?ref={'a' * 40}") for call in scaffold_calls))

    def test_unconfigured_approved_template_revision_blocks(self):
        planner.STANDARD["approvedTemplateRevision"] = None
        client = FakeClient(ready_responses())

        plan = planner.build_plan("example-site", client)

        self.assertFalse(plan["ready"])
        self.assertIn("approved template revision is not configured", plan["blockers"])
        self.assertFalse(any("/contents/" in call for call in client.calls))

    def test_template_revision_drift_blocks(self):
        planner.STANDARD["approvedTemplateRevision"] = "b" * 40
        client = FakeClient(ready_responses())

        plan = planner.build_plan("example-site", client)

        self.assertFalse(plan["ready"])
        self.assertIn(
            f"approved template revision drifted: expected {'b' * 40}, got {'a' * 40}",
            plan["blockers"],
        )
        self.assertFalse(any("/contents/" in call for call in client.calls))

    def test_existing_target_blocks_without_planning_an_overwrite(self):
        responses = ready_responses()
        responses["/repos/sisyphus-org/example-site"] = result(
            200, {"full_name": "sisyphus-org/example-site"}
        )
        plan = planner.build_plan("example-site", FakeClient(responses))
        self.assertFalse(plan["ready"])
        self.assertIn(
            "target repository already exists: sisyphus-org/example-site", plan["blockers"]
        )

    def test_missing_template_blocks_and_skips_scaffold_reads(self):
        responses = ready_responses()
        template = planner.STANDARD["templateRepository"]
        responses[f"/repos/{template}"] = result(404, {"message": "Not Found"})
        for path in planner.STANDARD["requiredTemplateFiles"]:
            responses.pop(planner.content_path(template, path, ref="a" * 40))
        client = FakeClient(responses)
        plan = planner.build_plan("example-site", client)
        self.assertFalse(plan["ready"])
        self.assertIn(f"approved template repository is missing: {template}", plan["blockers"])
        self.assertFalse(any("/contents/" in call for call in client.calls))

    def test_missing_required_scaffold_file_blocks(self):
        responses = ready_responses()
        template = planner.STANDARD["templateRepository"]
        missing = ".github/workflows/pr-preview.yml"
        responses[planner.content_path(template, missing, ref="a" * 40)] = result(
            404, {"message": "Not Found"}
        )
        plan = planner.build_plan("example-site", FakeClient(responses))
        self.assertFalse(plan["ready"])
        scaffold = next(item for item in plan["checks"] if item["name"] == "template_scaffold")
        self.assertIn(missing, scaffold["detail"])

    def test_non_file_or_wrong_path_scaffold_entry_blocks(self):
        template = planner.STANDARD["templateRepository"]
        required_path = "README.md"
        for payload in (
            {"type": "dir", "path": required_path},
            {"type": "file", "path": "different.md"},
            ["not", "an", "object"],
        ):
            with self.subTest(payload=payload):
                responses = ready_responses()
                responses[
                    planner.content_path(template, required_path, ref="a" * 40)
                ] = result(200, payload)

                plan = planner.build_plan("example-site", FakeClient(responses))

                self.assertFalse(plan["ready"])
                scaffold = next(
                    item for item in plan["checks"]
                    if item["name"] == "template_scaffold"
                )
                self.assertIn(required_path, scaffold["detail"])

    def test_unverifiable_org_membership_is_a_blocker(self):
        responses = ready_responses()
        responses["/user/memberships/orgs/sisyphus-org"] = result(403, {"message": "Forbidden"})
        plan = planner.build_plan("example-site", FakeClient(responses))
        self.assertFalse(plan["ready"])
        self.assertIn(
            "GitHub token cannot verify organization membership/repository-create capability",
            plan["blockers"],
        )

    def test_missing_secret_names_block_without_exposing_values(self):
        responses = ready_responses()
        responses["/orgs/sisyphus-org/actions/secrets/CLOUDFLARE_API_TOKEN"] = result(
            404, {"message": "Not Found"}
        )
        plan = planner.build_plan("example-site", FakeClient(responses))
        serialized = json.dumps(plan)
        self.assertFalse(plan["ready"])
        self.assertIn("CLOUDFLARE_API_TOKEN", serialized)
        self.assertNotIn("super-secret-value", serialized)

    def test_unexpected_actor_blocks(self):
        responses = ready_responses()
        responses["/user"] = result(200, {"login": "someone-else"})
        plan = planner.build_plan("example-site", FakeClient(responses))
        self.assertFalse(plan["ready"])
        self.assertIn("GitHub actor must be alexxpetrov, got someone-else", plan["blockers"])

    def test_active_non_admin_membership_does_not_claim_create_capability(self):
        responses = ready_responses()
        responses["/user/memberships/orgs/sisyphus-org"] = result(
            200, {"state": "active", "role": "member"}
        )
        plan = planner.build_plan("example-site", FakeClient(responses))
        self.assertFalse(plan["ready"])
        self.assertIn(
            "active GitHub organization role does not prove repository-create capability: member",
            plan["blockers"],
        )

    def test_global_secret_visibility_requires_no_selected_grant(self):
        responses = ready_responses()
        for name in planner.STANDARD["requiredOrgSecrets"]:
            responses[f"/orgs/sisyphus-org/actions/secrets/{name}"] = result(
                200, {"name": name, "visibility": "all"}
            )
        plan = planner.build_plan("example-site", FakeClient(responses))
        self.assertTrue(plan["ready"])
        access = next(
            item
            for item in plan["plannedActions"]
            if item["action"] == "ensure_repository_access_to_org_secrets"
        )
        self.assertEqual(access["selectedSecretNames"], [])
        self.assertEqual(access["alreadyGlobalSecretNames"], planner.STANDARD["requiredOrgSecrets"])

    def test_private_only_secret_blocks_public_standard(self):
        responses = ready_responses()
        responses["/orgs/sisyphus-org/actions/secrets/CLOUDFLARE_API_TOKEN"] = result(
            200, {"name": "CLOUDFLARE_API_TOKEN", "visibility": "private"}
        )
        plan = planner.build_plan("example-site", FakeClient(responses))
        self.assertFalse(plan["ready"])
        self.assertIn(
            "required organization Actions secrets are private-repository-only",
            plan["blockers"],
        )

    def test_template_must_be_marked_as_template(self):
        responses = ready_responses()
        template = planner.STANDARD["templateRepository"]
        responses[f"/repos/{template}"] = result(200, {"is_template": False})
        for path in planner.STANDARD["requiredTemplateFiles"]:
            responses.pop(planner.content_path(template, path, ref="a" * 40))
        plan = planner.build_plan("example-site", FakeClient(responses))
        self.assertFalse(plan["ready"])
        self.assertIn(
            f"approved template is not marked as a GitHub template: {template}",
            plan["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
