import json
import unittest
from dataclasses import dataclass

from plugins.linear_source_route.ops_route import (
    OperationsRouteError,
    build_operations_task_body,
    route_operations_request,
)
from plugins.linear_source_route import handle_operations_source_request
from plugins.linear_source_route.route import SourceContext


REQUEST = {
    "request_id": "56040553-7de4-4849-a16d-a2a0ea8b749a",
    "integration": "github",
    "operation": "repository_access",
    "arguments": {"repository": "sisyphus-org/swamp-ops"},
    "mode": "plan",
}


def source(profile="swe", user_id="442308262"):
    return SourceContext(
        session_id="20260828_120000_abcdef12",
        profile=profile,
        platform="telegram",
        chat_id="442308262",
        user_id=user_id,
        chat_type="dm",
        thread_id="455313",
    )


@dataclass
class FakeBoard:
    existing: dict | None = None

    def __post_init__(self):
        self.created_kwargs = None
        self.wake = None
        self.released = None

    def get_or_create_task(self, key, **kwargs):
        self.created_kwargs = kwargs
        if self.existing is not None:
            return self.existing, False
        return {
            "id": "t_12345678",
            "status": "triage",
            "session_id": kwargs["session_id"],
            "idempotency_key": key,
            "body": kwargs["body"],
            "result": None,
        }, True

    def set_wake_route(self, task_id, source_context):
        self.wake = (task_id, source_context)

    def audit_route(self, task_id, source_context):
        return {"result": "pass"}

    def release(self, task_id, reason):
        self.released = (task_id, reason)


class OperationsSourceRouteTests(unittest.TestCase):
    def test_public_handler_uses_runtime_profile_and_exact_session(self):
        board = FakeBoard()
        values = {
            "HERMES_SESSION_ID": source().session_id,
            "HERMES_SESSION_PROFILE": "swe",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "455313",
        }
        result = json.loads(handle_operations_source_request(
            REQUEST,
            session_id=source().session_id,
            runtime_profile_getter=lambda: "swe",
            session_getter=lambda name, default="": values.get(name, default),
            board_factory=lambda **_: board,
        ))
        self.assertEqual(result, {"status": "queued"})
        self.assertEqual(
            json.loads(board.created_kwargs["body"])["command"]["caller"], "swe"
        )

    def test_routes_bounded_request_to_operations_manager_through_kanban(self):
        board = FakeBoard()
        result = route_operations_request(REQUEST, source=source(), board=board)
        self.assertEqual(result, {"status": "queued"})
        self.assertEqual(board.created_kwargs["assignee"], "operations-manager")
        self.assertEqual(board.created_kwargs["skills"], ["operations-manager-worker"])
        self.assertTrue(board.created_kwargs["triage"])
        self.assertEqual(board.created_kwargs["session_id"], source().session_id)
        envelope = json.loads(board.created_kwargs["body"])
        self.assertEqual(envelope["schema_version"], "operations-kanban-task.v1")
        self.assertEqual(envelope["worker_contract"]["tool"], "om_ops_execute")
        self.assertEqual(envelope["command"]["caller"], "swe")
        self.assertEqual(envelope["command"]["request"], REQUEST)
        self.assertNotIn("GH_TOKEN", board.created_kwargs["body"])
        self.assertIsNotNone(board.wake)
        self.assertIsNotNone(board.released)

    def test_default_owner_identity_is_derived_from_exact_session(self):
        board = FakeBoard()
        route_operations_request(REQUEST, source=source(profile="default"), board=board)
        envelope = json.loads(board.created_kwargs["body"])
        self.assertEqual(envelope["command"]["caller"], "owner")

    def test_default_non_owner_session_is_rejected(self):
        with self.assertRaisesRegex(OperationsRouteError, "authenticated owner"):
            route_operations_request(
                REQUEST,
                source=source(profile="default", user_id="123456"),
                board=FakeBoard(),
            )

    def test_completed_replay_requires_bound_verified_result(self):
        first = FakeBoard()
        route_operations_request(REQUEST, source=source(), board=first)
        body = first.created_kwargs["body"]
        command = json.loads(body)["command"]
        result = {
            "schema_version": "operations-result.v1",
            "command_id": command["command_id"],
            "idempotency_key": command["idempotency_key"],
            "source_profile": "swe",
            "caller": "swe",
            "request_id": REQUEST["request_id"],
            "integration": "github",
            "operation": "github.repository_access",
            "mode": "plan",
            "status": "ok",
            "result": {"repository": "sisyphus-org/swamp-ops", "accessible": True},
            "verified": True,
        }
        task = {
            "id": "t_12345678",
            "status": "done",
            "session_id": source().session_id,
            "idempotency_key": first.created_kwargs["idempotency_key"],
            "body": body,
            "result": json.dumps(result, sort_keys=True),
        }
        replay = route_operations_request(REQUEST, source=source(), board=FakeBoard(task))
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(replay["operation"], "github.repository_access")
        self.assertEqual(replay["result"], result["result"])

    def test_completed_replay_rejects_wrong_worker_contract(self):
        first = FakeBoard()
        route_operations_request(REQUEST, source=source(), board=first)
        envelope = json.loads(first.created_kwargs["body"])
        envelope["worker_contract"]["profile"] = "broker"
        task = {
            "id": "t_12345678",
            "status": "done",
            "session_id": source().session_id,
            "idempotency_key": first.created_kwargs["idempotency_key"],
            "body": json.dumps(envelope),
            "result": "{}",
        }
        with self.assertRaisesRegex(OperationsRouteError, "invalid envelope"):
            route_operations_request(REQUEST, source=source(), board=FakeBoard(task))

    def test_task_body_contains_no_caller_supplied_identity_field(self):
        command = {
            "schema_version": "operations-command.v1",
            "command_id": "56040553-7de4-4849-a16d-a2a0ea8b749a",
            "idempotency_key": "operations:v1:" + "a" * 32,
            "source_profile": "swe",
            "source_session_id": source().session_id,
            "caller": "swe",
            "request": REQUEST,
        }
        body = build_operations_task_body(command)
        self.assertNotIn("caller_profile", body)
        self.assertEqual(json.loads(body)["worker_contract"]["profile"], "operations-manager")


if __name__ == "__main__":
    unittest.main()
