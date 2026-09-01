import copy
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from plugins.linear_source_route import route
from plugins.project_manager_linear import execute_claimed_task, lane
from scripts import linear_owner_approval as owner_approval
from tests.test_linear_command_lane import FakeClient, command, owner_policy
from tests.test_linear_source_route import approval_reference, uuid_factory


class WorkflowStateClient(FakeClient):
    STATE_TYPES: ClassVar[dict[str, str]] = {
        "Backlog": "backlog",
        "Todo": "unstarted",
        "Research": "started",
        "In Progress": "started",
        "In Review": "started",
        "Done": "completed",
        "Canceled": "canceled",
        "Duplicate": "duplicate",
    }
    TERMINAL_TYPES: ClassVar[dict[str, str]] = {
        name: state_type
        for name, state_type in STATE_TYPES.items()
        if name in {"Done", "Canceled", "Duplicate"}
    }

    def list_states(self, team_id):
        return [
            {"id": f"state-{name}", "name": name, "type": state_type}
            for name, state_type in self.STATE_TYPES.items()
        ]

    def update_issue_state(self, issue_id, state_id):
        super().update_issue_state(issue_id, state_id)
        name = state_id.removeprefix("state-")
        self.current["state"]["type"] = self.STATE_TYPES[name]


def standard_state_command(state="Done", *, key=None):
    return command(
        "change_state",
        {"state": state},
        key=key or f"linear:SIS-102:state:{state.lower().replace(' ', '-')}",
    )


