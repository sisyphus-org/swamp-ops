import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


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


def approval_reference(**overrides):
    value = {
        "workflow": "linear-destructive-owner-approval-attest",
        "model": "linear-destructive-owner-approval-attest",
        "run_id": "55555555-5555-4555-8555-555555555555",
        "artifact_version": 7,
        "checksum": "a" * 64,
        "intent_hash": "b" * 64,
        "before_state_hash": "c" * 64,
        "expires_at": "2026-08-31T23:00:00Z",
    }
    value.update(overrides)
    return value


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

    def get_or_create_task(self, delivery_key, **kwargs):
        self.calls.append(("get_or_create", delivery_key, kwargs))
        if self.existing is not None:
            existing = dict(self.existing)
            existing.setdefault("idempotency_key", delivery_key)
            return existing, False
        self.created = {
            "id": f"t_{delivery_key.rsplit(':', 1)[-1][:8]}",
            "status": "triage",
            "session_id": kwargs["session_id"],
            "idempotency_key": kwargs["idempotency_key"],
        }
        return self.created, True

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
    def test_search_and_inventory_requests_become_workspace_read_commands(self):
        requests = (
            (
                {
                    "operation": "search_linear",
                    "query": "Straße",
                    "entity_types": ["issues", "projects"],
                    "include_archived": False,
                },
                {
                    "query": "Straße",
                    "entity_types": ["issues", "projects"],
                    "include_archived": False,
                },
            ),
            (
                {
                    "operation": "inventory_linear",
                    "entity_types": ["milestones", "initiatives"],
                    "include_archived": True,
                },
                {
                    "entity_types": ["milestones", "initiatives"],
                    "include_archived": True,
                },
            ),
        )
        for request, expected_change in requests:
            with self.subTest(operation=request["operation"]):
                parsed = route.parse_linear_request(
                    request,
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )
                self.assertEqual(parsed.command["operation"], request["operation"])
                self.assertEqual(
                    parsed.command["target"],
                    {"type": "workspace", "identifier": "current"},
                )
                self.assertEqual(parsed.command["change"], expected_change)

    def test_workspace_reads_require_exact_nonempty_bounded_inputs(self):
        invalid = (
            {"operation": "inventory_linear", "entity_types": [], "include_archived": False},
            {"operation": "inventory_linear", "entity_types": ["issues", "issues"], "include_archived": False},
            {"operation": "inventory_linear", "entity_types": [["issues"]], "include_archived": False},
            {"operation": "inventory_linear", "entity_types": [None], "include_archived": False},
            {"operation": "inventory_linear", "entity_types": ["users"], "include_archived": False},
            {"operation": "inventory_linear", "entity_types": ["issues"], "include_archived": 1},
            {"operation": "search_linear", "query": "", "entity_types": ["issues"], "include_archived": False},
            {"operation": "search_linear", "query": "   ", "entity_types": ["issues"], "include_archived": False},
            {"operation": "search_linear", "query": "x", "entity_types": ["issues"], "include_archived": False, "graphql": "query"},
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(route.RouteError):
                route.parse_linear_request(
                    request,
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )

    def test_structured_issue_relation_request_becomes_bounded_command(self):
        parsed = route.parse_linear_request(
            {
                "operation": "create_issue_relation",
                "identifier": "SIS-77",
                "related_identifier": "SIS-94",
                "relation_type": "blocked_by",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )

        self.assertEqual(parsed.command["operation"], "create_issue_relation")
        self.assertEqual(
            parsed.command["target"], {"type": "issue", "identifier": "SIS-77"}
        )
        self.assertEqual(
            parsed.command["change"],
            {"related_identifier": "SIS-94", "relation_type": "blocked_by"},
        )

    def test_owner_approved_relation_removal_and_replacement_become_exact_commands(self):
        reference = approval_reference()
        cases = (
            (
                {
                    "operation": "remove_issue_relation",
                    "identifier": "SIS-77",
                    "related_identifier": "SIS-94",
                    "relation_type": "blocked_by",
                    "approval": reference,
                },
                {
                    "related_identifier": "SIS-94",
                    "relation_type": "blocked_by",
                },
            ),
            (
                {
                    "operation": "replace_issue_relation",
                    "identifier": "SIS-77",
                    "old_related_identifier": "SIS-94",
                    "old_relation_type": "blocked_by",
                    "new_related_identifier": "SIS-95",
                    "new_relation_type": "related",
                    "approval": reference,
                },
                {
                    "old_related_identifier": "SIS-94",
                    "old_relation_type": "blocked_by",
                    "new_related_identifier": "SIS-95",
                    "new_relation_type": "related",
                },
            ),
        )
        for request, expected_change in cases:
            with self.subTest(operation=request["operation"]):
                parsed = route.parse_linear_request(
                    request,
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )
                self.assertEqual(parsed.command["operation"], request["operation"])
                self.assertEqual(
                    parsed.command["target"],
                    {"type": "issue", "identifier": "SIS-77"},
                )
                self.assertEqual(parsed.command["change"], expected_change)
                self.assertEqual(
                    parsed.command["policy"],
                    {"mode": "owner_approved", "approval": reference},
                )

    def test_relation_removal_and_replacement_fail_closed_without_exact_approval(self):
        cases = (
            {
                "operation": "remove_issue_relation",
                "identifier": "SIS-77",
                "related_identifier": "SIS-94",
                "relation_type": "blocks",
            },
            {
                "operation": "replace_issue_relation",
                "identifier": "SIS-77",
                "old_related_identifier": "SIS-94",
                "old_relation_type": "blocks",
                "new_related_identifier": "SIS-95",
                "new_relation_type": "related",
            },
        )
        for request in cases:
            with self.subTest(operation=request["operation"]), self.assertRaisesRegex(
                route.RouteError, "owner approval"
            ):
                route.parse_linear_request(
                    request,
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )

    def test_issue_relation_source_shape_rejects_self_internal_ids_and_unbounded_types(self):
        invalid = (
            {
                "identifier": "SIS-77",
                "related_identifier": "SIS-77",
                "relation_type": "blocks",
            },
            {
                "identifier": "SIS-77",
                "related_identifier": {"id": "internal"},
                "relation_type": "blocks",
            },
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(route.RouteError):
                route.parse_linear_request(
                    {"operation": "create_issue_relation", **values},
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )

        duplicate = route.parse_linear_request(
            {
                "operation": "create_issue_relation",
                "identifier": "SIS-77",
                "related_identifier": "SIS-94",
                "relation_type": "duplicate",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(duplicate.command["policy"], {"mode": "standard"})
        self.assertEqual(duplicate.command["change"]["relation_type"], "duplicate")

    def test_structured_standalone_and_issue_tree_requests_become_bounded_commands(self):
        standalone = {
            "operation": "create_standalone_issue",
            "project": {"name": "Wardrobe & Style", "description": "Wardrobe"},
            "milestone": {"name": "Autumn 2026", "description": "Autumn"},
            "issue": {
                "title": "Выбрать и купить костюм",
                "description": "https://example.com/suits",
                "state": "Todo",
                "priority": "High",
            },
        }
        tree = json.loads(json.dumps(standalone, ensure_ascii=False))
        tree["operation"] = "converge_issue_tree"
        tree["sub_issues"] = [
            {
                "title": title,
                "description": f"Прочитать {title}",
                "state": "Todo",
                "priority": "Medium",
            }
            for title in ("Король Лир", "Макбет", "Гамлет", "Отелло")
        ]

        for request in (standalone, tree):
            with self.subTest(operation=request["operation"]):
                parsed = route.parse_linear_request(
                    request,
                    source_profile="books",
                    uuid_factory=uuid_factory(),
                )
                self.assertEqual(parsed.command["operation"], request["operation"])
                self.assertEqual(
                    parsed.command["target"], {"type": "team", "identifier": "SIS"}
                )
                self.assertEqual(
                    parsed.command["change"],
                    {key: value for key, value in request.items() if key != "operation"},
                )

    def test_structured_hierarchy_request_becomes_bounded_command(self):
        request = {
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
        }

        parsed = route.parse_linear_request(
            request,
            source_profile="default",
            uuid_factory=uuid_factory(),
        )

        self.assertEqual(parsed.command["operation"], "converge_hierarchy")
        self.assertEqual(
            parsed.command["target"], {"type": "team", "identifier": "SIS"}
        )
        self.assertEqual(
            parsed.command["change"],
            {key: value for key, value in request.items() if key != "operation"},
        )

    def test_hierarchy_reserved_markers_fail_before_task_creation(self):
        base = {
            "operation": "converge_hierarchy",
            "project": {"name": "health", "description": "project detail"},
            "milestone": {"name": "Подолог", "description": "milestone detail"},
            "issue": {
                "title": "Сходить в Solomia и записаться",
                "description": "https://solomia.in.ua",
            },
        }
        fields = (
            ("project", "name"),
            ("project", "description"),
            ("milestone", "name"),
            ("milestone", "description"),
            ("issue", "title"),
            ("issue", "description"),
        )
        for kind, field in fields:
            with self.subTest(kind=kind, field=field):
                request = json.loads(json.dumps(base, ensure_ascii=False))
                request[kind][field] = "<!-- linear-command:v2 reserved -->"
                board = FakeBoard()
                with self.assertRaisesRegex(route.RouteError, "reserved marker"):
                    route.route_request(
                        request,
                        source=source_context(),
                        board=board,
                        uuid_factory=uuid_factory(),
                    )
                self.assertEqual(board.calls, [])

    def test_hierarchy_replay_key_ignores_random_command_ids_and_changes_with_semantics(self):
        request = {
            "operation": "converge_hierarchy",
            "project": {"name": "health", "description": "private project detail"},
            "milestone": {"name": "Подолог"},
            "issue": {
                "title": "Сходить в Solomia и записаться",
                "description": "https://solomia.in.ua",
                "state": "Todo",
            },
        }
        first = route.parse_linear_request(
            request, source_profile="default", uuid_factory=uuid_factory()
        ).command
        alternate_ids = iter(reversed(UUID_VALUES))
        second = route.parse_linear_request(
            request, source_profile="default", uuid_factory=lambda: next(alternate_ids)
        ).command
        changed_request = json.loads(json.dumps(request, ensure_ascii=False))
        changed_request["issue"]["description"] += "/changed"
        changed = route.parse_linear_request(
            changed_request, source_profile="default", uuid_factory=uuid_factory()
        ).command

        self.assertNotEqual(first["command_id"], second["command_id"])
        self.assertNotEqual(first["correlation_id"], second["correlation_id"])
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertNotEqual(first["idempotency_key"], changed["idempotency_key"])
        for entity in ("project", "milestone", "issue"):
            self.assertNotIn("id", first["change"][entity])

    def test_mutation_key_excludes_source_profile(self):
        text = "Добавь к SIS-61 комментарий: Globally identical mutation."
        default = route.parse_linear_request(
            text, source_profile="default", uuid_factory=uuid_factory()
        ).command
        ideas = route.parse_linear_request(
            text, source_profile="ideas", uuid_factory=uuid_factory()
        ).command

        self.assertEqual(default["idempotency_key"], ideas["idempotency_key"])
        self.assertNotEqual(default["source_profile"], ideas["source_profile"])

    def test_all_structured_state_operations_share_safe_state_allowlist(self):
        extended = set(route.SAFE_STATES) | {"Review Canary"}
        requests = [
            {
                "operation": "change_state",
                "identifier": "SIS-68",
                "state": "Review Canary",
            },
            {
                "operation": "create_issue",
                "title": "Shared state allowlist proof",
                "description": "",
                "parent_identifier": "SIS-68",
                "state": "Review Canary",
                "priority": "Low",
            },
        ]
        with mock.patch.object(route, "SAFE_STATES", extended):
            for request in requests:
                with self.subTest(operation=request["operation"]):
                    parsed = route.parse_linear_request(
                        request,
                        source_profile="default",
                        uuid_factory=uuid_factory(),
                    )
                    self.assertEqual(parsed.command["change"]["state"], "Review Canary")

    def test_exact_comment_request_becomes_bounded_command(self):
        parsed = route.parse_linear_request(
            "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
            uuid_factory=uuid_factory(),
        )

        command = parsed.command
        self.assertEqual(command["schema_version"], "linear-command.v2")
        self.assertEqual(command["source_profile"], "swe")
        self.assertEqual(command["operation"], "add_comment")
        self.assertEqual(command["target"], {"type": "issue", "identifier": "SIS-61"})
        self.assertEqual(command["change"], {"body": "SIS-61 E2E proof A."})
        self.assertEqual(command["policy"], {"mode": "standard"})
        self.assertRegex(command["idempotency_key"], r"^linear:v2:[a-f0-9]{32}$")

    def test_structured_comment_request_becomes_same_bounded_command(self):
        structured = route.parse_linear_request(
            {
                "operation": "add_comment",
                "identifier": "SIS-70",
                "body": "Краткие выводы после чтения.",
            },
            source_profile="books",
            uuid_factory=uuid_factory(),
        ).command
        legacy = route.parse_linear_request(
            "Добавь к SIS-70 комментарий: Краткие выводы после чтения.",
            source_profile="books",
            uuid_factory=uuid_factory(),
        ).command

        self.assertEqual(structured["operation"], "add_comment")
        self.assertEqual(
            structured["target"], {"type": "issue", "identifier": "SIS-70"}
        )
        self.assertEqual(structured["change"], {"body": "Краткие выводы после чтения."})
        self.assertEqual(structured["idempotency_key"], legacy["idempotency_key"])

    def test_structured_comment_accepts_positive_issue_number(self):
        command = route.parse_linear_request(
            {
                "operation": "add_comment",
                "issue_number": 70,
                "body": "Books proof.",
            },
            source_profile="books",
            uuid_factory=uuid_factory(),
        ).command
        self.assertEqual(command["target"]["identifier"], "SIS-70")

    def test_structured_comment_fails_closed_on_invalid_shape_or_body(self):
        invalid = (
            {"operation": "add_comment", "identifier": "SIS-70"},
            {
                "operation": "add_comment",
                "identifier": "SIS-70",
                "issue_number": 70,
                "body": "ambiguous target",
            },
            {
                "operation": "add_comment",
                "identifier": "SIS-70",
                "body": "Authorization: Bearer secret-shaped-value",
            },
            {
                "operation": "add_comment",
                "identifier": "SIS-70",
                "body": "bounded",
                "unexpected": True,
            },
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(route.RouteError):
                route.parse_linear_request(request, uuid_factory=uuid_factory())

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

    def test_terminal_state_request_needs_no_separate_approval(self):
        for state in ("Done", "Canceled"):
            with self.subTest(state=state):
                parsed = route.parse_linear_request(
                    {
                        "operation": "change_state",
                        "identifier": "SIS-102",
                        "state": state,
                    },
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )
                self.assertEqual(parsed.command["change"], {"state": state})
                self.assertEqual(parsed.command["policy"], {"mode": "standard"})
        with self.assertRaises(route.RouteError):
            route.parse_linear_request(
                {
                    "operation": "change_state",
                    "identifier": "SIS-102",
                    "state": "Duplicate",
                },
                source_profile="default",
                uuid_factory=uuid_factory(),
            )

    def test_structured_issue_update_preserves_only_explicit_fields(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "description": "Школа на Яр валу.",
                "priority": "Medium",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(parsed.command["operation"], "update_issue")
        self.assertEqual(
            parsed.command["target"], {"type": "issue", "identifier": "SIS-94"}
        )
        self.assertEqual(
            parsed.command["change"],
            {"description": "Школа на Яр валу.", "priority": "Medium"},
        )

    def test_structured_issue_update_preserves_exact_title(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "title": "Записаться на урок фортепиано",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(
            parsed.command["change"],
            {"title": "Записаться на урок фортепиано"},
        )

    def test_structured_issue_update_preserves_assignee_name_and_null_unassign(self):
        assigned = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "assignee": "Alexey Petrov",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        unassigned = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "assignee": None,
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(assigned.command["change"], {"assignee": "Alexey Petrov"})
        self.assertEqual(unassigned.command["change"], {"assignee": None})

    def test_structured_issue_update_preserves_exact_label_set(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "labels": ["area:linear", "priority:owner"],
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(
            parsed.command["change"],
            {"labels": ["area:linear", "priority:owner"]},
        )

    def test_structured_issue_update_preserves_due_date_and_estimate_values(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "due_date": "2026-09-30",
                "estimate": 8,
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(
            parsed.command["change"],
            {"due_date": "2026-09-30", "estimate": 8},
        )

    def test_structured_issue_update_preserves_null_due_date_and_estimate_clears(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "due_date": None,
                "estimate": None,
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(
            parsed.command["change"],
            {"due_date": None, "estimate": None},
        )

    def test_structured_issue_update_preserves_exact_parent_identifier_and_clear_request(self):
        attached = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "parent_identifier": "SIS-68",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        clear = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "parent_identifier": None,
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(attached.command["change"], {"parent_identifier": "SIS-68"})
        self.assertEqual(clear.command["change"], {"parent_identifier": None})

    def test_parent_only_owner_approval_emits_exact_policy_and_changes_replay_identity(self):
        standard = route.parse_linear_request(
            {"operation": "update_issue", "identifier": "SIS-94", "parent_identifier": None},
            source_profile="default",
            uuid_factory=uuid_factory(),
        ).command
        reference = approval_reference()
        request = {
            "operation": "update_issue",
            "identifier": "SIS-94",
            "parent_identifier": None,
            "approval": reference,
        }
        approved = route.parse_linear_request(
            request, source_profile="default", uuid_factory=uuid_factory()
        ).command
        replay = route.parse_linear_request(
            request, source_profile="default", uuid_factory=uuid_factory()
        ).command
        self.assertEqual(
            approved["policy"],
            {"mode": "owner_approved", "approval": reference},
        )
        self.assertEqual(approved["change"], {"parent_identifier": None})
        self.assertNotEqual(standard["idempotency_key"], approved["idempotency_key"])
        self.assertEqual(approved["idempotency_key"], replay["idempotency_key"])

    def test_source_rejects_forged_or_unbounded_approval_surfaces(self):
        base = {
            "operation": "update_issue",
            "identifier": "SIS-94",
            "parent_identifier": "SIS-68",
        }
        cases = (
            {**base, "approval": True},
            {**base, "approved": True},
            {**base, "policy": {"mode": "owner_approved"}},
            {**base, "approval": {**approval_reference(), "path": "/tmp/a"}},
            {**base, "approval": {**approval_reference(), "manifest": "raw"}},
            {**base, "approval": {**approval_reference(), "shell": "env"}},
            {**base, "approval": approval_reference(workflow="attacker")},
            {**base, "description": "mixed", "approval": approval_reference()},
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(route.RouteError):
                route.parse_linear_request(
                    value, source_profile="default", uuid_factory=uuid_factory()
                )

    def test_structured_issue_update_rejects_malformed_parent_identifier(self):
        for parent_identifier in ("sis-68", "SIS-0", "SIS-68 ", 68, {"id": "internal"}):
            with self.subTest(parent_identifier=parent_identifier), self.assertRaisesRegex(
                route.RouteError, "parent_identifier"
            ):
                route.parse_linear_request(
                    {
                        "operation": "update_issue",
                        "identifier": "SIS-94",
                        "parent_identifier": parent_identifier,
                    },
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )

    def test_structured_issue_update_preserves_exact_project_and_milestone_names(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "project": "Hermes Experience",
                "milestone": "Personal productivity integrations",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(
            parsed.command["change"],
            {
                "project": "Hermes Experience",
                "milestone": "Personal productivity integrations",
            },
        )

    def test_structured_issue_update_preserves_null_project_and_milestone_clear(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-94",
                "project": None,
                "milestone": None,
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(
            parsed.command["change"],
            {"project": None, "milestone": None},
        )

    def test_structured_issue_update_requires_a_complete_project_milestone_pair(self):
        invalid = (
            {"project": "Hermes Experience"},
            {"milestone": "Personal productivity integrations"},
            {"project": None, "milestone": "Personal productivity integrations"},
            {"project": "Hermes Experience", "milestone": None},
            {"project": "", "milestone": "Personal productivity integrations"},
            {"project": "Hermes Experience", "milestone": ""},
            {"project": {"id": "forbidden"}, "milestone": "Exact Name"},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(route.RouteError):
                route.parse_linear_request(
                    {
                        "operation": "update_issue",
                        "identifier": "SIS-94",
                        **change,
                    },
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )

    def test_structured_issue_update_rejects_invalid_due_dates_and_estimates(self):
        invalid = (
            {"due_date": "2026-02-30"},
            {"due_date": "2026-9-01"},
            {"due_date": 20260901},
            {"estimate": -1},
            {"estimate": 1.5},
            {"estimate": True},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises(route.RouteError):
                    route.parse_linear_request(
                        {
                            "operation": "update_issue",
                            "identifier": "SIS-94",
                            **change,
                        },
                        source_profile="default",
                        uuid_factory=uuid_factory(),
                    )

    def test_sub_issue_inventory_request_targets_exact_parent(self):
        parsed = route.parse_linear_request(
            {
                "operation": "inventory_sub_issues",
                "identifier": "SIS-86",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(parsed.command["operation"], "inventory_sub_issues")
        self.assertEqual(
            parsed.command["target"],
            {"type": "issue", "identifier": "SIS-86"},
        )
        self.assertEqual(parsed.command["change"], {})

    def test_sub_issue_update_request_preserves_exact_description(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_sub_issues",
                "identifier": "SIS-86",
                "description": "",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(parsed.command["operation"], "update_sub_issues")
        self.assertEqual(
            parsed.command["target"],
            {"type": "issue", "identifier": "SIS-86"},
        )
        self.assertEqual(parsed.command["change"], {"description": ""})
    def test_issue_number_targets_the_single_sis_team_without_model_normalization(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_issue",
                "issue_number": 86,
                "description_transform": "remove_links",
                "state": "Todo",
            },
            source_profile="books",
            uuid_factory=uuid_factory(),
        )

        self.assertEqual(
            parsed.command["target"], {"type": "issue", "identifier": "SIS-86"}
        )
        self.assertEqual(
            parsed.command["change"],
            {"description_transform": "remove_links", "state": "Todo"},
        )

    def test_issue_number_and_exact_identifier_are_mutually_exclusive(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_issue",
                "identifier": "SIS-86",
                "parent_identifier": None,
                "approval": approval_reference(),
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertIn("parent_identifier", parsed.command["change"])
        self.assertIsNone(parsed.command["change"]["parent_identifier"])

        with self.assertRaisesRegex(route.RouteError, "description_transform"):
            route.parse_linear_request(
                {
                    "operation": "update_issue",
                    "identifier": "SIS-86",
                    "description_transform": ["remove_links"],
                },
                source_profile="default",
                uuid_factory=uuid_factory(),
            )

        with self.assertRaisesRegex(route.RouteError, "exactly one issue target"):
            route.parse_linear_request(
                {
                    "operation": "change_state",
                    "identifier": "SIS-86",
                    "issue_number": 86,
                    "state": "Todo",
                },
                uuid_factory=uuid_factory(),
            )

    def test_structured_create_request_becomes_bounded_team_command(self):
        parsed = route.parse_linear_request(
            {
                "operation": "create_issue",
                "title": "  Universal routing tracer bullet  ",
                "description": "  Bounded verification issue.  ",
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
        self.assertEqual(parsed.command["change"]["title"], "  Universal routing tracer bullet  ")
        self.assertEqual(parsed.command["change"]["description"], "  Bounded verification issue.  ")

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
            "Добавь к SIS-61 комментарий: lin_api_" + "A" * 32,
        )
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(route.RouteError):
                    route.parse_linear_request(text, uuid_factory=uuid_factory())

    def test_initiative_update_preserves_exact_managed_fields_and_null_date(self):
        parsed = route.parse_linear_request(
            {
                "operation": "update_initiative",
                "name": "Personal operating system",
                "new_name": "Personal systems",
                "description": "Unified personal systems",
                "target_date": None,
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(
            parsed.command["target"],
            {"type": "workspace", "identifier": "current"},
        )
        self.assertEqual(
            parsed.command["change"],
            {
                "name": "Personal operating system",
                "new_name": "Personal systems",
                "description": "Unified personal systems",
                "target_date": None,
            },
        )

    def test_destructive_or_broad_initiative_operations_remain_unexposed(self):
        for operation in (
            "unlink_project_from_initiative",
            "archive_initiative",
            "delete_initiative",
            "reparent_initiative",
            "search_initiatives",
            "bulk_update_initiatives",
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                route.RouteError, "not allowed"
            ):
                route.parse_linear_request(
                    {
                        "operation": operation,
                        "name": "Personal operating system",
                    },
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )

    def test_initiative_link_request_uses_exact_project_and_initiative_names(self):
        parsed = route.parse_linear_request(
            {
                "operation": "link_project_to_initiative",
                "project": "Hermes Experience",
                "initiative": "Personal operating system",
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )

        self.assertEqual(parsed.command["operation"], "link_project_to_initiative")
        self.assertEqual(
            parsed.command["target"], {"type": "team", "identifier": "SIS"}
        )
        self.assertEqual(
            parsed.command["change"],
            {
                "project": "Hermes Experience",
                "initiative": "Personal operating system",
            },
        )

    def test_initiative_create_request_preserves_only_exact_bounded_fields(self):
        request = {
            "operation": "create_initiative",
            "name": "Personal operating system",
            "description": "Connected personal systems",
            "target_date": "2026-12-31",
        }

        parsed = route.parse_linear_request(
            request, source_profile="default", uuid_factory=uuid_factory()
        )

        self.assertEqual(parsed.command["operation"], "create_initiative")
        self.assertEqual(
            parsed.command["target"], {"type": "workspace", "identifier": "current"}
        )
        self.assertEqual(
            parsed.command["change"],
            {key: value for key, value in request.items() if key != "operation"},
        )
        self.assertFalse(
            set(parsed.command["change"])
            & {"id", "owner", "status", "labels", "parent", "team"}
        )

    def test_project_management_requests_preserve_only_exact_bounded_fields(self):
        requests = (
            {"operation": "create_project", "name": "Hermes Experience", "description": "Integrations", "target_date": "2026-12-31"},
            {"operation": "create_milestone", "project": "Hermes Experience", "name": "Calendar", "target_date": "2026-10-01"},
            {"operation": "update_project", "name": "Hermes Experience", "new_name": "Hermes Personal Experience", "target_date": None},
            {"operation": "update_milestone", "project": "Hermes Experience", "name": "Calendar", "description": "Calendar milestone"},
        )
        for request in requests:
            with self.subTest(operation=request["operation"]):
                parsed = route.parse_linear_request(request, source_profile="default", uuid_factory=uuid_factory())
                self.assertEqual(parsed.command["operation"], request["operation"])
                self.assertEqual(parsed.command["target"], {"type": "team", "identifier": "SIS"})
                self.assertEqual(parsed.command["change"], {key: value for key, value in request.items() if key != "operation"})
                self.assertFalse(set(parsed.command["change"]) & {"id", "team", "team_id"})

        invalid = dict(requests[0], target_date="2026-02-30")
        with self.assertRaisesRegex(route.RouteError, "valid calendar date"):
            route.parse_linear_request(invalid, uuid_factory=uuid_factory())


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
        self.assertEqual(result["task_id"], board.created["id"])
        self.assertFalse(result["replayed"])
        self.assertEqual([call[0] for call in board.calls], ["get_or_create", "route", "audit", "release"])
        created = board.calls[0][2]
        self.assertTrue(created["triage"])
        self.assertEqual(created["assignee"], "project-manager")
        self.assertEqual(created["skills"], ["project-manager-linear-worker"])
        self.assertEqual(created["session_id"], source_context().session_id)
        self.assertEqual(created["idempotency_key"], result["delivery_key"])
        self.assertNotEqual(result["delivery_key"], result["idempotency_key"])
        body = json.loads(created["body"])
        self.assertEqual(body["schema_version"], "linear-kanban-task.v2")
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
        body = json.loads(board.calls[0][2]["body"])
        self.assertEqual(body["command"]["source_profile"], "books")

    def test_cross_profile_and_session_deliveries_share_mutation_key_but_create_separate_tasks(self):
        text = "Добавь к SIS-61 комментарий: One global change."
        deliveries = []
        mutation_keys = []
        task_ids = []
        for source in (
            source_context(profile="default"),
            source_context(profile="ideas"),
            source_context(session_id="20260828_120001_deadbeef"),
        ):
            board = FakeBoard()
            result = route.route_request(
                text, source=source, board=board, uuid_factory=uuid_factory()
            )
            deliveries.append(result["delivery_key"])
            mutation_keys.append(result["idempotency_key"])
            task_ids.append(result["task_id"])

        self.assertEqual(len(set(mutation_keys)), 1)
        self.assertEqual(len(set(deliveries)), 3)
        self.assertEqual(len(set(task_ids)), 3)

    def test_route_drift_leaves_task_in_triage(self):
        board = FakeBoard(audit_result="drift")
        with self.assertRaisesRegex(route.RouteError, "route audit failed"):
            route.route_request(
                "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
                source=source_context(),
                board=board,
                uuid_factory=uuid_factory(),
            )
        self.assertEqual([call[0] for call in board.calls], ["get_or_create", "route", "audit"])

    def test_completed_initiative_replay_binds_exact_names(self):
        cases = (
            (
                {
                    "operation": "create_initiative",
                    "name": "Personal operating system",
                    "target_date": "2026-12-31",
                },
                {"type": "initiative", "identifier": "Personal operating system"},
                {"name": "Personal operating system", "target_date": "2026-12-31"},
            ),
            (
                {
                    "operation": "link_project_to_initiative",
                    "project": "Hermes Experience",
                    "initiative": "Personal operating system",
                },
                {
                    "type": "initiative_project",
                    "initiative": "Personal operating system",
                    "project": "Hermes Experience",
                },
                {
                    "initiative": "Personal operating system",
                    "project": "Hermes Experience",
                },
            ),
        )
        for request, target, after in cases:
            with self.subTest(operation=request["operation"]):
                persisted = route.parse_linear_request(
                    request,
                    source_profile=source_context().profile,
                    uuid_factory=uuid_factory(),
                ).command
                result = {
                    "schema_version": "linear-result.v2",
                    "command_id": persisted["command_id"],
                    "correlation_id": persisted["correlation_id"],
                    "idempotency_key": persisted["idempotency_key"],
                    "source_profile": persisted["source_profile"],
                    "operation": persisted["operation"],
                    "mode": "apply",
                    "target": target,
                    "result": "applied",
                    "before": None,
                    "after": after,
                    "plan": [{"action": persisted["operation"]}],
                    "no_op": False,
                    "verified": True,
                }
                board = FakeBoard(
                    existing={
                        "id": "t_1234abcd",
                        "status": "done",
                        "session_id": source_context().session_id,
                        "body": route.build_task_body(persisted),
                        "result": json.dumps(result),
                    }
                )

                replay = route.route_request(
                    request,
                    source=source_context(),
                    board=board,
                    uuid_factory=uuid_factory(),
                )
                self.assertEqual(replay["status"], "verified_no_op")
                self.assertEqual([call[0] for call in board.calls], ["get_or_create"])

    def test_completed_workspace_read_literal_replay_uses_same_task_and_result(self):
        requests_and_targets = (
            (
                {"operation": "create_issue_relation", "identifier": "SIS-77", "related_identifier": "SIS-94", "relation_type": "related"},
                {"type": "issue_relation", "identifier": "SIS-77", "related_identifier": "SIS-94", "relation_type": "related"},
            ),
            (
                {"operation": "remove_issue_relation", "identifier": "SIS-77", "related_identifier": "SIS-94", "relation_type": "related", "approval": approval_reference()},
                {"type": "issue_relation", "identifier": "SIS-77", "related_identifier": "SIS-94", "relation_type": "related"},
            ),
            (
                {"operation": "replace_issue_relation", "identifier": "SIS-77", "old_related_identifier": "SIS-94", "old_relation_type": "related", "new_related_identifier": "SIS-95", "new_relation_type": "blocks", "approval": approval_reference()},
                {"type": "issue_relation_replacement", "old": {"identifier": "SIS-77", "related_identifier": "SIS-94", "relation_type": "related"}, "new": {"identifier": "SIS-77", "related_identifier": "SIS-95", "relation_type": "blocks"}},
            ),
        )
        for relation_request, relation_target in requests_and_targets:
            with self.subTest(operation=relation_request["operation"]):
                persisted = route.parse_linear_request(
                    relation_request,
                    source_profile=source_context().profile,
                    uuid_factory=uuid_factory(),
                ).command
                linear_result = {
                    "schema_version": "linear-result.v2",
                    "command_id": persisted["command_id"],
                    "correlation_id": persisted["correlation_id"],
                    "idempotency_key": persisted["idempotency_key"],
                    "source_profile": persisted["source_profile"],
                    "operation": persisted["operation"],
                    "mode": "apply",
                    "target": relation_target,
                    "result": "applied",
                    "before": {},
                    "after": relation_target,
                    "plan": [{"action": persisted["operation"]}],
                    "no_op": False,
                    "verified": True,
                }
                board = FakeBoard(existing={"id": "t_1234abcd", "status": "done", "session_id": source_context().session_id, "body": route.build_task_body(persisted), "result": json.dumps(linear_result)})
                replay = route.route_request(
                    relation_request,
                    source=source_context(),
                    board=board,
                    uuid_factory=uuid_factory(),
                )
                self.assertEqual(replay["status"], "verified_no_op")

        request = {
            "operation": "inventory_linear",
            "entity_types": ["issues"],
            "include_archived": False,
        }
        persisted = route.parse_linear_request(
            request,
            source_profile=source_context().profile,
            uuid_factory=uuid_factory(),
        ).command
        facts = {
            "entity_types": ["issues"],
            "include_archived": False,
            "counts": {"issues": 0},
            "entities": {"issues": []},
        }
        linear_result = {
            "schema_version": "linear-result.v2",
            "command_id": persisted["command_id"],
            "correlation_id": persisted["correlation_id"],
            "idempotency_key": persisted["idempotency_key"],
            "source_profile": persisted["source_profile"],
            "operation": persisted["operation"],
            "mode": "apply",
            "target": {"type": "workspace", "identifier": "current"},
            "result": "read",
            "before": facts,
            "after": facts,
            "plan": [],
            "no_op": True,
            "verified": True,
        }
        board = FakeBoard(
            existing={
                "id": "t_1234abcd",
                "status": "done",
                "session_id": source_context().session_id,
                "body": route.build_task_body(persisted),
                "result": json.dumps(linear_result),
            }
        )
        first = route.route_request(
            request,
            source=source_context(),
            board=board,
            uuid_factory=uuid_factory(),
        )
        second = route.route_request(
            request,
            source=source_context(),
            board=board,
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["linear_result"], second["linear_result"])
        self.assertEqual(first["status"], "verified_no_op")
        self.assertEqual(
            [call[0] for call in board.calls], ["get_or_create", "get_or_create"]
        )

    def test_completed_v2_replay_is_verified_noop_without_new_task_or_route_write(self):
        request = "Добавь к SIS-61 комментарий: SIS-61 E2E proof A."
        persisted = route.parse_linear_request(
            request,
            source_profile=source_context().profile,
            uuid_factory=uuid_factory(),
        ).command
        existing = {
            "id": "t_1234abcd",
            "status": "done",
            "session_id": source_context().session_id,
            "body": route.build_task_body(persisted),
            "result": json.dumps(
                {
                    "schema_version": "linear-result.v2",
                    "command_id": persisted["command_id"],
                    "correlation_id": persisted["correlation_id"],
                    "idempotency_key": persisted["idempotency_key"],
                    "source_profile": persisted["source_profile"],
                    "operation": persisted["operation"],
                    "mode": "apply",
                    "target": {
                        "type": "issue",
                        "identifier": "SIS-61",
                        "url": "https://linear.app/example/SIS-61",
                    },
                    "result": "applied",
                    "before": {},
                    "after": {},
                    "plan": [{"action": "add_comment"}],
                    "no_op": False,
                    "verified": True,
                }
            ),
        }
        board = FakeBoard(existing=existing)
        result = route.route_request(
            request,
            source=source_context(),
            board=board,
            uuid_factory=uuid_factory(),
        )

        self.assertEqual(result["status"], "verified_no_op")
        self.assertTrue(result["replayed"])
        self.assertTrue(result["linear_result"]["verified"])
        self.assertEqual([call[0] for call in board.calls], ["get_or_create"])

    def test_completed_v2_result_for_another_mutation_is_rejected_fail_closed(self):
        request = "Добавь к SIS-61 комментарий: SIS-61 E2E proof A."
        persisted = route.parse_linear_request(
            request,
            source_profile=source_context().profile,
            uuid_factory=uuid_factory(),
        ).command
        existing = {
            "id": "t_1234abcd",
            "status": "done",
            "session_id": source_context().session_id,
            "body": route.build_task_body(persisted),
            "result": json.dumps(
                {
                    "schema_version": "linear-result.v2",
                    "command_id": persisted["command_id"],
                    "correlation_id": persisted["correlation_id"],
                    "idempotency_key": "linear:v2:" + "f" * 32,
                    "source_profile": persisted["source_profile"],
                    "operation": persisted["operation"],
                    "mode": "apply",
                    "target": {
                        "type": "issue",
                        "identifier": "SIS-61",
                        "url": "https://linear.app/example/SIS-61",
                    },
                    "result": "applied",
                    "before": {},
                    "after": {},
                    "plan": [],
                    "no_op": False,
                    "verified": True,
                }
            ),
        }
        board = FakeBoard(existing=existing)

        with self.assertRaisesRegex(route.RouteError, "does not match persisted command"):
            route.route_request(
                request,
                source=source_context(),
                board=board,
                uuid_factory=uuid_factory(),
            )

        self.assertEqual([call[0] for call in board.calls], ["get_or_create"])

    def test_completed_create_result_requires_nonempty_identifier_and_url(self):
        request = {
            "operation": "create_issue",
            "title": "Replay target proof",
            "description": "",
            "parent_identifier": "SIS-68",
            "state": "Todo",
            "priority": "Low",
        }
        persisted = route.parse_linear_request(
            request,
            source_profile=source_context().profile,
            uuid_factory=uuid_factory(),
        ).command
        result = {
            "schema_version": "linear-result.v2",
            "command_id": persisted["command_id"],
            "correlation_id": persisted["correlation_id"],
            "idempotency_key": persisted["idempotency_key"],
            "source_profile": persisted["source_profile"],
            "operation": persisted["operation"],
            "mode": "apply",
            "target": {"type": "issue"},
            "result": "applied",
            "before": None,
            "after": {},
            "plan": [],
            "no_op": False,
            "verified": True,
        }
        board = FakeBoard(
            existing={
                "id": "t_1234abcd",
                "status": "done",
                "session_id": source_context().session_id,
                "body": route.build_task_body(persisted),
                "result": json.dumps(result),
            }
        )

        with self.assertRaisesRegex(route.RouteError, "invalid verified target"):
            route.route_request(
                request,
                source=source_context(),
                board=board,
                uuid_factory=uuid_factory(),
            )

    def test_completed_replay_rejects_malformed_persisted_command_ids(self):
        request = "Добавь к SIS-61 комментарий: SIS-61 E2E proof A."
        persisted = route.parse_linear_request(
            request,
            source_profile=source_context().profile,
            uuid_factory=uuid_factory(),
        ).command
        persisted["command_id"] = None
        persisted["correlation_id"] = None
        result = {
            "schema_version": "linear-result.v2",
            "command_id": None,
            "correlation_id": None,
            "idempotency_key": persisted["idempotency_key"],
            "source_profile": persisted["source_profile"],
            "operation": persisted["operation"],
            "mode": "apply",
            "target": {
                "type": "issue",
                "identifier": "SIS-61",
                "url": "https://linear.app/example/SIS-61",
            },
            "result": "applied",
            "before": {},
            "after": {},
            "plan": [],
            "no_op": False,
            "verified": True,
        }
        board = FakeBoard(
            existing={
                "id": "t_1234abcd",
                "status": "done",
                "session_id": source_context().session_id,
                "body": route.build_task_body(persisted),
                "result": json.dumps(result),
            }
        )

        with self.assertRaisesRegex(route.RouteError, "invalid persisted command"):
            route.route_request(
                request,
                source=source_context(),
                board=board,
                uuid_factory=uuid_factory(),
            )

    def test_completed_noncurrent_result_is_rejected_fail_closed(self):
        board = FakeBoard(
            existing={
                "id": "t_1234abcd",
                "status": "done",
                "session_id": source_context().session_id,
                "result": json.dumps(
                    {
                        "schema_version": "linear-result.unsupported",
                        "result": "applied",
                        "verified": True,
                    }
                ),
            }
        )

        with self.assertRaisesRegex(route.RouteError, "linear-result.v2"):
            route.route_request(
                "Добавь к SIS-61 комментарий: SIS-61 E2E proof A.",
                source=source_context(),
                board=board,
                uuid_factory=uuid_factory(),
            )

        self.assertEqual([call[0] for call in board.calls], ["get_or_create"])

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
