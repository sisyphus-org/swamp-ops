import copy
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.project_manager_linear import approval
from plugins.project_manager_linear import execute_claimed_task, execute_pm_command
from plugins.project_manager_linear import lane
from scripts import linear_owner_approval as owner_approval
from plugins.ops_broker import broker


RUN_ID = "11111111-1111-4111-8111-111111111111"
ATTEST_RUN_ID = "22222222-2222-4222-8222-222222222222"
BEFORE_HASH = "b" * 64
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
EXPIRES = "2026-08-31T13:00:00Z"


def intent():
    return {
        "operation": "update_issue",
        "target": {"type": "issue", "identifier": "SIS-77"},
        "change": {"parent_identifier": None},
    }


def plan():
    return owner_approval.build_plan(
        intent(), BEFORE_HASH, EXPIRES, now=NOW
    )


def attestation():
    result = {
        "schemaVersion": owner_approval.ATTESTATION_SCHEMA_VERSION,
        "mode": "attestation",
        "decision": "owner_approved",
        "workflow": owner_approval.ATTEST_WORKFLOW,
        "model": owner_approval.ATTEST_MODEL,
        "plan": {
            "workflow": owner_approval.PLAN_WORKFLOW,
            "model": owner_approval.PLAN_MODEL,
            "runId": RUN_ID,
            "artifactVersion": 7,
            "checksum": plan()["checksum"],
        },
        "intent": intent(),
        "intentHash": owner_approval.canonical_sha256(intent()),
        "beforeStateHash": BEFORE_HASH,
        "expiresAt": EXPIRES,
    }
    result["checksum"] = owner_approval.artifact_checksum(result)
    return result


def policy(attest=None):
    attest = attest or attestation()
    return {
        "mode": "owner_approved",
        "approval": {
            "workflow": owner_approval.ATTEST_WORKFLOW,
            "model": owner_approval.ATTEST_MODEL,
            "run_id": ATTEST_RUN_ID,
            "artifact_version": 9,
            "checksum": attest["checksum"],
            "intent_hash": attest["intentHash"],
            "before_state_hash": attest["beforeStateHash"],
            "expires_at": attest["expiresAt"],
        },
    }


def artifact(payload, *, run_id=ATTEST_RUN_ID, version=9, model=None):
    return {
        "modelName": model or owner_approval.ATTEST_MODEL,
        "name": "result",
        "version": version,
        "ownerDefinition": {"workflowRunId": run_id},
        "content": {"exitCode": 0, "stdout": json.dumps(payload)},
    }


def history(*, status="succeeded", run_id=ATTEST_RUN_ID, workflow=None):
    return {
        "id": run_id,
        "workflowName": workflow or owner_approval.ATTEST_WORKFLOW,
        "status": status,
        "jobs": [
            {
                "name": "attest",
                "steps": [
                    {
                        "name": "approve-linear-destructive-intent",
                        "status": "succeeded" if status == "succeeded" else "waiting_approval",
                    }
                ],
            }
        ],
        "inputs": {
            "intent": owner_approval.encode_intent(intent()),
            "beforeStateHash": BEFORE_HASH,
            "expiresAt": EXPIRES,
            "planRunId": RUN_ID,
            "planArtifactVersion": 7,
            "planChecksum": plan()["checksum"],
        },
    }


