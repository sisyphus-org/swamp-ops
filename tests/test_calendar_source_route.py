import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.linear_source_route import calendar_route  # noqa: E402
from plugins.linear_source_route.route import SourceContext  # noqa: E402


class CalendarCommandTests(unittest.TestCase):
    def test_bounded_events_request_becomes_calendar_command_v1(self):
        parsed = calendar_route.parse_calendar_request(
            {"operation": "events", "window": "next-7-days"},
            source_profile="default",
            uuid_factory=lambda: "11111111-1111-4111-8111-111111111111",
        )
        command = parsed.command
        self.assertEqual(command["schema_version"], "calendar-command.v1")
        self.assertEqual(command["operation"], "events")
        self.assertEqual(command["request"], {"window": "next-7-days"})
        self.assertEqual(command["source_profile"], "default")
        self.assertRegex(command["idempotency_key"], r"^calendar:v1:[a-f0-9]{32}$")
        self.assertNotIn("google", json.dumps(command).lower())

    def test_write_request_is_exact_bounded_and_preserves_canonical_linear_url(self):
        request = {
            "operation": "create",
            "block_key": "focus",
            "summary": "Review SIS-123",
            "start": "2026-09-07T10:00",
            "end": "2026-09-07T10:30",
            "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/marshrutizirovat-calendar-komandy",
            "details": "Owner-visible detail",
        }
        command = calendar_route.parse_calendar_request(
            request,
            source_profile="books",
            uuid_factory=lambda: "11111111-1111-4111-8111-111111111111",
        ).command
        self.assertEqual(command["operation"], "plan_write")
        self.assertEqual(command["request"], request)
        self.assertEqual(command["source_profile"], "books")

    def test_standalone_write_without_linear_url_is_canonicalized_with_empty_link(self):
        request = {
            "operation": "create",
            "block_key": "lavina-rusanovka-2026-09-06",
            "summary": "Поехать посмотреть вещи: Lavina + Русановка",
            "start": "2026-09-06T10:00",
            "end": "2026-09-06T12:00",
            "details": "",
        }

        command = calendar_route.parse_calendar_request(
            request,
            source_profile="ideas",
            uuid_factory=lambda: "11111111-1111-4111-8111-111111111111",
        ).command

        self.assertEqual(command["operation"], "plan_write")
        self.assertEqual(command["request"], {**request, "linear_url": ""})
        self.assertEqual(command["source_profile"], "ideas")

    def test_write_request_rejects_non_positive_interval_before_queueing(self):
        for end in ("2026-09-07T10:00", "2026-09-07T09:59"):
            with self.subTest(end=end), self.assertRaisesRegex(
                calendar_route.CalendarRouteError, "end must be after start"
            ):
                calendar_route.parse_calendar_request(
                    {
                        "operation": "create", "block_key": "primary",
                        "summary": "Review SIS-123", "start": "2026-09-07T10:00",
                        "end": end,
                        "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing",
                        "details": "",
                    },
                    source_profile="default",
                )

    def test_unbounded_or_credential_shaped_requests_fail_closed(self):
        invalid = (
            {"operation": "events", "window": "custom"},
            {"operation": "events", "window": "today", "calendar_id": "other"},
            {
                "operation": "create",
                "block_key": "primary",
                "summary": "Authorization: Bearer secret-value",
                "start": "2026-09-07T10:00",
                "end": "2026-09-07T10:30",
                "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing",
                "details": "",
            },
            {"operation": "approve", "approval_reference": "not-opaque"},
            {
                "operation": "create", "block_key": "primary",
                "summary": "Owner's review", "start": "2026-09-07T10:00",
                "end": "2026-09-07T10:30",
                "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing",
                "details": "",
            },
            {
                "operation": "create", "block_key": "primary",
                "summary": "Owner review", "start": "2026-09-07T10:00",
                "end": "2026-09-07T10:30",
                "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing",
                "details": "line one\nline two",
            },
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(calendar_route.CalendarRouteError):
                calendar_route.parse_calendar_request(request, source_profile="default")


class FakeBoard:
    def __init__(self, existing=None, audit="pass"):
        self.existing = existing
        self.audit = audit
        self.calls = []

    def get_or_create_task(self, delivery_key, **kwargs):
        self.calls.append(("get_or_create", delivery_key, kwargs))
        if self.existing is not None:
            return self.existing, False
        return {
            "id": "t_deadbeef",
            "status": "triage",
            "session_id": kwargs["session_id"],
            "idempotency_key": delivery_key,
            "body": kwargs["body"],
            "result": None,
        }, True

    def set_wake_route(self, task_id, source):
        self.calls.append(("route", task_id, source))

    def audit_route(self, task_id, source):
        self.calls.append(("audit", task_id, source))
        return {"result": self.audit}

    def release(self, task_id, reason):
        self.calls.append(("release", task_id, reason))

    def block_reason(self, task_id):
        return "Calendar route unavailable"


def source_context(**overrides):
    values = {
        "session_id": "20260904_120000_abcdef12",
        "profile": "default",
        "platform": "telegram",
        "chat_id": "442308262",
        "user_id": "442308262",
        "chat_type": "dm",
        "thread_id": "448864",
    }
    values.update(overrides)
    return SourceContext(**values)


class CalendarRoutingTests(unittest.TestCase):
    def test_route_creates_exact_pa_task_then_audits_and_releases(self):
        board = FakeBoard()
        output = calendar_route.route_calendar_request(
            {"operation": "events", "window": "today"},
            source=source_context(),
            board=board,
            uuid_factory=lambda: "11111111-1111-4111-8111-111111111111",
        )
        self.assertEqual(output["status"], "queued")
        create = board.calls[0][2]
        self.assertEqual(create["assignee"], "personal-assistant")
        self.assertEqual(create["skills"], ["personal-assistant-calendar-worker"])
        envelope = json.loads(create["body"])
        self.assertEqual(envelope["schema_version"], "calendar-kanban-task.v1")
        self.assertEqual(envelope["worker_contract"]["tool"], "pa_calendar_execute")
        self.assertEqual(create["session_id"], source_context().session_id)
        self.assertEqual([call[0] for call in board.calls], ["get_or_create", "route", "audit", "release"])

    def test_completed_plan_replay_returns_exact_preview_and_opaque_reference(self):
        source = source_context()
        request = {
            "operation": "create",
            "block_key": "primary",
            "summary": "Review SIS-123",
            "start": "2026-09-07T10:00",
            "end": "2026-09-07T10:30",
            "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing",
            "details": "",
        }
        command = calendar_route.parse_calendar_request(
            request,
            source_profile=source.profile,
            uuid_factory=lambda: "11111111-1111-4111-8111-111111111111",
        ).command
        envelope = calendar_route.build_calendar_task_body(command)
        preview = {
            "operation": "create",
            "block_key": "primary",
            "summary": "Review SIS-123",
            "details": "",
            "start": "2026-09-07T10:00:00+03:00",
            "end": "2026-09-07T10:30:00+03:00",
            "timezone": "Europe/Kyiv",
            "linear_url": request["linear_url"],
        }
        plan_reference = {
            "run_id": "22222222-2222-4222-8222-222222222222",
            "artifact_version": 7,
            "checksum": "a" * 64,
            "before_state_hash": "d" * 64,
        }
        approval_binding = {
            "command_id": command["command_id"],
            "idempotency_key": command["idempotency_key"],
            "plan": plan_reference,
            "session_id": source.session_id,
        }
        approval_reference = "calendar-approval:v1:" + hashlib.sha256(
            json.dumps(approval_binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result = {
            "schema_version": "calendar-result.v1",
            "command_id": command["command_id"],
            "idempotency_key": command["idempotency_key"],
            "source_profile": source.profile,
            "operation": "plan_write",
            "phase": "awaiting_approval",
            "outcome": "planned",
            "preview": preview,
            "approval_reference": approval_reference,
            "plan_reference": plan_reference,
            "verified": True,
        }
        board = FakeBoard(existing={
            "id": "t_deadbeef",
            "status": "done",
            "session_id": source.session_id,
            "idempotency_key": calendar_route.delivery_key(command["idempotency_key"], source),
            "body": envelope,
            "result": json.dumps(result),
        })
        output = calendar_route.route_calendar_request(request, source=source, board=board)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["preview"], preview)
        self.assertEqual(output["approval_reference"], result["approval_reference"])
        self.assertNotIn("task_id", output)
        public_text = json.dumps(output, sort_keys=True)
        for internal in ("checksum", "eventId", "run_id", "artifact_version"):
            self.assertNotIn(internal, public_text)

    def test_completed_standalone_apply_accepts_null_linear_issue(self):
        source = source_context()
        request = {"operation": "approve", "approval_reference": "calendar-approval:v1:" + "a" * 64}
        command = calendar_route.parse_calendar_request(
            request,
            source_profile=source.profile,
            uuid_factory=lambda: "11111111-1111-4111-8111-111111111111",
        ).command
        result = {
            "schema_version": "calendar-result.v1",
            "command_id": command["command_id"],
            "idempotency_key": command["idempotency_key"],
            "source_profile": source.profile,
            "operation": "approve_write",
            "phase": "completed",
            "outcome": "applied",
            "data": {
                "operation": "create",
                "status": "verified",
                "reused": False,
                "blockKey": "lavina-rusanovka-2026-09-06",
            },
            "verified": True,
        }
        board = FakeBoard(existing={
            "id": "t_deadbeef",
            "status": "done",
            "session_id": source.session_id,
            "idempotency_key": calendar_route.delivery_key(command["idempotency_key"], source),
            "body": calendar_route.build_calendar_task_body(command),
            "result": json.dumps(result),
        })

        output = calendar_route.route_calendar_request(request, source=source, board=board)

        self.assertEqual(output["status"], "completed")
        self.assertNotIn("linearIssue", output["data"])
        self.assertTrue(output["changed"])

    def test_completed_plan_replay_rejects_substituted_opaque_reference(self):
        source = source_context()
        request = {
            "operation": "create", "block_key": "primary", "summary": "Review SIS-123",
            "start": "2026-09-07T10:00", "end": "2026-09-07T10:30",
            "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing", "details": "",
        }
        command = calendar_route.parse_calendar_request(
            request, source_profile=source.profile,
            uuid_factory=lambda: "11111111-1111-4111-8111-111111111111",
        ).command
        plan_reference = {
            "run_id": "22222222-2222-4222-8222-222222222222",
            "artifact_version": 7,
            "checksum": "a" * 64,
            "before_state_hash": "d" * 64,
        }
        result = {
            "schema_version": "calendar-result.v1", "command_id": command["command_id"],
            "idempotency_key": command["idempotency_key"], "source_profile": source.profile,
            "operation": "plan_write", "phase": "awaiting_approval", "outcome": "planned",
            "preview": {
                "operation": "create", "block_key": "primary", "summary": "Review SIS-123",
                "details": "", "start": "2026-09-07T10:00:00+03:00",
                "end": "2026-09-07T10:30:00+03:00", "timezone": "Europe/Kyiv",
                "linear_url": request["linear_url"],
            },
            "approval_reference": "calendar-approval:v1:" + "f" * 64,
            "plan_reference": plan_reference, "verified": True,
        }
        key = calendar_route.delivery_key(command["idempotency_key"], source)
        board = FakeBoard(existing={
            "id": "t_deadbeef", "status": "done", "session_id": source.session_id,
            "idempotency_key": key, "body": calendar_route.build_calendar_task_body(command),
            "result": json.dumps(result),
        })
        with self.assertRaises(calendar_route.CalendarRouteError):
            calendar_route.route_calendar_request(request, source=source, board=board)

    def test_failed_route_audit_leaves_calendar_task_in_triage(self):
        board = FakeBoard(audit="fail")
        with self.assertRaisesRegex(calendar_route.CalendarRouteError, "route audit failed"):
            calendar_route.route_calendar_request(
                {"operation": "events", "window": "today"},
                source=source_context(), board=board,
            )
        self.assertEqual([call[0] for call in board.calls], ["get_or_create", "route", "audit"])

    def test_existing_triage_replay_repairs_route_and_releases(self):
        source = source_context()
        request = {"operation": "events", "window": "today"}
        command = calendar_route.parse_calendar_request(
            request, source_profile=source.profile,
            uuid_factory=lambda: "11111111-1111-4111-8111-111111111111",
        ).command
        key = calendar_route.delivery_key(command["idempotency_key"], source)
        board = FakeBoard(existing={
            "id": "t_deadbeef", "status": "triage", "session_id": source.session_id,
            "idempotency_key": key, "body": calendar_route.build_calendar_task_body(command),
            "result": None,
        })
        self.assertEqual(
            calendar_route.route_calendar_request(request, source=source, board=board),
            {"status": "queued"},
        )
        self.assertEqual([call[0] for call in board.calls], ["get_or_create", "route", "audit", "release"])

    def test_completed_read_rejects_unexpected_private_fields(self):
        source = source_context()
        request = {"operation": "events", "window": "today"}
        command = calendar_route.parse_calendar_request(
            request, source_profile=source.profile,
            uuid_factory=lambda: "11111111-1111-4111-8111-111111111111",
        ).command
        key = calendar_route.delivery_key(command["idempotency_key"], source)
        result = {
            "schema_version": "calendar-result.v1", "command_id": command["command_id"],
            "idempotency_key": command["idempotency_key"], "source_profile": source.profile,
            "operation": "events", "phase": "completed", "outcome": "read",
            "verified": True,
            "data": {
                "operation": "events", "status": "ok", "timezone": "Europe/Kyiv",
                "window": "today",
                "bounds": {
                    "start": "2026-09-04T00:00:00+03:00",
                    "end": "2026-09-05T00:00:00+03:00",
                    "summary": "PRIVATE_EVENT_TITLE",
                },
            },
        }
        board = FakeBoard(existing={
            "id": "t_deadbeef", "status": "done", "session_id": source.session_id,
            "idempotency_key": key, "body": calendar_route.build_calendar_task_body(command),
            "result": json.dumps(result),
        })
        with self.assertRaisesRegex(calendar_route.CalendarRouteError, "read completion"):
            calendar_route.route_calendar_request(request, source=source, board=board)

    def test_same_intent_different_session_has_a_distinct_delivery_key(self):
        request = {"operation": "freebusy", "window": "today"}
        first = FakeBoard()
        second = FakeBoard()
        calendar_route.route_calendar_request(request, source=source_context(), board=first)
        calendar_route.route_calendar_request(
            request,
            source=source_context(session_id="20260904_120001_deadbeef"),
            board=second,
        )
        self.assertNotEqual(first.calls[0][1], second.calls[0][1])


if __name__ == "__main__":
    unittest.main()
