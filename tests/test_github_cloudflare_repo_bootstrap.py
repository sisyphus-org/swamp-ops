import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "github_cloudflare_repo_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("github_cloudflare_repo_bootstrap", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import bootstrap: {SCRIPT}")
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.blob_index = 0

    def _response(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        configured = self.responses.get((method, path))
        if configured is not None:
            return configured
        if method == "POST" and path.endswith("/git/blobs"):
            self.blob_index += 1
            return bootstrap.ApiResult(201, {"sha": f"{self.blob_index:040x}"})
        raise AssertionError(f"unexpected GitHub call: {method} {path}")

    def get(self, path):
        return self._response("GET", path)

    def post(self, path, payload):
        return self._response("POST", path, payload)

    def put(self, path, payload=None):
        return self._response("PUT", path, payload)

    def patch(self, path, payload):
        return self._response("PATCH", path, payload)


def result(status, payload=None):
    return bootstrap.ApiResult(status, payload if payload is not None else {})


def ready_responses(repository="example-site"):
    org = bootstrap.STANDARD["organization"]
    reviewer = bootstrap.STANDARD["requiredReviewer"]
    return {
        ("GET", "/user"): result(200, {"login": reviewer}),
        ("GET", f"/repos/{org}/{repository}"): result(404, {"message": "Not Found"}),
        ("GET", f"/user/memberships/orgs/{org}"): result(
            200, {"state": "active", "role": "admin"}
        ),
        ("GET", f"/users/{reviewer}"): result(200, {"login": reviewer, "id": 42}),
        ("GET", f"/orgs/{org}/actions/secrets/CLOUDFLARE_API_TOKEN"): result(
            200, {"name": "CLOUDFLARE_API_TOKEN", "visibility": "selected"}
        ),
        ("GET", f"/orgs/{org}/actions/secrets/CLOUDFLARE_ACCOUNT_ID"): result(
            200, {"name": "CLOUDFLARE_ACCOUNT_ID", "visibility": "selected"}
        ),
    }


class InputAndTemplateTests(unittest.TestCase):
    def test_repository_is_the_only_unbound_project_value(self):
        args = bootstrap.parse_args(["--repository", "new-site"])
        self.assertEqual(args.repository, "new-site")
        self.assertEqual(args.mode, "plan")
        with self.assertRaises(SystemExit):
            bootstrap.parse_args(["--repository", "new-site", "--mode", "apply"])

    def test_rejects_invalid_names_without_normalizing(self):
        for value in ("My-Site", "my_site", "a", "1site", "x" * 56):
            with self.subTest(value=value):
                with self.assertRaises(bootstrap.ContractError):
                    bootstrap.validate_repository(value)

    def test_production_worker_is_nonce_bound_unique_and_dns_safe(self):
        worker = bootstrap.derive_production_worker(
            "example-site", "0123456789abcdef01234567"
        )
        self.assertEqual(worker, "example-site-0123456789abcdef01234567")
        self.assertRegex(worker, r"^[a-z][a-z0-9-]{0,53}$")
        self.assertLessEqual(len(worker), 54)
        long_worker = bootstrap.derive_production_worker(
            "a" + "b" * 53, "0123456789abcdef01234567"
        )
        self.assertLessEqual(len(long_worker), 54)
        self.assertTrue(long_worker.endswith("-0123456789abcdef01234567"))
        self.assertNotEqual(
            worker,
            bootstrap.derive_production_worker(
                "example-site", "1123456789abcdef01234567"
            ),
        )

    def test_preview_worker_is_deterministic_short_and_dns_safe(self):
        value = bootstrap.derive_preview_worker("a" + "b" * 53)
        self.assertEqual(value, bootstrap.derive_preview_worker("a" + "b" * 53))
        self.assertLessEqual(len(value), 27)
        self.assertRegex(value, r"^[a-z][a-z0-9-]+$")

    def test_environment_reviewer_parser_matches_github_shape(self):
        payload = {
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "reviewers": [
                        {
                            "type": "User",
                            "reviewer": {"login": "alexxpetrov", "id": 56254806},
                        }
                    ],
                }
            ]
        }
        self.assertTrue(bootstrap._environment_has_reviewer(payload, 56254806))
        self.assertFalse(bootstrap._environment_has_reviewer(payload, 1))

    def test_rendered_template_has_no_placeholders_and_stable_manifest(self):
        template = bootstrap.load_template_files()
        production_worker = bootstrap.derive_production_worker(
            "example-site", "0123456789abcdef01234567"
        )
        rendered = bootstrap.render_template_files(
            "example-site", template, production_worker
        )
        joined = b"\n".join(rendered.values())
        self.assertNotIn(b"__REPOSITORY__", joined)
        self.assertNotIn(b"__PREVIEW_WORKER__", joined)
        self.assertNotIn(b"SIS-54", joined)
        self.assertNotIn(b"CI fixture", joined)
        self.assertIn(b"example-site", joined)
        self.assertIn(bootstrap.derive_preview_worker("example-site").encode(), joined)
        self.assertIn(production_worker.encode(), rendered["wrangler.jsonc"])
        self.assertEqual(
            bootstrap.files_checksum(rendered),
            bootstrap.files_checksum(
                bootstrap.render_template_files(
                    "example-site", template, production_worker
                )
            ),
        )
        preview_workflow = rendered[".github/workflows/pr-preview.yml"].decode()
        token_name = "CLOUDFLARE_" + "API_TOKEN"
        self.assertIn(f"Authorization: Bearer ${{{token_name}}}", preview_workflow)
        self.assertNotIn("Authorization: Bearer ***", preview_workflow)
        self.assertNotIn("CLOU...KEN", preview_workflow)
        self.assertIn("package-lock.json", rendered)
        self.assertIn(".github/workflows/ci.yml", rendered)
        self.assertIn(".github/workflows/deploy.yml", rendered)
        self.assertIn(".github/workflows/pr-preview.yml", rendered)


