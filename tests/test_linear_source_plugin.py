import concurrent.futures
import contextlib
import json
import os
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
        elif operation == "create_issue_relation":
            target = {
                "type": "issue_relation",
                "identifier": command["target"]["identifier"],
                **command["change"],
            }
            after = {
                "identifier": command["target"]["identifier"],
                **command["change"],
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
                else dict(command["change"])
                if operation == "update_issue"
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
    def test_tool_schema_exposes_structured_comment_shape(self):
        parameters = LINEAR_SOURCE_REQUEST_SCHEMA["parameters"]
        self.assertIn("add_comment", parameters["properties"]["operation"]["enum"])
        self.assertEqual(
            parameters["properties"]["body"],
            {"type": "string", "minLength": 1, "maxLength": 4000},
        )
        self.assertIn(
            "Legacy-only", parameters["properties"]["request"]["description"]
        )
        self.assertIn(
            "use operation=add_comment", parameters["properties"]["request"]["description"]
        )
        jsonschema.validate(
            {
                "operation": "add_comment",
                "identifier": "SIS-70",
                "body": "Краткие выводы после чтения.",
            },
            parameters,
        )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {
                    "operation": "add_comment",
                    "identifier": "SIS-70",
                    "body": "Краткие выводы после чтения.",
                    "state": "Todo",
                },
                parameters,
            )
        for hybrid in (
            {
                "request": "Добавь к SIS-70 комментарий: replay",
                "body": "extra",
            },
            {
                "request": "Добавь к SIS-70 комментарий: replay",
                "operation": "add_comment",
                "body": "extra",
            },
        ):
            with self.subTest(hybrid=hybrid), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.validate(hybrid, parameters)

    def test_tool_schema_exposes_terminal_state_without_approval(self):
        schema = LINEAR_SOURCE_REQUEST_SCHEMA["parameters"]
        request = {
            "operation": "change_state",
            "identifier": "SIS-102",
            "state": "Done",
        }
        jsonschema.validate(request, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {**request, "approval": {
                    "workflow": "linear-destructive-owner-approval-attest",
                    "model": "linear-destructive-owner-approval-attest",
                    "run_id": "55555555-5555-4555-8555-555555555555",
                    "artifact_version": 7,
                    "checksum": "a" * 64,
                    "intent_hash": "b" * 64,
                    "before_state_hash": "c" * 64,
                    "expires_at": "2026-09-01T13:00:00Z",
                }},
                schema,
            )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({**request, "state": "Duplicate"}, schema)
        jsonschema.validate(
            {
                "operation": "create_issue_relation",
                "identifier": "SIS-102",
                "related_identifier": "SIS-77",
                "relation_type": "duplicate",
            },
            schema,
        )

    def test_tool_schema_exposes_exact_workspace_read_shapes(self):
        parameters = LINEAR_SOURCE_REQUEST_SCHEMA["parameters"]
        self.assertEqual(
            parameters["properties"]["entity_types"],
            {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": ["issues", "projects", "milestones", "initiatives"],
                },
            },
        )
        self.assertEqual(parameters["properties"]["include_archived"], {"type": "boolean"})
        for operation, required in (
            (
                "search_linear",
                ["operation", "query", "entity_types", "include_archived"],
            ),
            (
                "inventory_linear",
                ["operation", "entity_types", "include_archived"],
            ),
        ):
            branch = next(
                item
                for item in parameters["oneOf"]
                if item.get("properties", {}).get("operation", {}).get("const")
                == operation
            )
            self.assertEqual(branch["required"], required)
        serialized = json.dumps(parameters, sort_keys=True).lower()
        for forbidden in ("graphql", "url", "description_filter", "user", "email"):
            self.assertNotIn(forbidden, serialized)

    def test_source_plugin_contains_no_linear_credential_or_network_client(self):
        plugin_root = ROOT / "plugins" / "linear_source_route"
        source = "\n".join(path.read_text() for path in sorted(plugin_root.glob("*.py")))
        self.assertNotIn("LINEAR_TOKEN", source)
        self.assertNotIn("api.linear.app", source)
        self.assertNotIn("urllib", source)
        self.assertNotIn("requests.", source)

    def test_tool_schema_exposes_only_exact_bounded_issue_relation_shape(self):
        parameters = LINEAR_SOURCE_REQUEST_SCHEMA["parameters"]

        self.assertEqual(
            parameters["properties"]["related_identifier"],
            {"type": "string", "pattern": "^SIS-[1-9][0-9]*$"},
        )
        self.assertEqual(
            parameters["properties"]["relation_type"],
            {
                "type": "string",
                "enum": ["blocks", "blocked_by", "related", "duplicate"],
            },
        )
        branch = next(
            item
            for item in parameters["oneOf"]
            if item.get("properties", {}).get("operation", {}).get("const")
            == "create_issue_relation"
        )
        self.assertEqual(
            branch["required"],
            ["operation", "identifier", "related_identifier", "relation_type"],
        )
        self.assertNotIn("id", parameters["properties"])
        self.assertNotIn("graphql", parameters["properties"])

    def test_tool_schema_exposes_owner_approved_exact_relation_change_shapes(self):
        parameters = LINEAR_SOURCE_REQUEST_SCHEMA["parameters"]
        for field in (
            "old_related_identifier",
            "new_related_identifier",
        ):
            self.assertEqual(
                parameters["properties"][field],
                {"type": "string", "pattern": "^SIS-[1-9][0-9]*$"},
            )
        for field in ("old_relation_type", "new_relation_type"):
            self.assertEqual(
                parameters["properties"][field],
                {"type": "string", "enum": ["blocks", "blocked_by", "related"]},
            )
        expected = {
            "remove_issue_relation": [
                "operation",
                "identifier",
                "related_identifier",
                "relation_type",
                "approval",
            ],
            "replace_issue_relation": [
                "operation",
                "identifier",
                "old_related_identifier",
                "old_relation_type",
                "new_related_identifier",
                "new_relation_type",
                "approval",
            ],
        }
        for operation, required in expected.items():
            branch = next(
                item
                for item in parameters["oneOf"]
                if item.get("properties", {}).get("operation", {}).get("const")
                == operation
            )
            self.assertEqual(branch["required"], required)
        self.assertNotIn("relation_id", parameters["properties"])

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

    def test_tool_schema_exposes_only_non_destructive_initiative_shapes(self):
        parameters = LINEAR_SOURCE_REQUEST_SCHEMA["parameters"]
        operations = parameters["properties"]["operation"]["enum"]
        for operation in (
            "create_initiative",
            "update_initiative",
            "link_project_to_initiative",
        ):
            self.assertIn(operation, operations)
            branch = next(
                item
                for item in parameters["oneOf"]
                if item.get("properties", {}).get("operation", {}).get("const")
                == operation
            )
            self.assertNotIn("id", branch.get("required", []))
        self.assertEqual(
            parameters["properties"]["initiative"],
            {"type": "string", "minLength": 1, "maxLength": 200},
        )
        serialized = json.dumps(parameters, sort_keys=True).lower()
        for forbidden in (
            "unlink",
            "archive_initiative",
            "delete_initiative",
            "parent_initiative",
            "initiative_status",
            "initiative_owner",
            "initiative_labels",
            "search_initiative",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_tool_schema_exposes_only_bounded_project_management_shapes(self):
        parameters = LINEAR_SOURCE_REQUEST_SCHEMA["parameters"]
        for operation in ("create_project", "create_milestone", "update_project", "update_milestone"):
            self.assertIn(operation, parameters["properties"]["operation"]["enum"])
            branch = next(item for item in parameters["oneOf"] if item.get("properties", {}).get("operation", {}).get("const") == operation)
            self.assertNotIn("id", branch.get("required", []))
            self.assertNotIn("team", branch.get("required", []))
            if operation.endswith("milestone"):
                self.assertEqual(
                    branch["properties"]["project"],
                    {"type": "string", "minLength": 1, "maxLength": 200},
                )
        self.assertEqual(parameters["properties"]["target_date"]["oneOf"][1], {"type": "null"})
        serialized = json.dumps(
            [
                branch
                for branch in parameters["oneOf"]
                if branch.get("properties", {}).get("operation", {}).get("const")
                in {
                    "create_project",
                    "create_milestone",
                    "update_project",
                    "update_milestone",
                }
            ],
            sort_keys=True,
        )
        for forbidden in ("archive", "delete", "lead", "member", "approval"):
            self.assertNotIn(forbidden, serialized.lower())


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
                "body",
                "query",
                "entity_types",
                "include_archived",
                "items",
                "entity_type",
                "selector",
                "identifier",
                "related_identifier",
                "old_related_identifier",
                "new_related_identifier",
                "relation_type",
                "old_relation_type",
                "new_relation_type",
                "issue_number",
                "state",
                "title",
                "name",
                "new_name",
                "initiative",
                "description",
                "target_date",
                "description_transform",
                "parent_identifier",
                "priority",
                "assignee",
                "labels",
                "due_date",
                "estimate",
                "project",
                "milestone",
                "issue",
                "sub_issues",
                "approval",
            },
        )
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(
            parameters["properties"]["due_date"],
            {
                "oneOf": [
                    {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                    {"type": "null"},
                ]
            },
        )
        self.assertEqual(
            parameters["properties"]["estimate"],
            {
                "oneOf": [
                    {"type": "integer", "minimum": 0},
                    {"type": "null"},
                ]
            },
        )
        for field in ("project", "milestone"):
            variants = parameters["properties"][field]["oneOf"]
            self.assertEqual(
                variants[1:],
                [
                    {"type": "string", "minLength": 1, "maxLength": 200},
                    {"type": "null"},
                ],
            )
            self.assertNotIn("id", variants[0]["properties"])
        update_branch = next(
            branch
            for branch in parameters["oneOf"]
            if branch.get("properties", {}).get("operation", {}).get("const")
            == "update_issue"
        )
        self.assertIn(
            {"required": ["project", "milestone"]},
            update_branch["anyOf"],
        )
        self.assertIn(
            {"required": ["parent_identifier"]},
            update_branch["anyOf"],
        )
        self.assertEqual(
            update_branch["properties"]["parent_identifier"],
            {
                "oneOf": [
                    {"type": "string", "pattern": "^SIS-[1-9][0-9]*$"},
                    {"type": "null"},
                ]
            },
        )
        approval_schema = parameters["properties"]["approval"]
        self.assertFalse(approval_schema["additionalProperties"])
        self.assertEqual(
            set(approval_schema["required"]),
            {
                "workflow", "model", "run_id", "artifact_version", "checksum",
                "intent_hash", "before_state_hash", "expires_at",
            },
        )
        self.assertNotIn("policy", parameters["properties"])
        self.assertNotIn("approved", parameters["properties"])
        create_branch = next(
            branch
            for branch in parameters["oneOf"]
            if branch.get("properties", {}).get("operation", {}).get("const")
            == "create_issue"
        )
        self.assertEqual(
            create_branch["properties"]["parent_identifier"],
            {"type": "string", "pattern": "^SIS-[1-9][0-9]*$"},
        )
        self.assertEqual(len(parameters["oneOf"]), 24)
        self.assertEqual(
            parameters["properties"]["description_transform"]["enum"],
            ["remove_links"],
        )
        self.assertEqual(
            parameters["properties"]["operation"]["enum"],
            [
                "bulk_linear_operations",
                "add_comment",
                "change_state",
                "update_issue",
                "inventory_sub_issues",
                "update_sub_issues",
                "create_issue",
                "converge_hierarchy",
                "create_standalone_issue",
                "converge_issue_tree",
                "create_issue_relation",
                "remove_issue_relation",
                "replace_issue_relation",
                "create_project",
                "create_milestone",
                "update_project",
                "update_milestone",
                "create_initiative",
                "update_initiative",
                "link_project_to_initiative",
                "search_linear",
                "inventory_linear",
                "archive_linear_entity",
                "delete_linear_entity",
            ],
        )
        self.assertEqual(
            [branch.get("properties", {}).get("operation", {}).get("const") for branch in parameters["oneOf"]],
            [
                None,
                "add_comment",
                "bulk_linear_operations",
                "search_linear",
                "inventory_linear",
                None,
                "change_state",
                "update_issue",
                "inventory_sub_issues",
                "create_issue_relation",
                "remove_issue_relation",
                "replace_issue_relation",
                "update_sub_issues",
                "create_issue",
                "converge_hierarchy",
                "create_standalone_issue",
                "converge_issue_tree",
                "create_project",
                "create_milestone",
                "update_project",
                "update_milestone",
                "create_initiative",
                "update_initiative",
                "link_project_to_initiative",
            ],
        )
        for branch in (
            item
            for item in parameters["oneOf"]
            if item.get("properties", {}).get("operation", {}).get("const")
            in {"create_standalone_issue", "converge_issue_tree"}
        ):
            self.assertEqual(
                branch["properties"]["issue"]["required"],
                ["title", "description", "state", "priority"],
            )
            for scope in ("project", "milestone"):
                self.assertEqual(
                    branch["properties"][scope],
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 200,
                            },
                            "description": {"type": "string", "maxLength": 10000},
                        },
                        "required": ["name"],
                    },
                )
        entity_schema = parameters["properties"]["issue"]
        self.assertNotIn("id", entity_schema["properties"])
        self.assertNotIn("id", entity_schema["required"])

    def test_handler_queues_workspace_reads_through_pm_task_lane(self):
        session_values = {
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449233",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        for request in (
            {
                "operation": "search_linear",
                "query": "Straße",
                "entity_types": ["issues", "projects"],
                "include_archived": False,
            },
            {
                "operation": "inventory_linear",
                "entity_types": ["milestones", "initiatives"],
                "include_archived": True,
            },
        ):
            fake_board = mock.Mock()

            def create_task(_delivery_key, **kwargs):
                envelope = json.loads(kwargs["body"])
                self.assertEqual(envelope["command"]["operation"], request["operation"])
                return (
                    {
                        "id": "t_1234abcd",
                        "status": "triage",
                        "session_id": kwargs["session_id"],
                        "idempotency_key": kwargs["idempotency_key"],
                    },
                    True,
                )

            fake_board.get_or_create_task.side_effect = create_task
            fake_board.audit_route.return_value = {"result": "pass"}
            result = json.loads(
                handle_linear_source_request(
                    request,
                    session_id="20260828_120000_abcdef12",
                    board_factory=lambda **_kwargs: fake_board,
                    session_getter=lambda name, default="": session_values.get(
                        name, default
                    ),
                    runtime_profile_getter=lambda: "default",
                )
            )
            self.assertEqual(result, {"status": "queued"})

    def test_public_result_projects_workspace_reads_without_sensitive_fields(self):
        after = {
            "query": "STRASSE",
            "entity_types": ["issues", "projects"],
            "include_archived": False,
            "counts": {"issues": 1, "projects": 1},
            "scanned_counts": {"issues": 4, "projects": 2},
            "entities": {
                "issues": [
                    {
                        "type": "issue",
                        "identifier": "SIS-9",
                        "title": "Straße rollout",
                        "state": "In Progress",
                        "team": "SIS",
                        "parent_identifier": "SIS-1",
                        "project": "Hermes",
                        "milestone": "Read lane",
                        "archived": False,
                    }
                ],
                "projects": [
                    {
                        "type": "project",
                        "name": "Straße Program",
                        "team_keys": ["SIS"],
                        "archived": False,
                    }
                ],
            },
        }
        result = _public_result(
            {
                "status": "verified_no_op",
                "linear_result": {
                    "verified": True,
                    "result": "read",
                    "operation": "search_linear",
                    "target": {"type": "workspace", "identifier": "current"},
                    "after": after,
                },
            }
        )
        self.assertEqual(
            result,
            {
                "status": "completed",
                "changed": False,
                "target": {"type": "workspace", "identifier": "current"},
                "context": {
                    key: value for key, value in after.items() if key != "query"
                },
            },
        )
        serialized = json.dumps(result, ensure_ascii=False).lower()
        for forbidden in (
            "description",
            "https://",
            "internal",
            "email",
            "user",
            "task_id",
            "run_id",
            "command_id",
            "query",
        ):
            self.assertNotIn(forbidden, serialized)

        tampered = json.loads(json.dumps(after, ensure_ascii=False))
        tampered["entities"]["issues"][0]["description"] = "must not be ignored"
        with self.assertRaises(RouteError):
            _public_result(
                {
                    "status": "verified_no_op",
                    "linear_result": {
                        "verified": True,
                        "result": "read",
                        "operation": "search_linear",
                        "target": {"type": "workspace", "identifier": "current"},
                        "after": tampered,
                    },
                }
            )

        tampered = json.loads(json.dumps(after, ensure_ascii=False))
        tampered["entity_types"] = [["issues"]]
        with self.assertRaises(RouteError):
            _public_result(
                {
                    "status": "verified_no_op",
                    "linear_result": {
                        "verified": True,
                        "result": "read",
                        "operation": "search_linear",
                        "target": {"type": "workspace", "identifier": "current"},
                        "after": tampered,
                    },
                }
            )

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

    def test_public_result_exposes_issue_relation_identifiers_and_type_only(self):
        result = _public_result(
            {
                "status": "verified_no_op",
                "linear_result": {
                    "verified": True,
                    "result": "applied",
                    "operation": "create_issue_relation",
                    "target": {
                        "type": "issue_relation",
                        "identifier": "SIS-59",
                        "related_identifier": "SIS-56",
                        "relation_type": "blocked_by",
                    },
                    "after": {
                        "identifier": "SIS-59",
                        "related_identifier": "SIS-56",
                        "relation_type": "blocked_by",
                        "id": "must-not-leak",
                    },
                },
            }
        )

        self.assertEqual(
            result,
            {
                "status": "completed",
                "changed": True,
                "target": {
                    "type": "issue_relation",
                    "identifier": "SIS-59",
                    "related_identifier": "SIS-56",
                    "relation_type": "blocked_by",
                },
            },
        )
        self.assertNotIn("must-not-leak", json.dumps(result))

    def test_public_result_exposes_duplicate_relation_without_internal_ids(self):
        result = _public_result(
            {
                "status": "verified_no_op",
                "linear_result": {
                    "verified": True,
                    "result": "applied",
                    "operation": "create_issue_relation",
                    "target": {
                        "type": "issue_relation",
                        "identifier": "SIS-102",
                        "related_identifier": "SIS-77",
                        "relation_type": "duplicate",
                    },
                    "after": {
                        "identifier": "SIS-102",
                        "related_identifier": "SIS-77",
                        "relation_type": "duplicate",
                        "id": "must-not-leak",
                    },
                },
            }
        )
        self.assertEqual(result["target"]["relation_type"], "duplicate")
        self.assertNotIn("must-not-leak", json.dumps(result))

    def test_public_result_projects_relation_removal_and_replacement_without_raw_ids(self):
        cases = (
            (
                "remove_issue_relation",
                {
                    "type": "issue_relation",
                    "identifier": "SIS-59",
                    "related_identifier": "SIS-56",
                    "relation_type": "blocked_by",
                },
                {
                    "identifier": "SIS-59",
                    "related_identifier": "SIS-56",
                    "relation_type": "blocked_by",
                    "present": False,
                    "id": "must-not-leak-old",
                },
            ),
            (
                "replace_issue_relation",
                {
                    "type": "issue_relation_replacement",
                    "old": {
                        "identifier": "SIS-59",
                        "related_identifier": "SIS-56",
                        "relation_type": "blocked_by",
                    },
                    "new": {
                        "identifier": "SIS-59",
                        "related_identifier": "SIS-94",
                        "relation_type": "related",
                    },
                },
                {
                    "identifier": "SIS-59",
                    "related_identifier": "SIS-94",
                    "relation_type": "related",
                    "id": "must-not-leak-new",
                },
            ),
        )
        for operation, target, after in cases:
            with self.subTest(operation=operation):
                result = _public_result(
                    {
                        "status": "verified_no_op",
                        "linear_result": {
                            "verified": True,
                            "result": "applied",
                            "operation": operation,
                            "target": target,
                            "after": after,
                        },
                    }
                )
                self.assertEqual(result["target"], target)
                self.assertNotIn("must-not-leak", json.dumps(result))

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

    def test_public_result_exposes_only_exact_parent_identifier(self):
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
                        "parent_identifier": "SIS-68",
                        "parent_id": "must-not-leak",
                    },
                },
            }
        )
        self.assertEqual(result["target"]["parent_identifier"], "SIS-68")
        self.assertNotIn("parent_id", result["target"])
        with self.assertRaisesRegex(RouteError, "parent"):
            _public_result(
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
                        "after": {"parent_identifier": "sis-68"},
                    },
                }
            )

    def test_public_result_exposes_only_validated_due_date_and_estimate(self):
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
                        "due_date": "2026-09-30",
                        "estimate": 8,
                        "description": "must stay private",
                    },
                },
            }
        )
        self.assertEqual(result["target"]["due_date"], "2026-09-30")
        self.assertEqual(result["target"]["estimate"], 8)
        self.assertNotIn("description", result["target"])

    def test_public_result_exposes_project_and_milestone_names_without_ids(self):
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
                        "project": "Project Two",
                        "milestone": "Milestone Two",
                    },
                },
            }
        )
        self.assertEqual(result["target"]["project"], "Project Two")
        self.assertEqual(result["target"]["milestone"], "Milestone Two")
        self.assertNotIn("project-two", json.dumps(result))

        cleared = _public_result(
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
                    "after": {"project": None, "milestone": None},
                },
            }
        )
        self.assertIsNone(cleared["target"]["project"])
        self.assertIsNone(cleared["target"]["milestone"])

    def test_public_result_rejects_invalid_due_date_or_estimate(self):
        for after in (
            {"due_date": "2026-02-30"},
            {"estimate": -1},
            {"estimate": True},
        ):
            with self.subTest(after=after), self.assertRaises(RouteError):
                _public_result(
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
                            "after": after,
                        },
                    }
                )

    def test_handler_accepts_all_bounded_initiative_shapes(self):
        session_values = {
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449233",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        requests = (
            {
                "operation": "create_initiative",
                "name": "Personal operating system",
                "description": "Connected systems",
                "target_date": "2026-12-31",
            },
            {
                "operation": "update_initiative",
                "name": "Personal operating system",
                "new_name": "Personal systems",
                "target_date": None,
            },
            {
                "operation": "link_project_to_initiative",
                "project": "Hermes Experience",
                "initiative": "Personal operating system",
            },
        )
        for request in requests:
            with self.subTest(operation=request["operation"]):
                fake_board = mock.Mock()

                def create_task(_delivery_key, **kwargs):
                    return (
                        {
                            "id": "t_1234abcd",
                            "status": "triage",
                            "session_id": kwargs["session_id"],
                            "idempotency_key": kwargs["idempotency_key"],
                        },
                        True,
                    )

                fake_board.get_or_create_task.side_effect = create_task
                fake_board.audit_route.return_value = {"result": "pass"}
                result = json.loads(
                    handle_linear_source_request(
                        request,
                        session_id="20260828_120000_abcdef12",
                        board_factory=lambda **_kwargs: fake_board,
                        session_getter=lambda name, default="": session_values.get(
                            name, default
                        ),
                        runtime_profile_getter=lambda: "default",
                    )
                )
                self.assertEqual(result["status"], "queued")

    def test_handler_accepts_due_date_and_estimate_update_shape(self):
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
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449233",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {
                    "operation": "update_issue",
                    "identifier": "SIS-94",
                    "due_date": None,
                    "estimate": 8,
                },
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "default",
            )
        )
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["target"]["due_date"])
        self.assertEqual(result["target"]["estimate"], 8)

    def test_handler_accepts_explicit_null_parent_update(self):
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
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449233",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {
                    "operation": "update_issue",
                    "identifier": "SIS-94",
                    "parent_identifier": None,
                    "approval": {
                        "workflow": "linear-destructive-owner-approval-attest",
                        "model": "linear-destructive-owner-approval-attest",
                        "run_id": "55555555-5555-4555-8555-555555555555",
                        "artifact_version": 7,
                        "checksum": "a" * 64,
                        "intent_hash": "b" * 64,
                        "before_state_hash": "c" * 64,
                        "expires_at": "2026-09-01T13:00:00Z",
                    },
                },
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "default",
            )
        )
        self.assertEqual(result["status"], "completed")
        self.assertIn("parent_identifier", result["target"])
        self.assertIsNone(result["target"]["parent_identifier"])

    def test_handler_accepts_exact_project_milestone_move_shape(self):
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
            "HERMES_SESSION_PROFILE": "default",
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "442308262",
            "HERMES_SESSION_USER_ID": "442308262",
            "HERMES_SESSION_CHAT_TYPE": "dm",
            "HERMES_SESSION_THREAD_ID": "449233",
            "HERMES_SESSION_ID": "20260828_120000_abcdef12",
        }
        result = json.loads(
            handle_linear_source_request(
                {
                    "operation": "update_issue",
                    "identifier": "SIS-94",
                    "project": "Project Two",
                    "milestone": "Milestone Two",
                },
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "default",
            )
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["target"]["project"], "Project Two")
        self.assertEqual(result["target"]["milestone"], "Milestone Two")

    def test_tool_schema_rejects_conflicting_issue_targets_and_description_modes(self):
        invalid = (
            {
                "operation": "change_state",
                "identifier": "SIS-86",
                "issue_number": 86,
                "state": "Todo",
            },
            {
                "operation": "update_issue",
                "identifier": "SIS-86",
                "issue_number": 86,
                "state": "Todo",
            },
            {
                "operation": "update_issue",
                "issue_number": 86,
                "description": "literal",
                "description_transform": "remove_links",
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(payload, LINEAR_SOURCE_REQUEST_SCHEMA["parameters"])

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

    def test_handler_accepts_structured_comment_without_hidden_prose_grammar(self):
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
                        "identifier": "SIS-70",
                        "url": "https://linear.app/example/issue/SIS-70/fixture",
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

        result = json.loads(
            handle_linear_source_request(
                {
                    "operation": "add_comment",
                    "identifier": "SIS-70",
                    "body": "Краткие выводы после чтения.",
                },
                session_id="20260828_120000_abcdef12",
                board_factory=lambda **_kwargs: fake_board,
                session_getter=lambda name, default="": session_values.get(name, default),
                runtime_profile_getter=lambda: "books",
            )
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["target"]["identifier"], "SIS-70")
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

    def test_handler_accepts_structured_bounded_state_requests(self):
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
                "operation": "change_state",
                "identifier": "SIS-68",
                "state": "Done",
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

    def test_public_block_reason_preserves_exact_owner_approval_capability_blocker(self):
        reason = "owner approval required: clearing or replacing an issue parent"
        self.assertEqual(
            _public_result(
                {
                    "status": "blocked",
                    "operation": "update_issue",
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

    def test_initiative_public_projection_contains_names_and_dates_only(self):
        for operation, target, after, expected in (
            (
                "create_initiative",
                {"type": "initiative", "identifier": "Personal operating system"},
                {
                    "id": "initiative-secret",
                    "name": "Personal operating system",
                    "description": "private",
                    "target_date": "2026-12-31",
                },
                {
                    "type": "initiative",
                    "name": "Personal operating system",
                    "target_date": "2026-12-31",
                },
            ),
            (
                "update_initiative",
                {"type": "initiative", "identifier": "Personal systems"},
                {
                    "id": "initiative-secret",
                    "name": "Personal systems",
                    "description": "private",
                    "target_date": None,
                },
                {
                    "type": "initiative",
                    "name": "Personal systems",
                    "target_date": None,
                },
            ),
            (
                "link_project_to_initiative",
                {
                    "type": "initiative_project",
                    "initiative": "Personal operating system",
                    "project": "Hermes Experience",
                },
                {
                    "initiative": "Personal operating system",
                    "project": "Hermes Experience",
                    "id": "link-secret",
                },
                {
                    "type": "initiative_project",
                    "initiative": "Personal operating system",
                    "project": "Hermes Experience",
                },
            ),
        ):
            with self.subTest(operation=operation):
                result = _public_result(
                    {
                        "status": "verified_no_op",
                        "linear_result": {
                            "verified": True,
                            "result": "applied",
                            "operation": operation,
                            "target": target,
                            "after": after,
                        },
                    }
                )
                self.assertEqual(result["target"], expected)
                serialized = json.dumps(result)
                self.assertNotIn("secret", serialized)
                self.assertNotIn("description", serialized)

    def test_project_management_public_projection_contains_names_and_dates_only(self):
        for operation, after, expected in (
            ("create_project", {"id": "project-secret", "name": "Hermes Experience", "description": "secret", "target_date": "2026-12-31"}, {"type": "project", "name": "Hermes Experience", "target_date": "2026-12-31"}),
            ("update_milestone", {"id": "milestone-secret", "project": "Hermes Experience", "name": "Calendar", "description": "secret", "target_date": None}, {"type": "milestone", "project": "Hermes Experience", "name": "Calendar", "target_date": None}),
        ):
            result = _public_result({
                "status": "verified_no_op",
                "linear_result": {
                    "verified": True, "result": "applied", "operation": operation,
                    "target": {"type": "internal", "identifier": "secret-id"},
                    "after": after,
                },
            })
            self.assertEqual(result, {"status": "completed", "changed": True, "target": expected})
            serialized = json.dumps(result)
            self.assertNotIn("secret", serialized)
            self.assertNotIn("description", serialized)
            self.assertNotIn('"id"', serialized)

    def test_initiative_public_blocker_preserves_only_safe_exact_name_reason(self):
        safe = _public_result(
            {
                "status": "blocked",
                "operation": "update_initiative",
                "reason": (
                    "Linear command failed: exact Linear initiative not found: "
                    "Personal operating system"
                ),
            }
        )
        self.assertEqual(
            safe,
            {
                "status": "blocked",
                "message": (
                    "Не удалось выполнить: exact Linear initiative not found: "
                    "Personal operating system."
                ),
            },
        )
        hidden = _public_result(
            {
                "status": "blocked",
                "operation": "link_project_to_initiative",
                "reason": "Linear command failed: raw internal GraphQL diagnostics",
            }
        )
        self.assertNotIn("GraphQL", json.dumps(hidden))

    def test_project_management_public_blocker_preserves_safe_exact_name_reason(self):
        result = _public_result({
            "status": "blocked",
            "operation": "update_milestone",
            "reason": "Linear command failed: exact Linear project not found: Hermes Experience",
        })
        self.assertEqual(
            result,
            {
                "status": "blocked",
                "message": "Не удалось выполнить: exact Linear project not found: Hermes Experience.",
            },
        )


if __name__ == "__main__":
    unittest.main()
