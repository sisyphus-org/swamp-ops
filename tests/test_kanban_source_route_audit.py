import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "kanban_source_route_audit.py"
SPEC = importlib.util.spec_from_file_location("kanban_source_route_audit", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import source-route audit: {SCRIPT}")
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


def create_db(path: Path, *, owner="swe", cursor=5):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE kanban_notify_subs (
          task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
          chat_type TEXT, notifier_profile TEXT, delivery_mode TEXT,
          delivery_metadata TEXT, last_event_id INTEGER
        );
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO kanban_notify_subs VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "t_1234abcd",
            "telegram",
            "442308262",
            "447017",
            "thread",
            owner,
            "notify+wake",
            json.dumps(
                {
                    "thread_id": "447017",
                    "telegram_dm_topic_created_for_send": True,
                }
            ),
            cursor,
        ),
    )
    conn.execute("INSERT INTO task_events VALUES (5, ?, 'claimed')", ("t_1234abcd",))
    conn.commit()
    conn.close()


class RouteAuditTests(unittest.TestCase):
    def test_exact_source_owned_thread_route_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kanban.db"
            create_db(path)
            report = audit.audit_route(
                path,
                task_id="t_1234abcd",
                source_profile="swe",
                chat_id="442308262",
                thread_id="447017",
            )
        self.assertEqual(report["result"], "pass")
        self.assertTrue(report["readOnly"])
        self.assertEqual(report["route"]["delivery_mode"], "notify+wake")
        self.assertEqual(report["route"]["chat_type"], "thread")
        self.assertEqual(
            report["route"]["delivery_metadata"],
            {
                "thread_id": "447017",
                "telegram_dm_topic_created_for_send": True,
            },
        )
        self.assertEqual(report["pending_terminal_events"], [])

    def test_broker_cannot_own_source_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kanban.db"
            create_db(path, owner="broker")
            with self.assertRaisesRegex(audit.AuditError, "broker"):
                audit.audit_route(
                    path,
                    task_id="t_1234abcd",
                    source_profile="broker",
                    chat_id="442308262",
                    thread_id="447017",
                )

    def test_wrong_route_reports_drift_and_pending_terminal_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kanban.db"
            create_db(path, cursor=5)
            conn = sqlite3.connect(path)
            conn.execute(
                "UPDATE kanban_notify_subs SET delivery_mode = 'notify', chat_type = 'dm', delivery_metadata = '{\"direct_messages_topic_id\":\"447017\"}'"
            )
            conn.execute(
                "INSERT INTO task_events VALUES (6, ?, 'completed')",
                ("t_1234abcd",),
            )
            conn.commit()
            conn.close()
            report = audit.audit_route(
                path,
                task_id="t_1234abcd",
                source_profile="swe",
                chat_id="442308262",
                thread_id="447017",
            )
        self.assertEqual(report["result"], "drift")
        self.assertEqual(report["pending_terminal_events"], ["completed"])
        self.assertEqual(
            set(report["mismatches"]),
            {"chat_type", "delivery_mode", "delivery_metadata"},
        )

    def test_board_path_is_fixed_and_rejects_traversal(self):
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
        workflow = (
            Path(__file__).parents[1]
            / "workflows"
            / "workflow-kanban-source-route-audit.yaml"
        ).read_text()
        self.assertIn('pattern: "^[a-z0-9][a-z0-9_-]{0,63}$"', workflow)
        self.assertIn('pattern: "^t_[a-f0-9]{8}$"', workflow)
        self.assertIn('pattern: "^[a-z][a-z0-9-]{1,30}$"', workflow)
        self.assertIn('pattern: "^[1-9][0-9]*$"', workflow)
        self.assertNotIn("--delivery-mode", workflow)


if __name__ == "__main__":
    unittest.main()
