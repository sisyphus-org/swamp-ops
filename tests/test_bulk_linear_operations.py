from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from plugins.linear_source_route import route
from plugins.project_manager_linear import approval_contract, bulk, lane


PARENT_IDS = {
    "command_id": "11111111-1111-4111-8111-111111111111",
    "correlation_id": "22222222-2222-4222-8222-222222222222",
}


def item(index: int, operation: str = "update_issue") -> dict:
    return {
        "operation": operation,
        "target": {"type": "issue", "identifier": f"SIS-{index + 1}"},
        "change": {"description": f"value-{index}"},
    }


def approval_policy() -> dict:
    return {
        "mode": "owner_approved",
        "approval": {
            "workflow": "linear-destructive-owner-approval-attest",
            "model": "linear-destructive-owner-approval-attest",
            "run_id": "55555555-5555-4555-8555-555555555555",
            "artifact_version": 1,
            "checksum": "a" * 64,
            "intent_hash": "b" * 64,
            "before_state_hash": "c" * 64,
            "expires_at": "2026-09-01T23:00:00Z",
        },
    }


def parent(items: list[dict], *, policy: dict | None = None) -> dict:
    return {
        "schema_version": "linear-command.v2",
        **PARENT_IDS,
        "idempotency_key": "linear:v2:" + "a" * 32,
        "source_profile": "swe",
        "operation": "bulk_linear_operations",
        "target": {"type": "workspace", "identifier": "current"},
        "change": {"items": items},
        "policy": policy or {"mode": "standard"},
    }


def child_plan(
    child: dict,
    *,
    before: dict,
    after: dict,
    actions: list[dict] | None = None,
    verified: bool = False,
    recovered: bool = False,
    recovery_evidence: dict | None = None,
) -> dict:
    actions = actions if actions is not None else [{"action": child["operation"]}]
    result = {
        "schema_version": "linear-result.v2",
        "operation": child["operation"],
        "mode": "plan",
        "target": copy.deepcopy(child["target"]),
        "result": "no_op" if verified else "planned",
        "before": copy.deepcopy(before),
        "after": copy.deepcopy(after),
        "plan": copy.deepcopy(actions),
        "no_op": verified,
        "verified": verified,
    }
    if recovered:
        result["recovered"] = True
    if recovery_evidence is not None:
        result["recovery_evidence"] = copy.deepcopy(recovery_evidence)
    return result


