import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.linear_source_route import (  # noqa: E402
    CALENDAR_SOURCE_REQUEST_SCHEMA,
    HermesKanbanBoard,
    handle_calendar_source_request,
    register,
)
from plugins.linear_source_route.calendar_route import route_calendar_request  # noqa: E402
from plugins.linear_source_route.route import SourceContext  # noqa: E402


class Registry:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


class CalendarSourcePluginTests(unittest.TestCase):
    def test_plugin_registers_bounded_calendar_source_tool(self):
        registry = Registry()
        register(registry)
        self.assertEqual(set(registry.tools), {"linear_source_request", "calendar_source_request"})
        schema = CALENDAR_SOURCE_REQUEST_SCHEMA["parameters"]
        jsonschema.validate({"operation": "inventory", "window": "today"}, schema)
        jsonschema.validate(
            {
                "operation": "create",
                "block_key": "primary",
                "summary": "Review SIS-123",
                "start": "2026-09-07T10:00",
                "end": "2026-09-07T10:30",
                "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing",
                "details": "",
            },
            schema,
        )
        jsonschema.validate(
            {"operation": "approve", "approval_reference": "calendar-approval:v1:" + "a" * 64},
            schema,
        )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"operation": "events", "window": "today", "calendar_id": "other"}, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"operation": "events", "window": "today", "summary": "extra"}, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({
                "operation": "approve", "approval_reference": "calendar-approval:v1:" + "a" * 64,
                "window": "today",
            }, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({
                "operation": "create", "block_key": "primary", "summary": "Owner's review",
                "start": "2026-09-07T10:00", "end": "2026-09-07T10:30",
                "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing",
                "details": "",
            }, schema)

    def test_handler_routes_with_runtime_source_context_and_returns_only_public_state(self):
        board = mock.Mock()
        board.get_or_create_task.return_value = ({
            "id": "t_deadbeef",
            "status": "triage",
            "session_id": "20260904_120000_abcdef12",
            "idempotency_key": mock.ANY,
        }, True)

        def create(_key, **kwargs):
            return ({
                "id": "t_deadbeef",
                "status": "triage",
                "session_id": kwargs["session_id"],
                "idempotency_key": kwargs["idempotency_key"],
            }, True)

        board.get_or_create_task.side_effect = create
        board.audit_route.return_value = {"result": "pass"}
        session = {
            "HERMES_SESSION_ID": "20260904_120000_abcdef12",
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "448864",
        }
        result = json.loads(handle_calendar_source_request(
            {"operation": "freebusy", "window": "today"},
            session_id=session["HERMES_SESSION_ID"],
            runtime_profile_getter=lambda: "default",
            session_getter=lambda key, default="": session.get(key, default),
            board_factory=lambda **_kwargs: board,
        ))
        self.assertEqual(result, {"status": "queued"})

    def test_handler_converts_board_failures_to_truthful_safe_capability_error(self):
        board = mock.Mock()
        board.get_or_create_task.side_effect = sqlite3.OperationalError(
            "/Users/hermes/private/token.json PRIVATE_EVENT_TITLE"
        )
        session = {
            "HERMES_SESSION_ID": "20260904_120000_abcdef12",
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "448864",
        }
        result = handle_calendar_source_request(
            {"operation": "events", "window": "today"},
            session_id=session["HERMES_SESSION_ID"],
            runtime_profile_getter=lambda: "default",
            session_getter=lambda key, default="": session.get(key, default),
            board_factory=lambda **_kwargs: board,
        )
        payload = json.loads(result)
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("routing is unavailable", payload["message"])
        self.assertNotIn("PRIVATE_EVENT_TITLE", result)
        self.assertNotIn("token.json", result)

    def test_source_and_broker_code_have_no_google_credentials_or_calendar_client(self):
        roots = (ROOT / "plugins" / "linear_source_route", ROOT / "plugins" / "ops_broker")
        source = "\n".join(
            path.read_text()
            for root in roots
            for path in sorted(root.glob("*.py"))
        )
        for forbidden in ("GOOGLE_CLIENT_SECRET", "googleapiclient", "google.oauth2", "from google"):
            self.assertNotIn(forbidden, source)
    def test_literal_replay_uses_one_real_task_and_one_wake_subscription(self):
        from hermes_cli import kanban_db as kb
        source = SourceContext(
            session_id="20260904_120000_abcdef12", profile="default", platform="telegram",
            chat_id="442308262", user_id="442308262", chat_type="dm", thread_id="448864",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanban.db"
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_DB": str(db_path)}):
                kb.init_db(db_path=db_path)
                def dispatch():
                    board = HermesKanbanBoard(
                        source_profile="default", kb=kb,
                        audit_func=lambda *_args, **_kwargs: {"result": "pass"},
                    )
                    return route_calendar_request(
                        {"operation": "inventory", "window": "today"},
                        source=source, board=board,
                    )
                first = dispatch()
                second = dispatch()
                conn = kb.connect(db_path=db_path)
                try:
                    task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                    sub_count = conn.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0]
                finally:
                    conn.close()
        self.assertEqual(first, {"status": "queued"})
        self.assertEqual(second, {"status": "queued"})
        self.assertEqual(task_count, 1)
        self.assertEqual(sub_count, 1)

    def test_source_and_worker_runbook_records_protocol_and_pending_production_e2e(self):
        runbook = (ROOT / "docs" / "universal-calendar-routing-e2e.md").read_text()
        for marker in (
            "calendar-command.v1", "calendar-kanban-task.v1", "calendar-result.v1",
            "b46867c677ad1ae2aefb515b7cb6662c101f316c", "Production E2E: NOT COMPLETE",
        ):
            self.assertIn(marker, runbook)
        self.assertIn("calendar_source_request", runbook)
        self.assertIn("pa_calendar_execute", runbook)


if __name__ == "__main__":
    unittest.main()
