import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plugins.ops_broker import (
    OPS_BROKER_SCHEMA,
    _verify_runtime_workspace,
    handle_ops_broker,
)
from plugins.ops_broker.broker import (
    _canonical_plan_checksum,
    build_command,
    execute_request,
    resolve_caller,
    validate_request,
)


def repository_plan(repository="example-site", *, ready=False, blockers=None):
    plan = {
        "schemaVersion": 2,
        "mode": "plan",
        "readOnly": True,
        "ready": ready,
        "target": {"repository": f"sisyphus-org/{repository}"},
        "blockers": list(blockers or ([] if ready else ["not ready"])),
    }
    plan["checksum"] = _canonical_plan_checksum(plan)
    return plan


def plan_artifact(plan, *, run_id, version):
    return {
        "modelName": "github-cloudflare-repo-bootstrap",
        "name": "result",
        "version": version,
        "ownerDefinition": {"workflowRunId": run_id},
        "content": {"exitCode": 0, "stdout": json.dumps(plan)},
    }


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
    def test_public_schema_exposes_owner_approval_operations(self):
        operations = set(
            OPS_BROKER_SCHEMA["parameters"]["properties"]["operation"]["enum"]
        )
        self.assertTrue(
            {
                "plan_linear_destructive_owner_approval",
                "start_linear_destructive_owner_approval_attest",
                "approve_linear_destructive_owner_approval_attest",
            }.issubset(operations)
        )

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
    def test_canonical_plan_checksum_is_utf8_sorted_and_compact(self):
        self.assertEqual(
            _canonical_plan_checksum({"b": [2, 1], "a": "Привет"}),
            "f5fed935f7e86bc457004bc3780183ef93581c2188663342fe41ee1285ae96a8",
        )

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
    def test_repository_plan_start_are_swe_capabilities_but_approval_is_owner_only(self):
        path = Path(__file__).parents[1] / "plugins" / "ops_broker" / "policy.json"
        policy = json.loads(path.read_text())
        plan_operation = "swamp.plan_github_cloudflare_repository"
        start_operation = "swamp.start_github_cloudflare_repository_apply"
        approve_operation = "swamp.approve_github_cloudflare_repository_apply"

        self.assertIn(plan_operation, policy["peers"]["swe"]["operations"])
        self.assertIn(start_operation, policy["peers"]["swe"]["operations"])
        self.assertNotIn(approve_operation, policy["peers"]["swe"]["operations"])
        self.assertIn(approve_operation, policy["peers"]["owner"]["operations"])
        self.assertEqual(
            policy["ownerIdentities"],
            [{"source": "telegram", "user_id": "442308262", "caller": "owner"}],
        )
        for peer in ("books", "crypto-analyst", "ideas"):
            self.assertNotIn(plan_operation, policy["peers"][peer]["operations"])
            self.assertNotIn(start_operation, policy["peers"][peer]["operations"])
            self.assertNotIn(approve_operation, policy["peers"][peer]["operations"])
        self.assertEqual(
            policy["workspace"], "/Users/hermes/workspaces/swamp-ops-runtime"
        )
        self.assertNotEqual(policy["workspace"], "/Users/hermes/workspaces/swamp-ops")
        self.assertEqual(
            policy["workspaceRevisionFile"],
            "/Users/hermes/.hermes/plugin-data/ops-broker/runtime-revision",
        )
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
        plan = repository_plan(blockers=["template missing"])

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
                            "stdout": json.dumps(plan),
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
                "plan": plan,
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

        with self.assertRaisesRegex(ValueError, "provenance is invalid"):
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
        other_plan = repository_plan("other-site", blockers=["template missing"])

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
                        "stdout": json.dumps(other_plan),
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


class ApplyBrokerTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads(
            (
                Path(__file__).parents[1]
                / "plugins"
                / "ops_broker"
                / "policy.json"
            ).read_text()
        )
        self.plan_run_id = "11111111-1111-4111-8111-111111111111"
        self.apply_run_id = "22222222-2222-4222-8222-222222222222"
        self.plan = repository_plan(ready=True)

    def start_request(self):
        return validate_request(
            {
                "request_id": "31058213-709a-47c1-a541-fd15f9169527",
                "integration": "swamp",
                "operation": "start_github_cloudflare_repository_apply",
                "arguments": {
                    "repository": "example-site",
                    "plan_run_id": self.plan_run_id,
                    "plan_checksum": self.plan["checksum"],
                    "artifact_version": 7,
                },
                "mode": "apply",
            }
        )

    def write_apply_gate(self, audit: Path, **overrides):
        record = {
            "event": "apply_gate",
            "caller": "swe",
            "apply_run_id": self.apply_run_id,
            "repository": "example-site",
            "plan_run_id": self.plan_run_id,
            "plan_checksum": self.plan["checksum"],
            "artifact_version": 7,
        }
        record.update(overrides)
        audit.write_text(json.dumps(record) + "\n")

    def test_start_apply_requires_writable_audit_before_workflow_invocation(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return {"returncode": 0, "stdout": "{}", "stderr": ""}

        with self.assertRaisesRegex(ValueError, "immutable audit path"):
            execute_request(
                self.start_request(),
                caller="swe",
                policy=self.policy,
                runner=runner,
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                audit_path=None,
            )
        self.assertEqual(calls, [])

    def test_start_apply_builds_only_fixed_checksum_bound_workflow_argv(self):
        command = build_command(
            "swamp.start_github_cloudflare_repository_apply",
            self.start_request()["arguments"],
            self.policy,
        )
        self.assertEqual(
            command,
            [
                "swamp",
                "workflow",
                "run",
                "github-cloudflare-repo-bootstrap-apply",
                "--input",
                "repository=example-site",
                "--input",
                f"planRunId={self.plan_run_id}",
                "--input",
                f"planChecksum={self.plan['checksum']}",
                "--input",
                "artifactVersion:json=7",
                "--json",
            ],
        )
        arguments = dict(self.start_request()["arguments"])
        arguments["url"] = "https://example.com"
        with self.assertRaisesRegex(ValueError, "unexpected arguments"):
            build_command(
                "swamp.start_github_cloudflare_repository_apply",
                arguments,
                self.policy,
            )

    def test_start_rechecks_plan_then_registers_suspended_run_in_audit(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            if argv[:3] == ["swamp", "data", "get"]:
                payload = plan_artifact(
                    self.plan, run_id=self.plan_run_id, version=7
                )
            else:
                payload = {"id": self.apply_run_id, "status": "suspended"}
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            response = execute_request(
                self.start_request(),
                caller="swe",
                policy=self.policy,
                runner=runner,
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                audit_path=audit,
            )
            records = [json.loads(line) for line in audit.read_text().splitlines()]

        self.assertEqual(calls[0][:4], ["swamp", "data", "get", "github-cloudflare-repo-bootstrap"])
        self.assertEqual(calls[1][:4], ["swamp", "workflow", "run", "github-cloudflare-repo-bootstrap-apply"])
        self.assertEqual(response["result"]["status"], "suspended")
        self.assertEqual(records[-1]["event"], "apply_gate")
        self.assertEqual(records[-1]["plan_checksum"], self.plan["checksum"])

    def test_blocked_plan_never_creates_manual_approval_run(self):
        request = self.start_request()
        blocked = repository_plan(ready=False, blockers=["target exists"])
        request["arguments"]["plan_checksum"] = blocked["checksum"]
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return {
                "returncode": 0,
                "stdout": json.dumps(
                    plan_artifact(blocked, run_id=self.plan_run_id, version=7)
                ),
                "stderr": "",
            }

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "not ready for apply"):
                execute_request(
                    request,
                    caller="swe",
                    policy=self.policy,
                    runner=runner,
                    workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                    audit_path=Path(tmp) / "audit.jsonl",
                )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ["swamp", "data", "get"])

    def test_wrong_plan_checksum_rejects_before_apply_workflow(self):
        request = self.start_request()
        request["arguments"]["plan_checksum"] = "0" * 64
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return {
                "returncode": 0,
                "stdout": json.dumps(
                    plan_artifact(self.plan, run_id=self.plan_run_id, version=7)
                ),
                "stderr": "",
            }

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "does not match approval"):
                execute_request(
                    request,
                    caller="swe",
                    policy=self.policy,
                    runner=runner,
                    workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                    audit_path=Path(tmp) / "audit.jsonl",
                )
        self.assertEqual(len(calls), 1)

    def test_approval_rejects_history_with_mismatched_bound_inputs(self):
        approve = validate_request(
            {
                "request_id": "81058213-709a-47c1-a541-fd15f9169527",
                "integration": "swamp",
                "operation": "approve_github_cloudflare_repository_apply",
                "arguments": {"apply_run_id": self.apply_run_id},
                "mode": "apply",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            self.write_apply_gate(audit)

            def runner(_argv, **_kwargs):
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "id": self.apply_run_id,
                            "workflowName": "github-cloudflare-repo-bootstrap-apply",
                            "inputs": {
                                "repository": "different-site",
                                "planRunId": self.plan_run_id,
                                "planChecksum": self.plan["checksum"],
                                "artifactVersion": 7,
                            },
                            "status": "suspended",
                            "jobs": [],
                        }
                    ),
                    "stderr": "",
                }

            with self.assertRaisesRegex(ValueError, "bound inputs"):
                execute_request(
                    approve,
                    caller="owner",
                    policy=self.policy,
                    runner=runner,
                    workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                    audit_path=audit,
                )

    def test_approval_retry_resumes_when_authoritative_step_is_already_succeeded(self):
        approve = validate_request(
            {
                "request_id": "61058213-709a-47c1-a541-fd15f9169527",
                "integration": "swamp",
                "operation": "approve_github_cloudflare_repository_apply",
                "arguments": {"apply_run_id": self.apply_run_id},
                "mode": "apply",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            self.write_apply_gate(audit)
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                if argv[:4] == ["swamp", "workflow", "history", "get"]:
                    payload = {
                        "id": self.apply_run_id,
                        "workflowName": "github-cloudflare-repo-bootstrap-apply",
                        "inputs": {
                            "repository": "example-site",
                            "planRunId": self.plan_run_id,
                            "planChecksum": self.plan["checksum"],
                            "artifactVersion": 7,
                        },
                        "status": "suspended",
                        "jobs": [
                            {
                                "name": "apply",
                                "steps": [
                                    {"name": "approve-create", "status": "succeeded"}
                                ],
                            }
                        ],
                    }
                else:
                    payload = {"id": self.apply_run_id, "status": "succeeded"}
                return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

            response = execute_request(
                approve,
                caller="owner",
                policy=self.policy,
                runner=runner,
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                audit_path=audit,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][:3], ["swamp", "workflow", "resume"])
        self.assertEqual(response["result"]["status"], "succeeded")

    def test_approval_retry_attests_authoritative_completed_run_without_resume(self):
        approve = validate_request(
            {
                "request_id": "71058213-709a-47c1-a541-fd15f9169527",
                "integration": "swamp",
                "operation": "approve_github_cloudflare_repository_apply",
                "arguments": {"apply_run_id": self.apply_run_id},
                "mode": "apply",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            self.write_apply_gate(audit)
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "id": self.apply_run_id,
                            "workflowName": "github-cloudflare-repo-bootstrap-apply",
                            "inputs": {
                                "repository": "example-site",
                                "planRunId": self.plan_run_id,
                                "planChecksum": self.plan["checksum"],
                                "artifactVersion": 7,
                            },
                            "status": "succeeded",
                            "jobs": [],
                        }
                    ),
                    "stderr": "",
                }

            response = execute_request(
                approve,
                caller="owner",
                policy=self.policy,
                runner=runner,
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                audit_path=audit,
            )
            records = [json.loads(line) for line in audit.read_text().splitlines()]

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["swamp", "workflow", "history", "get"])
        self.assertEqual(response["result"]["status"], "succeeded")
        self.assertEqual(records[-1]["event"], "apply_result")

    def test_approve_resumes_only_registered_exact_run_once(self):
        approve = validate_request(
            {
                "request_id": "41058213-709a-47c1-a541-fd15f9169527",
                "integration": "swamp",
                "operation": "approve_github_cloudflare_repository_apply",
                "arguments": {"apply_run_id": self.apply_run_id},
                "mode": "apply",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            self.write_apply_gate(audit)
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                if argv[:4] == ["swamp", "workflow", "history", "get"]:
                    payload = {
                        "id": self.apply_run_id,
                        "workflowName": "github-cloudflare-repo-bootstrap-apply",
                        "inputs": {
                            "repository": "example-site",
                            "planRunId": self.plan_run_id,
                            "planChecksum": self.plan["checksum"],
                            "artifactVersion": 7,
                        },
                        "status": "suspended",
                        "jobs": [
                            {
                                "name": "apply",
                                "steps": [
                                    {"name": "approve-create", "status": "waiting_approval"}
                                ],
                            }
                        ],
                    }
                elif argv[:3] == ["swamp", "workflow", "approve"]:
                    payload = {"runId": self.apply_run_id, "approved": True}
                else:
                    payload = {"id": self.apply_run_id, "status": "succeeded"}
                return {
                    "returncode": 0,
                    "stdout": json.dumps(payload),
                    "stderr": "",
                }

            response = execute_request(
                approve,
                caller="owner",
                policy=self.policy,
                runner=runner,
                workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                audit_path=audit,
            )
            with self.assertRaisesRegex(ValueError, "already approved"):
                execute_request(
                    approve,
                    caller="owner",
                    policy=self.policy,
                    runner=runner,
                    workspace=Path("/Users/hermes/workspaces/swamp-ops"),
                    audit_path=audit,
                )

        self.assertEqual(
            calls[0],
            [
                "swamp",
                "workflow",
                "history",
                "get",
                self.apply_run_id,
                "--json",
            ],
        )
        self.assertEqual(
            calls[1],
            [
                "swamp",
                "workflow",
                "approve",
                "github-cloudflare-repo-bootstrap-apply",
                "approve-create",
                "--run",
                self.apply_run_id,
                "--json",
            ],
        )
        self.assertEqual(
            calls[2],
            [
                "swamp",
                "workflow",
                "resume",
                "github-cloudflare-repo-bootstrap-apply",
                "--run",
                self.apply_run_id,
                "--json",
            ],
        )
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["result"]["status"], "succeeded")


class RuntimeWorkspaceTests(unittest.TestCase):
    def test_runtime_workspace_requires_attested_head_and_clean_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            revision = "a" * 40
            revision_file = root / "runtime-revision"
            revision_file.write_text(revision + "\n")
            policy = {"workspaceRevisionFile": str(revision_file)}
            with mock.patch(
                "plugins.ops_broker.default_runner",
                side_effect=[
                    {"returncode": 0, "stdout": revision + "\n", "stderr": ""},
                    {"returncode": 0, "stdout": "", "stderr": ""},
                ],
            ) as runner:
                _verify_runtime_workspace(policy, root)

        self.assertEqual(
            runner.call_args_list,
            [
                mock.call(["git", "rev-parse", "HEAD"], cwd=root, timeout=10),
                mock.call(
                    ["git", "status", "--porcelain", "--untracked-files=all"],
                    cwd=root,
                    timeout=10,
                ),
            ],
        )

    def test_runtime_workspace_rejects_mismatch_and_dirty_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            revision_file = root / "runtime-revision"
            revision_file.write_text("a" * 40 + "\n")
            policy = {"workspaceRevisionFile": str(revision_file)}
            with mock.patch(
                "plugins.ops_broker.default_runner",
                return_value={"returncode": 0, "stdout": "b" * 40, "stderr": ""},
            ):
                with self.assertRaisesRegex(ValueError, "HEAD does not match"):
                    _verify_runtime_workspace(policy, root)
            with mock.patch(
                "plugins.ops_broker.default_runner",
                side_effect=[
                    {"returncode": 0, "stdout": "a" * 40, "stderr": ""},
                    {"returncode": 0, "stdout": "untracked.txt\n", "stderr": ""},
                ],
            ):
                with self.assertRaisesRegex(ValueError, "not clean"):
                    _verify_runtime_workspace(policy, root)
            (root / ".swamp-sources.yaml").write_text("sources: []\n")
            with mock.patch(
                "plugins.ops_broker.default_runner",
                side_effect=[
                    {"returncode": 0, "stdout": "a" * 40, "stderr": ""},
                    {"returncode": 0, "stdout": "", "stderr": ""},
                ],
            ):
                with self.assertRaisesRegex(ValueError, "source override"):
                    _verify_runtime_workspace(policy, root)


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
            revision = "a" * 40
            revision_file = root / "runtime-revision"
            revision_file.write_text(revision + "\n")
            policy = {
                "peers": {
                    "swe": {"operations": ["github.repository_access"]}
                },
                "github": {"repositories": ["sisyphus-org/swamp-ops"]},
                "workspace": str(root),
                "workspaceRevisionFile": str(revision_file),
            }
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(policy))
            untrusted_policy_path = root / "untrusted-policy.json"
            untrusted_policy_path.write_text(
                json.dumps(
                    {
                        **policy,
                        "workspace": "/Users/hermes/workspaces/swamp-ops",
                    }
                )
            )
            audit_path = root / "audit.jsonl"

            def runner(argv, **_kwargs):
                if argv == ["git", "rev-parse", "HEAD"]:
                    return {"returncode": 0, "stdout": revision + "\n", "stderr": ""}
                if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
                    return {"returncode": 0, "stdout": "", "stderr": ""}
                return {
                    "returncode": 0,
                    "stdout": '{"full_name":"sisyphus-org/swamp-ops"}',
                    "stderr": "",
                }

            with mock.patch.dict(
                "os.environ",
                {
                    "HERMES_HOME": str(root),
                    "OPS_BROKER_POLICY": str(untrusted_policy_path),
                    "OPS_BROKER_WORKSPACE": "/Users/hermes/workspaces/swamp-ops",
                    "OPS_BROKER_AUDIT": str(audit_path),
                },
                clear=False,
            ), mock.patch(
                "plugins.ops_broker.PLUGIN_ROOT", root
            ), mock.patch(
                "plugins.ops_broker.default_runner",
                side_effect=runner,
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
                "plugins.ops_broker._verify_runtime_workspace"
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

    def test_resolve_caller_accepts_only_policy_bound_owner_session(self):
        identities = [
            {"source": "telegram", "user_id": "442308262", "caller": "owner"}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT)"
            )
            connection.executemany(
                "INSERT INTO sessions (id, source, user_id) VALUES (?, ?, ?)",
                [
                    ("owner-session", "telegram", "442308262"),
                    ("other-session", "telegram", "999"),
                    ("a2a-owner-session", "a2a", "owner"),
                ],
            )
            connection.commit()
            connection.close()

            self.assertEqual(
                resolve_caller("owner-session", db_path, identities), "owner"
            )
            with self.assertRaisesRegex(ValueError, "not an authenticated A2A peer or owner"):
                resolve_caller("other-session", db_path, identities)
            with self.assertRaisesRegex(ValueError, "reserved privileged principal"):
                resolve_caller("a2a-owner-session", db_path, identities)


if __name__ == "__main__":
    unittest.main()
