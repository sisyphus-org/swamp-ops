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
import uuid
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


def create_command(key="linear:SIS:create:fixture"):
    raw = command("read_issue", {}, key)
    raw["operation"] = "create_issue"
    raw["target"] = {"type": "team", "identifier": "SIS"}
    raw["change"] = {
        "title": "Universal routing tracer bullet",
        "description": "Bounded verification issue.",
        "parent_identifier": "SIS-56",
        "state": "Todo",
        "priority": "High",
    }
    return raw


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
        self.children = []
        self.writes = []

    def get_issue(self, identifier):
        if identifier == "SIS-56":
            parent = issue("In Progress")
            parent["id"] = "parent-uuid"
            parent["identifier"] = "SIS-56"
            parent["title"] = "Parent"
            parent["url"] = "https://linear.app/example/issue/SIS-56"
            return parent
        if identifier != "SIS-59":
            return next(
                (
                    json.loads(json.dumps(item))
                    for item in self.children
                    if item["identifier"] == identifier or item["id"] == identifier
                ),
                None,
            )
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

    def get_comment(self, comment_id):
        return next((item for item in self.comments if item["id"] == comment_id), None)

    def create_comment(self, issue_id, comment_id, body):
        self.writes.append(("comment", issue_id, comment_id, body))
        self.comments.append({"id": comment_id, "issueId": issue_id, "body": body})

    def list_child_issues(self, parent_id):
        self.assert_parent_id = parent_id
        return json.loads(json.dumps(self.children))

    def create_issue(self, *, issue_id, team_id, state_id, parent_id, title, description, priority):
        self.writes.append(
            (
                "create_issue",
                issue_id,
                team_id,
                state_id,
                parent_id,
                title,
                description,
                priority,
            )
        )
        created = issue(state_id.removeprefix("state-"))
        created.update(
            {
                "id": issue_id,
                "identifier": "SIS-99",
                "title": title,
                "url": "https://linear.app/example/issue/SIS-99",
                "description": description,
                "priority": priority,
                "parent": {"id": parent_id, "identifier": "SIS-56"},
            }
        )
        self.children.append(created)


