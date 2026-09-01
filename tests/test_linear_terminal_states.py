import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar
from unittest import mock

from plugins.linear_source_route import route
from plugins.project_manager_linear import approval, execute_claimed_task, lane
from scripts import linear_owner_approval as owner_approval
from tests.test_linear_command_lane import FakeClient, command, owner_policy
from tests.test_linear_source_route import approval_reference, uuid_factory

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class TerminalClient(FakeClient):
    TERMINAL_TYPES: ClassVar[dict[str, str]] = {
        "Done": "completed",
        "Canceled": "canceled",
        "Duplicate": "duplicate",
    }

    def list_states(self, team_id):
        return [
            *super().list_states(team_id),
            *[
                {"id": f"state-{name}", "name": name, "type": state_type}
                for name, state_type in self.TERMINAL_TYPES.items()
            ],
        ]

    def update_issue_state(self, issue_id, state_id):
        super().update_issue_state(issue_id, state_id)
        name = state_id.removeprefix("state-")
        self.current["state"]["type"] = self.TERMINAL_TYPES.get(name, "started")


def owner_terminal_command(state="Done", *, key=None):
    raw = command(
        "change_state",
        {"state": state},
        key=key or f"linear:SIS-59:owner-terminal:{state.lower()}",
    )
    raw["policy"] = owner_policy()
    return raw


def bind_owner_command(raw, before):
    bound = copy.deepcopy(raw)
    exact_intent = {
        "operation": bound["operation"],
        "target": bound["target"],
        "change": bound["change"],
    }
    before_hash = owner_approval.canonical_sha256(before)
    bound["policy"]["approval"]["intent_hash"] = owner_approval.canonical_sha256(
        exact_intent
    )
    bound["policy"]["approval"]["before_state_hash"] = before_hash
    verified = approval.VerifiedOwnerApproval(
        {},
        exact_intent,
        before_hash,
        bound["policy"]["approval"]["checksum"],
        _marker=approval._VERIFIED_MARKER,
    )
    return bound, verified


