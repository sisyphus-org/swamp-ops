import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plugins.ops_broker import handle_ops_broker
from plugins.ops_broker.broker import (
    build_command,
    execute_request,
    resolve_caller,
    validate_request,
)


class SmokeScriptTests(unittest.TestCase):
    def test_smoke_script_reports_readonly_json(self):
        script = Path(__file__).parents[1] / "scripts" / "ops_broker_readonly_smoke.py"
        completed = subprocess.run(
            ["python3", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["mode"], "read-only")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checks"], ["broker-runtime-available"])


class RequestValidationTests(unittest.TestCase):
    def test_validate_request_accepts_minimal_readonly_request(self):
        request = validate_request(
            {
                "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                "integration": "github",
                "operation": "repository_access",
                "arguments": {"repository": "sisyphus-org/swamp-ops"},
                "mode": "plan",
            }
        )

        self.assertEqual(request["integration"], "github")
        self.assertEqual(request["operation"], "repository_access")
        self.assertEqual(
            request["arguments"], {"repository": "sisyphus-org/swamp-ops"}
        )
        self.assertEqual(request["mode"], "plan")

    def test_validate_request_rejects_non_allowlisted_operation(self):
        with self.assertRaisesRegex(ValueError, "operation is not allowed"):
            validate_request(
                {
                    "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                    "integration": "github",
                    "operation": "run_shell",
                    "arguments": {"command": "env"},
                    "mode": "plan",
                }
            )

    def test_validate_request_rejects_apply_mode_for_readonly_operation(self):
        with self.assertRaisesRegex(ValueError, "apply mode is not available"):
            validate_request(
                {
                    "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                    "integration": "github",
                    "operation": "repository_access",
                    "arguments": {"repository": "sisyphus-org/swamp-ops"},
                    "mode": "apply",
                }
            )

    def test_validate_request_rejects_caller_identity_in_body(self):
        with self.assertRaisesRegex(ValueError, "unexpected request fields"):
            validate_request(
                {
                    "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                    "caller_profile": "default",
                    "integration": "github",
                    "operation": "repository_access",
                    "arguments": {"repository": "sisyphus-org/swamp-ops"},
                    "mode": "plan",
                }
            )

    def test_validate_request_rejects_non_uuid_request_id(self):
        with self.assertRaisesRegex(ValueError, "request_id must be a UUID"):
            validate_request(
                {
                    "request_id": "not-a-uuid",
                    "integration": "github",
                    "operation": "repository_access",
                    "arguments": {"repository": "sisyphus-org/swamp-ops"},
                    "mode": "plan",
                }
            )


class CommandConstructionTests(unittest.TestCase):
    def test_allowlisted_operations_build_fixed_argv(self):
        policy = {
            "github": {"repositories": ["sisyphus-org/swamp-ops"]},
            "swamp": {
                "repositoryBootstrapWorkflow": "github-cloudflare-repo-bootstrap",
                "models": ["ops-broker-readonly-smoke"],
                "workflows": ["ops-broker-readonly-smoke"],
                "data": [
                    {
                        "model": "ops-broker-readonly-smoke",
                        "name": "result",
                    }
                ],
            },
        }
        cases = [
            (
                "github.repository_access",
                {"repository": "sisyphus-org/swamp-ops"},
                ["gh", "api", "repos/sisyphus-org/swamp-ops"],
            ),
            (
                "github.list_pull_requests",
                {"repository": "sisyphus-org/swamp-ops"},
                ["gh", "api", "repos/sisyphus-org/swamp-ops/pulls"],
            ),
            (
                "github.pull_request_checks",
                {"repository": "sisyphus-org/swamp-ops", "pull_request": 1},
                [
                    "gh",
                    "pr",
                    "checks",
                    "1",
                    "--repo",
                    "sisyphus-org/swamp-ops",
                    "--json",
                    "name,state,link,bucket,event,workflow",
                ],
            ),
            ("swamp.auth_whoami", {}, ["swamp", "auth", "whoami", "--json"]),
            (
                "swamp.validate_model",
                {"model": "ops-broker-readonly-smoke"},
                [
                    "swamp",
                    "model",
                    "validate",
                    "ops-broker-readonly-smoke",
                    "--json",
                ],
            ),
            (
                "swamp.validate_workflow",
                {"workflow": "ops-broker-readonly-smoke"},
                [
                    "swamp",
                    "workflow",
                    "validate",
                    "ops-broker-readonly-smoke",
                    "--json",
                ],
            ),
            (
                "swamp.run_readonly_workflow",
                {"workflow": "ops-broker-readonly-smoke"},
                [
                    "swamp",
                    "workflow",
                    "run",
                    "ops-broker-readonly-smoke",
                    "--json",
                ],
            ),
            (
                "swamp.plan_github_cloudflare_repository",
                {"repository": "example-site"},
                [
                    "swamp",
                    "workflow",
                    "run",
                    "github-cloudflare-repo-bootstrap",
                    "--input",
                    "repository=example-site",
                    "--json",
                ],
            ),
            (
                "swamp.get_result",
                {"model": "ops-broker-readonly-smoke", "name": "result"},
                [
                    "swamp",
                    "data",
                    "get",
                    "ops-broker-readonly-smoke",
                    "result",
                    "--json",
                ],
            ),
        ]

        for operation, arguments, expected in cases:
            with self.subTest(operation=operation):
                self.assertEqual(build_command(operation, arguments, policy), expected)

    def test_operation_arguments_are_exact_not_extensible(self):
        policy = {
            "github": {"repositories": ["sisyphus-org/swamp-ops"]},
            "swamp": {"models": [], "workflows": [], "data": []},
        }
        with self.assertRaisesRegex(ValueError, "unexpected arguments"):
            build_command("swamp.auth_whoami", {"command": "env"}, policy)
        with self.assertRaisesRegex(ValueError, "unexpected arguments"):
            build_command(
                "github.repository_access",
                {"repository": "sisyphus-org/swamp-ops", "url": "http://example.com"},
                policy,
            )
        with self.assertRaisesRegex(ValueError, "arguments must be an object"):
            build_command("swamp.auth_whoami", [], policy)  # type: ignore[arg-type]

    def test_repository_bootstrap_name_is_strict_and_not_shell_extensible(self):
        policy = {
            "swamp": {
                "repositoryBootstrapWorkflow": "github-cloudflare-repo-bootstrap"
            }
        }
        for value in (
            "Example-Site",
            "example_site",
            "a",
            "1example",
            "example-site;env",
            "example-site' --input mode=apply",
            "x" * 56,
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "repository must match"):
                    build_command(
                        "swamp.plan_github_cloudflare_repository",
                        {"repository": value},
                        policy,
                    )

    def test_repository_bootstrap_requires_dedicated_policy_target(self):
        with self.assertRaisesRegex(ValueError, "bootstrap workflow is not allowed"):
            build_command(
                "swamp.plan_github_cloudflare_repository",
                {"repository": "example-site"},
                {"swamp": {"workflows": ["github-cloudflare-repo-bootstrap"]}},
            )


class PolicyTests(unittest.TestCase):
    def test_repository_bootstrap_capability_is_swe_only(self):
        path = Path(__file__).parents[1] / "plugins" / "ops_broker" / "policy.json"
        policy = json.loads(path.read_text())
        operation = "swamp.plan_github_cloudflare_repository"

        self.assertIn(operation, policy["peers"]["swe"]["operations"])
        for peer in ("books", "crypto-analyst", "ideas"):
            self.assertNotIn(operation, policy["peers"][peer]["operations"])
        self.assertEqual(
            policy["swamp"]["repositoryBootstrapWorkflow"],
            "github-cloudflare-repo-bootstrap",
        )
        self.assertNotIn(
            "github-cloudflare-repo-bootstrap", policy["swamp"]["workflows"]
        )
        self.assertNotIn("github-cloudflare-repo-bootstrap", policy["swamp"]["models"])
        self.assertNotIn(
            {"model": "github-cloudflare-repo-bootstrap", "name": "result"},
            policy["swamp"]["data"],
        )


class ExecutionTests(unittest.TestCase):
    def test_repository_access_uses_fixed_gh_argv(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return {
                "returncode": 0,
                "stdout": '{"full_name":"sisyphus-org/swamp-ops","private":true}',
                "stderr": "",
            }

        response = execute_request(
            validate_request(
                {
                    "request_id": "e7ba0358-034b-4f79-9b28-42c9212e7716",
                    "integration": "github",
                    "operation": "repository_access",
                    "arguments": {"repository": "sisyphus-org/swamp-ops"},
                    "mode": "plan",
                }
            ),
            caller="swe",
            policy={
                "peers": {"swe": {"operations": ["github.repository_access"]}},
                "github": {"repositories": ["sisyphus-org/swamp-ops"]},
            },
            runner=runner,
            workspace=Path("/Users/hermes/workspaces/swamp-ops"),
        )

        self.assertEqual(
            calls[0][0],
            ["gh", "api", "repos/sisyphus-org/swamp-ops"],
        )
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["result"]["full_name"], "sisyphus-org/swamp-ops")

    def test_validate_workflow_uses_allowlisted_name_and_fixed_argv(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return {
                "returncode": 0,
                "stdout": '{"valid":true}',
                "stderr": "",
            }

        response = execute_request(
            validate_request(
                {
                    "request_id": "b2f786db-6f89-4558-9d82-17e1fa45df7e",
                    "integration": "swamp",
                    "operation": "validate_workflow",
                    "arguments": {"workflow": "ops-broker-readonly-smoke"},
                    "mode": "plan",
                }
            ),
            caller="books",
            policy={
                "peers": {"books": {"operations": ["swamp.validate_workflow"]}},
                "swamp": {"workflows": ["ops-broker-readonly-smoke"]},
            },
            runner=runner,
            workspace=Path("/Users/hermes/workspaces/swamp-ops"),
        )

        self.assertEqual(
            calls[0][0],
            [
                "swamp",
                "workflow",
                "validate",
                "ops-broker-readonly-smoke",
                "--json",
            ],
        )
        self.assertEqual(response["result"], {"valid": True})

    def test_repository_bootstrap_executes_fixed_argv_for_swe_only(self):
        policy_path = (
            Path(__file__).parents[1] / "plugins" / "ops_broker" / "policy.json"
        )
        policy = json.loads(policy_path.read_text())
        request = validate_request(
            {
                "request_id": "50fc8927-c331-4d1b-bf42-b192c3f8e43a",
                "integration": "swamp",
                "operation": "plan_github_cloudflare_repository",
                "arguments": {"repository": "example-site"},
                "mode": "plan",
            }
        )
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[:3] == ["swamp", "workflow", "run"]:
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "id": "run-1",
                            "jobs": [
                                {
                                    "steps": [
                                        {
                                            "dataArtifacts": [
                                                {"name": "result", "version": 7}
                                            ]
                                        }
                                    ]
                                }
                            ],
                        }
                    ),
                    "stderr": "",
                }
            return {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "modelName": "github-cloudflare-repo-bootstrap",
                        "name": "result",
                        "version": 7,
                        "ownerDefinition": {"workflowRunId": "run-1"},
                        "content": {
                            "exitCode": 0,
                            "stdout": json.dumps(
                                {
                                    "schemaVersion": 1,
                                    "mode": "plan",
                                    "readOnly": True,
                                    "ready": False,
                                    "target": {
                                        "repository": "sisyphus-org/example-site"
                                    },
                                    "blockers": ["template missing"],
                                }
                            ),
                        },
                    }
                ),
                "stderr": "",
            }

        response = execute_request(
            request,
            caller="swe",
            policy=policy,
            runner=runner,
            workspace=Path("/Users/hermes/workspaces/swamp-ops"),
        )
        self.assertEqual(
            calls[0][0],
            [
                "swamp",
                "workflow",
                "run",
                "github-cloudflare-repo-bootstrap",
                "--input",
                "repository=example-site",
                "--json",
            ],
        )
        self.assertEqual(
            calls[1][0],
            [
                "swamp",
                "data",
                "get",
                "github-cloudflare-repo-bootstrap",
                "result",
                "--version",
                "7",
                "--json",
            ],
        )
        self.assertEqual(
            response["result"],
            {
                "workflowRunId": "run-1",
                "artifactVersion": 7,
                "plan": {
                    "schemaVersion": 1,
                    "mode": "plan",
                    "readOnly": True,
                    "ready": False,
                    "target": {
                        "repository": "sisyphus-org/example-site"
                    },
                    "blockers": ["template missing"],
                },
            },
        )

        with self.assertRaisesRegex(ValueError, "not allowed for caller"):
            execute_request(
                request,
                caller="books",
                policy=policy,
                runner=lambda *_args, **_kwargs: self.fail("runner must not execute"),
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
            )

    def test_repository_bootstrap_rejects_artifact_from_another_run(self):
        policy = json.loads(
            (
                Path(__file__).parents[1]
                / "plugins"
                / "ops_broker"
                / "policy.json"
            ).read_text()
        )
        request = validate_request(
            {
                "request_id": "7a69c848-8711-47a5-a55b-22fcace1b7c8",
                "integration": "swamp",
                "operation": "plan_github_cloudflare_repository",
                "arguments": {"repository": "example-site"},
                "mode": "plan",
            }
        )

        def runner(argv, **_kwargs):
            if argv[:3] == ["swamp", "workflow", "run"]:
                payload = {
                    "id": "run-expected",
                    "jobs": [
                        {
                            "steps": [
                                {"dataArtifacts": [{"name": "result", "version": 8}]}
                            ]
                        }
                    ],
                }
            else:
                payload = {
                    "modelName": "github-cloudflare-repo-bootstrap",
                    "name": "result",
                    "version": 8,
                    "ownerDefinition": {"workflowRunId": "run-other"},
                    "content": {
                        "exitCode": 0,
                        "stdout": json.dumps(
                            {
                                "schemaVersion": 1,
                                "mode": "plan",
                                "readOnly": True,
                                "ready": True,
                                "blockers": [],
                            }
                        ),
                    },
                }
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        with self.assertRaisesRegex(ValueError, "provenance is invalid"):
            execute_request(
                request,
                caller="swe",
                policy=policy,
                runner=runner,
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
            )

    def test_repository_bootstrap_rejects_non_object_artifact(self):
        policy = json.loads(
            (
                Path(__file__).parents[1]
                / "plugins"
                / "ops_broker"
                / "policy.json"
            ).read_text()
        )
        request = validate_request(
            {
                "request_id": "54550fa0-df15-4264-bf19-30169f65d53a",
                "integration": "swamp",
                "operation": "plan_github_cloudflare_repository",
                "arguments": {"repository": "example-site"},
                "mode": "plan",
            }
        )

        def runner(argv, **_kwargs):
            if argv[:3] == ["swamp", "workflow", "run"]:
                stdout = json.dumps(
                    {
                        "id": "run-expected",
                        "jobs": [
                            {
                                "steps": [
                                    {
                                        "dataArtifacts": [
                                            {"name": "result", "version": 9}
                                        ]
                                    }
                                ]
                            }
                        ],
                    }
                )
            else:
                stdout = "[]"
            return {"returncode": 0, "stdout": stdout, "stderr": ""}

        with self.assertRaisesRegex(ValueError, "invalid JSON object"):
            execute_request(
                request,
                caller="swe",
                policy=policy,
                runner=runner,
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
            )

    def test_repository_bootstrap_rejects_plan_for_another_repository(self):
        policy = json.loads(
            (
                Path(__file__).parents[1]
                / "plugins"
                / "ops_broker"
                / "policy.json"
            ).read_text()
        )
        request = validate_request(
            {
                "request_id": "b0b3c9cb-3ae5-4580-a969-d5b8e301124a",
                "integration": "swamp",
                "operation": "plan_github_cloudflare_repository",
                "arguments": {"repository": "example-site"},
                "mode": "plan",
            }
        )

        def runner(argv, **_kwargs):
            if argv[:3] == ["swamp", "workflow", "run"]:
                payload = {
                    "id": "run-expected",
                    "jobs": [
                        {
                            "steps": [
                                {"dataArtifacts": [{"name": "result", "version": 10}]}
                            ]
                        }
                    ],
                }
            else:
                payload = {
                    "modelName": "github-cloudflare-repo-bootstrap",
                    "name": "result",
                    "version": 10,
                    "ownerDefinition": {"workflowRunId": "run-expected"},
                    "content": {
                        "exitCode": 0,
                        "stdout": json.dumps(
                            {
                                "schemaVersion": 1,
                                "mode": "plan",
                                "readOnly": True,
                                "ready": False,
                                "target": {
                                    "repository": "sisyphus-org/other-site"
                                },
                                "blockers": ["template missing"],
                            }
                        ),
                    },
                }
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        with self.assertRaisesRegex(ValueError, "target does not match request"):
            execute_request(
                request,
                caller="swe",
                policy=policy,
                runner=runner,
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
            )

    def test_pending_github_checks_exit_code_is_a_typed_result(self):
        response = execute_request(
            validate_request(
                {
                    "request_id": "324765ed-593d-4624-81e8-1ba1af22b335",
                    "integration": "github",
                    "operation": "pull_request_checks",
                    "arguments": {
                        "repository": "sisyphus-org/swamp-ops",
                        "pull_request": 1,
                    },
                    "mode": "plan",
                }
            ),
            caller="swe",
            policy={
                "peers": {
                    "swe": {"operations": ["github.pull_request_checks"]}
                },
                "github": {"repositories": ["sisyphus-org/swamp-ops"]},
            },
            runner=lambda *_args, **_kwargs: {
                "returncode": 8,
                "stdout": '[{"name":"tests","state":"PENDING"}]',
                "stderr": "",
            },
            workspace=Path("/Users/hermes/workspaces/swamp-ops"),
        )

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["result"][0]["state"], "PENDING")

    def test_execution_appends_secret_free_audit_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"

            def runner(argv, **kwargs):
                return {
                    "returncode": 0,
                    "stdout": '{"full_name":"sisyphus-org/swamp-ops"}',
                    "stderr": "authorization: Bearer should-not-leak",
                }

            execute_request(
                validate_request(
                    {
                        "request_id": "27a71a3e-cc72-448c-8df2-d50575e7a40f",
                        "integration": "github",
                        "operation": "repository_access",
                        "arguments": {"repository": "sisyphus-org/swamp-ops"},
                        "mode": "plan",
                    }
                ),
                caller="ideas",
                policy={
                    "peers": {
                        "ideas": {"operations": ["github.repository_access"]}
                    },
                    "github": {"repositories": ["sisyphus-org/swamp-ops"]},
                },
                runner=runner,
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                audit_path=audit_path,
            )

            audit_text = audit_path.read_text()
            self.assertIn('"caller": "ideas"', audit_text)
            self.assertIn('"operation": "github.repository_access"', audit_text)
            self.assertNotIn("should-not-leak", audit_text)

    def test_rejected_execution_is_audited_without_secret_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"

            with self.assertRaisesRegex(ValueError, "not allowed for caller"):
                execute_request(
                    validate_request(
                        {
                            "request_id": "4a1c240f-ef31-4bea-8d2c-611f1e89784e",
                            "integration": "github",
                            "operation": "repository_access",
                            "arguments": {"repository": "sisyphus-org/swamp-ops"},
                            "mode": "plan",
                        }
                    ),
                    caller="unknown-peer",
                    policy={
                        "peers": {},
                        "github": {"repositories": ["sisyphus-org/swamp-ops"]},
                    },
                    runner=lambda *_args, **_kwargs: self.fail("runner must not execute"),
                    workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                    audit_path=audit_path,
                )

            record = json.loads(audit_path.read_text().strip())
            self.assertEqual(record["status"], "rejected")
            self.assertEqual(record["caller"], "unknown-peer")
            self.assertNotIn("error", record)