class StandardWorkflowStateTests(unittest.TestCase):
    def test_every_directly_writable_state_is_reachable_from_any_current_state_without_approval(self):
        destinations = tuple(
            state for state in WorkflowStateClient.STATE_TYPES if state != "Duplicate"
        )
        source_states = (*destinations, "Duplicate", "Awaiting External")
        for source_state in source_states:
            for target_state in destinations:
                with self.subTest(source=source_state, target=target_state), tempfile.TemporaryDirectory() as tmp:
                    client = WorkflowStateClient()
                    client.current["state"] = {
                        "id": f"state-{source_state}",
                        "name": source_state,
                        "type": client.STATE_TYPES.get(source_state, "started"),
                    }
                    raw = standard_state_command(
                        target_state,
                        key=(
                            "linear:SIS-102:"
                            f"{source_state.replace(' ', '-')}:"
                            f"{target_state.replace(' ', '-')}"
                        ),
                    )
                    with mock.patch(
                        "plugins.project_manager_linear.approval.verify_owner_approval"
                    ) as verifier:
                        result = execute_claimed_task(
                            raw,
                            task_id="t_1234abcd",
                            lane=lane,
                            client=client,
                            journal_path=Path(tmp) / "journal.json",
                        )
                    self.assertEqual(result["result"]["after"]["state"], target_state)
                    verifier.assert_not_called()

    def test_done_through_public_claimed_task_is_minimal_verified_and_replay_safe(self):
        client = WorkflowStateClient()
        raw = standard_state_command("Done")
        unmanaged_before = copy.deepcopy(
            {key: value for key, value in client.current.items() if key != "state"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            first = execute_claimed_task(
                raw,
                task_id="t_1234abcd",
                lane=lane,
                client=client,
                journal_path=journal,
            )
            replay = execute_claimed_task(
                raw,
                task_id="t_1234abcd",
                lane=lane,
                client=client,
                journal_path=journal,
            )
        self.assertEqual(first["result"]["result"], "applied")
        self.assertEqual(first["result"]["after"]["state"], "Done")
        self.assertEqual(client.writes, [("state", "issue-uuid", "state-Done")])
        self.assertEqual(
            {key: value for key, value in client.current.items() if key != "state"},
            unmanaged_before,
        )
        self.assertEqual(replay["result"]["result"], "no_op")
        self.assertTrue(replay["result"]["verified"])

    def test_source_terminal_requests_emit_standard_policy(self):
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

    def test_terminal_state_missing_ambiguous_or_wrong_type_fails_before_write(self):
        class StateInventoryClient(WorkflowStateClient):
            def __init__(self, terminal_states):
                super().__init__()
                self.terminal_states = terminal_states

            def list_states(self, team_id):
                return [
                    *[
                        item
                        for item in super().list_states(team_id)
                        if item["name"] not in {"Done", "Canceled"}
                    ],
                    *self.terminal_states,
                ]

        cases = (
            ([], "not found", "Done"),

            (
                [{"id": "done", "name": "Done", "type": "canceled"}],
                "incompatible semantic type",
                "Done",
            ),
            (
                [{"id": "canceled", "name": "Canceled", "type": "completed"}],
                "incompatible semantic type",
                "Canceled",
            ),
        )
        for terminal_states, message, requested in cases:
            client = StateInventoryClient(terminal_states)
            with self.subTest(terminal_states=terminal_states), self.assertRaisesRegex(
                lane.ContractError, message
            ):
                lane.execute_command(
                    client,
                    standard_state_command(requested),
                    mode="plan",
                )
            self.assertEqual(client.writes, [])

    def test_owner_approved_policy_is_rejected_for_state_changes(self):
        for state in ("Done", "Canceled", "In Review"):
            raw = standard_state_command(state)
            raw["policy"] = owner_policy()
            with self.subTest(state=state), self.assertRaisesRegex(
                lane.ContractError, "not allowed"
            ):
                lane.validate_command(raw)

    def test_terminal_state_intents_are_not_owner_approvable(self):
        for state in ("Done", "Canceled", "Duplicate"):
            with self.subTest(state=state), self.assertRaisesRegex(
                owner_approval.ContractError, "not owner-approvable"
            ):
                owner_approval.build_plan(
                    {
                        "operation": "change_state",
                        "target": {"type": "issue", "identifier": "SIS-102"},
                        "change": {"state": state},
                    },
                    "b" * 64,
                    "2026-09-01T13:00:00Z",
                )

    def test_process_crash_after_terminal_write_recovers_without_second_write(self):
        class CrashAfterState(WorkflowStateClient):
            crashed = False

            def update_issue_state(self, issue_id, state_id):
                super().update_issue_state(issue_id, state_id)
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process death")

        client = CrashAfterState()
        raw = standard_state_command("Canceled", key="linear:SIS-102:terminal-crash")
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            with self.assertRaises(KeyboardInterrupt):
                execute_claimed_task(
                    raw,
                    task_id="t_1234abcd",
                    lane=lane,
                    client=client,
                    journal_path=journal,
                )
            recovered = execute_claimed_task(
                raw,
                task_id="t_1234abcd",
                lane=lane,
                client=client,
                journal_path=journal,
            )
        self.assertEqual(recovered["result"]["result"], "no_op")
        self.assertEqual(client.writes, [("state", "issue-uuid", "state-Canceled")])

    def test_terminal_readback_state_or_unmanaged_drift_fails_closed(self):
        class ReadbackDrift(WorkflowStateClient):
            def __init__(self, field):
                super().__init__()
                self.field = field

            def update_issue_state(self, issue_id, state_id):
                super().update_issue_state(issue_id, state_id)
                if self.field == "state":
                    self.current["state"]["type"] = "started"
                else:
                    self.current["title"] = "drifted"

        for field, message in (
            ("state", "state read-back verification failed"),
            ("title", "state read-back changed unmanaged fields"),
        ):
            client = ReadbackDrift(field)
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
                lane.ContractError, message
            ):
                execute_claimed_task(
                    standard_state_command(
                        "Done", key=f"linear:SIS-102:readback:{field}"
                    ),
                    task_id="t_1234abcd",
                    lane=lane,
                    client=client,
                    journal_path=Path(tmp) / "journal.json",
                )

    def test_arbitrary_state_names_are_rejected(self):
        for state in ("Duplicate", "Completed", "Archived", "done", "Review Canary"):
            with self.subTest(state=state), self.assertRaises(lane.ContractError):
                lane.validate_command(standard_state_command(state))

    def test_source_rejects_approval_field_on_state_changes(self):
        valid = approval_reference()
        cases = (
            valid,
            {**valid, "approved": True},
            {**valid, "expires_at": "not-a-timestamp"},
        )
        for approval_value in cases:
            with self.subTest(approval=approval_value), self.assertRaises(route.RouteError):
                route.parse_linear_request(
                    {
                        "operation": "change_state",
                        "identifier": "SIS-102",
                        "state": "Done",
                        "approval": approval_value,
                    },
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )


if __name__ == "__main__":
    unittest.main()