class OwnerApprovedTerminalStateTests(unittest.TestCase):
    def test_done_through_public_claimed_task_is_minimal_verified_and_replay_safe(self):
        client = TerminalClient()
        raw = owner_terminal_command("Done")
        planned = lane.execute_command(client, raw, mode="plan")
        raw, verified = bind_owner_command(raw, planned["before"])
        unmanaged_before = copy.deepcopy(
            {key: value for key, value in client.current.items() if key != "state"}
        )

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ) as verifier:
            journal = Path(tmp) / "journal.json"
            first = execute_claimed_task(
                raw,
                task_id="t_1234abcd",
                lane=lane,
                client=client,
                journal_path=journal,
                approval_now=NOW,
                approval_lease_seconds=1,
            )
            verifier.reset_mock()
            replay = execute_claimed_task(
                raw,
                task_id="t_1234abcd",
                lane=lane,
                client=client,
                journal_path=journal,
                approval_now=NOW + timedelta(seconds=2),
                approval_lease_seconds=1,
            )

        self.assertEqual(first["result"]["result"], "applied")
        self.assertEqual(first["result"]["after"]["state"], "Done")
        self.assertEqual(client.writes, [("state", "issue-uuid", "state-Done")])
        self.assertEqual(
            {key: value for key, value in client.current.items() if key != "state"},
            unmanaged_before,
        )
        self.assertEqual(replay["result"]["result"], "no_op")
        self.assertTrue(replay["result"]["recovered"])
        verifier.assert_not_called()

    def test_canceled_source_request_emits_only_exact_owner_policy(self):
        reference = approval_reference()
        parsed = route.parse_linear_request(
            {
                "operation": "change_state",
                "identifier": "SIS-59",
                "state": "Canceled",
                "approval": reference,
            },
            source_profile="default",
            uuid_factory=uuid_factory(),
        )
        self.assertEqual(parsed.command["operation"], "change_state")
        self.assertEqual(parsed.command["change"], {"state": "Canceled"})
        self.assertEqual(
            parsed.command["policy"],
            {"mode": "owner_approved", "approval": reference},
        )

    def test_canceled_and_duplicate_apply_through_public_claimed_task(self):
        for state in ("Canceled", "Duplicate"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                client = TerminalClient()
                raw = owner_terminal_command(state)
                planned = lane.execute_command(client, raw, mode="plan")
                raw, verified = bind_owner_command(raw, planned["before"])
                with mock.patch(
                    "plugins.project_manager_linear.approval.verify_owner_approval",
                    return_value=verified,
                ):
                    result = execute_claimed_task(
                        raw,
                        task_id="t_1234abcd",
                        lane=lane,
                        client=client,
                        journal_path=Path(tmp) / "journal.json",
                        approval_now=NOW,
                    )
                self.assertEqual(result["result"]["after"]["state"], state)
                self.assertEqual(
                    client.writes,
                    [("state", "issue-uuid", f"state-{state}")],
                )

    def test_approval_plan_contract_accepts_only_three_exact_terminal_states(self):
        for state in ("Done", "Canceled", "Duplicate"):
            exact = {
                "operation": "change_state",
                "target": {"type": "issue", "identifier": "SIS-77"},
                "change": {"state": state},
            }
            planned = owner_approval.build_plan(
                exact,
                "b" * 64,
                "2026-09-01T13:00:00Z",
                now=NOW,
            )
            self.assertEqual(planned["plannedActions"], [exact])
        for state in ("In Review", "Archived", "Completed", "done"):
            with self.subTest(state=state), self.assertRaises(
                owner_approval.ContractError
            ):
                owner_approval.build_plan(
                    {
                        "operation": "change_state",
                        "target": {"type": "issue", "identifier": "SIS-77"},
                        "change": {"state": state},
                    },
                    "b" * 64,
                    "2026-09-01T13:00:00Z",
                    now=NOW,
                )

    def test_terminal_state_missing_ambiguous_or_wrong_type_fails_before_write(self):
        class StateInventoryClient(TerminalClient):
            def __init__(self, terminal_states):
                super().__init__()
                self.terminal_states = terminal_states

            def list_states(self, team_id):
                return [*FakeClient.list_states(self, team_id), *self.terminal_states]

        cases = (
            ([], "not found"),
            (
                [
                    {"id": "duplicate-a", "name": "Duplicate", "type": "canceled"},
                    {"id": "duplicate-b", "name": "Duplicate", "type": "canceled"},
                ],
                "not found",
            ),
            (
                [{"id": "duplicate", "name": "Duplicate", "type": "completed"}],
                "incompatible semantic type",
            ),
            (
                [{"id": "duplicate", "name": "Duplicate", "type": "canceled"}],
                "incompatible semantic type",
            ),
            (
                [{"id": "done", "name": "Done", "type": "canceled"}],
                "incompatible semantic type",
            ),
            (
                [{"id": "canceled", "name": "Canceled", "type": "completed"}],
                "incompatible semantic type",
            ),
        )
        for terminal_states, message in cases:
            with self.subTest(terminal_states=terminal_states), self.assertRaisesRegex(
                lane.ContractError, message
            ):
                lane.execute_command(
                    StateInventoryClient(terminal_states),
                    owner_terminal_command(terminal_states[0]["name"] if terminal_states else "Done"),
                    mode="plan",
                )

    def test_standard_terminal_policy_and_direct_lane_bypass_are_blocked(self):
        for state in ("Done", "Canceled", "Duplicate"):
            standard = command("change_state", {"state": state})
            with self.subTest(state=state), self.assertRaisesRegex(
                lane.ContractError, "owner approval required"
            ):
                lane.validate_command(standard)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            lane.ContractError, "consumed owner approval"
        ):
            lane.execute_command(
                TerminalClient(),
                owner_terminal_command("Done"),
                mode="apply",
                journal_path=Path(tmp) / "journal.json",
            )

    def test_arbitrary_or_nonterminal_owner_approval_is_rejected(self):
        for state in ("In Review", "Completed", "Archived", "done"):
            raw = command("change_state", {"state": state})
            raw["policy"] = owner_policy()
            with self.subTest(state=state), self.assertRaises(lane.ContractError):
                lane.validate_command(raw)

    def test_process_crash_after_terminal_write_recovers_without_second_write(self):
        class CrashAfterState(TerminalClient):
            crashed = False

            def update_issue_state(self, issue_id, state_id):
                super().update_issue_state(issue_id, state_id)
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process death")

        client = CrashAfterState()
        raw = owner_terminal_command("Duplicate", key="linear:SIS-59:terminal-crash")
        planned = lane.execute_command(client, raw, mode="plan")
        raw, verified = bind_owner_command(raw, planned["before"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ) as verifier:
            journal = Path(tmp) / "journal.json"
            with self.assertRaises(KeyboardInterrupt):
                execute_claimed_task(
                    raw,
                    task_id="t_1234abcd",
                    lane=lane,
                    client=client,
                    journal_path=journal,
                    approval_now=NOW,
                    approval_lease_seconds=1,
                )
            verifier.reset_mock()
            recovered = execute_claimed_task(
                raw,
                task_id="t_1234abcd",
                lane=lane,
                client=client,
                journal_path=journal,
                approval_now=NOW + timedelta(seconds=2),
                approval_lease_seconds=1,
            )
        self.assertEqual(recovered["result"]["result"], "no_op")
        self.assertEqual(client.writes, [("state", "issue-uuid", "state-Duplicate")])
        verifier.assert_not_called()

    def test_terminal_readback_state_or_unmanaged_drift_fails_closed(self):
        class ReadbackDrift(TerminalClient):
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
            raw = owner_terminal_command("Done", key=f"linear:SIS-59:readback:{field}")
            planned = lane.execute_command(client, raw, mode="plan")
            raw, verified = bind_owner_command(raw, planned["before"])
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp, mock.patch(
                "plugins.project_manager_linear.approval.verify_owner_approval",
                return_value=verified,
            ), self.assertRaisesRegex(lane.ContractError, message):
                execute_claimed_task(
                    raw,
                    task_id="t_1234abcd",
                    lane=lane,
                    client=client,
                    journal_path=Path(tmp) / "journal.json",
                    approval_now=NOW,
                )

    def test_terminal_before_state_drift_after_approval_fails_without_claim_or_write(self):
        class DriftOnLiveReplan(TerminalClient):
            def __init__(self):
                super().__init__()
                self.reads = 0

            def get_issue(self, identifier):
                self.reads += 1
                if self.reads == 3:
                    self.current["state"] = {
                        "id": "state-Todo",
                        "name": "Todo",
                        "type": "unstarted",
                    }
                return super().get_issue(identifier)

        client = DriftOnLiveReplan()
        raw = owner_terminal_command("Done", key="linear:SIS-59:terminal-drift")
        planned = lane.execute_command(client, raw, mode="plan")
        raw, verified = bind_owner_command(raw, planned["before"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ), self.assertRaisesRegex(approval.ApprovalError, "drifted"):
            execute_claimed_task(
                raw,
                task_id="t_1234abcd",
                lane=lane,
                client=client,
                journal_path=Path(tmp) / "journal.json",
                approval_now=NOW,
            )
        self.assertEqual(client.writes, [])

    def test_terminal_approval_forged_expired_or_wrong_binding_fails_closed(self):
        exact_intent = {
            "operation": "change_state",
            "target": {"type": "issue", "identifier": "SIS-77"},
            "change": {"state": "Done"},
        }
        before_hash = "b" * 64
        expires = "2026-09-01T13:00:00Z"
        plan_run = "11111111-1111-4111-8111-111111111111"
        attest_run = "22222222-2222-4222-8222-222222222222"
        planned = owner_approval.build_plan(
            exact_intent, before_hash, expires, now=NOW
        )
        attested = owner_approval.build_attestation(
            intent=exact_intent,
            before_state_hash=before_hash,
            expires_at=expires,
            plan_run_id=plan_run,
            plan_artifact_version=7,
            plan_checksum=planned["checksum"],
            plan_loader=lambda **_kwargs: planned,
            now=NOW,
        )
        policy = {
            "mode": "owner_approved",
            "approval": {
                "workflow": owner_approval.ATTEST_WORKFLOW,
                "model": owner_approval.ATTEST_MODEL,
                "run_id": attest_run,
                "artifact_version": 9,
                "checksum": attested["checksum"],
                "intent_hash": attested["intentHash"],
                "before_state_hash": before_hash,
                "expires_at": expires,
            },
        }
        history = {
            "id": attest_run,
            "workflowName": owner_approval.ATTEST_WORKFLOW,
            "status": "succeeded",
            "jobs": [
                {
                    "name": "attest",
                    "steps": [
                        {
                            "name": "approve-linear-destructive-intent",
                            "status": "succeeded",
                        }
                    ],
                }
            ],
            "inputs": {
                "intent": owner_approval.encode_intent(exact_intent),
                "beforeStateHash": before_hash,
                "expiresAt": expires,
                "planRunId": plan_run,
                "planArtifactVersion": 7,
                "planChecksum": planned["checksum"],
            },
        }

        def runner_for(payload):
            def run(argv, **_kwargs):
                value = (
                    history
                    if argv[:4] == ["swamp", "workflow", "history", "get"]
                    else {
                        "modelName": owner_approval.ATTEST_MODEL,
                        "name": "result",
                        "version": 9,
                        "ownerDefinition": {"workflowRunId": attest_run},
                        "content": {"exitCode": 0, "stdout": json.dumps(payload)},
                    }
                )
                return {"returncode": 0, "stdout": json.dumps(value), "stderr": ""}

            return run

        verified = approval.verify_owner_approval(
            policy,
            expected_intent=exact_intent,
            expected_before_state_hash=before_hash,
            runner=runner_for(attested),
            workspace=Path(__file__).parents[1],
            now=NOW,
        )
        self.assertEqual(verified["intent"], exact_intent)

        forged = copy.deepcopy(attested)
        forged["decision"] = "owner_approved_by_caller"
        wrong_policy = copy.deepcopy(policy)
        wrong_policy["approval"]["intent_hash"] = "0" * 64
        cases = (
            (policy, runner_for(forged), NOW, exact_intent),
            (policy, runner_for(attested), NOW + timedelta(hours=2), exact_intent),
            (wrong_policy, runner_for(attested), NOW, exact_intent),
            (
                policy,
                runner_for(attested),
                NOW,
                {**exact_intent, "change": {"state": "Canceled"}},
            ),
        )
        for policy_value, runner, now, expected in cases:
            with self.subTest(policy=policy_value, now=now, expected=expected), self.assertRaises(
                approval.ApprovalError
            ):
                approval.verify_owner_approval(
                    policy_value,
                    expected_intent=expected,
                    expected_before_state_hash=before_hash,
                    runner=runner,
                    workspace=Path(__file__).parents[1],
                    now=now,
                )

    def test_terminal_source_rejects_missing_extra_or_malformed_approval(self):
        valid = approval_reference()
        cases = (
            {
                "operation": "change_state",
                "identifier": "SIS-59",
                "state": "Done",
            },
            {
                "operation": "change_state",
                "identifier": "SIS-59",
                "state": "Done",
                "approval": {**valid, "approved": True},
            },
            {
                "operation": "change_state",
                "identifier": "SIS-59",
                "state": "Done",
                "approval": {**valid, "expires_at": "not-a-timestamp"},
            },
            {
                "operation": "change_state",
                "identifier": "SIS-59",
                "state": "In Review",
                "approval": valid,
            },
        )
        for request in cases:
            with self.subTest(request=request), self.assertRaises(route.RouteError):
                route.parse_linear_request(
                    request,
                    source_profile="default",
                    uuid_factory=uuid_factory(),
                )


if __name__ == "__main__":
    unittest.main()
