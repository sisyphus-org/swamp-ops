import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.swe_linear_route import (  # noqa: E402
    SWE_LINEAR_REQUEST_SCHEMA,
    HermesKanbanBoard,
    handle_swe_linear_request,
)


class FakeTask:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeConnection:
    def __init__(self, kb):
        self.kb = kb

    def execute(self, query, params):
        self.kb.calls.append(("sql", " ".join(query.split()), params))
        key = params[0]
        task = self.kb.by_key.get(key)

        class Result:
            def fetchall(self_inner):
                return [{"id": task.id}] if task else []

        return Result()

    def close(self):
        self.kb.calls.append(("close",))


class FakeKanban:
    def __init__(self):
        self.calls = []
        self.by_key = {}
        self.by_id = {}

    def connect(self, *, board):
        self.calls.append(("connect", board))
        return FakeConnection(self)

    def get_task(self, conn, task_id):
        self.calls.append(("get", task_id))
        return self.by_id.get(task_id)

    def create_task(self, conn, **kwargs):
        self.calls.append(("create", kwargs))
        task = FakeTask(
            id="t_1234abcd",
            status="triage",
            session_id=kwargs["session_id"],
            idempotency_key=kwargs["idempotency_key"],
            result=None,
        )
        self.by_key[task.idempotency_key] = task
        self.by_id[task.id] = task
        return task.id

    def add_notify_sub(self, conn, **kwargs):
        self.calls.append(("notify", kwargs))

    def specify_triage_task(self, conn, task_id, **kwargs):
        self.calls.append(("specify", task_id, kwargs))
        self.by_id[task_id].status = "ready"
        return True

    def promote_task(self, conn, task_id, **kwargs):
        self.calls.append(("promote", task_id, kwargs))
        self.by_id[task_id].status = "ready"
        return True, None

    def kanban_db_path(self, board):
        self.calls.append(("db_path", board))
        return Path("/tmp/fake-kanban.db")


class AdapterTests(unittest.TestCase):
    def test_adapter_persists_exact_session_and_root_dm_wake_route(self):
        kb = FakeKanban()
        audits = []

        def audit(db_path, **kwargs):
            audits.append((db_path, kwargs))
            return {"result": "pass", "pending_terminal_events": [], "mismatches": {}}

        board = HermesKanbanBoard(board="default", kb=kb, audit_func=audit)
        created = board.create_task(
            title="Linear add_comment SIS-61",
            body="{}",
            assignee="project-manager",
            triage=True,
            idempotency_key="linear:v1:" + "a" * 32,
            session_id="20260828_120000_abcdef12",
            max_runtime_seconds=300,
        )
        self.assertEqual(created["session_id"], "20260828_120000_abcdef12")

        from plugins.swe_linear_route.route import SourceContext

        source = SourceContext(
            session_id="20260828_120000_abcdef12",
            profile="swe",
            platform="telegram",
            chat_id="442308262",
            user_id="442308262",
            chat_type="dm",
            thread_id="",
        )
        board.set_wake_route(created["id"], source)
        notify = next(call[1] for call in kb.calls if call[0] == "notify")
        self.assertEqual(notify["platform"], "telegram")
        self.assertEqual(notify["thread_id"], None)
        self.assertEqual(notify["notifier_profile"], "swe")
        self.assertEqual(notify["delivery_mode"], "wake")
        self.assertEqual(notify["delivery_metadata"], {"chat_type": "dm"})

        report = board.audit_route(created["id"], source)
        self.assertEqual(report["result"], "pass")
        self.assertEqual(audits[0][1]["source_session_id"], source.session_id)

    def test_adapter_refuses_failed_promotion(self):
        kb = FakeKanban()
        task = FakeTask(
            id="t_1234abcd",
            status="triage",
            session_id="20260828_120000_abcdef12",
            idempotency_key="linear:v1:" + "a" * 32,
            result=None,
        )
        kb.by_id[task.id] = task
        kb.specify_triage_task = mock.Mock(return_value=False)
        board = HermesKanbanBoard(
            board="default",
            kb=kb,
            audit_func=lambda *_args, **_kwargs: {"result": "pass"},
        )
        with self.assertRaisesRegex(RuntimeError, "triage release failed"):
            board.release("t_1234abcd", "verified")


class PluginTests(unittest.TestCase):
    def test_swe_plugin_has_no_linear_client_or_mutable_runtime_dependency(self):
        plugin_root = ROOT / "plugins" / "swe_linear_route"
        source = "\n".join(
            path.read_text()
            for path in sorted(plugin_root.glob("*.py"))
        )
        self.assertNotIn("LINEAR_TOKEN", source)
        self.assertNotIn("LinearClient", source)
        self.assertNotIn("swamp-ops-runtime", source)

    def test_tool_schema_accepts_only_original_request_text(self):
        parameters = SWE_LINEAR_REQUEST_SCHEMA["parameters"]
        self.assertEqual(set(parameters["properties"]), {"request"})
        self.assertEqual(parameters["required"], ["request"])
        self.assertFalse(parameters["additionalProperties"])

    def test_handler_uses_request_scoped_gateway_identity(self):
        fake_board = mock.Mock()
        fake_board.find_task.return_value = {
            "id": "t_1234abcd",
            "status": "done",
            "session_id": "20260828_120000_abcdef12",
            "result": json.dumps(
                {
                    "schema_version": "linear-result.v1",
                    "verified": True,
                    "result": "applied",
                    "target": {
                        "identifier": "SIS-61",
                        "url": "https://linear.app/example/SIS-61",
                    },
                }
            ),
        }
        session_values = {
            "HERMES_SESSION_PROFILE": "swe",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }

        result = json.loads(
            handle_swe_linear_request(
                {"request": "Добавь к SIS-61 комментарий: SIS-61 E2E proof A."},
                session_id="20260828_120000_abcdef12",
                board_factory=lambda: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
            )
        )

        self.assertEqual(result["status"], "verified_no_op")
        fake_board.find_task.assert_called_once()

    def test_handler_rejects_session_id_mismatch_before_board_access(self):
        fake_board = mock.Mock()
        session_values = {
            "HERMES_SESSION_PROFILE": "swe",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_swe_linear_request(
                {"request": "Добавь к SIS-61 комментарий: proof"},
                session_id="20260828_120001_deadbeef",
                board_factory=lambda: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
            )
        )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("session", result["error"])
        fake_board.find_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
