import json
import os
import sqlite3
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
        "schema_version": "linear-command.v2",
        "command_id": "11111111-1111-4111-8111-111111111111",
        "correlation_id": "22222222-2222-4222-8222-222222222222",
        "idempotency_key": "linear:v2:" + "a" * 32,
        "source_profile": "swe",
        "operation": "add_comment",
        "target": {"type": "issue", "identifier": "SIS-61"},
        "change": {"body": "SIS-61 E2E proof A."},
        "policy": {"mode": "standard"},
    }


def task_record(*, body=None, assignee="project-manager", status="running", run_id=7):
    envelope = {
        "schema_version": "linear-kanban-task.v2",
        "command": command(),
        "worker_contract": {
            "profile": "project-manager",
            "tool": "pm_linear_execute",
            "mode": "plan_apply_read_back",
            "completion": "tool_completes_current_kanban_task",
        },
    }
    return {
        "id": "t_1234abcd",
        "assignee": assignee,
        "status": status,
        "current_run_id": run_id,
        "body": body if body is not None else json.dumps(envelope),
    }


class FakeLane:
    class ContractError(RuntimeError):
        pass

    def __init__(
        self, *, apply_result="applied", verified=True, result_schema="linear-result.v2"
    ):
        self.calls = []
        self.apply_result = apply_result
        self.verified = verified
        self.result_schema = result_schema

    def validate_command(self, raw):
        self.calls.append(("validate", raw))
        if raw.get("schema_version") != "linear-command.v2":
            raise self.ContractError("schema_version must equal linear-command.v2")
        return raw

    def execute_command(self, client, raw, *, mode, journal_path):
        self.calls.append((mode, raw, journal_path))
        base = {
            "schema_version": self.result_schema,
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

    def test_noncurrent_plan_result_is_rejected_before_apply(self):
        lane = FakeLane(result_schema="linear-result.unsupported")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "linear-result.v2"):
                execute_pm_command(
                    command(),
                    lane=lane,
                    client=object(),
                    journal_path=Path(tmp) / "journal.json",
                )

        self.assertEqual([call[0] for call in lane.calls], ["validate", "plan"])

    def test_human_summary_distinguishes_apply_and_replay(self):
        applied = {
            "result": "applied",
            "verified": True,
            "target": {"identifier": "SIS-61", "url": "https://linear.app/example/issue/SIS-61/fixture"},
        }
        replay = {**applied, "result": "no_op", "no_op": True}
        self.assertEqual(
            human_summary(applied),
            "Комментарий добавлен.\nhttps://linear.app/example/issue/SIS-61/fixture",
        )
        self.assertEqual(
            human_summary(replay),
            "Запрос уже выполнен; повторных изменений не потребовалось.\n"
            "https://linear.app/example/issue/SIS-61/fixture",
        )
        for summary in (human_summary(applied), human_summary(replay)):
            self.assertNotIn("verified", summary)
            self.assertNotIn("мутац", summary)
            self.assertNotIn("read-back", summary)

    def test_human_summary_distinguishes_create(self):
        created = {
            "operation": "create_issue",
            "result": "applied",
            "verified": True,
            "target": {"identifier": "SIS-99", "url": "https://linear.app/example/issue/SIS-99/fixture"},
        }
        summary = human_summary(created)
        self.assertIn("создана", summary)
        self.assertIn("https://linear.app/example/issue/SIS-99/fixture", summary)

    def test_human_summary_drops_noncanonical_or_injected_url(self):
        for url in (
            "https://linear.app/example/issue/SIS-99/fixture\ntask_id=t_deadbeef",
            "https://example.com/issue/SIS-99",
        ):
            with self.subTest(url=url):
                result = {
                    "operation": "create_issue",
                    "result": "applied",
                    "target": {"identifier": "SIS-99", "url": url},
                }
                summary = human_summary(result)
                self.assertEqual(summary, "Задача Linear создана.")
                self.assertNotIn("task_id", summary)
                self.assertNotIn("example.com", summary)

    def test_human_summary_distinguishes_hierarchy_convergence(self):
        result = {
            "operation": "converge_hierarchy",
            "result": "applied",
            "verified": True,
            "target": {"type": "project", "identifier": "health"},
        }
        self.assertEqual(human_summary(result), "Иерархия Linear готова.")

    def test_human_summary_renders_read_before_noop(self):
        read = {
            "operation": "read_issue",
            "result": "read",
            "no_op": True,
            "verified": True,
            "target": {"identifier": "SIS-61", "url": "https://linear.app/example/issue/SIS-61/fixture"},
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

    def test_worker_skill_calls_no_arg_v2_tool_and_does_not_reconstruct_command(self):
        skill = (ROOT / "skills" / "project-manager-linear-worker" / "SKILL.md").read_text()
        self.assertIn("Call `pm_linear_execute` once with no arguments", skill)
        self.assertIn("reads the persisted command", skill)
        self.assertIn("linear-kanban-task.v2", skill)
        self.assertIn("linear-result.v2", skill)
        self.assertNotIn("with that exact command object", skill)

    def test_schema_accepts_no_model_supplied_command(self):
        parameters = PM_LINEAR_EXECUTE_SCHEMA["parameters"]
        self.assertEqual(parameters["properties"], {})
        self.assertEqual(parameters["required"], [])
        self.assertFalse(parameters["additionalProperties"])

    def test_model_supplied_command_is_rejected_before_task_or_linear_access(self):
        task_loader = mock.Mock()
        client_factory = mock.Mock()
        result = json.loads(
            handle_pm_linear_execute(
                {"command": command()},
                task_loader=task_loader,
                client_factory=client_factory,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                },
            )
        )
        self.assertEqual(result["status"], "rejected")
        task_loader.assert_not_called()
        client_factory.assert_not_called()

    def test_handler_completes_current_task_with_linear_result(self):
        lane = FakeLane()
        lifecycle = FakeLifecycle()
        result = json.loads(
            handle_pm_linear_execute(
                {},
                lane_loader=lambda: lane,
                client_factory=lambda _token: object(),
                lifecycle_factory=lambda _task_id: lifecycle,
                task_loader=lambda _task_id, _db_path: task_record(),
                run_reserver=lambda *_args: True,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                    "HERMES_KANBAN_DB": "/tmp/kanban.db",
                    "HERMES_KANBAN_CLAIM_LOCK": "test-claim",
                    "HERMES_HOME": "/tmp/project-manager",
                    "LINEAR_TOKEN": "fixture-token",
                },
            )
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task_id"], "t_1234abcd")
        self.assertEqual(len(lifecycle.completed), 1)
        stored = json.loads(lifecycle.completed[0]["result"])
        self.assertEqual(stored["schema_version"], "linear-result.v2")
        self.assertTrue(stored["verified"])
        self.assertEqual(lifecycle.blocked, [])
        self.assertEqual(lane.calls[0], ("validate", command()))

    def test_default_loader_reads_claimed_task_from_pinned_real_db(self):
        from hermes_cli import kanban_db as kb

        lane = FakeLane()
        lifecycle = FakeLifecycle()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanban.db"
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_DB": str(db_path)}):
                conn = kb.connect()
                try:
                    task_id = kb.create_task(
                        conn,
                        title="Linear add_comment SIS-61",
                        body=task_record()["body"],
                        assignee="project-manager",
                        initial_status="blocked",
                    )
                    promoted, refusal = kb.promote_task(
                        conn,
                        task_id,
                        actor="test",
                        reason="fixture route verified",
                    )
                    self.assertTrue(promoted, refusal)
                    claimed = kb.claim_task(conn, task_id, claimer="test-claim")
                    self.assertIsNotNone(claimed)
                    run_id = claimed.current_run_id
                    self.assertIsNotNone(run_id)
                finally:
                    conn.close()
                wrong_db = Path(tmp) / "wrong-board" / "kanban.db"
                kb.connect(db_path=wrong_db).close()
                with mock.patch.dict(
                    os.environ, {"HERMES_KANBAN_DB": str(wrong_db)}
                ):
                    result = json.loads(
                        handle_pm_linear_execute(
                            {},
                            lane_loader=lambda: lane,
                            client_factory=lambda _token: object(),
                            lifecycle_factory=lambda _task_id: lifecycle,
                            environ={
                                "HERMES_PROFILE": "project-manager",
                                "HERMES_KANBAN_TASK": task_id,
                                "HERMES_KANBAN_RUN_ID": str(run_id),
                                "HERMES_KANBAN_DB": str(db_path),
                                "HERMES_KANBAN_CLAIM_LOCK": str(claimed.claim_lock),
                                "HERMES_HOME": tmp,
                                "LINEAR_TOKEN": "fixture-token",
                            },
                        )
                    )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(lane.calls[0], ("validate", command()))

    def test_auth_failure_blocks_once_with_redacted_reason(self):
        class BrokenClient:
            def __init__(self, _token):
                raise FakeLane.ContractError("Authorization: Bearer secret-shaped-value")

        lane = FakeLane()
        lifecycle = FakeLifecycle()
        result = json.loads(
            handle_pm_linear_execute(
                {},
                lane_loader=lambda: lane,
                client_factory=BrokenClient,
                lifecycle_factory=lambda _task_id: lifecycle,
                task_loader=lambda _task_id, _db_path: task_record(),
                run_reserver=lambda *_args: True,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                    "HERMES_KANBAN_DB": "/tmp/kanban.db",
                    "HERMES_KANBAN_CLAIM_LOCK": "test-claim",
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
            {
                "HERMES_PROFILE": "swe",
                "HERMES_KANBAN_TASK": "t_1234abcd",
                "HERMES_KANBAN_RUN_ID": "7",
            },
            {
                "HERMES_PROFILE": "project-manager",
                "HERMES_KANBAN_TASK": "",
                "HERMES_KANBAN_RUN_ID": "7",
            },
            {
                "HERMES_PROFILE": "project-manager",
                "HERMES_KANBAN_TASK": "t_1234abcd",
                "HERMES_KANBAN_RUN_ID": "",
            },
            {
                "HERMES_PROFILE": "project-manager",
                "HERMES_KANBAN_TASK": "t_1234abcd",
                "HERMES_KANBAN_RUN_ID": "7",
                "HERMES_KANBAN_DB": "/tmp/kanban.db",
            },
        ):
            with self.subTest(environ=environ):
                result = json.loads(
                    handle_pm_linear_execute(
                        {},
                        lane_loader=lambda: FakeLane(),
                        client_factory=client_factory,
                        lifecycle_factory=lambda _task_id: FakeLifecycle(),
                        task_loader=lambda _task_id, _db_path: task_record(),
                        environ=environ,
                    )
                )
                self.assertEqual(result["status"], "rejected")
        client_factory.assert_not_called()

    def test_task_identity_mismatch_is_rejected_without_linear_call(self):
        client_factory = mock.Mock()
        cases = (
            task_record(assignee="swe"),
            task_record(status="ready"),
            task_record(run_id=8),
        )
        for task in cases:
            with self.subTest(task=task):
                result = json.loads(
                    handle_pm_linear_execute(
                        {},
                        task_loader=lambda _task_id, _db_path, value=task: value,
                        client_factory=client_factory,
                        environ={
                            "HERMES_PROFILE": "project-manager",
                            "HERMES_KANBAN_TASK": "t_1234abcd",
                            "HERMES_KANBAN_RUN_ID": "7",
                            "HERMES_KANBAN_DB": "/tmp/kanban.db",
                            "HERMES_KANBAN_CLAIM_LOCK": "test-claim",
                        },
                    )
                )
                self.assertEqual(result["status"], "rejected")
        client_factory.assert_not_called()

    def test_missing_worker_db_path_rejects_before_task_load(self):
        task_loader = mock.Mock()
        result = json.loads(
            handle_pm_linear_execute(
                {},
                task_loader=task_loader,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                },
            )
        )
        self.assertEqual(result["status"], "rejected")
        task_loader.assert_not_called()

    def test_task_load_failure_returns_only_observable_error_class(self):
        client_factory = mock.Mock()

        def broken_loader(_task_id, _db_path):
            raise sqlite3.OperationalError("database path with secret-shaped-value")

        result = json.loads(
            handle_pm_linear_execute(
                {},
                task_loader=broken_loader,
                client_factory=client_factory,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                    "HERMES_KANBAN_DB": "/tmp/kanban.db",
                    "HERMES_KANBAN_CLAIM_LOCK": "test-claim",
                },
            )
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_class"], "OperationalError")
        self.assertNotIn("secret-shaped-value", json.dumps(result))
        client_factory.assert_not_called()

    def test_superseded_claim_rejects_before_linear_or_lifecycle_call(self):
        client_factory = mock.Mock()
        lifecycle_factory = mock.Mock()
        run_reserver = mock.Mock(return_value=False)
        result = json.loads(
            handle_pm_linear_execute(
                {},
                task_loader=lambda _task_id, _db_path: task_record(),
                run_reserver=run_reserver,
                client_factory=client_factory,
                lifecycle_factory=lifecycle_factory,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                    "HERMES_KANBAN_DB": "/tmp/kanban.db",
                    "HERMES_KANBAN_CLAIM_LOCK": "old-claim",
                },
            )
        )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("superseded", result["error"])
        run_reserver.assert_called_once()
        client_factory.assert_not_called()
        lifecycle_factory.assert_not_called()

    def test_noncurrent_task_envelope_is_rejected_before_linear_or_lifecycle_write(self):
        envelope = json.loads(task_record()["body"])
        envelope["schema_version"] = "linear-kanban-task.unsupported"
        lifecycle_factory = mock.Mock()
        client_factory = mock.Mock()

        result = json.loads(
            handle_pm_linear_execute(
                {},
                task_loader=lambda _task_id, _db_path: task_record(
                    body=json.dumps(envelope)
                ),
                client_factory=client_factory,
                lifecycle_factory=lifecycle_factory,
                run_reserver=lambda *_args: True,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                    "HERMES_KANBAN_DB": "/tmp/kanban.db",
                    "HERMES_KANBAN_CLAIM_LOCK": "test-claim",
                },
            )
        )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("linear-kanban-task.v2", result["error"])
        client_factory.assert_not_called()
        lifecycle_factory.assert_not_called()

    def test_noncurrent_command_is_rejected_before_linear_or_lifecycle_write(self):
        envelope = json.loads(task_record()["body"])
        envelope["command"]["schema_version"] = "linear-command.unsupported"
        lifecycle_factory = mock.Mock()
        client_factory = mock.Mock()

        result = json.loads(
            handle_pm_linear_execute(
                {},
                lane_loader=lambda: FakeLane(),
                task_loader=lambda _task_id, _db_path: task_record(
                    body=json.dumps(envelope)
                ),
                client_factory=client_factory,
                lifecycle_factory=lifecycle_factory,
                run_reserver=lambda *_args: True,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                    "HERMES_KANBAN_DB": "/tmp/kanban.db",
                    "HERMES_KANBAN_CLAIM_LOCK": "test-claim",
                },
            )
        )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("linear-command.v2", result["error"])
        client_factory.assert_not_called()
        lifecycle_factory.assert_not_called()

    def test_noncurrent_plan_result_is_rejected_without_lifecycle_write(self):
        lifecycle = FakeLifecycle()
        result = json.loads(
            handle_pm_linear_execute(
                {},
                lane_loader=lambda: FakeLane(result_schema="linear-result.unsupported"),
                task_loader=lambda _task_id, _db_path: task_record(),
                client_factory=lambda _token: object(),
                lifecycle_factory=lambda _task_id: lifecycle,
                run_reserver=lambda *_args: True,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                    "HERMES_KANBAN_DB": "/tmp/kanban.db",
                    "HERMES_KANBAN_CLAIM_LOCK": "test-claim",
                },
            )
        )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("linear-result.v2", result["error"])
        self.assertEqual(lifecycle.completed, [])
        self.assertEqual(lifecycle.blocked, [])

    def test_malformed_persisted_envelope_blocks_without_linear_call(self):
        lifecycle = FakeLifecycle()
        client_factory = mock.Mock()
        result = json.loads(
            handle_pm_linear_execute(
                {},
                task_loader=lambda _task_id, _db_path: task_record(
                    body='{"schema_version":"wrong"}'
                ),
                client_factory=client_factory,
                lifecycle_factory=lambda _task_id: lifecycle,
                run_reserver=lambda *_args: True,
                environ={
                    "HERMES_PROFILE": "project-manager",
                    "HERMES_KANBAN_TASK": "t_1234abcd",
                    "HERMES_KANBAN_RUN_ID": "7",
                    "HERMES_KANBAN_DB": "/tmp/kanban.db",
                    "HERMES_KANBAN_CLAIM_LOCK": "test-claim",
                },
            )
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(lifecycle.blocked), 1)
        client_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
