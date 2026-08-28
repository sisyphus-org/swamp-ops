import importlib.util
import json
import sys
import unittest
from pathlib import Path


MODULE = Path(__file__).parents[1] / "plugins" / "linear_source_route" / "route.py"
SPEC = importlib.util.spec_from_file_location("linear_source_route", MODULE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import SWE Linear route: {MODULE}")
route = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = route
SPEC.loader.exec_module(route)


UUID_VALUES = [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
]


def uuid_factory():
    values = iter(UUID_VALUES)
    return lambda: next(values)


def source_context(**overrides):
    values = {
        "session_id": "20260828_120000_abcdef12",
        "profile": "swe",
        "platform": "telegram",
        "chat_id": "442308262",
        "user_id": "442308262",
        "chat_type": "dm",
        "thread_id": "448864",
    }
    values.update(overrides)
    return route.SourceContext(**values)


class FakeBoard:
    def __init__(self, *, existing=None, audit_result="pass"):
        self.existing = existing
        self.audit_result = audit_result
        self.calls = []
        self.created = None

    def find_task(self, idempotency_key):
        self.calls.append(("find", idempotency_key))
        return self.existing

    def create_task(self, **kwargs):
        self.calls.append(("create", kwargs))
        self.created = {
            "id": "t_1234abcd",
            "status": "triage",
            "session_id": kwargs["session_id"],
            "idempotency_key": kwargs["idempotency_key"],
        }
        return self.created

    def set_wake_route(self, task_id, source):
        self.calls.append(("route", task_id, source))

    def audit_route(self, task_id, source):
        self.calls.append(("audit", task_id, source))
        return {
            "result": self.audit_result,
            "task_id": task_id,
            "mismatches": {} if self.audit_result == "pass" else {"delivery_mode": {}},
            "pending_terminal_events": [],
        }

    def release(self, task_id, reason):
        self.calls.append(("release", task_id, reason))


class ParseTests(unittest.TestCase):
    def test_exact_comment_request_becomes_bounded_command(self):
        parsed = route.parse_linear_request(
            "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
            uuid_factory=uuid_factory(),
        )

        command = parsed.command
        self.assertEqual(command["schema_version"], "linear-command.v1")
        self.assertEqual(command["source_profile"], "swe")
        self.assertEqual(command["operation"], "add_comment")
        self.assertEqual(command["target"], {"type": "issue", "identifier": "SIS-61"})
        self.assertEqual(command["change"], {"body": "SIS-61 E2E proof A."})
        self.assertEqual(command["policy"], {"mode": "standard"})
        self.assertRegex(command["idempotency_key"], r"^linear:v1:[a-f0-9]{32}$")

    def test_exact_replay_keeps_idempotency_key(self):
        factory = uuid_factory()
        first = route.parse_linear_request(
            "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
            uuid_factory=factory,
        )
        second = route.parse_linear_request(
            "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
            uuid_factory=factory,
        )

        self.assertEqual(first.command["idempotency_key"], second.command["idempotency_key"])
        self.assertNotEqual(first.command["command_id"], second.command["command_id"])

    def test_source_profile_is_runtime_bound_not_swe_constant(self):
        parsed = route.parse_linear_request(
            "Добавь к SIS-61 комментарий: Ideas proof.",
            source_profile="ideas",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(parsed.command["source_profile"], "ideas")

    def test_structured_state_request_becomes_bounded_command(self):
        parsed = route.parse_linear_request(
            {
                "operation": "change_state",
                "identifier": "SIS-68",
                "state": "In Review",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(parsed.command["source_profile"], "default")
        self.assertEqual(parsed.command["operation"], "change_state")
        self.assertEqual(parsed.command["target"]["identifier"], "SIS-68")
        self.assertEqual(parsed.command["change"], {"state": "In Review"})

    def test_structured_create_request_becomes_bounded_team_command(self):
        parsed = route.parse_linear_request(
            {
                "operation": "create_issue",
                "title": "Universal routing tracer bullet",
                "description": "Bounded verification issue.",
                "parent_identifier": "SIS-56",
                "state": "Todo",
                "priority": "High",
            },
            source_profile="ideas",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(parsed.command["source_profile"], "ideas")
        self.assertEqual(parsed.command["operation"], "create_issue")
        self.assertEqual(parsed.command["target"], {"type": "team", "identifier": "SIS"})
        self.assertEqual(parsed.command["change"]["parent_identifier"], "SIS-56")
        self.assertEqual(parsed.command["change"]["priority"], "High")

    def test_malformed_missing_or_credential_shaped_input_fails_before_dispatch(self):
        cases = (
            "Добавь комментарий: no target",
            "Добавь к sis-61 комментарий: lower-case target",
            "Добавь к SIS-61 комментарий:",
            "Добавь к SIS-0 комментарий: invalid target",
            "Удали SIS-61",
            "Добавь к SIS-61 комментарий: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
            "Добавь к SIS-61 комментарий: Authorization: Bearer secret-shaped-value",
            "Добавь к SIS-61 комментарий: Authorization: Basic secret-shaped-value",
        )
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(route.RouteError):
                    route.parse_linear_request(text, uuid_factory=uuid_factory())


class SourceContextTests(unittest.TestCase):
    def test_accepts_every_user_facing_profile_and_rejects_special_roles(self):
        for profile in (
            "default",
            "ideas",
            "swe",
            "books",
            "crypto-analyst",
            "future-profile",
        ):
            with self.subTest(profile=profile):
                route.validate_source_context(source_context(profile=profile))
        for profile in ("broker", "project-manager", "UNKNOWN"):
            with self.subTest(profile=profile):
                with self.assertRaises(route.RouteError):
                    route.validate_source_context(source_context(profile=profile))

    def test_requires_exact_telegram_session_thread(self):
        route.validate_source_context(source_context())
        cases = (
            source_context(platform="discord"),
            source_context(chat_type="group"),
            source_context(thread_id=""),
            source_context(thread_id="not-numeric"),
            source_context(session_id=""),
            source_context(user_id="not-numeric"),
        )
        for context in cases:
            with self.subTest(context=context):
                with self.assertRaises(route.RouteError):
                    route.validate_source_context(context)


class DispatchTests(unittest.TestCase):
    def test_new_request_is_audited_before_promotion(self):
        board = FakeBoard()
        result = route.route_request(
            "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
            source=source_context(),
            board=board,
            uuid_factory=uuid_factory(),
        )

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["task_id"], "t_1234abcd")
        self.assertFalse(result["replayed"])
        self.assertEqual([call[0] for call in board.calls], ["find", "create", "route", "audit", "release"])
        created = board.calls[1][1]
        self.assertTrue(created["triage"])
        self.assertEqual(created["assignee"], "project-manager")
        self.assertEqual(created["skills"], ["project-manager-linear-worker"])
        self.assertEqual(created["session_id"], source_context().session_id)
        self.assertEqual(created["idempotency_key"], result["idempotency_key"])
        body = json.loads(created["body"])
        self.assertEqual(body["schema_version"], "linear-kanban-task.v1")
        self.assertEqual(body["command"]["target"]["identifier"], "SIS-61")
        self.assertEqual(body["worker_contract"]["tool"], "pm_linear_execute")

    def test_new_request_carries_exact_source_profile(self):
        board = FakeBoard()
        source = source_context(profile="books")
        route.route_request(
            "Добавь к SIS-61 комментарий: Books proof.",
            source=source,
            board=board,
            uuid_factory=uuid_factory(),
        )
        body = json.loads(board.calls[1][1]["body"])
        self.assertEqual(body["command"]["source_profile"], "books")

    def test_route_drift_leaves_task_in_triage(self):
        board = FakeBoard(audit_result="drift")
        with self.assertRaisesRegex(route.RouteError, "route audit failed"):
            route.route_request(
                "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
                source=source_context(),
                board=board,
                uuid_factory=uuid_factory(),
            )
        self.assertEqual([call[0] for call in board.calls], ["find", "create", "route", "audit"])

    def test_completed_replay_is_verified_noop_without_new_task_or_route_write(self):
        existing = {
            "id": "t_1234abcd",
            "status": "done",
            "session_id": source_context().session_id,
            "result": json.dumps(
                {
                    "schema_version": "linear-result.v1",
                    "result": "applied",
                    "verified": True,
                    "target": {
                        "identifier": "SIS-61",
                        "url": "https://linear.app/example/SIS-61",
                    },
                }
            ),
        }
        board = FakeBoard(existing=existing)
        result = route.route_request(
            "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
            source=source_context(),
            board=board,
            uuid_factory=uuid_factory(),
        )

        self.assertEqual(result["status"], "verified_no_op")
        self.assertTrue(result["replayed"])
        self.assertTrue(result["linear_result"]["verified"])
        self.assertEqual([call[0] for call in board.calls], ["find"])

    def test_existing_task_from_other_source_session_fails_closed(self):
        board = FakeBoard(
            existing={
                "id": "t_1234abcd",
                "status": "done",
                "session_id": "20260828_115959_deadbeef",
                "result": None,
            }
        )
        with self.assertRaisesRegex(route.RouteError, "source session"):
            route.route_request(
                "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
                source=source_context(),
                board=board,
                uuid_factory=uuid_factory(),
            )


if __name__ == "__main__":
    unittest.main()
