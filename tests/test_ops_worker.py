import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from plugins.linear_source_route.ops_route import build_operations_task_body
from plugins.ops_broker import (
    _command_from_task,
    _verify_source_route,
    handle_om_ops_execute,
)


REQUEST = {
    "request_id": "56040553-7de4-4849-a16d-a2a0ea8b749a",
    "integration": "github",
    "operation": "repository_access",
    "arguments": {"repository": "sisyphus-org/swamp-ops"},
    "mode": "plan",
}
COMMAND = {
    "schema_version": "operations-command.v1",
    "command_id": "41058213-709a-47c1-a541-fd15f9169527",
    "idempotency_key": "operations:v1:0cac78bb8c8fbbaaf58bbb15cd2a7b43",
    "source_profile": "swe",
    "source_session_id": "20260828_120000_abcdef12",
    "caller": "swe",
    "request": REQUEST,
}


class Lifecycle:
    def __init__(self, task_id):
        self.task_id = task_id
        self.completed = None
        self.blocked = None

    def complete(self, *, summary, result):
        self.completed = (summary, result)

    def block(self, *, reason, kind):
        self.blocked = (reason, kind)


class OperationsManagerWorkerTests(unittest.TestCase):
    def test_source_worker_and_policy_share_exact_owner_identity_contract(self):
        root = Path(__file__).parents[1] / "plugins"
        source_identity = json.loads(
            (root / "linear_source_route" / "operations_owner_identity.json").read_text()
        )
        worker_identity = json.loads(
            (root / "ops_broker" / "operations_owner_identity.json").read_text()
        )
        policy = json.loads((root / "ops_broker" / "policy.json").read_text())
        self.assertEqual(source_identity, worker_identity)
        self.assertEqual(policy["ownerIdentities"], [worker_identity])

    def environ(self, db_path):
        return {
            "HERMES_PROFILE": "operations-manager",
            "HERMES_HOME": str(db_path.parent / "operations-manager"),
            "HERMES_KANBAN_TASK": "t_12345678",
            "HERMES_KANBAN_RUN_ID": "7",
            "HERMES_KANBAN_DB": str(db_path),
            "HERMES_KANBAN_CLAIM_LOCK": "claim-lock",
            "GH_TOKEN": "present-not-printed",
            "SWAMP_API_KEY": "present-not-printed",
        }

    def task(self):
        return {
            "id": "t_12345678",
            "assignee": "operations-manager",
            "created_by": "swe",
            "status": "running",
            "current_run_id": 7,
            "session_id": COMMAND["source_session_id"],
            "idempotency_key": "operations-delivery:v1:91c6a5f6305f31f4e5c50f9258a84fc8",
            "body": build_operations_task_body(COMMAND),
        }

    def test_executes_only_persisted_command_and_completes_same_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "kanban.db"
            db_path.write_text("")
            lifecycle = Lifecycle("t_12345678")
            policy = {
                "workspace": str(root),
                "workspaceRevisionFile": str(root / "revision"),
                "ownerIdentities": [
                    {
                        "source": "telegram",
                        "user_id": "442308262",
                        "caller": "owner",
                    }
                ],
                "peers": {
                    "swe": {"operations": ["github.repository_access"]}
                },
                "github": {"repositories": ["sisyphus-org/swamp-ops"]},
                "swamp": {"models": [], "workflows": [], "data": []},
            }
            def runner(_argv, **_kwargs):
                return {
                    "returncode": 0,
                    "stdout": json.dumps({
                        "nameWithOwner": "sisyphus-org/swamp-ops",
                        "viewerPermission": "READ",
                    }),
                    "stderr": "",
                }
            response = json.loads(handle_om_ops_execute(
                {},
                environ=self.environ(db_path),
                task_loader=lambda *_: self.task(),
                run_reserver=lambda *_: True,
                route_verifier=lambda *_: True,
                lifecycle_factory=lambda _: lifecycle,
                policy=policy,
                workspace=root,
                workspace_verifier=lambda *_: None,
                runner=runner,
            ))
            self.assertEqual(response["status"], "completed")
            self.assertTrue(response["verified"])
            self.assertIsNotNone(lifecycle.completed)
            persisted = json.loads(lifecycle.completed[1])
            self.assertEqual(persisted["schema_version"], "operations-result.v1")
            self.assertEqual(persisted["source_profile"], "swe")
            self.assertEqual(persisted["caller"], "swe")
            self.assertEqual(persisted["operation"], "github.repository_access")
            self.assertNotIn("present-not-printed", lifecycle.completed[1])

    def test_rejects_model_supplied_command(self):
        response = json.loads(handle_om_ops_execute(
            {"request": REQUEST}, environ={"HERMES_PROFILE": "operations-manager"}
        ))
        self.assertEqual(response["status"], "rejected")
        self.assertIn("no model-supplied", response["error"])

    def test_rejects_command_when_semantic_key_does_not_match_request(self):
        tampered = {
            **COMMAND,
            "request": {
                **REQUEST,
                "arguments": {"repository": "sisyphus-org/other"},
            },
        }
        task = {**self.task(), "body": build_operations_task_body(tampered)}
        with self.assertRaisesRegex(RuntimeError, "idempotency"):
            _command_from_task(task)

    def test_owner_authority_requires_exact_default_wake_user(self):
        class Connection:
            def __init__(self, row):
                self.row = row
            def execute(self, *_args):
                class Rows:
                    def __init__(self, row):
                        self.row = row
                    def fetchall(self):
                        return [self.row]
                return Rows(self.row)
            def close(self):
                pass

        base_row = {
            "platform": "telegram",
            "chat_id": "442308262",
            "thread_id": "455313",
            "user_id": "442308262",
            "chat_type": "dm",
            "notifier_profile": "default",
            "delivery_mode": "wake",
        }
        owner_command = {
            **COMMAND,
            "source_profile": "default",
            "caller": "owner",
            "idempotency_key": "operations:v1:4f402c5e6a8bb3f8e20e4206aa5f97d7",
        }
        owner_task = {
            **self.task(),
            "created_by": "default",
            "idempotency_key": "operations-delivery:v1:5edaebfc879eb54af4195b9e89a1912f",
        }
        with mock.patch(
            "hermes_cli.kanban_db.connect", return_value=Connection(base_row)
        ):
            self.assertTrue(_verify_source_route(owner_task, owner_command, Path("/tmp/kanban.db")))
        wrong_user = {**base_row, "user_id": "123456"}
        with mock.patch(
            "hermes_cli.kanban_db.connect", return_value=Connection(wrong_user)
        ):
            self.assertFalse(_verify_source_route(owner_task, owner_command, Path("/tmp/kanban.db")))

    def test_requires_operations_manager_profile(self):
        response = json.loads(handle_om_ops_execute({}, environ={"HERMES_PROFILE": "broker"}))
        self.assertEqual(response["status"], "rejected")
        self.assertIn("operations-manager", response["error"])

    def test_requires_profile_local_provider_credentials_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanban.db"
            db_path.write_text("")
            environ = self.environ(db_path)
            environ.pop("GH_TOKEN")
            response = json.loads(handle_om_ops_execute(
                {},
                environ=environ,
                task_loader=lambda *_: self.task(),
                run_reserver=lambda *_: True,
                route_verifier=lambda *_: True,
            ))
            self.assertEqual(response["status"], "rejected")
            self.assertIn("profile-local credentials", response["error"])

    def test_rejects_wrong_task_assignee_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanban.db"
            db_path.write_text("")
            task = {**self.task(), "assignee": "broker"}
            response = json.loads(handle_om_ops_execute(
                {}, environ=self.environ(db_path), task_loader=lambda *_: task
            ))
            self.assertEqual(response["status"], "rejected")
            self.assertIn("task/run binding", response["error"])


if __name__ == "__main__":
    unittest.main()