class PlanTests(unittest.TestCase):
    def test_ready_plan_is_read_only_checksum_bound_and_complete(self):
        client = FakeClient(ready_responses())
        plan = bootstrap.build_plan("example-site", client)

        self.assertTrue(plan["ready"])
        self.assertTrue(plan["readOnly"])
        self.assertEqual(plan["mode"], "plan")
        self.assertTrue(bootstrap.verify_plan_checksum(plan))
        self.assertEqual(plan["target"]["repository"], "sisyphus-org/example-site")
        self.assertRegex(
            plan["target"]["productionWorker"],
            r"^example-site-[0-9a-f]{24}$",
        )
        self.assertEqual(
            plan["target"]["productionUrl"],
            f"https://{plan['target']['productionWorker']}.sisyphus-org.workers.dev",
        )
        self.assertEqual(
            plan["target"]["previewWorker"],
            bootstrap.derive_preview_worker("example-site"),
        )
        self.assertRegex(plan["source"]["templateChecksum"], r"^[0-9a-f]{64}$")
        self.assertRegex(plan["source"]["implementationChecksum"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            len(plan["source"]["implementationManifest"]),
            len(bootstrap.IMPLEMENTATION_PATHS),
        )
        self.assertRegex(plan["source"]["renderedChecksum"], r"^[0-9a-f]{64}$")
        self.assertGreater(len(plan["source"]["renderedManifest"]), 20)
        self.assertNotIn("secretValues", json.dumps(plan))

    def test_existing_repository_blocks_without_writes(self):
        responses = ready_responses()
        responses[("GET", "/repos/sisyphus-org/example-site")] = result(
            200, {"full_name": "sisyphus-org/example-site"}
        )
        client = FakeClient(responses)
        plan = bootstrap.build_plan("example-site", client)
        self.assertFalse(plan["ready"])
        self.assertIn(
            "target repository already exists: sisyphus-org/example-site",
            plan["blockers"],
        )
        self.assertTrue(all(call[0] == "GET" for call in client.calls))

    def test_plan_does_not_read_membership_or_secret_metadata(self):
        client = FakeClient(ready_responses())
        plan = bootstrap.build_plan("example-site", client)
        paths = [path for method, path, _payload in client.calls if method == "GET"]
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(plan["warnings"], [])
        self.assertFalse(any("memberships" in path for path in paths))
        self.assertFalse(any("actions/secrets" in path for path in paths))


class ApplyTests(unittest.TestCase):
    def ready_plan(self):
        return bootstrap.build_plan("example-site", FakeClient(ready_responses()))

    def test_invalid_reviewer_id_rejects_before_first_external_write(self):
        plan = self.ready_plan()
        plan["resolved"]["reviewerId"] = None
        plan["checksum"] = bootstrap._plan_checksum(plan)
        client = FakeClient({})
        with self.assertRaisesRegex(bootstrap.ContractError, "reviewer id"):
            bootstrap.apply_plan(
                "example-site",
                plan,
                "00000000-0000-4000-8000-000000000001",
                plan["checksum"],
                client,
            )
        self.assertEqual(client.calls, [])

    def test_production_worker_target_substitution_rejects_before_first_external_write(self):
        plan = self.ready_plan()
        plan["target"]["productionWorker"] = "attacker-worker"
        plan["target"]["productionUrl"] = (
            "https://attacker-worker.sisyphus-org.workers.dev"
        )
        plan["checksum"] = bootstrap._plan_checksum(plan)
        client = FakeClient({})
        with self.assertRaisesRegex(bootstrap.ContractError, "production Worker target"):
            bootstrap.apply_plan(
                "example-site",
                plan,
                "00000000-0000-4000-8000-000000000001",
                plan["checksum"],
                client,
            )
        self.assertEqual(client.calls, [])

    def test_wrong_checksum_rejects_before_first_external_write(self):
        plan = self.ready_plan()
        client = FakeClient()
        with self.assertRaisesRegex(bootstrap.ContractError, "checksum-bound"):
            bootstrap.apply_plan(
                "example-site",
                plan,
                "11111111-1111-4111-8111-111111111111",
                "0" * 64,
                client,
            )
        self.assertEqual(client.calls, [])

    def test_template_drift_rejects_before_first_external_write(self):
        plan = self.ready_plan()
        plan["source"]["templateChecksum"] = "0" * 64
        plan["checksum"] = bootstrap._plan_checksum(plan)
        client = FakeClient()
        with self.assertRaisesRegex(bootstrap.ContractError, "template changed"):
            bootstrap.apply_plan(
                "example-site",
                plan,
                "11111111-1111-4111-8111-111111111111",
                plan["checksum"],
                client,
            )
        self.assertEqual(client.calls, [])

    def test_implementation_drift_rejects_before_first_external_write(self):
        plan = self.ready_plan()
        plan["source"]["implementationChecksum"] = "0" * 64
        plan["checksum"] = bootstrap._plan_checksum(plan)
        client = FakeClient()
        with self.assertRaisesRegex(bootstrap.ContractError, "implementation changed"):
            bootstrap.apply_plan(
                "example-site",
                plan,
                "11111111-1111-4111-8111-111111111111",
                plan["checksum"],
                client,
            )
        self.assertEqual(client.calls, [])

    def test_verify_repository_reads_back_tree_reviewer_deploy_and_health(self):
        plan = self.ready_plan()
        target = "sisyphus-org/example-site"
        main_sha = "d" * 40
        expected_tree = [
            {
                "path": item["path"],
                "sha": item["gitBlobSha"],
                "type": "blob",
            }
            for item in plan["source"]["renderedManifest"]
        ]
        deploy_path = (
            f"/repos/{target}/actions/workflows/deploy.yml/runs?"
            f"event=push&head_sha={main_sha}&per_page=20"
        )
        client = FakeClient(
            {
                ("GET", f"/repos/{target}"): result(200, {"full_name": target}),
                ("GET", f"/repos/{target}/commits/main"): result(
                    200, {"sha": main_sha}
                ),
                ("GET", f"/repos/{target}/git/trees/main?recursive=1"): result(
                    200, {"tree": expected_tree}
                ),
                (
                    "GET",
                    f"/repos/{target}/environments/branch-preview",
                ): result(
                    200,
                    {
                        "protection_rules": [
                            {
                                "type": "required_reviewers",
                                "reviewers": [
                                    {
                                        "type": "User",
                                        "reviewer": {
                                            "id": plan["resolved"]["reviewerId"]
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                ),
                ("GET", deploy_path): result(
                    200,
                    {
                        "workflow_runs": [
                            {
                                "id": 123,
                                "status": "completed",
                                "conclusion": "success",
                                "html_url": "https://github.example/run/123",
                            }
                        ]
                    },
                ),
            }
        )

        class HealthResponse(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with mock.patch.object(
            bootstrap.urllib.request,
            "urlopen",
            return_value=HealthResponse(b'{"status":"ok"}'),
        ) as urlopen:
            verified = bootstrap.verify_repository(
                "example-site",
                plan,
                plan["checksum"],
                client,
                wait_seconds=0,
            )

        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["mainSha"], main_sha)
        self.assertEqual(verified["deployRunId"], 123)
        self.assertEqual(verified["health"], {"status": "ok"})
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(client.calls[-1], ("GET", deploy_path, None))

    def test_apply_creates_exact_tree_environment_and_settings(self):
        plan = self.ready_plan()
        target = "sisyphus-org/example-site"
        commit_sha = "a" * 40
        initial_sha = "c" * 40
        responses = {
            ("GET", f"/repos/{target}"): result(404),
            ("POST", "/orgs/sisyphus-org/repos"): result(201, {"id": 99}),
            ("GET", f"/repos/{target}/commits/main"): result(200, {"sha": initial_sha}),
            ("POST", f"/repos/{target}/git/trees"): result(201, {"sha": "b" * 40}),
            ("POST", f"/repos/{target}/git/commits"): result(201, {"sha": commit_sha}),
            ("PATCH", f"/repos/{target}/git/refs/heads/main"): result(200, {"ref": "refs/heads/main"}),
            ("PUT", f"/repos/{target}/environments/branch-preview"): result(200),
            ("PATCH", f"/repos/{target}"): result(200, {"default_branch": "main"}),
        }
        client = FakeClient(responses)
        applied = bootstrap.apply_plan(
            "example-site",
            plan,
            "11111111-1111-4111-8111-111111111111",
            plan["checksum"],
            client,
        )

        self.assertEqual(applied["status"], "created")
        self.assertEqual(applied["mainSha"], commit_sha)
        methods = [call[:2] for call in client.calls]
        self.assertIn(("POST", "/orgs/sisyphus-org/repos"), methods)
        self.assertIn(("PUT", f"/repos/{target}/environments/branch-preview"), methods)
        create_call = next(
            call for call in client.calls if call[1] == "/orgs/sisyphus-org/repos"
        )
        self.assertTrue(create_call[2]["auto_init"])
        commit_call = next(call for call in client.calls if call[1].endswith("/git/commits"))
        self.assertEqual(commit_call[2]["parents"], [initial_sha])
        ref_call = next(call for call in client.calls if call[1].endswith("/git/refs/heads/main"))
        self.assertEqual(ref_call[0], "PATCH")
        self.assertEqual(ref_call[2], {"sha": commit_sha, "force": False})
        environment_call = next(
            call for call in client.calls if call[1].endswith("/environments/branch-preview")
        )
        self.assertEqual(environment_call[2]["reviewers"], [{"type": "User", "id": 42}])
        tree_call = next(call for call in client.calls if call[1].endswith("/git/trees"))
        self.assertEqual(len(tree_call[2]["tree"]), len(plan["source"]["renderedManifest"]))

    def test_existing_target_is_never_adopted_or_overwritten(self):
        plan = self.ready_plan()
        client = FakeClient(
            {
                ("GET", "/repos/sisyphus-org/example-site"): result(
                    200, {"full_name": "sisyphus-org/example-site"}
                )
            }
        )
        with self.assertRaisesRegex(bootstrap.ContractError, "refusing overwrite"):
            bootstrap.apply_plan(
                "example-site",
                plan,
                "11111111-1111-4111-8111-111111111111",
                plan["checksum"],
                client,
            )
        self.assertTrue(all(call[0] == "GET" for call in client.calls))


if __name__ == "__main__":
    unittest.main()
