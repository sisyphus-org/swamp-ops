import concurrent.futures
import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.linear_source_route import (  # noqa: E402
    LINEAR_SOURCE_REQUEST_SCHEMA,
    HermesKanbanBoard,
    _default_runtime_profile_getter,
    _public_result,
    handle_linear_source_request,
)
from plugins.linear_source_route.route import (  # noqa: E402
    RouteError,
    SourceContext,
    route_request,
)


class FakeTask:
    def __init__(self, **values):
        self.__dict__.update(values)


def set_existing_task(board, task):
    def get_or_create(delivery_key, **kwargs):
        envelope = json.loads(kwargs["body"])
        command = envelope["command"]
        operation = command["operation"]
        if operation == "converge_hierarchy":
            target = {
                "type": "project",
                "identifier": command["change"]["project"]["name"],
            }
            after = {
                "project": {"id": "project-internal", "name": "health"},
                "milestone": {
                    "id": "milestone-internal",
                    "name": "Подолог",
                    "project_id": "project-internal",
                },
                "issue": {
                    "id": "issue-internal",
                    "identifier": "SIS-999",
                    "url": "https://linear.app/example/issue/SIS-999/fixture",
                    "title": "Сходить в Solomia и записаться",
                    "state": "Todo",
                    "project_id": "project-internal",
                    "milestone_id": "milestone-internal",
                },
            }
        elif operation in {"create_standalone_issue", "converge_issue_tree"}:
            target = {
                "type": "issue",
                "identifier": "SIS-999",
                "url": "https://linear.app/example/issue/SIS-999/fixture",
            }
            issue = {
                "id": "issue-internal",
                "identifier": target["identifier"],
                "url": target["url"],
                "title": command["change"]["issue"]["title"],
                "state": command["change"]["issue"]["state"],
            }
            after = {
                "project": {"id": "project-internal", "name": command["change"]["project"]["name"]},
                "milestone": {
                    "id": "milestone-internal",
                    "name": command["change"]["milestone"]["name"],
                },
                "issue": issue,
            }
            if operation == "converge_issue_tree":
                after["sub_issues"] = [
                    {
                        "id": f"child-{index}",
                        "identifier": f"SIS-{1000 + index}",
                        "url": (
                            "https://linear.app/example/issue/"
                            f"SIS-{1000 + index}/fixture"
                        ),
                        "title": child["title"],
                        "state": child["state"],
                    }
                    for index, child in enumerate(command["change"]["sub_issues"])
                ]
        elif operation == "create_issue":
            target = {
                "type": "issue",
                "identifier": "SIS-999",
                "url": "https://linear.app/example/issue/SIS-999/fixture",
            }
            after = {
                "identifier": target["identifier"],
                "url": target["url"],
                "title": command["change"]["title"],
                "state": command["change"]["state"],
            }
        else:
            target = {
                "type": command["target"]["type"],
                "identifier": command["target"]["identifier"],
                "url": (
                    "https://linear.app/example/issue/"
                    f"{command['target']['identifier']}/fixture"
                ),
            }
            after = (
                {"state": command["change"]["state"]}
                if operation == "change_state"
                else {}
            )
        result = {
            "schema_version": "linear-result.v2",
            "command_id": command["command_id"],
            "correlation_id": command["correlation_id"],
            "idempotency_key": command["idempotency_key"],
            "source_profile": command["source_profile"],
            "operation": operation,
            "mode": "apply",
            "target": target,
            "result": "applied",
            "before": {},
            "after": after,
            "plan": [],
            "no_op": False,
            "verified": True,
        }
        return {
            **task,
            "idempotency_key": delivery_key,
            "body": kwargs["body"],
            "result": json.dumps(result),
        }, False

    board.get_or_create_task.side_effect = get_or_create


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

    @contextlib.contextmanager
    def write_txn(self, conn):
        self.calls.append(("write_txn",))
        yield conn

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
    def test_adapter_reads_latest_persisted_block_reason(self):
        from hermes_cli import kanban_db as kb

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanban.db"
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_DB": str(db_path)}):
                kb.init_db(db_path=db_path)
                board = HermesKanbanBoard(
                    board="default", source_profile="default", kb=kb
                )
                delivery_key = "linear-delivery:v2:" + "b" * 32
                task, _created = board.get_or_create_task(
                    delivery_key,
                    title="Linear create_issue SIS",
                    body="{}",
                    assignee="project-manager",
                    triage=True,
                    idempotency_key=delivery_key,
                    session_id="20260828_120000_abcdef12",
                    max_runtime_seconds=300,
                )
                board.release(task["id"], "test route verified")
                conn = kb.connect(db_path=db_path)
                try:
                    self.assertTrue(
                        kb.block_task(
                            conn,
                            task["id"],
                            reason=(
                                "Linear command failed: create_issue bounded field "
                                "read-back verification failed"
                            ),
                            kind="capability",
                        )
                    )
                finally:
                    conn.close()

                self.assertEqual(
                    board.block_reason(task["id"]),
                    (
                        "Linear command failed: create_issue bounded field "
                        "read-back verification failed"
                    ),
                )

    def test_atomic_get_or_create_race_persists_one_task_and_one_wake_subscription(self):
        from hermes_cli import kanban_db as kb

        source = SourceContext(
            session_id="20260828_120000_abcdef12",
            profile="default",
            platform="telegram",
            chat_id="442308262",
            user_id="442308262",
            chat_type="dm",
            thread_id="448864",
        )
        request = "Добавь к SIS-61 комментарий: Atomic delivery proof."
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kanban.db"
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_DB": str(db_path)}):
                kb.init_db(db_path=db_path)

                def dispatch():
                    board = HermesKanbanBoard(
                        board="default", source_profile="default", kb=kb
                    )
                    return route_request(request, source=source, board=board)

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _index: dispatch(), range(2)))

                conn = kb.connect(db_path=db_path)
                try:
                    task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                    sub_count = conn.execute(
                        "SELECT COUNT(*) FROM kanban_notify_subs"
                    ).fetchone()[0]
                finally:
                    conn.close()

        self.assertEqual(task_count, 1)
        self.assertEqual(sub_count, 1)
        self.assertEqual(len({item["task_id"] for item in results}), 1)
        self.assertEqual(len({item["delivery_key"] for item in results}), 1)

    def test_adapter_persists_exact_session_thread_wake_route(self):
        kb = FakeKanban()
        audits = []

        def audit(db_path, **kwargs):
            audits.append((db_path, kwargs))
            return {"result": "pass", "pending_terminal_events": [], "mismatches": {}}

        board = HermesKanbanBoard(board="default", kb=kb, audit_func=audit)
        delivery_key = "linear-delivery:v2:" + "a" * 32
        created, was_created = board.get_or_create_task(
            delivery_key,
            title="Linear add_comment SIS-61",
            body="{}",
            assignee="project-manager",
            triage=True,
            idempotency_key=delivery_key,
            session_id="20260828_120000_abcdef12",
            max_runtime_seconds=300,
        )
        self.assertTrue(was_created)
        self.assertEqual(created["session_id"], "20260828_120000_abcdef12")

        from plugins.linear_source_route.route import SourceContext

        source = SourceContext(
            session_id="20260828_120000_abcdef12",
            profile="swe",
            platform="telegram",
            chat_id="442308262",
            user_id="442308262",
            chat_type="dm",
            thread_id="448864",
        )
        board.set_wake_route(created["id"], source)
        notify = next(call[1] for call in kb.calls if call[0] == "notify")
        self.assertEqual(notify["platform"], "telegram")
        self.assertEqual(notify["thread_id"], "448864")
        self.assertEqual(notify["notifier_profile"], "swe")
        self.assertEqual(notify["delivery_mode"], "wake")
        self.assertEqual(notify["delivery_metadata"], {"chat_type": "dm"})

        report = board.audit_route(created["id"], source)
        self.assertEqual(report["result"], "pass")
        self.assertEqual(audits[0][1]["source_session_id"], source.session_id)
        self.assertEqual(audits[0][1]["source_thread_id"], source.thread_id)

    def test_adapter_uses_exact_source_profile_for_creator_and_notifier(self):
        kb = FakeKanban()
        board = HermesKanbanBoard(
            board="default",
            source_profile="books",
            kb=kb,
            audit_func=lambda *_args, **_kwargs: {
                "result": "pass",
                "pending_terminal_events": [],
                "mismatches": {},
            },
        )
        delivery_key = "linear-delivery:v2:" + "a" * 32
        created, was_created = board.get_or_create_task(
            delivery_key,
            title="Linear add_comment SIS-61",
            body="{}",
            assignee="project-manager",
            triage=True,
            idempotency_key=delivery_key,
            session_id="20260828_120000_abcdef12",
            max_runtime_seconds=300,
        )
        self.assertTrue(was_created)
        create = next(call[1] for call in kb.calls if call[0] == "create")
        self.assertEqual(create["created_by"], "books")

        from plugins.linear_source_route.route import SourceContext

        source = SourceContext(
            session_id="20260828_120000_abcdef12",
            profile="books",
            platform="telegram",
            chat_id="442308262",
            user_id="442308262",
            chat_type="dm",
            thread_id="448864",
        )
        board.set_wake_route(created["id"], source)
        notify = next(call[1] for call in kb.calls if call[0] == "notify")
        self.assertEqual(notify["notifier_profile"], "books")

    def test_adapter_refuses_failed_promotion(self):
        kb = FakeKanban()
        task = FakeTask(
            id="t_1234abcd",
            status="triage",
            session_id="20260828_120000_abcdef12",
            idempotency_key="linear:v2:" + "a" * 32,
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
    def test_default_runtime_profile_comes_from_resolved_profile_home(self):
        profile_home = "/Users/hermes/.hermes/profiles/swe"
        with mock.patch.dict("os.environ", {"HERMES_HOME": profile_home}, clear=False):
            self.assertEqual(_default_runtime_profile_getter(), "swe")

    def test_swe_plugin_has_no_linear_client_or_mutable_runtime_dependency(self):
        plugin_root = ROOT / "plugins" / "linear_source_route"
        source = "\n".join(
            path.read_text()
            for path in sorted(plugin_root.glob("*.py"))
        )
        self.assertNotIn("LINEAR_TOKEN", source)
        self.assertNotIn("LinearClient", source)
        self.assertNotIn("swamp-ops-runtime", source)

    def test_tool_schema_accepts_all_bounded_linear_operation_shapes(self):
        parameters = LINEAR_SOURCE_REQUEST_SCHEMA["parameters"]
        self.assertEqual(
            set(parameters["properties"]),
            {
                "request",
                "operation",
                "identifier",
                "state",
                "title",
                "description",
                "parent_identifier",
                "priority",
                "assignee",
                "labels",
                "project",
                "milestone",
                "issue",
                "sub_issues",
            },
        )
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(len(parameters["oneOf"]), 9)
        self.assertEqual(
            parameters["properties"]["operation"]["enum"],
            [
                "change_state",
                "update_issue",
                "inventory_sub_issues",
                "update_sub_issues",
                "create_issue",
                "converge_hierarchy",
                "create_standalone_issue",
                "converge_issue_tree",
            ],
        )
        self.assertEqual(
            [branch.get("properties", {}).get("operation", {}).get("const") for branch in parameters["oneOf"]],
            [
                None,
                "change_state",
                "update_issue",
                "inventory_sub_issues",
                "update_sub_issues",
                "create_issue",
                "converge_hierarchy",
                "create_standalone_issue",
                "converge_issue_tree",
            ],
        )
        for branch in parameters["oneOf"][-2:]:
            self.assertEqual(
                branch["properties"]["issue"]["required"],
                ["title", "description", "state", "priority"],
            )
        for entity in ("project", "milestone", "issue"):
            entity_schema = parameters["properties"][entity]
            self.assertNotIn("id", entity_schema["properties"])
            self.assertNotIn("id", entity_schema["required"])

    def test_public_result_exposes_recursive_sub_issue_inventory_without_internal_ids(self):
        result = _public_result(
            {
                "status": "verified_no_op",
                "linear_result": {
                    "verified": True,
                    "result": "read",
                    "operation": "inventory_sub_issues",
                    "target": {
                        "type": "issue",
                        "identifier": "SIS-86",
                        "url": "https://linear.app/example/issue/SIS-86/parent",
                    },
                    "after": [
                        {
                            "identifier": "SIS-87",
                            "title": "Child",
                            "url": "https://linear.app/example/issue/SIS-87/child",
                            "state": "Todo",
                            "parent_identifier": "SIS-86",
                        },
                        {
                            "identifier": "SIS-88",
                            "title": "Grandchild",
                            "url": "https://linear.app/example/issue/SIS-88/grandchild",
                            "state": "Done",
                            "parent_identifier": "SIS-87",
                        },
                    ],
                },
            }
        )
        self.assertEqual(result["target"]["identifier"], "SIS-86")
        self.assertEqual(
            result["context"]["sub_issues"],
            [
                {
                    "type": "issue",
                    "identifier": "SIS-87",
                    "url": "https://linear.app/example/issue/SIS-87/child",
                    "title": "Child",
                    "state": "Todo",
                    "parent_identifier": "SIS-86",
                },
                {
                    "type": "issue",
                    "identifier": "SIS-88",
                    "url": "https://linear.app/example/issue/SIS-88/grandchild",
                    "title": "Grandchild",
                    "state": "Done",
                    "parent_identifier": "SIS-87",
                },
            ],
        )
        self.assertNotIn("child-", json.dumps(result))

    def test_public_result_exposes_verified_assignee_name(self):
        result = _public_result(
            {
                "status": "verified_no_op",
                "linear_result": {
                    "verified": True,
                    "result": "applied",
                    "operation": "update_issue",
                    "target": {
                        "type": "issue",
                        "identifier": "SIS-94",
                        "url": "https://linear.app/example/issue/SIS-94/fixture",
                    },
                    "after": {
                        "assignee": "Alexey Petrov",
                        "labels": ["area:linear", "priority:owner"],
                    },
                },
            }
        )
        self.assertEqual(result["target"]["assignee"], "Alexey Petrov")
        self.assertEqual(
            result["target"]["labels"],
            ["area:linear", "priority:owner"],
        )

    def test_handler_uses_request_scoped_gateway_identity(self):
        fake_board = mock.Mock()
        existing = {
            "id": "t_1234abcd",
            "status": "done",
            "session_id": "20260828_120000_abcdef12",
            "result": json.dumps(
                {
                    "schema_version": "linear-result.v2",
                    "verified": True,
                    "result": "applied",
                    "target": {
                        "identifier": "SIS-61",
                        "url": "https://linear.app/example/issue/SIS-61/fixture",
                    },
                }
            ),
        }
        set_existing_task(fake_board, existing)
        session_values = {
            "HERMES_SESSION_PROFILE": "",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "448864",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }

        result = json.loads(
            handle_linear_source_request(
                {"request": "Добавь к SIS-61 комментарий: SIS-61 E2E proof A."},
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "swe",
            )
        )

        self.assertEqual(
            result,
            {
                "status": "completed",
                "changed": True,
                "target": {
                    "type": "issue",
                    "identifier": "SIS-61",
                    "url": "https://linear.app/example/issue/SIS-61/fixture",
                },
            },
        )
        self.assertNotIn("task_id", json.dumps(result))
        self.assertNotIn("idempotency", json.dumps(result))
        self.assertNotIn("delivery_key", json.dumps(result))
        fake_board.get_or_create_task.assert_called_once()

    def test_handler_accepts_any_allowlisted_runtime_profile(self):
        fake_board = mock.Mock()
        existing = {
            "id": "t_1234abcd",
            "status": "done",
            "session_id": "20260828_120000_abcdef12",
            "result": json.dumps(
                {
                    "schema_version": "linear-result.v2",
                    "verified": True,
                    "result": "applied",
                    "target": {
                        "identifier": "SIS-61",
                        "url": "https://linear.app/example/issue/SIS-61/fixture",
                    },
                }
            ),
        }
        set_existing_task(fake_board, existing)
        session_values = {
            "HERMES_SESSION_PROFILE": "books",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "448864",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        captured = {}

        def board_factory(**kwargs):
            captured.update(kwargs)
            return fake_board

        result = json.loads(
            handle_linear_source_request(
                {"request": "Добавь к SIS-61 комментарий: Books proof."},
                session_id="20260828_120000_abcdef12",
                board_factory=board_factory,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "books",
            )
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(captured, {"source_profile": "books"})

    def test_handler_returns_verified_issue_tree_to_exact_source_session(self):
        fake_board = mock.Mock()
        set_existing_task(
            fake_board,
            {
                "id": "t_1234abcd",
                "status": "done",
                "session_id": "20260828_120000_abcdef12",
            },
        )
        session_values = {
            "HERMES_SESSION_PROFILE": "books",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "448864",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        args = {
            "operation": "converge_issue_tree",
            "project": {"name": "Книги", "description": "Reading project"},
            "milestone": {
                "name": "Английская литература",
                "description": "English literature",
            },
            "issue": {
                "title": "Уильям Шекспир — великие трагедии",
                "description": "Основная четвёрка",
                "state": "Todo",
                "priority": "Medium",
            },
            "sub_issues": [
                {
                    "title": title,
                    "description": f"Прочитать {title}",
                    "state": "Todo",
                    "priority": "Medium",
                }
                for title in ("Король Лир", "Макбет", "Гамлет", "Отелло")
            ],
        }
        result = json.loads(
            handle_linear_source_request(
                args,
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "books",
            )
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["target"]["identifier"], "SIS-999")
        self.assertEqual(result["context"]["project"], "Книги")
        self.assertEqual(len(result["context"]["sub_issues"]), 4)
        self.assertNotIn("task_id", json.dumps(result))

    def test_handler_accepts_structured_safe_state_request(self):
        fake_board = mock.Mock()
        existing = {
            "id": "t_1234abcd",
            "status": "done",
            "session_id": "20260828_120000_abcdef12",
            "result": json.dumps(
                {
                    "schema_version": "linear-result.v2",
                    "verified": True,
                    "result": "applied",
                    "target": {
                        "identifier": "SIS-68",
                        "url": "https://linear.app/example/issue/SIS-68/fixture",
                    },
                }
            ),
        }
        set_existing_task(fake_board, existing)
        session_values = {
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449233",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        requests = [
            {
                "operation": "change_state",
                "identifier": "SIS-68",
                "state": "In Review",
            },
            {
                "operation": "create_issue",
                "title": "Universal routing tracer bullet",
                "description": "Bounded verification issue.",
                "parent_identifier": "SIS-56",
                "state": "Todo",
                "priority": "High",
            },
            {
                "operation": "converge_hierarchy",
                "project": {
                    "name": "health",
                },
                "milestone": {
                    "name": "Подолог",
                },
                "issue": {
                    "title": "Сходить в Solomia и записаться",
                    "description": "https://solomia.in.ua",
                },
            },
        ]
        for request in requests:
            with self.subTest(operation=request["operation"]):
                result = json.loads(
                    handle_linear_source_request(
                        request,
                        session_id="20260828_120000_abcdef12",
                        board_factory=lambda **_kwargs: fake_board,
                        session_getter=lambda name, default="": session_values.get(name, default),
                        runtime_profile_getter=lambda: "default",
                    )
                )
                self.assertEqual(result["status"], "completed")
                serialized = json.dumps(result)
                for forbidden in (
                    "task_id",
                    "idempotency",
                    "delivery_key",
                    "command_id",
                    "correlation_id",
                    "project_id",
                    "milestone_id",
                    "issue-internal",
                ):
                    self.assertNotIn(forbidden, serialized)
                if request["operation"] == "converge_hierarchy":
                    self.assertEqual(
                        result,
                        {
                            "status": "completed",
                            "changed": True,
                            "target": {
                                "type": "issue",
                                "identifier": "SIS-999",
                                "url": "https://linear.app/example/issue/SIS-999/fixture",
                                "title": "Сходить в Solomia и записаться",
                                "state": "Todo",
                            },
                            "context": {
                                "project": "health",
                                "milestone": "Подолог",
                            },
                        },
                    )

    def test_public_completion_preserves_applied_outcome(self):
        result = _public_result(
            {
                "status": "verified_no_op",
                "linear_result": {
                    "verified": True,
                    "result": "applied",
                    "operation": "change_state",
                    "target": {
                        "type": "issue",
                        "identifier": "SIS-68",
                        "url": "https://linear.app/example/issue/SIS-68/fixture",
                    },
                    "after": {"state": "In Review"},
                },
            }
        )
        self.assertTrue(result["changed"])
        self.assertEqual(result["target"]["state"], "In Review")

    def test_public_hierarchy_completion_allows_omitted_optional_state(self):
        result = _public_result(
            {
                "status": "verified_no_op",
                "linear_result": {
                    "verified": True,
                    "result": "applied",
                    "operation": "converge_hierarchy",
                    "target": {"type": "project", "identifier": "health"},
                    "after": {
                        "project": {"name": "health"},
                        "milestone": {"name": "Подолог"},
                        "issue": {
                            "identifier": "SIS-999",
                            "url": "https://linear.app/example/issue/SIS-999/fixture",
                            "title": "Сходить в Solomia и записаться",
                        },
                    },
                },
            }
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["changed"])
        self.assertNotIn("state", result["target"])

    def test_public_completion_rejects_uuid_or_protocol_marker_in_text(self):
        for title in (
            "Internal 123e4567-e89b-42d3-a456-426614174000",
            "Internal linear-command.v2",
            "Internal linear-delivery:v2:deadbeef",
        ):
            with self.subTest(title=title):
                payload = {
                    "status": "verified_no_op",
                    "linear_result": {
                        "verified": True,
                        "result": "applied",
                        "operation": "create_issue",
                        "target": {
                            "type": "issue",
                            "identifier": "SIS-999",
                            "url": "https://linear.app/example/issue/SIS-999/fixture",
                        },
                        "after": {
                            "identifier": "SIS-999",
                            "url": "https://linear.app/example/issue/SIS-999/fixture",
                            "title": title,
                            "state": "Todo",
                        },
                    },
                }
                with self.assertRaises(RouteError):
                    _public_result(payload)

    def test_public_completion_rejects_injected_or_missing_target_facts(self):
        base = {
            "status": "verified_no_op",
            "linear_result": {
                "verified": True,
                "result": "no_op",
                "operation": "change_state",
                "target": {
                    "type": "issue",
                    "identifier": "SIS-68",
                    "url": "https://linear.app/example/issue/SIS-68/fixture",
                },
                "after": {"state": "In Review"},
            },
        }
        cases = []
        injected = json.loads(json.dumps(base))
        injected["linear_result"]["target"]["url"] += "\ntask_id=t_deadbeef"
        cases.append(injected)
        internal_identifier = json.loads(json.dumps(base))
        internal_identifier["linear_result"]["target"]["identifier"] = (
            "bff72327-9104-4f49-a314-6d27b9c2a9bd"
        )
        cases.append(internal_identifier)
        missing_state = json.loads(json.dumps(base))
        missing_state["linear_result"]["after"] = {}
        cases.append(missing_state)
        missing_hierarchy_issue = json.loads(json.dumps(base))
        missing_hierarchy_issue["linear_result"].update(
            {
                "operation": "converge_hierarchy",
                "target": {"type": "project", "identifier": "health"},
                "after": {
                    "project": {"name": "health"},
                    "milestone": {"name": "Подолог"},
                    "issue": {"identifier": "SIS-999", "state": "Todo"},
                },
            }
        )
        cases.append(missing_hierarchy_issue)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(RouteError):
                    _public_result(payload)

    def test_handler_hides_internal_route_error(self):
        fake_board = mock.Mock()
        fake_board.get_or_create_task.side_effect = RouteError(
            "delivery key mismatch for t_deadbeef"
        )
        session_values = {
            "HERMES_SESSION_PROFILE": "ideas",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449189",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {
                    "operation": "change_state",
                    "identifier": "SIS-68",
                    "state": "In Review",
                },
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "ideas",
            )
        )
        self.assertEqual(
            result,
            {
                "status": "rejected",
                "message": "Не удалось безопасно обработать запрос.",
            },
        )
        self.assertNotIn("delivery", json.dumps(result))
        self.assertNotIn("t_deadbeef", json.dumps(result))

    def test_handler_hides_internal_metadata_for_queued_request(self):
        fake_board = mock.Mock()

        def get_or_create(delivery_key, **kwargs):
            return {
                "id": "t_1234abcd",
                "status": "triage",
                "session_id": kwargs["session_id"],
                "idempotency_key": delivery_key,
            }, True

        fake_board.get_or_create_task.side_effect = get_or_create
        fake_board.audit_route.return_value = {
            "result": "pass",
            "task_id": "t_1234abcd",
            "mismatches": {},
            "pending_terminal_events": [],
        }
        session_values = {
            "HERMES_SESSION_PROFILE": "ideas",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449189",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {
                    "operation": "change_state",
                    "identifier": "SIS-68",
                    "state": "In Review",
                },
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "ideas",
            )
        )
        self.assertEqual(result, {"status": "queued"})
        self.assertNotIn("task", json.dumps(result))
        self.assertNotIn("idempotency", json.dumps(result))
        self.assertNotIn("delivery", json.dumps(result))

    def test_handler_hides_internal_metadata_for_blocked_request(self):
        fake_board = mock.Mock()

        def get_or_create(delivery_key, **kwargs):
            return {
                "id": "t_1234abcd",
                "status": "blocked",
                "session_id": kwargs["session_id"],
                "idempotency_key": delivery_key,
            }, False

        fake_board.get_or_create_task.side_effect = get_or_create
        fake_board.block_reason.return_value = (
            "Linear command failed: create_issue read-back mismatched fields: description"
        )
        session_values = {
            "HERMES_SESSION_PROFILE": "ideas",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449189",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {
                    "operation": "create_issue",
                    "title": "Bounded child",
                    "description": "Bounded description",
                    "parent_identifier": "SIS-68",
                    "state": "In Progress",
                    "priority": "High",
                },
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "ideas",
            )
        )
        self.assertEqual(
            result,
            {
                "status": "blocked",
                "message": (
                    "Не удалось выполнить: create_issue read-back mismatched fields: "
                    "description."
                ),
            },
        )

    def test_handler_hides_operation_mismatched_block_reason(self):
        fake_board = mock.Mock()

        def get_or_create(delivery_key, **kwargs):
            return {
                "id": "t_1234abcd",
                "status": "blocked",
                "session_id": kwargs["session_id"],
                "idempotency_key": delivery_key,
            }, False

        fake_board.get_or_create_task.side_effect = get_or_create
        fake_board.block_reason.return_value = (
            "Linear command failed: create_issue bounded field read-back verification failed"
        )
        session_values = {
            "HERMES_SESSION_PROFILE": "ideas",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449189",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {
                    "operation": "change_state",
                    "identifier": "SIS-68",
                    "state": "In Review",
                },
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "ideas",
            )
        )
        self.assertEqual(
            result,
            {
                "status": "blocked",
                "message": "Не удалось выполнить запрос: безопасная причина недоступна.",
            },
        )

    def test_public_block_reason_allowlist_rejects_backend_and_invented_claims(self):
        reasons = (
            "Linear API HTTP 500: backend trace /internal/service.py:42",
            "Linear GraphQL error: resolver failed",
            "create_issue requires parent SIS-999",
            "create_issue only supports a full new hierarchy",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertEqual(
                    _public_result(
                        {
                            "status": "blocked",
                            "operation": "create_issue",
                            "reason": f"Linear command failed: {reason}",
                        }
                    ),
                    {
                        "status": "blocked",
                        "message": (
                            "Не удалось выполнить запрос: безопасная причина недоступна."
                        ),
                    },
                )

    def test_public_block_reason_preserves_factual_name_fallback_scope_failure(self):
        reason = "project exact-name match conflicts with live scope or name"
        self.assertEqual(
            _public_result(
                {
                    "status": "blocked",
                    "operation": "converge_hierarchy",
                    "reason": f"Linear command failed: {reason}",
                }
            ),
            {"status": "blocked", "message": f"Не удалось выполнить: {reason}."},
        )

    def test_public_block_reason_preserves_only_allowlisted_mismatch_fields(self):
        cases = (
            ("create_issue", "create_issue read-back mismatched fields: description, priority"),
            ("converge_hierarchy", "converge_hierarchy read-back mismatched fields: state"),
            (
                "create_standalone_issue",
                "create_standalone_issue read-back mismatched fields: parent, milestone",
            ),
            (
                "converge_issue_tree",
                "converge_issue_tree read-back mismatched fields: id/title, team",
            ),
        )
        for operation, reason in cases:
            with self.subTest(operation=operation):
                self.assertEqual(
                    _public_result(
                        {
                            "status": "blocked",
                            "operation": operation,
                            "reason": f"Linear command failed: {reason}",
                        }
                    ),
                    {"status": "blocked", "message": f"Не удалось выполнить: {reason}."},
                )

        unsafe = "create_issue read-back mismatched fields: description, live=secret"
        self.assertEqual(
            _public_result(
                {
                    "status": "blocked",
                    "operation": "create_issue",
                    "reason": f"Linear command failed: {unsafe}",
                }
            )["message"],
            "Не удалось выполнить запрос: безопасная причина недоступна.",
        )

    def test_handler_hides_unsafe_block_reason_without_inventing_another_reason(self):
        fake_board = mock.Mock()

        def get_or_create(delivery_key, **kwargs):
            return {
                "id": "t_1234abcd",
                "status": "blocked",
                "session_id": kwargs["session_id"],
                "idempotency_key": delivery_key,
            }, False

        fake_board.get_or_create_task.side_effect = get_or_create
        credential_fixture = "lin_api_" + "A" * 32
        fake_board.block_reason.return_value = (
            f"Linear command failed: {credential_fixture}"
        )
        session_values = {
            "HERMES_SESSION_PROFILE": "ideas",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449189",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {
                    "operation": "change_state",
                    "identifier": "SIS-68",
                    "state": "In Review",
                },
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "ideas",
            )
        )
        self.assertEqual(
            result,
            {
                "status": "blocked",
                "message": "Не удалось выполнить запрос: безопасная причина недоступна.",
            },
        )
        self.assertNotIn(credential_fixture, json.dumps(result))

    def test_handler_rejects_session_id_mismatch_before_board_access(self):
        fake_board = mock.Mock()
        session_values = {
            "HERMES_SESSION_PROFILE": "swe",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "448864",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {"request": "Добавь к SIS-61 комментарий: proof"},
                session_id="20260828_120001_deadbeef",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "swe",
            )
        )
        self.assertEqual(
            result,
            {
                "status": "rejected",
                "message": "Не удалось безопасно обработать запрос.",
            },
        )
        fake_board.get_or_create_task.assert_not_called()

    def test_handler_rejects_contextual_profile_conflicting_with_runtime(self):
        fake_board = mock.Mock()
        session_values = {
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "448864",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {"request": "Добавь к SIS-61 комментарий: proof"},
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "swe",
            )
        )
        self.assertEqual(
            result,
            {
                "status": "rejected",
                "message": "Не удалось безопасно обработать запрос.",
            },
        )
        fake_board.get_or_create_task.assert_not_called()

    def test_handler_rejects_contextual_swe_without_runtime_binding(self):
        fake_board = mock.Mock()
        session_values = {
            "HERMES_SESSION_PROFILE": "swe",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "448864",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {"request": "Добавь к SIS-61 комментарий: proof"},
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "",
            )
        )
        self.assertEqual(
            result,
            {
                "status": "rejected",
                "message": "Не удалось безопасно обработать запрос.",
            },
        )
        fake_board.get_or_create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