class BulkContractTests(unittest.TestCase):
    def test_accepts_fifty_and_rejects_fifty_one(self):
        self.assertEqual(len(lane.validate_command(parent([item(i) for i in range(50)]))["change"]["items"]), 50)
        with self.assertRaisesRegex(lane.ContractError, "1-50"):
            lane.validate_command(parent([item(i) for i in range(51)]))

    def test_rejects_nested_metadata_reads_and_bulk_recursion(self):
        bad_items = [
            {**item(0), "policy": {"mode": "standard"}},
            {**item(0), "command_id": PARENT_IDS["command_id"]},
            item(0, "read_issue"),
            item(0, "bulk_linear_operations"),
        ]
        for bad in bad_items:
            with self.subTest(bad=bad), self.assertRaises(lane.ContractError):
                lane.validate_command(parent([bad]))

    def test_rejects_duplicate_semantics_and_exact_target_conflicts(self):
        duplicate = item(0)
        with self.assertRaisesRegex(lane.ContractError, "duplicate"):
            lane.validate_command(parent([duplicate, copy.deepcopy(duplicate)]))
        conflicting = item(0)
        conflicting["change"] = {"priority": "High"}
        with self.assertRaisesRegex(lane.ContractError, "conflicting"):
            lane.validate_command(parent([item(0), conflicting]))

    def test_source_and_pm_reject_reversed_symmetric_related_pair(self):
        related = [
            {
                "operation": "create_issue_relation",
                "target": {"type": "issue", "identifier": "SIS-10"},
                "change": {
                    "related_identifier": "SIS-11",
                    "relation_type": "related",
                },
            },
            {
                "operation": "create_issue_relation",
                "target": {"type": "issue", "identifier": "SIS-11"},
                "change": {
                    "related_identifier": "SIS-10",
                    "relation_type": "related",
                },
            },
        ]
        with self.assertRaisesRegex(route.RouteError, "conflicting"):
            route.parse_linear_request(
                {"operation": "bulk_linear_operations", "items": related}
            )
        with self.assertRaisesRegex(lane.ContractError, "conflicting"):
            lane.validate_command(parent(related))

        directional = copy.deepcopy(related)
        directional[0]["change"]["relation_type"] = "blocks"
        directional[1]["change"]["relation_type"] = "blocks"
        route.parse_linear_request(
            {"operation": "bulk_linear_operations", "items": directional}
        )
        lane.validate_command(parent(directional))

    def test_derives_stable_domain_separated_child_identities_and_binds_order(self):
        team = {"type": "team", "identifier": "SIS"}
        workspace = {"type": "workspace", "identifier": "current"}
        distinct_creates = [
            {"operation": "create_project", "target": team, "change": {"name": "Project A", "description": "", "target_date": None}},
            {"operation": "create_project", "target": team, "change": {"name": "Project B", "description": "", "target_date": None}},
            {"operation": "create_milestone", "target": team, "change": {"project": "Project A", "name": "Milestone", "description": "", "target_date": None}},
            {"operation": "create_milestone", "target": team, "change": {"project": "Project B", "name": "Milestone", "description": "", "target_date": None}},
            {"operation": "create_initiative", "target": workspace, "change": {"name": "Initiative A", "description": "", "target_date": None}},
            {"operation": "create_initiative", "target": workspace, "change": {"name": "Initiative B", "description": "", "target_date": None}},
        ]
        self.assertEqual(
            lane.validate_command(parent(distinct_creates))["change"]["items"],
            distinct_creates,
        )

        same_entity = [copy.deepcopy(distinct_creates[2]), copy.deepcopy(distinct_creates[2])]
        same_entity[1]["change"]["description"] = "different"
        with self.assertRaisesRegex(lane.ContractError, "conflicting"):
            lane.validate_command(parent(same_entity))

        source_request = {
            "operation": "bulk_linear_operations",
            "items": distinct_creates[-2:],
        }
        self.assertEqual(
            route.parse_linear_request(source_request).command["change"]["items"],
            source_request["items"],
        )
        source_conflict = copy.deepcopy(source_request)
        source_conflict["items"][1]["change"].update(
            {"name": "Initiative A", "description": "different"}
        )
        with self.assertRaisesRegex(route.RouteError, "conflicting"):
            route.parse_linear_request(source_conflict)

        first = bulk.derive_child_command(parent([item(0), item(1)]), 0)
        again = bulk.derive_child_command(parent([item(0), item(1)]), 0)
        second = bulk.derive_child_command(parent([item(0), item(1)]), 1)
        self.assertEqual(first, again)
        self.assertNotEqual(first["command_id"], second["command_id"])
        self.assertNotEqual(first["correlation_id"], second["correlation_id"])
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])
        reordered = parent([item(1), item(0)])
        self.assertNotEqual(first["idempotency_key"], bulk.derive_child_command(reordered, 1)["idempotency_key"])

    def test_all_major_mutating_families_reuse_single_item_validation(self):
        team = {"type": "team", "identifier": "SIS"}
        workspace = {"type": "workspace", "identifier": "current"}
        families = [
            item(0),
            {"operation": "change_state", "target": {"type": "issue", "identifier": "SIS-8"}, "change": {"state": "In Review"}},
            {"operation": "add_comment", "target": {"type": "issue", "identifier": "SIS-9"}, "change": {"body": "bounded"}},
            {"operation": "create_issue_relation", "target": {"type": "issue", "identifier": "SIS-10"}, "change": {"related_identifier": "SIS-11", "relation_type": "related"}},
            {"operation": "create_issue", "target": team, "change": {"title": "Bulk issue", "description": "", "parent_identifier": "SIS-12", "state": "Todo", "priority": "Medium"}},
            {"operation": "converge_hierarchy", "target": team, "change": {"project": {"name": "P"}, "milestone": {"name": "M"}, "issue": {"title": "I", "description": ""}}},
            {"operation": "create_standalone_issue", "target": team, "change": {"project": {"name": "P"}, "milestone": {"name": "M"}, "issue": {"title": "I", "description": "", "state": "Todo", "priority": "Low"}}},
            {"operation": "create_project", "target": team, "change": {"name": "Project", "description": "", "target_date": None}},
            {"operation": "create_milestone", "target": team, "change": {"project": "Project", "name": "Milestone", "description": "", "target_date": None}},
            {"operation": "create_initiative", "target": workspace, "change": {"name": "Initiative", "description": "", "target_date": None}},
            {"operation": "link_project_to_initiative", "target": team, "change": {"project": "Project", "initiative": "Initiative"}},
        ]
        for child in families:
            with self.subTest(operation=child["operation"]):
                lane.validate_command(parent([child]))
        destructive = {"operation": "delete_linear_entity", "target": {"type": "issue", "selector": {"identifier": "SIS-13"}}, "change": {}}
        lane.validate_command(parent([destructive], policy=approval_policy()))

    def test_owner_approval_contract_binds_full_ordered_bulk_intent(self):
        destructive = {
            "operation": "delete_linear_entity",
            "target": {"type": "issue", "selector": {"identifier": "SIS-9"}},
            "change": {},
        }
        intent = bulk.parent_intent(parent([item(0), destructive], policy=approval_policy()))
        self.assertEqual(approval_contract.validate_intent(intent), intent)
        encoded = approval_contract.encode_intent(intent)
        self.assertEqual(approval_contract.decode_intent(encoded), intent)
        changed = copy.deepcopy(intent)
        changed["change"]["items"].reverse()
        self.assertNotEqual(approval_contract.canonical_sha256(intent), approval_contract.canonical_sha256(changed))

    def test_owner_policy_is_rejected_when_no_child_needs_it(self):
        with self.assertRaisesRegex(lane.ContractError, "requires an owner-controlled child"):
            lane.validate_command(parent([item(0)], policy=approval_policy()))

    def test_standard_parent_rejects_any_owner_controlled_child(self):
        destructive = {
            "operation": "delete_linear_entity",
            "target": {"type": "issue", "selector": {"identifier": "SIS-9"}},
            "change": {},
        }
        with self.assertRaisesRegex(lane.ContractError, "owner_approved"):
            lane.validate_command(parent([item(0), destructive]))