class PlanContractTests(unittest.TestCase):
    def test_plan_contract_accepts_only_exact_relation_removal_and_replacement_intents(self):
        intents = (
            {
                "operation": "remove_issue_relation",
                "target": {"type": "issue", "identifier": "SIS-77"},
                "change": {
                    "related_identifier": "SIS-94",
                    "relation_type": "blocked_by",
                },
            },
            {
                "operation": "replace_issue_relation",
                "target": {"type": "issue", "identifier": "SIS-77"},
                "change": {
                    "old_related_identifier": "SIS-94",
                    "old_relation_type": "blocked_by",
                    "new_related_identifier": "SIS-95",
                    "new_relation_type": "related",
                },
            },
        )
        for exact_intent in intents:
            with self.subTest(operation=exact_intent["operation"]):
                result = owner_approval.build_plan(
                    exact_intent, BEFORE_HASH, EXPIRES, now=NOW
                )
                self.assertEqual(result["intent"], exact_intent)
                self.assertEqual(result["plannedActions"], [exact_intent])

        forged = copy.deepcopy(intents[1])
        forged["change"]["relation_id"] = "raw-id"
        wrong_type = copy.deepcopy(intents[0])
        wrong_type["change"]["relation_type"] = "duplicate"
        self_relation = copy.deepcopy(intents[0])
        self_relation["change"]["related_identifier"] = "SIS-77"
        for invalid in (forged, wrong_type, self_relation):
            with self.subTest(invalid=invalid), self.assertRaises(
                owner_approval.ContractError
            ):
                owner_approval.build_plan(invalid, BEFORE_HASH, EXPIRES, now=NOW)

    def test_plan_is_deterministic_read_only_expiring_and_checksum_bound(self):
        first = plan()
        second = plan()
        self.assertEqual(first, second)
        self.assertEqual(first["schemaVersion"], owner_approval.PLAN_SCHEMA_VERSION)
        self.assertEqual(first["mode"], "plan")
        self.assertTrue(first["readOnly"])
        self.assertEqual(first["intent"], intent())
        self.assertEqual(first["intentHash"], owner_approval.canonical_sha256(intent()))
        self.assertEqual(first["beforeStateHash"], BEFORE_HASH)
        self.assertEqual(first["expiresAt"], EXPIRES)
        self.assertEqual(first["plannedActions"], [intent()])
        self.assertTrue(owner_approval.verify_artifact_checksum(first))

    def test_plan_rejects_expired_unbounded_and_not_yet_supported_shapes(self):
        cases = [
            {**intent(), "bulk": True},
            {**intent(), "target": {"type": "issue", "identifier": ["SIS-1"]}},
            {**intent(), "operation": "terminal"},
            {
                "operation": "update_issue",
                "target": {"type": "issue", "identifier": "SIS-77"},
                "change": {"parent_identifier": ["SIS-1", "SIS-2"]},
            },
            {**intent(), "operation": "archive_issue", "change": {}},
            {**intent(), "operation": "delete_issue", "change": {}},
            {
                **intent(),
                "change": {"parent_identifier": None, "description": "forbidden"},
            },
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(owner_approval.ContractError):
                owner_approval.build_plan(value, BEFORE_HASH, EXPIRES, now=NOW)
        with self.assertRaisesRegex(owner_approval.ContractError, "expired"):
            owner_approval.build_plan(
                intent(), BEFORE_HASH, "2026-08-31T11:59:59Z", now=NOW
            )

    def test_attestation_reloads_exact_plan_binding(self):
        expected = plan()
        result = owner_approval.build_attestation(
            intent=intent(),
            before_state_hash=BEFORE_HASH,
            expires_at=EXPIRES,
            plan_run_id=RUN_ID,
            plan_artifact_version=7,
            plan_checksum=expected["checksum"],
            plan_loader=lambda **_kwargs: expected,
            now=NOW,
        )
        self.assertEqual(result["decision"], "owner_approved")
        self.assertEqual(result["plan"]["runId"], RUN_ID)
        self.assertTrue(owner_approval.verify_artifact_checksum(result))


class PolicyValidationTests(unittest.TestCase):
    def base_command(self):
        return {
            "schema_version": "linear-command.v2",
            "command_id": "33333333-3333-4333-8333-333333333333",
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "idempotency_key": "linear:v2:" + "a" * 32,
            "source_profile": "swe",
            "operation": "read_issue",
            "target": {"type": "issue", "identifier": "SIS-77"},
            "change": {},
            "policy": {"mode": "standard"},
        }

    def test_standard_policy_remains_backward_compatible(self):
        self.assertEqual(lane.validate_command(self.base_command())["policy"], {"mode": "standard"})

    def test_forged_boolean_and_raw_reference_surfaces_fail_closed(self):
        for forged in (
            {"mode": "owner_approved", "approved": True},
            {"mode": "standard", "owner_approved": True},
            {"mode": "owner_approved", "manifest_id": "raw-id"},
            {"mode": "owner_approved", "artifact_path": "/tmp/approval.json"},
            {"mode": "owner_approved", "source_profile": "default"},
        ):
            command = self.base_command()
            command["policy"] = forged
            with self.subTest(forged=forged), self.assertRaises(lane.ContractError):
                lane.validate_command(command)

    def test_exact_owner_approved_reference_is_structurally_accepted_only(self):
        command = self.base_command()
        command["operation"] = "update_issue"
        command["change"] = {"parent_identifier": None}
        command["policy"] = policy()
        self.assertEqual(lane.validate_command(command)["policy"], policy())
        for operation in ("archive_issue", "delete_issue", "delete_issue_relation", "reparent_issue"):
            self.assertNotIn(operation, lane.OPERATIONS)
            destructive = self.base_command()
            destructive["operation"] = operation
            destructive["policy"] = policy()
            with self.assertRaisesRegex(lane.ContractError, "operation is not allowed"):
                lane.validate_command(destructive)


class PmExecutionBoundaryTests(unittest.TestCase):
    @staticmethod
    def bind_owner_command(raw, before):
        bound = copy.deepcopy(raw)
        exact_intent = {
            "operation": bound["operation"],
            "target": bound["target"],
            "change": bound["change"],
        }
        before_hash = owner_approval.canonical_sha256(before)
        bound["policy"]["approval"]["intent_hash"] = (
            owner_approval.canonical_sha256(exact_intent)
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

    class FakeLane:
        def __init__(self, plans=None):
            self.modes = []
            self.mutations = 0
            self.authorizations = []
            self.plans = list(plans or [self.plan_result(), self.plan_result()])

        @staticmethod
        def plan_result():
            return {
                "schema_version": "linear-result.v2",
                "operation": "update_issue",
                "mode": "plan",
                "target": {
                    "type": "issue",
                    "identifier": "SIS-77",
                    "url": "https://linear.app/example/SIS-77",
                },
                "result": "planned",
                "before": {"identifier": "SIS-77", "parent_identifier": "SIS-1"},
                "plan": [{"action": "update_issue", "fields": ["parent_identifier"]}],
                "verified": False,
            }

        def validate_command(self, command):
            return command

        def execute_command(
            self, _client, _command, *, mode, journal_path,
            owner_approval_authorization=None,
        ):
            self.modes.append(mode)
            if mode == "plan":
                return self.plans.pop(0)
            self.mutations += 1
            self.authorizations.append(owner_approval_authorization)
            return {
                "schema_version": "linear-result.v2",
                "operation": "update_issue",
                "mode": "apply",
                "target": {"type": "issue", "identifier": "SIS-77"},
                "result": "applied",
                "before": {"identifier": "SIS-77", "parent_identifier": "SIS-1"},
                "after": {"identifier": "SIS-77", "parent_identifier": None},
                "plan": [{"action": "update_issue", "fields": ["parent_identifier"]}],
                "verified": True,
            }

    def test_pm_verifies_exact_attestation_after_plan_and_before_apply(self):
        command = {
            "schema_version": "linear-command.v2",
            "command_id": "33333333-3333-4333-8333-333333333333",
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "idempotency_key": "linear:v2:" + "a" * 32,
            "source_profile": "swe",
            **intent(),
            "policy": policy(),
        }
        fake_lane = self.FakeLane()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            side_effect=approval.ApprovalError("wrong before-state"),
        ) as verifier:
            with self.assertRaisesRegex(approval.ApprovalError, "before-state"):
                execute_pm_command(
                    command,
                    lane=fake_lane,
                    client=object(),
                    journal_path=Path(tmp) / "journal.json",
                )
        self.assertEqual(fake_lane.modes, ["plan"])
        verifier.assert_called_once()
        self.assertEqual(verifier.call_args.kwargs["expected_intent"], intent())
        self.assertEqual(
            verifier.call_args.kwargs["expected_before_state_hash"],
            owner_approval.canonical_sha256(
                {"identifier": "SIS-77", "parent_identifier": "SIS-1"}
            ),
        )

    def test_relation_owner_gate_binds_exact_relation_plan_target_and_inventory_hash(self):
        relation_plan = self.FakeLane.plan_result()
        relation_plan.update(
            {
                "operation": "remove_issue_relation",
                "target": {
                    "type": "issue_relation",
                    "identifier": "SIS-77",
                    "related_identifier": "SIS-94",
                    "relation_type": "blocked_by",
                },
                "before": {
                    "identifier": "SIS-77",
                    "inventory": [
                        {
                            "id": "internal-relation-id",
                            "type": "blocks",
                            "issue": {"id": "issue-94", "identifier": "SIS-94"},
                            "relatedIssue": {
                                "id": "issue-77",
                                "identifier": "SIS-77",
                            },
                        }
                    ],
                },
                "plan": [
                    {
                        "action": "remove_issue_relation",
                        "identifier": "SIS-77",
                        "related_identifier": "SIS-94",
                        "relation_type": "blocked_by",
                    }
                ],
            }
        )
        fake_lane = self.FakeLane([relation_plan, copy.deepcopy(relation_plan)])
        command = {
            "schema_version": "linear-command.v2",
            "command_id": "33333333-3333-4333-8333-333333333333",
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "idempotency_key": "linear:v2:" + "d" * 32,
            "source_profile": "swe",
            "operation": "remove_issue_relation",
            "target": {"type": "issue", "identifier": "SIS-77"},
            "change": {
                "related_identifier": "SIS-94",
                "relation_type": "blocked_by",
            },
            "policy": policy(),
        }
        verified = object()
        authorization = object()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ) as verify, mock.patch(
            "plugins.project_manager_linear.approval.consume_owner_approval",
            return_value=authorization,
        ):
            execute_pm_command(
                command,
                lane=fake_lane,
                client=object(),
                journal_path=Path(tmp) / "journal.json",
            )
        self.assertEqual(
            verify.call_args.kwargs["expected_before_state_hash"],
            owner_approval.canonical_sha256(relation_plan["before"]),
        )
        self.assertEqual(fake_lane.modes, ["plan", "plan", "apply"])

    def test_completed_relation_recovery_replay_returns_verified_noop_without_reconsuming_approval(self):
        recovered = self.FakeLane.plan_result()
        recovered.update(
            {
                "operation": "remove_issue_relation",
                "target": {
                    "type": "issue_relation",
                    "identifier": "SIS-77",
                    "related_identifier": "SIS-94",
                    "relation_type": "blocked_by",
                },
                "result": "no_op",
                "before": {"identifier": "SIS-77", "inventory": []},
                "after": {
                    "identifier": "SIS-77",
                    "related_identifier": "SIS-94",
                    "relation_type": "blocked_by",
                    "present": False,
                },
                "plan": [],
                "no_op": True,
                "verified": True,
                "recovered": True,
            }
        )
        fake_lane = self.FakeLane([recovered])
        command = {
            "schema_version": "linear-command.v2",
            "command_id": "33333333-3333-4333-8333-333333333333",
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "idempotency_key": "linear:v2:" + "e" * 32,
            "source_profile": "swe",
            "operation": "remove_issue_relation",
            "target": {"type": "issue", "identifier": "SIS-77"},
            "change": {
                "related_identifier": "SIS-94",
                "relation_type": "blocked_by",
            },
            "policy": policy(),
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval"
        ) as verify, mock.patch(
            "plugins.project_manager_linear.approval.consume_owner_approval"
        ) as consume:
            result = execute_pm_command(
                command,
                lane=fake_lane,
                client=object(),
                journal_path=Path(tmp) / "journal.json",
            )
        self.assertEqual(result, {"plan": recovered, "result": recovered})
        self.assertEqual(fake_lane.modes, ["plan"])
        verify.assert_not_called()
        consume.assert_not_called()

    def test_live_before_state_drift_after_verification_fails_without_consuming_or_mutating(self):
        original = self.FakeLane.plan_result()
        drifted = copy.deepcopy(original)
        drifted["before"]["parent_identifier"] = "SIS-2"
        fake_lane = self.FakeLane([original, drifted])
        command = {
            "schema_version": "linear-command.v2",
            "command_id": "33333333-3333-4333-8333-333333333333",
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "idempotency_key": "linear:v2:" + "a" * 32,
            "source_profile": "swe",
            **intent(),
            "policy": policy(),
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=object(),
        ), mock.patch(
            "plugins.project_manager_linear.approval.consume_owner_approval"
        ) as consume:
            with self.assertRaisesRegex(approval.ApprovalError, "drift"):
                execute_pm_command(
                    command, lane=fake_lane, client=object(),
                    journal_path=Path(tmp) / "journal.json",
                )
        self.assertEqual(fake_lane.modes, ["plan", "plan"])
        self.assertEqual(fake_lane.mutations, 0)
        consume.assert_not_called()

    def test_live_operation_plan_or_target_drift_fails_without_consuming_or_mutating(self):
        for field, value in (
            ("operation", "delete_issue"),
            ("target", {"type": "issue", "identifier": "SIS-78"}),
            ("plan", [{"action": "delete_issue", "identifier": "SIS-77"}]),
        ):
            with self.subTest(field=field):
                original = self.FakeLane.plan_result()
                drifted = copy.deepcopy(original)
                drifted[field] = value
                fake_lane = self.FakeLane([original, drifted])
                command = {
                    "schema_version": "linear-command.v2",
                    "command_id": "33333333-3333-4333-8333-333333333333",
                    "correlation_id": "44444444-4444-4444-8444-444444444444",
                    "idempotency_key": "linear:v2:" + "a" * 32,
                    "source_profile": "swe",
                    **intent(),
                    "policy": policy(),
                }
                with tempfile.TemporaryDirectory() as tmp, mock.patch(
                    "plugins.project_manager_linear.approval.verify_owner_approval",
                    return_value=object(),
                ), mock.patch(
                    "plugins.project_manager_linear.approval.consume_owner_approval"
                ) as consume:
                    with self.assertRaisesRegex(approval.ApprovalError, "drift"):
                        execute_pm_command(
                            command, lane=fake_lane, client=object(),
                            journal_path=Path(tmp) / "journal.json",
                        )
                self.assertEqual(fake_lane.mutations, 0)
                consume.assert_not_called()

    def test_gate_consumes_after_matching_live_replan_and_passes_authorization_to_apply(self):
        fake_lane = self.FakeLane()
        command = {
            "schema_version": "linear-command.v2",
            "command_id": "33333333-3333-4333-8333-333333333333",
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "idempotency_key": "linear:v2:" + "a" * 32,
            "source_profile": "swe",
            **intent(),
            "policy": policy(),
        }
        verified = object()
        authorization = object()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ), mock.patch(
            "plugins.project_manager_linear.approval.consume_owner_approval",
            return_value=authorization,
        ) as consume:
            execute_pm_command(
                command, lane=fake_lane, client=object(),
                journal_path=Path(tmp) / "journal.json",
            )
        self.assertEqual(fake_lane.modes, ["plan", "plan", "apply"])
        self.assertEqual(fake_lane.mutations, 1)
        self.assertEqual(fake_lane.authorizations, [authorization])
        consume.assert_called_once()

    def test_public_claimed_task_recovers_relation_delete_after_process_crash(self):
        from tests.test_linear_command_lane import FakeClient, relation_change_command

        class CrashAfterDelete(FakeClient):
            crashed = False

            def delete_issue_relation(self, relation_id):
                super().delete_issue_relation(relation_id)
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process death")

        client = CrashAfterDelete()
        client.issue_relations = [
            {
                "id": "relation-old",
                "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        raw = relation_change_command("remove_issue_relation")
        initial = lane.execute_command(client, raw, mode="plan")
        raw, verified = self.bind_owner_command(raw, initial["before"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ) as verify:
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
            verify.reset_mock()
            for field, value in (
                ("command_id", "55555555-5555-4555-8555-555555555555"),
                ("correlation_id", "66666666-6666-4666-8666-666666666666"),
                ("source_profile", "default"),
            ):
                different = copy.deepcopy(raw)
                different[field] = value
                with self.subTest(field=field), self.assertRaisesRegex(
                    lane.ContractError, "conflict"
                ):
                    execute_claimed_task(
                        different, task_id="t_1234abcd", lane=lane,
                        client=client, journal_path=journal,
                        approval_now=NOW + timedelta(seconds=2),
                        approval_lease_seconds=1,
                    )
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
        self.assertEqual(
            [item[0] for item in client.writes], ["delete_issue_relation"]
        )
        verify.assert_not_called()

    def test_public_claimed_task_recovers_reparent_write_after_process_crash(self):
        from tests.test_linear_command_lane import FakeClient, command as lane_command

        class CrashAfterReparent(FakeClient):
            crashed = False

            def update_issue_fields(self, issue_id, **fields):
                super().update_issue_fields(issue_id, **fields)
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process death")

        client = CrashAfterReparent()
        raw = lane_command(
            "update_issue",
            {"parent_identifier": None},
            key="linear:SIS-59:owner-parent:public-crash",
        )
        raw["policy"] = policy()
        initial = lane.execute_command(client, raw, mode="plan")
        raw, verified = self.bind_owner_command(raw, initial["before"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ) as verify:
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
            verify.reset_mock()
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
        self.assertEqual(len([item for item in client.writes if item[0] == "fields"]), 1)
        verify.assert_not_called()

    def test_public_claimed_task_recovers_after_relation_create_without_duplicate(self):
        from tests.test_linear_command_lane import FakeClient, relation_change_command

        class CrashAfterCreate(FakeClient):
            crashed = False

            def create_issue_relation(self, **kwargs):
                super().create_issue_relation(**kwargs)
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process death")

        client = CrashAfterCreate()
        client.issue_relations = [
            {
                "id": "relation-old", "type": "blocks",
                "issue": {"id": "parent-uuid", "identifier": "SIS-56"},
                "relatedIssue": {"id": "issue-uuid", "identifier": "SIS-59"},
            }
        ]
        raw = relation_change_command("replace_issue_relation")
        raw["change"] = {
            "old_related_identifier": "SIS-56",
            "old_relation_type": "blocked_by",
            "new_related_identifier": "SIS-56",
            "new_relation_type": "related",
        }
        initial = lane.execute_command(client, raw, mode="plan")
        raw, verified = self.bind_owner_command(raw, initial["before"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ) as verify:
            journal = Path(tmp) / "journal.json"
            with self.assertRaises(KeyboardInterrupt):
                execute_claimed_task(
                    raw, task_id="t_1234abcd", lane=lane, client=client,
                    journal_path=journal, approval_now=NOW,
                    approval_lease_seconds=1,
                )
            verify.reset_mock()
            recovered = execute_claimed_task(
                raw, task_id="t_1234abcd", lane=lane, client=client,
                journal_path=journal, approval_now=NOW + timedelta(seconds=2),
                approval_lease_seconds=1,
            )
        self.assertEqual(recovered["result"]["result"], "applied")
        self.assertEqual(
            [item[0] for item in client.writes],
            ["create_issue_relation", "delete_issue_relation"],
        )
        self.assertEqual(len(client.issue_relations), 1)
        verify.assert_not_called()

    def test_public_claimed_task_concurrent_apply_completed_replay_and_evidenceless_stale(self):
        from tests.test_linear_command_lane import FakeClient, command as lane_command

        def owner_reparent(client, key):
            raw = lane_command("update_issue", {"parent_identifier": None}, key=key)
            raw["policy"] = policy()
            planned = lane.execute_command(client, raw, mode="plan")
            return self.bind_owner_command(raw, planned["before"])

        class PausingReparent(FakeClient):
            def __init__(self):
                super().__init__()
                self.mutated = threading.Event()
                self.release = threading.Event()

            def update_issue_fields(self, issue_id, **fields):
                super().update_issue_fields(issue_id, **fields)
                self.mutated.set()
                self.release.wait(5)

        concurrent_client = PausingReparent()
        raw, verified = owner_reparent(
            concurrent_client, "linear:SIS-59:owner-parent:concurrent"
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ):
            journal = Path(tmp) / "journal.json"
            first, first_error = [], []

            def run_first():
                try:
                    first.append(execute_claimed_task(
                        raw, task_id="t_1234abcd", lane=lane,
                        client=concurrent_client, journal_path=journal,
                        approval_now=NOW, approval_lease_seconds=30,
                    ))
                except BaseException as exc:
                    first_error.append(exc)

            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(concurrent_client.mutated.wait(5))
            with self.assertRaisesRegex(approval.ApprovalError, "already claimed"):
                execute_claimed_task(
                    raw, task_id="t_1234abcd", lane=lane,
                    client=concurrent_client, journal_path=journal,
                    approval_now=NOW, approval_lease_seconds=30,
                )
            concurrent_client.release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(first_error, [])
            self.assertEqual(len(first), 1)
            writes_before_replay = list(concurrent_client.writes)
            replay = execute_claimed_task(
                raw, task_id="t_1234abcd", lane=lane,
                client=concurrent_client, journal_path=journal,
                approval_now=NOW + timedelta(seconds=31),
                approval_lease_seconds=30,
            )
            self.assertEqual(replay["result"]["result"], "no_op")
            self.assertEqual(concurrent_client.writes, writes_before_replay)

        class CrashBeforeWrite(FakeClient):
            def update_issue_fields(self, issue_id, **fields):
                del issue_id, fields
                raise KeyboardInterrupt("simulated death before mutation")

        no_evidence_client = CrashBeforeWrite()
        raw, verified = owner_reparent(
            no_evidence_client, "linear:SIS-59:owner-parent:no-evidence"
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ):
            journal = Path(tmp) / "journal.json"
            with self.assertRaises(KeyboardInterrupt):
                execute_claimed_task(
                    raw, task_id="t_1234abcd", lane=lane,
                    client=no_evidence_client, journal_path=journal,
                    approval_now=NOW, approval_lease_seconds=1,
                )
            with self.assertRaisesRegex(approval.ApprovalError, "already claimed"):
                execute_claimed_task(
                    raw, task_id="t_1234abcd", lane=lane,
                    client=no_evidence_client, journal_path=journal,
                    approval_now=NOW + timedelta(seconds=2),
                    approval_lease_seconds=1,
                )


class PmVerifierTests(unittest.TestCase):
    @staticmethod
    def owner_command():
        return {
            "schema_version": "linear-command.v2",
            "command_id": "33333333-3333-4333-8333-333333333333",
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "idempotency_key": "linear:v2:" + "a" * 32,
            "source_profile": "swe",
            **intent(),
            "policy": policy(),
        }

    @staticmethod
    def recovery_evidence(command_value, *, phase="prepared"):
        return {
            "schema_version": "linear-owner-recovery.v1",
            "approval_checksum": command_value["policy"]["approval"]["checksum"],
            "intent_hash": command_value["policy"]["approval"]["intent_hash"],
            "command_hash": approval.command_binding_hash(command_value),
            "before_state_hash": BEFORE_HASH,
            "after_state_hash": "d" * 64,
            "phase": phase,
        }

    def runner(self, attest=None, *, history_payload=None, artifact_payload=None):
        attest = attest or attestation()
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            payload = (
                history_payload or history()
                if argv[:4] == ["swamp", "workflow", "history", "get"]
                else artifact_payload or artifact(attest)
            )
            return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

        return run, calls

    def verify(self, *, policy_value=None, attest=None, history_payload=None, now=NOW):
        attest = attest or attestation()
        runner, calls = self.runner(attest, history_payload=history_payload)
        with tempfile.TemporaryDirectory() as tmp:
            result = approval.verify_owner_approval(
                policy_value or policy(attest),
                expected_intent=intent(),
                expected_before_state_hash=BEFORE_HASH,
                runner=runner,
                workspace=ROOT,
                now=now,
            )
        return result, calls

    def test_exact_succeeded_attestation_is_accepted_from_fixed_model_and_workflow(self):
        result, calls = self.verify()
        self.assertEqual(result["decision"], "owner_approved")
        self.assertEqual(calls[0], ["swamp", "workflow", "history", "get", ATTEST_RUN_ID, "--json"])
        self.assertEqual(
            calls[1],
            [
                "swamp", "data", "get", owner_approval.ATTEST_MODEL, "result",
                "--version", "9", "--json",
            ],
        )

    def test_wrong_run_version_checksum_intent_before_state_or_expiry_fails_closed(self):
        mutations = []
        wrong_workflow = policy(); wrong_workflow["approval"]["workflow"] = "attacker-workflow"; mutations.append(wrong_workflow)
        wrong_model = policy(); wrong_model["approval"]["model"] = "attacker-model"; mutations.append(wrong_model)
        wrong_run = policy(); wrong_run["approval"]["run_id"] = RUN_ID; mutations.append(wrong_run)
        wrong_version = policy(); wrong_version["approval"]["artifact_version"] = 8; mutations.append(wrong_version)
        wrong_checksum = policy(); wrong_checksum["approval"]["checksum"] = "0" * 64; mutations.append(wrong_checksum)
        wrong_intent = policy(); wrong_intent["approval"]["intent_hash"] = "1" * 64; mutations.append(wrong_intent)
        wrong_before = policy(); wrong_before["approval"]["before_state_hash"] = "2" * 64; mutations.append(wrong_before)
        wrong_expiry = policy(); wrong_expiry["approval"]["expires_at"] = "2026-08-31T12:30:00Z"; mutations.append(wrong_expiry)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(approval.ApprovalError):
                self.verify(policy_value=value)

    def test_expired_unapproved_or_suspended_attestation_fails_closed(self):
        with self.assertRaisesRegex(approval.ApprovalError, "expired"):
            self.verify(now=NOW + timedelta(hours=2))
        for status in ("suspended", "running", "failed"):
            with self.subTest(status=status), self.assertRaises(approval.ApprovalError):
                self.verify(history_payload=history(status=status))

    def test_verification_is_non_consuming_and_concurrent_replay_consumes_once(self):
        attest = attestation()
        runner, _calls = self.runner(attest)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "approval-journal.json"
            verified = approval.verify_owner_approval(
                policy(attest), expected_intent=intent(),
                expected_before_state_hash=BEFORE_HASH,
                runner=runner, workspace=ROOT, now=NOW,
            )
            # Re-verifying the exact immutable artifact does not spend approval.
            approval.verify_owner_approval(
                policy(attest), expected_intent=intent(),
                expected_before_state_hash=BEFORE_HASH,
                runner=runner, workspace=ROOT, now=NOW,
            )
            barrier = threading.Barrier(2)
            successes = []
            failures = []

            def consume():
                barrier.wait()
                try:
                    successes.append(
                        approval.consume_owner_approval(verified, journal_path=journal)
                    )
                except approval.ApprovalError as exc:
                    failures.append(str(exc))

            threads = [threading.Thread(target=consume) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(successes), 1)
            self.assertEqual(failures, ["owner approval was already consumed"])

    def test_durable_exact_command_claim_allows_only_one_concurrent_apply(self):
        verified = approval.VerifiedOwnerApproval(
            attestation(), intent(), BEFORE_HASH, attestation()["checksum"],
            _marker=approval._VERIFIED_MARKER,
        )
        command_value = self.owner_command()
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "approval-journal.json"
            barrier = threading.Barrier(2)
            successes = []
            failures = []

            def claim():
                barrier.wait()
                try:
                    successes.append(
                        approval.claim_owner_approval(
                            verified,
                            command=command_value,
                            task_id="t_1234abcd",
                            journal_path=journal,
                            now=NOW,
                            lease_seconds=30,
                        )
                    )
                except approval.ApprovalError as exc:
                    failures.append(str(exc))

            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(successes), 1)
        self.assertEqual(failures, ["owner approval apply is already claimed"])

    def test_stale_claim_requires_exact_recovery_evidence_and_command_identity(self):
        verified = approval.VerifiedOwnerApproval(
            attestation(), intent(), BEFORE_HASH, attestation()["checksum"],
            _marker=approval._VERIFIED_MARKER,
        )
        command_value = self.owner_command()
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "approval-journal.json"
            approval.claim_owner_approval(
                verified,
                command=command_value,
                task_id="t_1234abcd",
                journal_path=journal,
                now=NOW,
                lease_seconds=30,
            )
            stale = NOW + timedelta(seconds=31)
            with self.assertRaisesRegex(approval.ApprovalError, "recovery evidence"):
                approval.recover_owner_approval(
                    command_value["policy"],
                    command=command_value,
                    task_id="t_1234abcd",
                    journal_path=journal,
                    recovery_evidence=None,
                    now=stale,
                )

            for field, replacement in (
                ("command_id", "55555555-5555-4555-8555-555555555555"),
                ("correlation_id", "66666666-6666-4666-8666-666666666666"),
                ("idempotency_key", "linear:v2:" + "f" * 32),
                ("source_profile", "default"),
            ):
                different = copy.deepcopy(command_value)
                different[field] = replacement
                with self.subTest(field=field), self.assertRaisesRegex(
                    approval.ApprovalError, "binding"
                ):
                    approval.recover_owner_approval(
                        different["policy"],
                        command=different,
                        task_id="t_1234abcd",
                        journal_path=journal,
                        recovery_evidence=self.recovery_evidence(command_value),
                        now=stale,
                    )

            changed_intent = copy.deepcopy(command_value)
            changed_intent["change"] = {"parent_identifier": "SIS-2"}
            with self.assertRaisesRegex(approval.ApprovalError, "intent binding"):
                approval.recover_owner_approval(
                    changed_intent["policy"],
                    command=changed_intent,
                    task_id="t_1234abcd",
                    journal_path=journal,
                    recovery_evidence=self.recovery_evidence(command_value),
                    now=stale,
                )
            with self.assertRaisesRegex(approval.ApprovalError, "binding"):
                approval.recover_owner_approval(
                    command_value["policy"],
                    command=command_value,
                    task_id="t_deadbeef",
                    journal_path=journal,
                    recovery_evidence=self.recovery_evidence(command_value),
                    now=stale,
                )

            recovered = approval.recover_owner_approval(
                command_value["policy"],
                command=command_value,
                task_id="t_1234abcd",
                journal_path=journal,
                recovery_evidence=self.recovery_evidence(command_value),
                now=stale,
            )
            approval.complete_owner_approval(
                recovered,
                journal_path=journal,
                recovery_evidence=self.recovery_evidence(
                    command_value, phase="completed"
                ),
            )
            self.assertTrue(
                approval.completed_owner_approval_matches(
                    command_value["policy"],
                    command=command_value,
                    task_id="t_1234abcd",
                    journal_path=journal,
                    recovery_evidence=self.recovery_evidence(
                        command_value, phase="completed"
                    ),
                )
            )
            with self.assertRaisesRegex(approval.ApprovalError, "completed"):
                approval.recover_owner_approval(
                    command_value["policy"],
                    command=command_value,
                    task_id="t_1234abcd",
                    journal_path=journal,
                    recovery_evidence=self.recovery_evidence(command_value),
                    now=stale + timedelta(seconds=31),
                )

    def test_lane_apply_rejects_owner_policy_without_consumed_gate(self):
        command = {
            "schema_version": "linear-command.v2",
            "command_id": "33333333-3333-4333-8333-333333333333",
            "correlation_id": "44444444-4444-4444-8444-444444444444",
            "idempotency_key": "linear:v2:" + "a" * 32,
            "source_profile": "swe",
            "operation": "update_issue",
            "target": {"type": "issue", "identifier": "SIS-77"},
            "change": {"parent_identifier": None},
            "policy": policy(),
        }
        client = mock.Mock()
        with self.assertRaisesRegex(lane.ContractError, "consumed owner approval"):
            lane.execute_command(client, command, mode="apply")
        self.assertEqual(client.method_calls, [])


class BrokerFoundationTests(unittest.TestCase):
    def broker_policy(self):
        return json.loads((ROOT / "plugins" / "ops_broker" / "policy.json").read_text())

    def test_plan_start_and_approve_build_only_fixed_typed_swamp_argv(self):
        encoded = owner_approval.encode_intent(intent())
        policy_value = self.broker_policy()
        plan_argv = broker.build_command(
            "swamp.plan_linear_destructive_owner_approval",
            {"intent": intent(), "before_state_hash": BEFORE_HASH, "expires_at": EXPIRES},
            policy_value,
        )
        self.assertEqual(
            plan_argv,
            [
                "swamp", "workflow", "run", owner_approval.PLAN_WORKFLOW,
                "--input", f"intent={encoded}",
                "--input", f"beforeStateHash={BEFORE_HASH}",
                "--input", f"expiresAt={EXPIRES}", "--json",
            ],
        )
        start_argv = broker.build_command(
            "swamp.start_linear_destructive_owner_approval_attest",
            {
                "intent": intent(), "before_state_hash": BEFORE_HASH,
                "expires_at": EXPIRES, "plan_run_id": RUN_ID,
                "plan_checksum": plan()["checksum"], "plan_artifact_version": 7,
            },
            policy_value,
        )
        self.assertEqual(start_argv[:4], ["swamp", "workflow", "run", owner_approval.ATTEST_WORKFLOW])
        self.assertNotIn(json.dumps(intent()), start_argv)
        approve_argv = broker.build_command(
            "swamp.approve_linear_destructive_owner_approval_attest",
            {"attest_run_id": ATTEST_RUN_ID},
            policy_value,
        )
        self.assertEqual(
            approve_argv,
            [
                "swamp", "workflow", "approve", owner_approval.ATTEST_WORKFLOW,
                "approve-linear-destructive-intent", "--run", ATTEST_RUN_ID, "--json",
            ],
        )

    def test_broker_rejects_boolean_path_shell_source_and_raw_manifest_arguments(self):
        base = {"intent": intent(), "before_state_hash": BEFORE_HASH, "expires_at": EXPIRES}
        for field, value in (
            ("approved", True),
            ("path", "/tmp/approval.json"),
            ("command", "env"),
            ("source_profile", "default"),
            ("manifest_id", "raw-id"),
        ):
            with self.subTest(field=field), self.assertRaises(broker.BrokerError):
                broker.build_command(
                    "swamp.plan_linear_destructive_owner_approval",
                    {**base, field: value},
                    self.broker_policy(),
                )

    def test_only_authenticated_owner_policy_can_approve(self):
        policy_value = self.broker_policy()
        operation = "swamp.approve_linear_destructive_owner_approval_attest"
        self.assertIn(operation, policy_value["peers"]["owner"]["operations"])
        for peer, peer_policy in policy_value["peers"].items():
            if peer != "owner":
                self.assertNotIn(operation, peer_policy["operations"])
    def test_broker_and_pm_bundle_the_same_approval_contract(self):
        self.assertEqual(
            (ROOT / "plugins" / "ops_broker" / "linear_approval_contract.py").read_bytes(),
            (ROOT / "plugins" / "project_manager_linear" / "approval_contract.py").read_bytes(),
        )

    def test_owner_approve_emits_exact_policy_and_replay_fails_closed(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        planned = owner_approval.build_plan(intent(), BEFORE_HASH, expires)
        encoded = owner_approval.encode_intent(intent())
        attest = {
            "schemaVersion": owner_approval.ATTESTATION_SCHEMA_VERSION,
            "mode": "attestation",
            "decision": "owner_approved",
            "workflow": owner_approval.ATTEST_WORKFLOW,
            "model": owner_approval.ATTEST_MODEL,
            "plan": {
                "workflow": owner_approval.PLAN_WORKFLOW,
                "model": owner_approval.PLAN_MODEL,
                "runId": RUN_ID,
                "artifactVersion": 7,
                "checksum": planned["checksum"],
            },
            "intent": intent(),
            "intentHash": owner_approval.canonical_sha256(intent()),
            "beforeStateHash": BEFORE_HASH,
            "expiresAt": expires,
        }
        attest["checksum"] = owner_approval.artifact_checksum(attest)
        request = broker.validate_request(
            {
                "request_id": "66666666-6666-4666-8666-666666666666",
                "integration": "swamp",
                "operation": "approve_linear_destructive_owner_approval_attest",
                "arguments": {"attest_run_id": ATTEST_RUN_ID},
                "mode": "apply",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            audit.write_text(
                json.dumps(
                    {
                        "event": "linear_owner_approval_gate",
                        "attest_run_id": ATTEST_RUN_ID,
                        "intent": encoded,
                        "before_state_hash": BEFORE_HASH,
                        "expires_at": expires,
                        "plan_run_id": RUN_ID,
                        "plan_checksum": planned["checksum"],
                        "plan_artifact_version": 7,
                    }
                )
                + "\n"
            )

            def runner(argv, **_kwargs):
                if argv[:4] == ["swamp", "workflow", "history", "get"]:
                    payload = {
                        "id": ATTEST_RUN_ID,
                        "workflowName": owner_approval.ATTEST_WORKFLOW,
                        "status": "suspended",
                        "inputs": {
                            "intent": encoded,
                            "beforeStateHash": BEFORE_HASH,
                            "expiresAt": expires,
                            "planRunId": RUN_ID,
                            "planArtifactVersion": 7,
                            "planChecksum": planned["checksum"],
                        },
                        "jobs": [{"name": "attest", "steps": [{"name": "approve-linear-destructive-intent", "status": "waiting_approval"}]}],
                    }
                elif argv[:3] == ["swamp", "workflow", "approve"]:
                    payload = {"runId": ATTEST_RUN_ID, "approved": True}
                elif argv[:3] == ["swamp", "workflow", "resume"]:
                    payload = {
                        "id": ATTEST_RUN_ID,
                        "status": "succeeded",
                        "jobs": [{"steps": [{"dataArtifacts": [{"name": "result", "version": 9}]}]}],
                    }
                else:
                    payload = artifact(attest, run_id=ATTEST_RUN_ID, version=9)
                return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

            response = broker.execute_request(
                request,
                caller="owner",
                policy=self.broker_policy(),
                runner=runner,
                workspace=ROOT,
                audit_path=audit,
            )
            self.assertEqual(response["result"]["policy"], policy(attest))
            with self.assertRaisesRegex(broker.BrokerError, "already approved"):
                broker.execute_request(
                    request,
                    caller="owner",
                    policy=self.broker_policy(),
                    runner=runner,
                    workspace=ROOT,
                    audit_path=audit,
                )

    def test_non_owner_cannot_approve_even_if_peer_policy_is_misconfigured(self):
        request = broker.validate_request(
            {
                "request_id": "55555555-5555-4555-8555-555555555555",
                "integration": "swamp",
                "operation": "approve_linear_destructive_owner_approval_attest",
                "arguments": {"attest_run_id": ATTEST_RUN_ID},
                "mode": "apply",
            }
        )
        policy_value = self.broker_policy()
        policy_value["peers"]["swe"]["operations"].append(
            "swamp.approve_linear_destructive_owner_approval_attest"
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            audit.write_text(json.dumps({"event": "linear_owner_approval_gate", "attest_run_id": ATTEST_RUN_ID}) + "\n")
            with self.assertRaisesRegex(broker.BrokerError, "authenticated owner"):
                broker.execute_request(
                    request,
                    caller="swe",
                    policy=policy_value,
                    runner=lambda *_args, **_kwargs: self.fail("runner must not execute"),
                    workspace=ROOT,
                    audit_path=audit,
                )


class WorkflowContractTests(unittest.TestCase):
    def test_attestation_workflow_has_explicit_approval_and_no_linear_or_arbitrary_shell(self):
        path = ROOT / "workflows" / "workflow-linear-destructive-owner-approval-attest.yaml"
        text = path.read_text()
        self.assertIn("type: manual_approval", text)
        self.assertIn("approve-linear-destructive-intent", text)
        self.assertIn("linear_owner_approval.py", text)
        self.assertNotIn("LINEAR_TOKEN", text)
        self.assertNotIn("inputs.command", text)
        self.assertNotIn("inputs.path", text)


if __name__ == "__main__":
    unittest.main()
