"""SIS-334 diagnostics exercised only through the public source handler."""
import json
import unittest

from plugins.linear_source_route import handle_linear_source_request


SESSION = {
    "HERMES_SESSION_ID": "20260828_120000_abcdef12",
    "HERMES_SESSION_PROFILE": "default",
    "HERMES_SESSION_PLATFORM": "telegram",
    "HERMES_SESSION_CHAT_TYPE": "dm",
    "HERMES_SESSION_CHAT_ID": "123",
    "HERMES_SESSION_USER_ID": "123",
    "HERMES_SESSION_THREAD_ID": "456",
}


class Board:
    def get_or_create_task(self, *args, **kwargs):
        raise AssertionError("invalid request must not reserve a task")


def call(request, **options):
    return json.loads(handle_linear_source_request(
        request,
        session_id=options.pop("session_id", SESSION["HERMES_SESSION_ID"]),
        session_getter=options.pop("session_getter", lambda name, default="": SESSION.get(name, default)),
        runtime_profile_getter=options.pop("runtime_profile_getter", lambda: "default"),
        board_factory=options.pop("board_factory", lambda **kwargs: Board()),
        **options,
    ))


class DiagnosticsTests(unittest.TestCase):
    def test_create_state_recovery_never_suggests_terminal_states(self):
        result = call({
            "operation": "create_standalone_issue",
            "project": {"name": "scope"}, "milestone": {"name": "scope"},
            "issue": {"title": "title", "description": "", "state": "Done", "priority": "Medium"},
        })
        self.assertEqual(result["error_code"], "invalid_state")
        self.assertIn("Research", result["message"])
        self.assertNotIn("Done", result["message"])
        self.assertNotIn("Canceled", result["message"])

    def test_infrastructure_errors_never_disclose_payload_or_claim_no_write(self):
        for exception_type, code in ((OSError, "os_error"), (ValueError, "value_error"), (KeyError, "key_error"), (TypeError, "type_error")):
            class FailingBoard:
                def get_or_create_task(self, *args, **kwargs):
                    raise exception_type("private-token /private/path SIS-999")
            with self.subTest(code=code):
                result = call({"operation": "create_project", "name": "private"}, board_factory=lambda **kwargs: FailingBoard())
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["outcome"], "unknown")
                self.assertEqual(result["recovery"], {"action": "reconcile_read_only"})
                self.assertNotIn("private", json.dumps(result))
                self.assertNotIn("SIS-999", json.dumps(result))

    def test_public_result_validation_failure_has_unknown_outcome(self):
        class CompletedBoard:
            def get_or_create_task(self, delivery_key, **kwargs):
                command = json.loads(kwargs["body"])["command"]
                result = {
                    "schema_version": "linear-result.v2",
                    **{key: command[key] for key in ("command_id", "correlation_id", "idempotency_key", "source_profile", "operation")},
                    "mode": "apply", "target": {"type": "project", "identifier": "private"},
                    "result": "applied", "before": {}, "after": {}, "plan": [], "no_op": False, "verified": True,
                }
                return {"id": "t_1234abcd", "status": "done", "session_id": SESSION["HERMES_SESSION_ID"], "idempotency_key": delivery_key, "body": kwargs["body"], "result": json.dumps(result)}, False
        result = call({"operation": "create_project", "name": "private"}, board_factory=lambda **kwargs: CompletedBoard())
        self.assertEqual(result["error_code"], "result_unverified")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["recovery"], {"action": "reconcile_read_only"})

    def test_bounded_issue_priority_and_text_security_have_fixed_diagnostics(self):
        base = {"operation": "create_standalone_issue", "project": {"name": "scope"}, "milestone": {"name": "scope"}, "issue": {"title": "title", "description": "", "state": "Todo", "priority": "private"}}
        for request, code in (
            (base, "invalid_priority"),
            ({**base, "issue": {**base["issue"], "priority": "Medium", "title": "lin_api_private_secret_value"}}, "unsafe_text"),
            ({"operation": "converge_hierarchy", "project": {"name": "scope"}, "milestone": {"name": "scope"}, "issue": {"title": "title", "description": "x" * 50000}}, "request_too_large"),
        ):
            with self.subTest(code=code):
                result = call(request)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["outcome"], "not_attempted")
                self.assertNotIn("private", json.dumps(result))

    def test_operation_shape_errors_name_required_fields_without_echoing_input(self):
        cases = (
            ({"operation": "create_project", "name": "private", "project": "private"}, "name"),
            ({"operation": "converge_hierarchy", "project": {"name": "private", "unknown": "private"}, "milestone": {"name": "private"}, "issue": {"title": "private"}}, "project"),
            ({"operation": "create_standalone_issue", "project": {"name": "private"}, "issue": {"title": "private"}}, "milestone"),
        )
        for request, required_field in cases:
            with self.subTest(operation=request["operation"]):
                result = call(request)
                self.assertEqual(result["error_code"], "invalid_request_shape")
                self.assertIn(required_field, result["message"])
                self.assertEqual(result["outcome"], "not_attempted")
                self.assertNotIn("private", json.dumps(result))

    def test_exact_context_failures_explain_safe_rebinding(self):
        cases = [
            ({"session_id": "20260828_120001_deadbeef"}, "source_session_mismatch"),
            ({"runtime_profile_getter": lambda: "swe"}, "source_profile_mismatch"),
            ({"runtime_profile_getter": lambda: "broker"}, "source_profile_invalid"),
            ({"session_id": "", "session_getter": lambda name, default="": "" if name == "HERMES_SESSION_ID" else SESSION.get(name, default)}, "source_context_invalid"),
        ]
        for options, code in cases:
            with self.subTest(code=code):
                result = call({"operation": "create_project", "name": "private"}, **options)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["outcome"], "not_attempted")
                self.assertEqual(result["recovery"], {"action": "restore_source_context"})
                self.assertNotIn("deadbeef", json.dumps(result))

    def test_missing_project_name_has_safe_actionable_shape_diagnostic(self):
        result = call({"operation": "create_project", "description": "private payload"})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], "invalid_request_shape")
        self.assertEqual(result["outcome"], "not_attempted")
        self.assertEqual(result["recovery"], {"action": "correct_request"})
        self.assertIn("bounded", result["message"])
        self.assertNotIn("private payload", json.dumps(result))

    def test_scoped_validators_report_only_allowlisted_field_reasons(self):
        issue = {"title": "private title", "description": "private description", "state": "Todo", "priority": "Medium"}
        requests = [
            ({"operation": "create_project", "name": ""}, "invalid_text"),
            ({"operation": "create_project", "name": "private", "target_date": "2026-02-30"}, "invalid_date"),
            ({"operation": "converge_hierarchy", "project": None, "milestone": {"name": "private"}, "issue": {"title": "private"}}, "invalid_scope"),
            ({"operation": "create_standalone_issue", "project": {"name": "private"}, "milestone": None, "issue": issue}, "invalid_scope"),
            ({"operation": "create_standalone_issue", "project": {"name": "private"}, "milestone": {"name": "private"}, "issue": {**issue, "state": "private"}}, "invalid_state"),
            ({"operation": "create_standalone_issue", "project": {"name": "private"}, "milestone": {"name": "private"}, "issue": {"title": "private"}}, "invalid_issue_spec"),
        ]
        for request, code in requests:
            with self.subTest(code=code):
                result = call(request)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["outcome"], "not_attempted")
                self.assertEqual(result["recovery"], {"action": "correct_request"})
                self.assertNotIn("private", json.dumps(result))