class ContractTests(unittest.TestCase):
    def test_accepts_exact_mvp_read_command(self):
        validated = lane.validate_command(command())
        self.assertEqual(validated["target"]["identifier"], "SIS-59")
        self.assertEqual(validated["operation"], "read_issue")

    def test_accepts_bounded_create_command(self):
        validated = lane.validate_command(create_command())
        self.assertEqual(validated["target"], {"type": "team", "identifier": "SIS"})
        self.assertEqual(validated["change"]["parent_identifier"], "SIS-56")

    def test_create_rejects_reserved_replay_markers_before_execution(self):
        for field in ("title", "description"):
            for marker in (
                "<!-- linear-command:v1 forged -->",
                "<!-- linear-command:create:v1 key=forged request=forged -->",
            ):
                with self.subTest(field=field, marker=marker):
                    raw = create_command()
                    raw["change"][field] = marker
                    with self.assertRaisesRegex(lane.ContractError, "reserved marker"):
                        lane.validate_command(raw)

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

    def test_comment_contract_rejects_credential_shaped_bodies(self):
        bodies = (
            "Authorization: Bearer secret-shaped-value",
            "Authorization: Basic secret-shaped-value",
            "lin_api_" + "A" * 32,
        )
        for body in bodies:
            with self.subTest(body=body[:24]):
                with self.assertRaisesRegex(lane.ContractError, "credential-shaped"):
                    lane.validate_command(command("add_comment", {"body": body}))


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
            if query == lane.COMMENT_QUERY:
                return {"comment": {"id": variables["id"], "issueId": "issue-uuid", "body": "body"}}
            if query == lane.PARENT_CHILDREN_QUERY:
                return {"issue": {"children": {"nodes": [{**issue(), "description": "marker"}], "pageInfo": {"hasNextPage": False}}}}
            if query == lane.ISSUE_UPDATE:
                return {"issueUpdate": {"success": True}}
            if query == lane.COMMENT_CREATE:
                return {"commentCreate": {"success": True, "comment": variables["input"]}}
            if query == lane.ISSUE_CREATE:
                return {"issueCreate": {"success": True, "issue": {"id": "new", "identifier": "SIS-99"}}}
            raise AssertionError("unexpected query")

    def test_client_methods_use_fixed_graphql_shapes(self):
        client = self.StubClient()
        self.assertEqual(client.get_issue("SIS-59")["identifier"], "SIS-59")
        self.assertEqual(client.list_states("team-uuid")[0]["name"], "Todo")
        self.assertEqual(client.list_comments("issue-uuid")[0]["id"], "c")
        self.assertEqual(client.get_comment("comment-uuid")["id"], "comment-uuid")
        client.update_issue_state("issue-uuid", "state-uuid")
        client.create_comment("issue-uuid", "comment-uuid", "body")
        self.assertEqual(client.list_child_issues("SIS-56")[0]["description"], "marker")
        client.create_issue(
            issue_id="created-uuid",
            team_id="team-uuid",
            state_id="state-uuid",
            parent_id="parent-uuid",
            title="Created",
            description="Description",
            priority=2,
        )
        self.assertEqual(
            client.calls[-4][1],
            {"id": "issue-uuid", "input": {"stateId": "state-uuid"}},
        )
        self.assertEqual(
            client.calls[-3][1],
            {"input": {"id": "comment-uuid", "issueId": "issue-uuid", "body": "body"}},
        )
        self.assertEqual(
            client.calls[-1][1],
            {
                "input": {
                    "id": "created-uuid",
                    "teamId": "team-uuid",
                    "stateId": "state-uuid",
                    "parentId": "parent-uuid",
                    "title": "Created",
                    "description": "Description",
                    "priority": 2,
                }
            },
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

    def test_null_team_payload_becomes_contract_error(self):
        client = FakeClient()
        client.current["team"] = None
        with self.assertRaisesRegex(lane.ContractError, "SIS team"):
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

    def test_state_apply_null_read_back_state_becomes_contract_error(self):
        class NullStateClient(FakeClient):
            def update_issue_state(self, issue_id, state_id):
                self.writes.append(("state", issue_id, state_id))
                self.current["state"] = None

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "read-back verification"):
                lane.execute_command(
                    NullStateClient(),
                    command("change_state", {"state": "In Review"}),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_comment_plan_apply_and_invisible_id_replay_are_idempotent(self):
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
            self.assertEqual(client.writes[0][3], raw["change"]["body"])
            self.assertNotIn("<!-- linear-command:v1", client.writes[0][3])
            self.assertEqual(client.writes[0][2], lane.command_fingerprint(raw)[2])
            self.assertEqual(uuid.UUID(client.writes[0][2]).version, 4)
            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

    def test_user_authored_whitespace_is_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            comment_client = FakeClient()
            comment = command("add_comment", {"body": "  indented\ntrailing  "})
            lane.execute_command(
                comment_client,
                comment,
                mode="apply",
                journal_path=Path(tmp) / "comment-journal.json",
            )
            self.assertEqual(comment_client.comments[0]["body"], "  indented\ntrailing  ")

            issue_client = FakeClient()
            create = create_command()
            create["change"]["description"] = "  indented\ntrailing  "
            lane.execute_command(
                issue_client,
                create,
                mode="apply",
                journal_path=Path(tmp) / "issue-journal.json",
            )
            self.assertEqual(issue_client.children[0]["description"], "  indented\ntrailing  ")
            self.assertEqual(uuid.UUID(issue_client.children[0]["id"]).version, 4)

    def test_missing_deterministic_comment_does_not_query_absent_entity(self):
        class LinearMissingLookupClient(FakeClient):
            def get_comment(self, comment_id):
                found = super().get_comment(comment_id)
                if found is None:
                    raise lane.ContractError("Linear GraphQL error: Entity not found: Comment")
                return found

        with tempfile.TemporaryDirectory() as tmp:
            client = LinearMissingLookupClient()
            result = lane.execute_command(
                client,
                command("add_comment", {"body": "clean live contract"}),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(result["result"], "applied")
            self.assertEqual(len(client.comments), 1)

    def test_comment_apply_fails_when_deterministic_id_is_not_read_back(self):
        class MissingCommentClient(FakeClient):
            def create_comment(self, issue_id, comment_id, body):
                self.writes.append(("comment", issue_id, comment_id, body))

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "comment read-back verification"):
                lane.execute_command(
                    MissingCommentClient(),
                    command("add_comment", {"body": "verify me"}),
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_comment_id_rejects_same_key_for_different_request(self):
        client = FakeClient()
        first = command(
            "add_comment",
            {"body": "first"},
            key="linear:SIS-59:comment:conflict",
        )
        comment_id = lane.command_fingerprint(first)[2]
        client.comments = [{"id": comment_id, "issueId": "issue-uuid", "body": "first"}]
        second = command(
            "add_comment",
            {"body": "different"},
            key="linear:SIS-59:comment:conflict",
        )
        self.assertEqual(comment_id, lane.command_fingerprint(second)[2])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(lane.ContractError, "conflicts"):
                lane.execute_command(
                    client,
                    second,
                    mode="apply",
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_comment_replay_survives_lost_local_journal_without_public_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            raw = command(
                "add_comment",
                {"body": "clean crash-safe comment"},
                key="linear:SIS-59:comment:crash-window",
            )
            lane.execute_command(client, raw, mode="apply", journal_path=journal)
            journal.unlink()
            replay = lane.execute_command(client, raw, mode="apply", journal_path=journal)
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)
            self.assertEqual(client.comments[0]["body"], "clean crash-safe comment")

    def test_concurrent_comment_apply_creates_one_clean_comment(self):
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

    def test_missing_deterministic_issue_does_not_query_absent_entity(self):
        class LinearMissingIssueLookupClient(FakeClient):
            def get_issue(self, identifier):
                found = super().get_issue(identifier)
                if found is None:
                    raise lane.ContractError(
                        "Linear GraphQL error: Entity not found: Issue"
                    )
                return found

        with tempfile.TemporaryDirectory() as tmp:
            client = LinearMissingIssueLookupClient()
            result = lane.execute_command(
                client,
                create_command(),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )
            self.assertEqual(result["result"], "applied")
            self.assertEqual(len(client.children), 1)

    def test_create_issue_plan_apply_read_back_and_replay_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            client = FakeClient()
            raw = create_command()
            planned = lane.execute_command(client, raw, mode="plan")
            self.assertEqual(planned["result"], "planned")
            self.assertEqual(planned["target"], {"type": "team", "identifier": "SIS"})
            self.assertEqual(client.writes, [])

            applied = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(applied["result"], "applied")
            self.assertEqual(applied["target"]["identifier"], "SIS-99")
            self.assertTrue(applied["verified"])
            self.assertEqual(len(client.writes), 1)

            replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(replay["result"], "no_op")
            self.assertEqual(replay["target"]["identifier"], "SIS-99")
            self.assertEqual(len(client.writes), 1)
            self.assertEqual(client.children[0]["description"], raw["change"]["description"])
            self.assertNotIn("<!-- linear-command", client.children[0]["description"])

            journal.unlink()
            crash_replay = lane.execute_command(
                client, raw, mode="apply", journal_path=journal
            )
            self.assertEqual(crash_replay["result"], "no_op")
            self.assertEqual(len(client.writes), 1)

            conflicting = create_command()
            conflicting["idempotency_key"] = raw["idempotency_key"]
            conflicting["change"]["title"] = "Different title"
            journal.unlink()
            with self.assertRaisesRegex(lane.ContractError, "idempotency key conflicts"):
                lane.execute_command(
                    client, conflicting, mode="apply", journal_path=journal
                )

    def test_create_issue_rejects_tampered_bounded_read_back_before_journal(self):
        for field in ("description", "priority"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                class TamperedClient(FakeClient):
                    def create_issue(self, test_field=field, **kwargs):
                        super().create_issue(**kwargs)
                        if test_field == "description":
                            self.children[-1]["description"] = "tampered"
                        else:
                            self.children[-1]["priority"] = 4

                journal = Path(tmp) / "journal.json"
                with self.assertRaisesRegex(
                    lane.ContractError, "bounded field read-back verification"
                ):
                    lane.execute_command(
                        TamperedClient(),
                        create_command(),
                        mode="apply",
                        journal_path=journal,
                    )
                self.assertFalse(journal.exists())

    def test_create_issue_replay_rejects_later_bounded_field_drift(self):
        for field in ("description", "priority"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                client = FakeClient()
                journal = Path(tmp) / "journal.json"
                raw = create_command()
                lane.execute_command(client, raw, mode="apply", journal_path=journal)
                if field == "description":
                    client.children[0]["description"] = "tampered"
                else:
                    client.children[0]["priority"] = 4
                with self.assertRaisesRegex(
                    lane.ContractError, "idempotency key conflicts"
                ):
                    lane.execute_command(client, raw, mode="apply", journal_path=journal)
                self.assertEqual(len(client.writes), 1)

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