class PluginHandlerTests(unittest.TestCase):
    def test_handler_derives_caller_from_session_and_returns_typed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / "state.db"
            connection = sqlite3.connect(state_db)
            connection.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT)"
            )
            connection.execute(
                "INSERT INTO sessions (id, source, user_id) VALUES (?, ?, ?)",
                ("session-1", "a2a", "swe"),
            )
            connection.commit()
            connection.close()
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "peers": {
                            "swe": {"operations": ["github.repository_access"]}
                        },
                        "github": {"repositories": ["sisyphus-org/swamp-ops"]},
                        "workspace": str(root),
                    }
                )
            )
            audit_path = root / "audit.jsonl"

            with mock.patch.dict(
                "os.environ",
                {
                    "HERMES_HOME": str(root),
                    "OPS_BROKER_POLICY": str(policy_path),
                    "OPS_BROKER_AUDIT": str(audit_path),
                },
                clear=False,
            ), mock.patch(
                "plugins.ops_broker.default_runner",
                return_value={
                    "returncode": 0,
                    "stdout": '{"full_name":"sisyphus-org/swamp-ops"}',
                    "stderr": "",
                },
            ) as runner_mock:
                result = json.loads(
                    handle_ops_broker(
                        {
                            "request_id": "56040553-7de4-4849-a16d-a2a0ea8b749a",
                            "integration": "github",
                            "operation": "repository_access",
                            "arguments": {"repository": "sisyphus-org/swamp-ops"},
                            "mode": "plan",
                        },
                        session_id="session-1",
                    )
                )

            self.assertEqual(result["caller"], "swe")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(runner_mock.call_args.kwargs["cwd"], root.resolve())

    def test_handler_returns_typed_rejection_on_command_timeout(self):
        request = {
            "request_id": "13b64b4c-cee5-4bd4-b2e6-45df0a43d6c5",
            "integration": "github",
            "operation": "repository_access",
            "arguments": {"repository": "sisyphus-org/swamp-ops"},
            "mode": "plan",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_path = root / "policy.json"
            policy_path.write_text('{"workspace":"/Users/hermes/workspaces/swamp-ops"}')
            with mock.patch.dict(
                "os.environ",
                {
                    "HERMES_HOME": str(root),
                    "OPS_BROKER_POLICY": str(policy_path),
                },
                clear=False,
            ), mock.patch(
                "plugins.ops_broker.resolve_caller", return_value="swe"
            ), mock.patch(
                "plugins.ops_broker.execute_request",
                side_effect=subprocess.TimeoutExpired(["gh", "api"], 60),
            ):
                result = json.loads(handle_ops_broker(request, session_id="session-1"))

        self.assertEqual(result["status"], "rejected")
        self.assertIn("timed out", result["error"].lower())


class CallerIdentityTests(unittest.TestCase):
    def test_resolve_caller_uses_authenticated_a2a_session_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT)"
            )
            connection.execute(
                "INSERT INTO sessions (id, source, user_id) VALUES (?, ?, ?)",
                ("session-1", "a2a", "swe"),
            )
            connection.commit()
            connection.close()

            self.assertEqual(resolve_caller("session-1", db_path), "swe")


if __name__ == "__main__":
    unittest.main()