class BulkExecutionTests(unittest.TestCase):
    def test_all_preflights_complete_before_first_write(self):
        events: list[str] = []

        def validate(child):
            return child

        def execute(child, mode, _auth=None):
            events.append(f"{mode}:{child['target'].get('identifier', 'workspace')}")
            if mode == "plan" and child["target"].get("identifier") == "SIS-2":
                raise RuntimeError("preflight failed")
            return bulk.fake_result(child, mode, no_op=False)

        with self.assertRaisesRegex(RuntimeError, "preflight failed"):
            bulk.execute_parent(parent([item(0), item(1)]), validate_child=validate, execute_child=execute)
        self.assertEqual(events, ["plan:SIS-1", "plan:SIS-2"])

    def test_partial_failure_persists_completed_prefix_and_resume_skips_it(self):
        calls: list[tuple[str, str]] = []
        fail = {"enabled": True}

        def execute(child, mode, _auth=None):
            identifier = child["target"]["identifier"]
            calls.append((mode, identifier))
            if mode == "apply" and identifier == "SIS-2" and fail["enabled"]:
                raise RuntimeError("api failed")
            return bulk.fake_result(child, mode, no_op=False)

        with tempfile.TemporaryDirectory() as tmp:
            recovery = Path(tmp) / "bulk.json"
            with self.assertRaisesRegex(bulk.PartialFailure, "1 of 2"):
                bulk.execute_parent(parent([item(0), item(1)]), validate_child=lambda value: value, execute_child=execute, recovery_path=recovery)
            fail["enabled"] = False
            result = bulk.execute_parent(parent([item(0), item(1)]), validate_child=lambda value: value, execute_child=execute, recovery_path=recovery)
        self.assertEqual([call for call in calls if call == ("apply", "SIS-1")], [("apply", "SIS-1")])
        self.assertEqual(result["counts"], {"total": 2, "applied": 2, "no_op": 0})

    def test_cross_target_indirect_drift_stops_before_second_write_and_retry_fails_closed(self):
        live = {"SIS-1": "before-1", "SIS-2": "before-2"}
        writes: list[str] = []

        def execute(child, mode, _auth=None):
            identifier = child["target"]["identifier"]
            planned = child_plan(
                child,
                before={"value": live[identifier]},
                after={"value": f"after-{identifier[-1]}"},
            )
            if mode == "plan":
                return planned
            writes.append(identifier)
            live[identifier] = f"after-{identifier[-1]}"
            if identifier == "SIS-1":
                live["SIS-2"] = "indirect-drift"
            return {**planned, "mode": "apply", "result": "applied", "verified": True}

        with tempfile.TemporaryDirectory() as tmp:
            recovery = Path(tmp) / "bulk.json"
            with self.assertRaisesRegex(bulk.PartialFailure, "1 of 2"):
                bulk.execute_parent(
                    parent([item(0), item(1)]),
                    validate_child=lambda value: value,
                    execute_child=execute,
                    recovery_path=recovery,
                )
            self.assertEqual(writes, ["SIS-1"])
            with self.assertRaisesRegex(bulk.PartialFailure, "1 of 2"):
                bulk.execute_parent(
                    parent([item(0), item(1)]),
                    validate_child=lambda value: value,
                    execute_child=execute,
                    recovery_path=recovery,
                )
            self.assertEqual(writes, ["SIS-1"])
            newly_approved = parent([item(0), item(1)])
            newly_approved["command_id"] = "99999999-9999-4999-8999-999999999999"
            newly_approved["correlation_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            newly_approved["idempotency_key"] = "linear:v2:" + "b" * 32
            result = bulk.execute_parent(
                newly_approved,
                validate_child=lambda value: value,
                execute_child=execute,
                recovery_path=Path(tmp) / "newly-approved-bulk.json",
            )
            self.assertTrue(result["verified"])
            self.assertEqual(writes, ["SIS-1", "SIS-1", "SIS-2"])

    def test_external_drift_in_any_canonical_plan_field_stops_before_first_write(self):
        drifts = {
            "operation": "add_comment",
            "target": {"type": "issue", "identifier": "SIS-99"},
            "before": {"value": "external-drift"},
            "after": {"value": "wrong-desired"},
            "plan": [{"action": "wrong-action"}],
        }
        for field, drifted_value in drifts.items():
            with self.subTest(field=field):
                plan_calls = 0
                writes = 0

                def execute(child, mode, _auth=None):
                    nonlocal plan_calls, writes
                    planned = child_plan(
                        child,
                        before={"value": "original"},
                        after={"value": "desired"},
                    )
                    if mode == "plan":
                        plan_calls += 1
                        if plan_calls > 2:
                            planned[field] = copy.deepcopy(drifted_value)
                        return planned
                    writes += 1
                    return {
                        **planned,
                        "mode": "apply",
                        "result": "applied",
                        "verified": True,
                    }

                with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
                    bulk.PartialFailure, "0 of 2"
                ):
                    bulk.execute_parent(
                        parent([item(0), item(1)]),
                        validate_child=lambda value: value,
                        execute_child=execute,
                        recovery_path=Path(tmp) / "bulk.json",
                    )
                self.assertEqual(writes, 0)

    def test_prepared_crash_accepts_exact_desired_after_with_exact_child_recovery(self):
        live = {"value": "before"}
        apply_attempts = 0
        remote_writes = 0
        crashed = False
        owner_item = item(0, "delete_linear_entity")
        owner_item["target"] = {
            "type": "issue",
            "selector": {"identifier": "SIS-1"},
        }
        owner_item["change"] = {}
        command = parent([owner_item], policy=approval_policy())
        child = bulk.derive_child_command(command, 0)

        def evidence(phase="prepared"):
            reference = child["policy"]["approval"]
            return {
                "schema_version": "linear-owner-recovery.v1",
                "approval_checksum": reference["checksum"],
                "intent_hash": reference["intent_hash"],
                "command_hash": bulk._hash(child),
                "before_state_hash": bulk._hash({"value": "before"}),
                "after_state_hash": bulk._hash({"value": "desired"}),
                "phase": phase,
            }

        def execute(current, mode, _auth=None):
            nonlocal apply_attempts, remote_writes, crashed
            if live["value"] == "desired":
                if mode == "apply":
                    apply_attempts += 1
                recovered = child_plan(
                    current,
                    before={"value": "desired"},
                    after={"value": "desired"},
                    actions=[],
                    verified=True,
                    recovered=True,
                    recovery_evidence=evidence(
                        "completed" if mode == "apply" else "prepared"
                    ),
                )
                recovered["mode"] = mode
                return recovered
            planned = child_plan(
                current,
                before={"value": live["value"]},
                after={"value": "desired"},
            )
            if mode == "plan":
                return planned
            apply_attempts += 1
            remote_writes += 1
            live["value"] = "desired"
            if not crashed:
                crashed = True
                raise KeyboardInterrupt("process death after remote write")
            return {**planned, "mode": "apply", "result": "no_op", "verified": True}

        with tempfile.TemporaryDirectory() as tmp:
            recovery = Path(tmp) / "bulk.json"
            with self.assertRaises(KeyboardInterrupt):
                bulk.execute_parent(
                    command,
                    validate_child=lambda value: value,
                    execute_child=execute,
                    recovery_path=recovery,
                )
            persisted = json.loads(recovery.read_text())
            self.assertRegex(persisted["items"][0]["plan_hash"], r"^[0-9a-f]{64}$")
            self.assertRegex(persisted["items"][0]["before_hash"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                persisted["items"][0]["desired_after_hash"], r"^[0-9a-f]{64}$"
            )
            result = bulk.execute_parent(
                command,
                validate_child=lambda value: value,
                execute_child=execute,
                recovery_path=recovery,
            )
        self.assertEqual(apply_attempts, 2)
        self.assertEqual(remote_writes, 1)
        self.assertEqual(result["items"][0]["outcome"], "no_op")

    def test_prepared_crash_rejects_wrong_current_state_without_another_write(self):
        live = {"value": "before"}
        apply_calls = 0
        crashed = False

        def execute(child, mode, _auth=None):
            nonlocal apply_calls, crashed
            planned = child_plan(
                child,
                before={"value": live["value"]},
                after={"value": "desired"},
            )
            if mode == "plan":
                return planned
            apply_calls += 1
            live["value"] = "wrong-after"
            if not crashed:
                crashed = True
                raise KeyboardInterrupt("process death after wrong remote write")
            return {**planned, "mode": "apply", "result": "applied", "verified": True}

        with tempfile.TemporaryDirectory() as tmp:
            recovery = Path(tmp) / "bulk.json"
            with self.assertRaises(KeyboardInterrupt):
                bulk.execute_parent(
                    parent([item(0)]),
                    validate_child=lambda value: value,
                    execute_child=execute,
                    recovery_path=recovery,
                )
            with self.assertRaisesRegex(bulk.PartialFailure, "0 of 1"):
                bulk.execute_parent(
                    parent([item(0)]),
                    validate_child=lambda value: value,
                    execute_child=execute,
                    recovery_path=recovery,
                )
        self.assertEqual(apply_calls, 1)

    def test_completed_replay_returns_ordered_aggregate_no_op(self):
        calls = 0

        def execute(child, mode, _auth=None):
            nonlocal calls
            calls += mode == "apply"
            return bulk.fake_result(child, mode, no_op=False)

        with tempfile.TemporaryDirectory() as tmp:
            recovery = Path(tmp) / "bulk.json"
            bulk.execute_parent(parent([item(0)]), validate_child=lambda value: value, execute_child=execute, recovery_path=recovery)
            replay = bulk.execute_parent(parent([item(0)]), validate_child=lambda value: value, execute_child=execute, recovery_path=recovery)
        self.assertEqual(calls, 1)
        self.assertTrue(replay["no_op"])
        self.assertEqual(replay["items"], [{"index": 0, "operation": "update_issue", "outcome": "no_op", "verified": True}])

    def test_changed_intent_or_order_conflicts_with_parent_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            recovery = Path(tmp) / "bulk.json"
            execute = lambda child, mode, _auth=None: bulk.fake_result(child, mode, no_op=False)
            bulk.execute_parent(parent([item(0), item(1)]), validate_child=lambda value: value, execute_child=execute, recovery_path=recovery)
            with self.assertRaisesRegex(lane.ContractError, "recovery binding"):
                bulk.execute_parent(parent([item(1), item(0)]), validate_child=lambda value: value, execute_child=execute, recovery_path=recovery)

    def test_concurrent_parent_claim_serializes_to_one_apply(self):
        applies = 0
        initial_plans = 0
        applies_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def execute(child, mode, _auth=None):
            nonlocal applies, initial_plans
            if mode == "plan":
                with applies_lock:
                    initial_plans += 1
                    wait_for_peer = initial_plans <= 2
                if wait_for_peer:
                    barrier.wait()
            else:
                with applies_lock:
                    applies += 1
            return bulk.fake_result(child, mode, no_op=False)

        results: list[dict] = []
        failures: list[BaseException] = []
        with tempfile.TemporaryDirectory() as tmp:
            recovery = Path(tmp) / "bulk.json"

            def run():
                try:
                    results.append(bulk.execute_parent(parent([item(0)]), validate_child=lambda value: value, execute_child=execute, recovery_path=recovery))
                except BaseException as exc:
                    failures.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(applies, 1)
        self.assertEqual(sorted(result["result"] for result in results), ["applied", "no_op"])

    def test_crash_before_write_reenters_prepared_child_when_original_plan_still_matches(self):
        crashed = False
        live = {"done": False}
        apply_calls = 0

        def execute(child, mode, _auth=None):
            nonlocal crashed, apply_calls
            if mode == "apply":
                apply_calls += 1
                if not crashed:
                    crashed = True
                    raise KeyboardInterrupt("process death before remote write")
                live["done"] = True
                return bulk.fake_result(child, mode, no_op=False)
            return bulk.fake_result(child, mode, no_op=live["done"])

        with tempfile.TemporaryDirectory() as tmp:
            recovery = Path(tmp) / "bulk.json"
            with self.assertRaises(KeyboardInterrupt):
                bulk.execute_parent(parent([item(0)]), validate_child=lambda value: value, execute_child=execute, recovery_path=recovery)
            result = bulk.execute_parent(parent([item(0)]), validate_child=lambda value: value, execute_child=execute, recovery_path=recovery)
        self.assertEqual(apply_calls, 2)
        self.assertEqual(result["items"][0]["outcome"], "applied")

    def test_parent_claim_can_only_mint_exact_owner_child_capability(self):
        approval = lane._load_approval()
        destructive = {
            "operation": "delete_linear_entity",
            "target": {"type": "issue", "selector": {"identifier": "SIS-9"}},
            "change": {},
        }
        raw_parent = parent([item(0), destructive], policy=approval_policy())
        # The parent itself has two distinct targets and therefore validates.
        validated = lane.validate_command(raw_parent)
        verified = approval.VerifiedOwnerApproval(
            {},
            bulk.parent_intent(validated),
            "c" * 64,
            "a" * 64,
            _marker=approval._VERIFIED_MARKER,
        )
        consumed = approval.ConsumedOwnerApproval(
            verified,
            _marker=approval._CONSUMED_MARKER,
            command_hash=approval.command_binding_hash(validated),
        )
        child = bulk.derive_child_command(validated, 1)
        narrowed = approval._mint_bulk_child_authorization(
            consumed, parent_command=validated, child_command=child
        )
        approval.require_consumed_owner_approval(
            narrowed,
            expected_intent={
                "operation": child["operation"],
                "target": child["target"],
                "change": child["change"],
            },
            expected_command=child,
        )
        with self.assertRaises(approval.ApprovalError):
            approval.require_consumed_owner_approval(
                consumed,
                expected_intent={
                    "operation": child["operation"],
                    "target": child["target"],
                    "change": child["change"],
                },
                expected_command=child,
            )

    def test_direct_internal_child_authorization_bypass_is_rejected(self):
        child = bulk.derive_child_command(parent([item(0)]), 0)
        with self.assertRaisesRegex(lane.ContractError, "authorization"):
            lane.execute_command(mock.Mock(), child, mode="apply", journal_path=Path("/tmp/not-used"), owner_approval_authorization=object())


class BulkSourceTests(unittest.TestCase):
    def test_duplicate_relation_child_is_standard_safe_in_bulk(self):
        request = {
            "operation": "bulk_linear_operations",
            "items": [
                {
                    "operation": "create_issue_relation",
                    "target": {"type": "issue", "identifier": "SIS-102"},
                    "change": {
                        "related_identifier": "SIS-77",
                        "relation_type": "duplicate",
                    },
                }
            ],
        }
        parsed = route.parse_linear_request(request)
        self.assertEqual(parsed.command["policy"], {"mode": "standard"})
        child = bulk.derive_child_command(parsed.command, 0)
        self.assertEqual(child["change"]["relation_type"], "duplicate")
        self.assertEqual(child["policy"], {"mode": "standard"})

    def test_terminal_state_child_does_not_require_parent_approval(self):
        request = {
            "operation": "bulk_linear_operations",
            "items": [
                {
                    "operation": "change_state",
                    "target": {"type": "issue", "identifier": "SIS-102"},
                    "change": {"state": "Done"},
                }
            ],
        }
        parsed = route.parse_linear_request(request)
        self.assertEqual(parsed.command["policy"], {"mode": "standard"})
        child = bulk.derive_child_command(parsed.command, 0)
        self.assertEqual(child["policy"], {"mode": "standard"})

    def test_source_accepts_only_exact_canonical_items_and_semantic_replay(self):
        request = {"operation": "bulk_linear_operations", "items": [item(0), item(1)]}
        parsed = route.parse_linear_request(request, uuid_factory=lambda: PARENT_IDS["command_id"])
        replay = route.parse_linear_request(request, uuid_factory=lambda: PARENT_IDS["correlation_id"])
        self.assertEqual(parsed.command["operation"], "bulk_linear_operations")
        self.assertEqual(parsed.command["idempotency_key"], replay.command["idempotency_key"])
        forged = copy.deepcopy(request)
        forged["items"][0]["policy"] = {"mode": "standard"}
        with self.assertRaises(route.RouteError):
            route.parse_linear_request(forged)
        malformed = copy.deepcopy(request)
        malformed["items"][0]["change"] = {"graphql": "mutation { unsafe }"}
        with self.assertRaises(route.RouteError):
            route.parse_linear_request(malformed)

    def test_public_projection_contains_only_ordered_safe_outcomes_and_counts(self):
        result = {
            "operation": "bulk_linear_operations",
            "target": {"type": "workspace", "identifier": "current"},
            "result": "applied",
            "verified": True,
            "no_op": False,
            "items": [
                {"index": 0, "operation": "update_issue", "outcome": "applied", "verified": True},
                {"index": 1, "operation": "delete_linear_entity", "outcome": "no_op", "verified": True},
            ],
            "counts": {"total": 2, "applied": 1, "no_op": 1},
            "command_id": PARENT_IDS["command_id"],
            "description": "must not escape",
        }
        target, context = __import__("plugins.linear_source_route", fromlist=["_public_target"])._public_target(result)
        self.assertEqual(target, {"type": "workspace", "identifier": "current"})
        self.assertEqual(context, {"items": result["items"], "counts": result["counts"]})
        serialized = json.dumps(context)
        for forbidden in ("command_id", "description", "hash", "task_id", "idempotency"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
