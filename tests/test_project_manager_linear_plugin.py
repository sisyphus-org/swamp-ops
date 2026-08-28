import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.project_manager_linear import (  # noqa: E402
    PM_LINEAR_EXECUTE_SCHEMA,
    _sanitize_error,
    execute_pm_command,
    handle_pm_linear_execute,
    human_summary,
)


def command():
    return {
        "schema_version": "linear-command.v1",
        "command_id": "11111111-1111-4111-8111-111111111111",
        "correlation_id": "22222222-2222-4222-8222-222222222222",
        "idempotency_key": "linear:v1:" + "a" * 32,
        "source_profile": "swe",
        "operation": "add_comment",
        "target": {"type": "issue", "identifier": "SIS-61"},
        "change": {"body": "SIS-61 E2E proof A."},
        "policy": {"mode": "standard"},
    }


class FakeLane:
    class ContractError(RuntimeError):
        pass

    def __init__(self, *, apply_result="applied", verified=True):
        self.calls = []
        self.apply_result = apply_result
        self.verified = verified

    def validate_command(self, raw):
        self.calls.append(("validate", raw))
        return raw

    def execute_command(self, client, raw, *, mode, journal_path):
        self.calls.append((mode, raw, journal_path))
        base = {
            "schema_version": "linear-result.v1",
            "result": "planned" if mode == "plan" else self.apply_result,
            "verified": False if mode == "plan" else self.verified,
            "no_op": self.apply_result == "no_op",
            "target": {
                "identifier": "SIS-61",
                "url": "https://linear.app/example/SIS-61",
            },
        }
        return base


class FakeLifecycle:
    def __init__(self):
        self.completed = []
        self.blocked = []

    def complete(self, *, summary, result):
        self.completed.append({"summary": summary, "result": result})

    def block(self, *, reason, kind):
        self.blocked.append({"reason": reason, "kind": kind})


class ExecutionTests(unittest.TestCase):
    def test_plan_runs_before_apply_and_returns_verified_result(self):
        lane = FakeLane()
        with tempfile.TemporaryDirectory() as tmp:
            output = execute_pm_command(
                command(),
                lane=lane,
                client=object(),
                journal_path=Path(tmp) / "journal.json",
            )
        self.assertEqual([call[0] for call in lane.calls], ["validate", "plan", "apply"])
        self.assertEqual(output["plan"]["result"], "planned")
        self.assertEqual(output["result"]["result"], "applied")
        self.assertTrue(output["result"]["verified"])

    def test_unverified_apply_fails_closed(self):
        lane = FakeLane(verified=False)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "verified"):
                execute_pm_command(
                    command(),
                    lane=lane,
                    client=object(),
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_human_summary_distinguishes_apply_and_replay(self):
        applied = {
            "result": "applied",
            "verified": True,
            "target": {"identifier": "SIS-61", "url": "https://linear.app/SIS-61"},
        }
        replay = {**applied, "result": "no_op", "no_op": True}
        self.assertIn("добавлен и прочитан обратно", human_summary(applied))
        self.assertIn("https://linear.app/SIS-61", human_summary(applied))
        self.assertIn("уже выполнен", human_summary(replay))

    def test_human_summary_renders_read_before_noop(self):
        read = {
            "operation": "read_issue",
            "result": "read",
            "no_op": True,
            "verified": True,
            "target": {"identifier": "SIS-61", "url": "https://linear.app/SIS-61"},
        }
        summary = human_summary(read)
        self.assertIn("прочитана", summary)
        self.assertNotIn("уже выполнен", summary)


class PluginTests(unittest.TestCase):
    def test_error_sanitizer_redacts_linear_key_before_truncation(self):
        error = RuntimeError("x" * 485 + " lin_api_" + "A" * 100)
        sanitized = _sanitize_error(error)
        self.assertLessEqual(len(sanitized), 500)
        self.assertNotIn("lin_api_", sanitized)
        self.assertIn("[credential-redacted]", sanitized)

    def test_error_sanitizer_redacts_basic_authorization(self):
        sanitized = _sanitize_error(
            RuntimeError("Authorization: Basic secret-shaped-value")
        )
        self.assertEqual(sanitized, "[credential-redacted]")

    def test_pm_plugin_bundles_lane_without_mutable_runtime_dependency(self):
        plugin_root = ROOT / "plugins" / "project_manager_linear"
        self.assertTrue((plugin_root / "lane.py").is_file())
        source = "\n".join(
            path.read_text()
            for path in sorted(plugin_root.glob("*.py"))
        )
        self.assertNotIn("swamp-ops-runtime", source)

    def test_schema_accepts_only_typed_command(self):
        parameters = PM_LINEAR_EXECUTE_SCHEMA["parameters"]
        self.assertEqual(set(parameters["properties"]), {"command"})
        self.assertEqual(parameters["required"], ["command"])
        self.assertFalse(parameters["additionalProperties"])
        command_schema = parameters["properties"]["command"]
        self.assertFalse(command_schema["additionalProperties"])
        self.assertEqual(
            set(command_schema["required"]),
            {
                "schema_version",
                "command_id",
                "correlation_id",
                "idempotency_key",
                "source_profile",
                "operation",
                "target",
                "change",
                "policy",
            },
        )
        self.assertEqual(command_schema["properties"]["schema_version"]["const"], "linear-command.v1")

    def test_handler_completes_current_task_with_linear_result(self):
        lane = FakeLane()
        lifecycle = FakeLifecycle()
        result = json.loads(
            handle_pm_linear_execute(
                {"command": command()},
                lane_loader=lambda: lane,
                client_factory=lambda _token: object(),
                lifecycle_factory=lambda _task_id: lifecycle,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_HOME": "/tmp/project-manager",
                    "LINEAR_TOKEN": "fixture-token",
                },
            )
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task_id"], "t_1234abcd")
        self.assertEqual(len(lifecycle.completed), 1)
        stored = json.loads(lifecycle.completed[0]["result"])
        self.assertEqual(stored["schema_version"], "linear-result.v1")
        self.assertTrue(stored["verified"])
        self.assertEqual(lifecycle.blocked, [])

    def test_auth_failure_blocks_once_with_redacted_reason(self):
        class BrokenClient:
            def __init__(self, _token):
                raise FakeLane.ContractError("Authorization: Bearer secret-shaped-value")

        lane = FakeLane()
        lifecycle = FakeLifecycle()
        result = json.loads(
            handle_pm_linear_execute(
                {"command": command()},
                lane_loader=lambda: lane,
                client_factory=BrokenClient,
                lifecycle_factory=lambda _task_id: lifecycle,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_HOME": "/tmp/project-manager",
                    "LINEAR_TOKEN": "fixture-token",
                },
            )
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(lifecycle.blocked), 1)
        self.assertNotIn("secret-shaped-value", json.dumps(result) + json.dumps(lifecycle.blocked))
        self.assertEqual(lifecycle.completed, [])

    def test_non_pm_or_non_worker_context_rejected_without_linear_call(self):
        client_factory = mock.Mock()
        for environ in (
            {"HERMES_PROFILE": "swe", "HERMES_KANBAN_TASK": "t_1234abcd"},
            {"HERMES_PROFILE": "project-manager", "HERMES_KANBAN_TASK": ""},
        ):
            with self.subTest(environ=environ):
                result = json.loads(
                    handle_pm_linear_execute(
                        {"command": command()},
                        lane_loader=lambda: FakeLane(),
                        client_factory=client_factory,
                        lifecycle_factory=lambda _task_id: FakeLifecycle(),
                        environ=environ,
                    )
                )
                self.assertEqual(result["status"], "rejected")
        client_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
