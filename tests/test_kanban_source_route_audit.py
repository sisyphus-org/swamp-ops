"""Regression tests for the SIS-60 source-route audit."""

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "kanban_source_route_audit.py"
SOURCE_SESSION = "20260828_120000_1234abcd"
SPEC = importlib.util.spec_from_file_location("kanban_source_route_audit", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import source-route audit: {SCRIPT}")
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


def create_db(
    path: Path,
    *,
    owner: str = "swe",
    cursor: int = 5,
    delivery_mode: str = "wake",
    thread_id: str | None = "",
    chat_type: str = "dm",
    user_id: str = "442308262",
    delivery_metadata: dict[str, object] | None = None,
    session_id: str | None = SOURCE_SESSION,
) -> None:
    """Create the smallest task/route/event database needed by the audit."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE kanban_notify_subs (
          task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
          user_id TEXT, chat_type TEXT, notifier_profile TEXT, delivery_mode TEXT,
          delivery_metadata TEXT, last_event_id INTEGER
        );
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT
        );
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, session_id TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO kanban_notify_subs VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "t_1234abcd",
            "telegram",
            "442308262",
            thread_id,
            user_id,
            chat_type,
            owner,
            delivery_mode,
            json.dumps(delivery_metadata) if delivery_metadata is not None else None,
            cursor,
        ),
    )
    conn.execute("INSERT INTO task_events VALUES (5, ?, 'claimed')", ("t_1234abcd",))
    conn.execute("INSERT INTO tasks VALUES (?, ?)", ("t_1234abcd", session_id))
    conn.commit()
    conn.close()


def run_audit(path: Path, **overrides: str):
    """Call the audit with the healthy source identity plus explicit overrides."""
    values = {
        "task_id": "t_1234abcd",
        "source_profile": "swe",
        "chat_id": "442308262",
        "user_id": "442308262",
        "source_session_id": SOURCE_SESSION,
    }
    values.update(overrides)
    return audit.audit_route(path, **values)


class RouteAuditTests(unittest.TestCase):
    """Verify the supported root-DM wake-only source route."""

    def test_exact_source_owned_root_dm_wake_route_passes(self):
        """A source-profile DM with exact session and wake-only delivery is healthy."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kanban.db"
            create_db(path)
            report = run_audit(path)
        self.assertEqual(report["result"], "pass")
        self.assertTrue(report["readOnly"])
        self.assertEqual(report["source_session_id"], SOURCE_SESSION)
        self.assertEqual(report["route"]["delivery_mode"], "wake")
        self.assertEqual(report["route"]["chat_type"], "dm")
        self.assertIsNone(report["route"]["thread_id"])
        self.assertIsNone(report["route"]["delivery_metadata"])
        self.assertEqual(report["pending_terminal_events"], [])

    def test_model_tool_root_dm_chat_type_metadata_passes(self):
        """The shipped model-tool path may persist only the DM chat type."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kanban.db"
            create_db(path, delivery_metadata={"chat_type": "dm"})
            report = run_audit(path)
        self.assertEqual(report["result"], "pass")
        self.assertEqual(report["route"]["delivery_metadata"], {"chat_type": "dm"})

    def test_broker_cannot_own_source_delivery(self):
        """The headless broker can never be the Telegram notifier."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kanban.db"
            create_db(path, owner="broker")
            with self.assertRaisesRegex(audit.AuditError, "broker"):
                run_audit(path, source_profile="broker")

    def test_topic_or_passive_route_reports_drift(self):
        """Task topics and passive Kanban pings are outside the chosen UX."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kanban.db"
            create_db(
                path,
                delivery_mode="notify+wake",
                thread_id="447017",
                chat_type="thread",
                delivery_metadata={
                    "thread_id": "447017",
                    "telegram_dm_topic_created_for_send": True,
                },
            )
            report = run_audit(path)
        self.assertEqual(report["result"], "drift")
        self.assertEqual(
            set(report["mismatches"]),
            {"thread_id", "chat_type", "delivery_mode", "delivery_metadata"},
        )

    def test_pending_terminal_event_makes_exact_route_drift_and_cli_nonzero(self):
        """An exact route cannot pass before its terminal event is consumed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kanban.db"
            create_db(path, cursor=5)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO task_events VALUES (6, ?, 'completed')",
                ("t_1234abcd",),
            )
            conn.commit()
            conn.close()
            report = run_audit(path)
            self.assertEqual(report["result"], "drift")
            self.assertEqual(report["mismatches"], {})
            self.assertEqual(report["pending_terminal_events"], ["completed"])

            stdout = io.StringIO()
            argv = [
                "kanban_source_route_audit.py",
                "--board",
                "sis60-routing",
                "--task-id",
                "t_1234abcd",
                "--source-profile",
                "swe",
                "--chat-id",
                "442308262",
                "--user-id",
                "442308262",
                "--source-session-id",
                SOURCE_SESSION,
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(audit, "board_db_path", return_value=path),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = audit.main()
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["result"], "drift")

    def test_wrong_user_id_reports_drift(self):
        """Wake delivery must rebuild the exact source participant session."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kanban.db"
            create_db(path, user_id="999")
            report = run_audit(path)
        self.assertEqual(report["result"], "drift")
        self.assertEqual(set(report["mismatches"]), {"user_id"})

    def test_missing_or_wrong_task_session_reports_drift(self):
        """A route cannot wake an arbitrary recent DM session in the profile."""
        for stored_session in (None, "20260828_120001_deadbeef"):
            with self.subTest(stored_session=stored_session):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "kanban.db"
                    create_db(path, session_id=stored_session)
                    report = run_audit(path)
                self.assertEqual(report["result"], "drift")
                self.assertEqual(set(report["mismatches"]), {"session_id"})

    def test_board_path_is_fixed_and_rejects_traversal(self):
        """Board slugs cannot escape the fixed Hermes Kanban root."""
        root = Path("/fixed/hermes")
        self.assertEqual(
            audit.board_db_path("sis60-routing", root=root),
            root / "kanban" / "boards" / "sis60-routing" / "kanban.db",
        )
        self.assertEqual(
            audit.board_db_path("default", root=root),
            root / "kanban.db",
        )
        for slug in ("../escape", "bad/slash", "UPPER", ""):
            with self.assertRaisesRegex(audit.AuditError, "board slug"):
                audit.board_db_path(slug, root=root)

    def test_workflow_bounds_every_interpolated_input(self):
        """The workflow exposes identifiers only, never arbitrary commands."""
        workflow = (
            Path(__file__).parents[1]
            / "workflows"
            / "workflow-kanban-source-route-audit.yaml"
        ).read_text()
        self.assertIn('pattern: "^[a-z0-9][a-z0-9_-]{0,63}$"', workflow)
        self.assertIn('pattern: "^t_[a-f0-9]{8}$"', workflow)
        self.assertIn('pattern: "^[a-z][a-z0-9-]{1,30}$"', workflow)
        self.assertGreaterEqual(workflow.count('pattern: "^[1-9][0-9]*$"'), 2)
        self.assertIn('pattern: "^[0-9]{8}_[0-9]{6}_[a-f0-9]{8}$"', workflow)
        self.assertIn("--user-id", workflow)
        self.assertIn("--source-session-id", workflow)
        self.assertNotIn("--thread-id", workflow)
        self.assertNotIn("--delivery-mode", workflow)


if __name__ == "__main__":
    unittest.main()
