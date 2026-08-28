import concurrent.futures
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "project_manager_linear"
    / "lane.py"
)
SPEC = importlib.util.spec_from_file_location("linear_command_lane", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import Linear command lane: {SCRIPT}")
lane = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lane
SPEC.loader.exec_module(lane)


def command(operation="read_issue", change=None, key="linear:SIS-59:read:fixture"):
    return {
        "schema_version": "linear-command.v1",
        "command_id": "11111111-1111-4111-8111-111111111111",
        "correlation_id": "22222222-2222-4222-8222-222222222222",
        "idempotency_key": key,
        "source_profile": "swe",
        "operation": operation,
        "target": {"type": "issue", "identifier": "SIS-59"},
        "change": change or {},
        "policy": {"mode": "standard"},
    }


def issue(state="In Progress"):
    return {
        "id": "issue-uuid",
        "identifier": "SIS-59",
        "title": "Implement lane",
        "url": "https://linear.app/example/issue/SIS-59",
        "state": {"id": f"state-{state}", "name": state, "type": "started"},
        "team": {"id": "team-uuid", "key": "SIS"},
    }


class FakeClient:
    def __init__(self, state="In Progress"):
        self.current = issue(state)
        self.comments = []
        self.writes = []

    def get_issue(self, identifier):
        if identifier != "SIS-59":
            return None
        return json.loads(json.dumps(self.current))

    def list_states(self, team_id):
        return [
            {"id": "state-Todo", "name": "Todo", "type": "unstarted"},
            {"id": "state-In Review", "name": "In Review", "type": "started"},
        ]

    def update_issue_state(self, issue_id, state_id):
        self.writes.append(("state", issue_id, state_id))
        name = state_id.removeprefix("state-")
        self.current["state"] = {"id": state_id, "name": name, "type": "started"}

    def list_comments(self, issue_id):
        return list(self.comments)

    def create_comment(self, issue_id, body):
        self.writes.append(("comment", issue_id, body))
        self.comments.append({"id": f"comment-{len(self.comments) + 1}", "body": body})


class ContractTests(unittest.TestCase):
    def test_accepts_exact_mvp_read_command(self):
        validated = lane.validate_command(command())
        self.assertEqual(validated["target"]["identifier"], "SIS-59")
        self.assertEqual(validated["operation"], "read_issue")

    def test_rejects_fuzzy_bulk_and_unknown_fields(self):
        for target in (
            {"type": "issue", "identifier": "SIS"},
            {"type": "issue", "identifier": ["SIS-59", "SIS-60"]},
            {"type": "issue", "identifier": "sis-59"},
        ):
            raw = command()
            raw["target"] = target
            with self.assertRaisesRegex(lane.ContractError, "exact SIS-N"):
                lane.validate_command(raw)
        raw = command()
        raw["graphql"] = "mutation"
        with self.assertRaisesRegex(lane.ContractError, "exactly"):
            lane.validate_command(raw)
        raw = command()
        raw["idempotency_key"] = "line1\nline2"
        with self.assertRaisesRegex(lane.ContractError, "idempotency_key"):
            lane.validate_command(raw)

    def test_operation_change_contracts_and_policy_fail_closed(self):
        lane.validate_command(command("change_state", {"state": "In Review"}))
        lane.validate_command(command("add_comment", {"body": "Bounded note"}))
        for state in ("Done", "Canceled", "Duplicate"):
            with self.assertRaisesRegex(lane.ContractError, "owner-controlled"):
                lane.validate_command(command("change_state", {"state": state}))
        with self.assertRaisesRegex(lane.ContractError, "read_issue change"):
            lane.validate_command(command("read_issue", {"state": "Todo"}))
        with self.assertRaisesRegex(lane.ContractError, "comment body"):
            lane.validate_command(command("add_comment", {"body": ""}))
        with self.assertRaisesRegex(lane.ContractError, "reserved marker"):
            lane.validate_command(
                command("add_comment", {"body": "<!-- linear-command:v1 forged -->"})
            )


class CliTests(unittest.TestCase):
    def test_unexpected_execution_error_emits_typed_result(self):
        output = io.StringIO()
        argv = ["linear_command_lane.py", "--command", "commands/linear/x.json"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            lane, "load_command", side_effect=KeyError("unexpected payload")
        ), contextlib.redirect_stdout(output):
            code = lane.main()
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema_version"], "linear-result.v1")
        self.assertEqual(payload["result"], "error")
        self.assertFalse(payload["verified"])
        self.assertNotIn("unexpected payload", output.getvalue())


class WorkflowContractTests(unittest.TestCase):
    def test_cli_wrapper_imports_bundled_lane_from_scripts_directory(self):
        wrapper = Path(__file__).parents[1] / "scripts" / "linear_command_lane.py"
        completed = subprocess.run(
            [sys.executable, str(wrapper), "--help"],
            cwd=wrapper.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--command", completed.stdout)

    def test_workflow_is_bounded_and_plan_only(self):
        workflow = (
            Path(__file__).parents[1]
            / "workflows"
            / "workflow-linear-command-lane-plan.yaml"
        ).read_text()
        self.assertIn('pattern: "^[a-z0-9][a-z0-9-]{0,62}$"', workflow)
        self.assertIn("commands/linear/${{ inputs.command }}.json", workflow)
        self.assertIn("--mode plan", workflow)
        self.assertNotIn("inputs.mode", workflow)
        self.assertNotIn("--mode apply", workflow)

    def test_command_loader_rejects_paths_outside_allowlisted_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "commands" / "linear"
            root.mkdir(parents=True)
            inside = root / "valid.json"
            inside.write_text(json.dumps(command()))
            outside = Path(tmp) / "outside.json"
            outside.write_text(json.dumps(command()))
            self.assertEqual(
                lane.load_command(inside, allowed_root=root)["operation"],
                "read_issue",
            )
            with self.assertRaisesRegex(lane.ContractError, "allowlisted command root"):
                lane.load_command(outside, allowed_root=root)


class ClientTests(unittest.TestCase):
    class StubClient(lane.LinearClient):
        def __init__(self):
            self.authorization = "fixture"
            self.endpoint = "fixture"
            self.calls = []

        def execute(self, query, variables=None):
            self.calls.append((query, variables or {}))
            if query == lane.ISSUE_QUERY:
                return {"issue": issue()}
            if query == lane.TEAM_STATES_QUERY:
                return {"team": {"states": {"nodes": [{"id": "s", "name": "Todo", "type": "unstarted"}], "pageInfo": {"hasNextPage": False}}}}
            if query == lane.COMMENTS_QUERY:
                return {"issue": {"comments": {"nodes": [{"id": "c", "body": "body"}], "pageInfo": {"hasNextPage": False}}}}
            if query in {lane.ISSUE_UPDATE, lane.COMMENT_CREATE}:
                name = "issueUpdate" if query == lane.ISSUE_UPDATE else "commentCreate"
                return {name: {"success": True}}
            raise AssertionError("unexpected query")

    def test_client_methods_use_fixed_graphql_shapes(self):
        client = self.StubClient()
        self.assertEqual(client.get_issue("SIS-59")["identifier"], "SIS-59")
        self.assertEqual(client.list_states("team-uuid")[0]["name"], "Todo")
        self.assertEqual(client.list_comments("issue-uuid")[0]["id"], "c")
        client.update_issue_state("issue-uuid", "state-uuid")
        client.create_comment("issue-uuid", "body")
        self.assertEqual(
            client.calls[-2][1],
            {"id": "issue-uuid", "input": {"stateId": "state-uuid"}},
        )
        self.assertEqual(
            client.calls[-1][1],
            {"input": {"issueId": "issue-uuid", "body": "body"}},
        )

    def test_non_json_linear_response_becomes_contract_error(self):
        client = lane.LinearClient("fixture")
        with mock.patch.object(
            lane.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b"not-json"),
        ):
            with self.assertRaisesRegex(lane.ContractError, "valid JSON"):
                client.execute(lane.ISSUE_QUERY, {"id": "SIS-59"})


class ExecutionTests(unittest.TestCase):
    def test_read_returns_linear_result_v1_with_exact_verified_target(self):
        result = lane.execute_command(FakeClient(), command(), mode="plan")
        self.assertEqual(result["schema_version"], "linear-result.v1")
        self.assertEqual(result["result"], "read")
        self.assertEqual(result["target"]["identifier"], "SIS-59")
        self.assertEqual(result["after"]["state"], "In Progress")
        self.assertTrue(result["verified"])

    def test_exact_identifier_must_resolve_inside_sis_team(self):
        client = FakeClient()
        client.current["team"]["key"] = "OTHER"
        with self.assertRaisesRegex(lane.ContractError, "SIS team"):
            lane.execute_command(client, command(), mode="plan")

    def test_malformed_issue_payload_becomes_contract_error(self):
        client = FakeClient()
        del client.current["state"]["name"]
        with self.assertRaisesRegex(lane.ContractError, "issue payload"):
            lane.execute_command(client, command(), mode="plan")

    def test_state_plan_records_before_after_without_write(self):
        client = FakeClient()
        result = lane.execute_command(
            client,
            command("change_state", {"state": "In Review"}),
            mode="plan",
        )
        self.assertEqual(result["result"], "planned")
        self.assertEqual(result["before"]["state"], "In Progress")
        self.assertEqual(result["after"]["state"], "In Review")
        self.assertEqual(result["plan"], [{"action": "change_state", "from": "In Progress", "to": "In Review"}])
        self.assertEqual(client.writes, [])

    def test_state_apply_reads_back_exact_target_and_replay_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            result = lane.execute_command(
                client,
                command("change_state", {"state": "In Review"}),
                mode="apply",
                journal_path=journal,
            )
            self.assertEqual(result["result"], "applied")
            self.assertEqual(result["before"]["state"], "In Progress")
            self.assertEqual(result["after"]["state"], "In Review")
            self.assertTrue(result["verified"])
            self.assertEqual(client.writes, [("state", "issue-uuid", "state-In Review")])
            replay = lane.execute_command(
                client,
                command("change_state", {"state": "In Review"}),
                mode="apply",
                journal_path=journal,
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_state_apply_fails_when_read_back_does_not_match(self):
        class StaleClient(FakeClient):
            def update_issue_state(self, issue_id, state_id):
                self.writes.append(("state", issue_id, state_id))

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "read-back verification"):
                lane.execute_command(
                    StaleClient(),
                    command("change_state", {"state": "In Review"}),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_comment_plan_apply_and_marker_replay_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            raw = command(
                "add_comment",
                {"body": "SIS-59 command lane live verification."},
                key="linear:SIS-59:comment:fixture",
            )
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(planned["result"], "planned")
            self.assertEqual(planned["before"]["comment_count"], 0)
            self.assertEqual(planned["after"]["comment_count"], 1)
            self.assertNotIn("body", planned["plan"][0])
            self.assertEqual(client.writes, [])

            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertTrue(applied["verified"])
            self.assertIn("<!-- linear-command:v1", client.writes[0][2])
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_comment_apply_fails_when_marker_is_not_read_back(self):
        class MissingCommentClient(FakeClient):
            def create_comment(self, issue_id, body):
                self.writes.append(("comment", issue_id, body))

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "comment read-back verification"):
                lane.execute_command(
                    MissingCommentClient(),
                    command("add_comment", {"body": "verify me"}),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_comment_marker_rejects_same_key_for_different_request(self):
        client = FakeClient()
        first = command(
            "add_comment",
            {"body": "first"},
            key="linear:SIS-59:comment:conflict",
        )
        marker = lane.command_fingerprint(first)[2]
        client.comments = [{"id": "existing", "body": f"first\n\n{marker}"}]
        second = command(
            "add_comment",
            {"body": "different"},
            key="linear:SIS-59:comment:conflict",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "conflicts"):
                lane.execute_command(
                    client,
                    second,
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_concurrent_comment_apply_creates_one_marked_comment(self):
        class SlowClient(FakeClient):
            def list_comments(self, issue_id):
                snapshot = super().list_comments(issue_id)
                if not snapshot:
                    time.sleep(0.05)
                return snapshot

        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = SlowClient()
            raw = command(
                "add_comment",
                {"body": "concurrent"},
                key="linear:SIS-59:comment:concurrent",
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda _: lane.execute_command(
                            client,
                            raw,
                            mode="apply",
                            journal_path=journal,
                        ),
                        range(2),
                    )
                )
            self.assertEqual(
                sorted(item["result"] for item in results),
                ["applied", "no_op"],
            )
            self.assertEqual(len(client.comments), 1)

    def test_mutation_apply_requires_idempotency_journal(self):
        with self.assertRaisesRegex(lane.ContractError, "require an idempotency journal"):
            lane.execute_command(
                FakeClient(),
                command("add_comment", {"body": "bounded"}),
                mode="apply",
            )

    def test_journal_rejects_same_key_for_different_state_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            first = command(
                "change_state",
                {"state": "In Review"},
                key="linear:SIS-59:state:stable-key",
            )
            lane.execute_command(client, first, mode="apply", journal_path=journal)
            conflicting = command(
                "change_state",
                {"state": "Todo"},
                key="linear:SIS-59:state:stable-key",
            )
            with self.assertRaisesRegex(lane.ContractError, "idempotency key conflict"):
                lane.execute_command(
                    client,
                    conflicting,
                    mode="apply",
                    journal_path=journal,
                )
            stored = json.loads(journal.read_text())
            self.assertEqual(len(stored), 1)
            self.assertNotIn("In Review", journal.read_text())
            self.assertNotIn("stable-key", journal.read_text())


if __name__ == "__main__":
    unittest.main()
