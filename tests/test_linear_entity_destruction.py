import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from plugins.linear_source_route import (
    LINEAR_SOURCE_REQUEST_SCHEMA,
    _public_target,
    route as source_route,
)
from plugins.project_manager_linear import approval, execute_claimed_task, lane
from scripts import linear_owner_approval as owner_approval
from tests.test_linear_command_lane import owner_policy

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def destructive_command(operation, entity_type, selector, *, key=None, policy=None):
    return {
        "schema_version": "linear-command.v2",
        "command_id": "11111111-1111-4111-8111-111111111111",
        "correlation_id": "22222222-2222-4222-8222-222222222222",
        "idempotency_key": key or f"linear:destroy:{operation}:{entity_type}:fixture",
        "source_profile": "swe",
        "operation": operation,
        "target": {"type": entity_type, "selector": selector},
        "change": {},
        "policy": owner_policy() if policy is None else policy,
    }


def bind(raw, before):
    bound = copy.deepcopy(raw)
    intent = {key: bound[key] for key in ("operation", "target", "change")}
    before_hash = owner_approval.canonical_sha256(before)
    bound["policy"]["approval"]["intent_hash"] = owner_approval.canonical_sha256(intent)
    bound["policy"]["approval"]["before_state_hash"] = before_hash
    verified = approval.VerifiedOwnerApproval(
        {}, intent, before_hash, bound["policy"]["approval"]["checksum"],
        _marker=approval._VERIFIED_MARKER,
    )
    return bound, verified


class DestructiveClient:
    def __init__(self):
        self.writes = []
        self.entities = {
            "issues": [{
                "id": "issue-id", "identifier": "SIS-77", "title": "Archive me",
                "url": "https://linear.app/acme/issue/SIS-77/archive-me",
                "description": "keep", "priority": 3, "dueDate": None, "estimate": 2,
                "archivedAt": None, "state": {"name": "Todo", "type": "unstarted"},
                "assignee": {"name": "Alex"}, "labels": {"nodes": [{"name": "safe"}]},
                "team": {"id": "team-sis", "key": "SIS"}, "parent": None,
                "project": {"name": "P"}, "projectMilestone": {"name": "M"},
            }],
            "projects": [{
                "id": "project-id", "name": "Empty project", "description": "keep",
                "targetDate": "2026-12-31", "archivedAt": None,
                "teams": {"nodes": [{"id": "team-sis", "key": "SIS"}]},
            }],
            "milestones": [{
                "id": "milestone-id", "name": "Empty milestone", "description": "keep",
                "targetDate": None, "archivedAt": None,
                "project": {"id": "project-id", "name": "Empty project", "teams": {"nodes": [{"key": "SIS"}]}}
            }],
            "initiatives": [{
                "id": "initiative-id", "name": "Empty initiative", "description": "keep",
                "targetDate": None, "archivedAt": None,
            }],
        }
        self.children = []
        self.project_issues = []
        self.project_milestones = []
        self.milestone_issues = []
        self.initiative_projects = []
        self.project_initiatives = []
        self.issue_relations = []

    def list_linear_entities(self, entity_type, *, include_archived):
        nodes = copy.deepcopy(self.entities[entity_type])
        return nodes if include_archived else [n for n in nodes if n.get("archivedAt") is None]

    def get_issue(self, identifier):
        return next((copy.deepcopy(n) for n in self.entities["issues"] if (n["identifier"] == identifier or n["id"] == identifier) and n.get("archivedAt") is None), None)

    def get_linear_entity(self, entity_type, entity_id):
        collection = {"issue": "issues", "project": "projects", "milestone": "milestones", "initiative": "initiatives"}[entity_type]
        return next((copy.deepcopy(n) for n in self.entities[collection] if n["id"] == entity_id), None)

    def list_child_issues(self, identifier):
        return copy.deepcopy(self.children) if identifier == "SIS-77" else []

    def list_project_issues(self, project_id):
        if project_id == "milestone-id":
            return copy.deepcopy(self.milestone_issues)
        return copy.deepcopy(self.project_issues)

    def list_project_milestones(self, project_id):
        return copy.deepcopy(self.project_milestones)

    def list_milestone_issues(self, milestone_id):
        return copy.deepcopy(self.milestone_issues)

    def list_initiative_projects(self, initiative_id):
        return copy.deepcopy(self.initiative_projects)

    def list_project_initiatives(self, project_id):
        return copy.deepcopy(self.project_initiatives)

    def list_issue_relations(self, identifier):
        return copy.deepcopy(self.issue_relations)

    def archive_linear_entity(self, entity_type, entity_id):
        self.writes.append(("archive", entity_type, entity_id))
        node = next(n for n in self.entities[{"issue": "issues", "project": "projects", "initiative": "initiatives"}[entity_type]] if n["id"] == entity_id)
        node["archivedAt"] = "2026-09-01T12:00:01Z"

    def delete_linear_entity(self, entity_type, entity_id):
        self.writes.append(("delete", entity_type, entity_id))
        collection = {"issue": "issues", "project": "projects", "milestone": "milestones", "initiative": "initiatives"}[entity_type]
        self.entities[collection] = [n for n in self.entities[collection] if n["id"] != entity_id]
        if entity_type == "issue":
            for child in self.entities["issues"]:
                if isinstance(child.get("parent"), dict) and child["parent"].get("id") == entity_id:
                    child["parent"] = None
            self.issue_relations = []
        elif entity_type == "project":
            for issue in self.entities["issues"]:
                if isinstance(issue.get("project"), dict) and issue["project"].get("id") == entity_id:
                    issue["project"] = None
                    issue["projectMilestone"] = None
            self.entities["milestones"] = [n for n in self.entities["milestones"] if n.get("project", {}).get("id") != entity_id]
            self.project_milestones = []
            self.initiative_projects = [n for n in self.initiative_projects if n.get("id") != entity_id]
        elif entity_type == "milestone":
            for issue in self.entities["issues"]:
                if isinstance(issue.get("projectMilestone"), dict) and issue["projectMilestone"].get("id") == entity_id:
                    issue["projectMilestone"] = None
        elif entity_type == "initiative":
            self.project_initiatives = [n for n in self.project_initiatives if n.get("id") != entity_id]

    def configure_nonempty_impact(self, entity_type):
        if entity_type == "issue":
            child = copy.deepcopy(self.entities["issues"][0])
            child.update({"id": "child-id", "identifier": "SIS-78", "title": "Child", "parent": {"id": "issue-id", "identifier": "SIS-77"}})
            self.entities["issues"].append(child)
            self.children = [copy.deepcopy(child)]
            self.issue_relations = [{"id": "relation-id", "type": "related", "issue": {"id": "issue-id", "identifier": "SIS-77"}, "relatedIssue": {"id": "child-id", "identifier": "SIS-78"}}]
        elif entity_type in {"project", "milestone"}:
            impacted = copy.deepcopy(self.entities["issues"][0])
            impacted.update({"id": "impacted-issue-id", "identifier": "SIS-78", "title": "Impacted", "project": {"id": "project-id", "name": "Empty project"}, "projectMilestone": {"id": "milestone-id", "name": "Empty milestone"}})
            self.entities["issues"].append(impacted)
            if entity_type == "project":
                self.project_issues = [copy.deepcopy(impacted)]
                self.project_milestones = [copy.deepcopy(self.entities["milestones"][0])]
                self.project_initiatives = [copy.deepcopy(self.entities["initiatives"][0])]
                self.initiative_projects = [copy.deepcopy(self.entities["projects"][0])]
            else:
                self.milestone_issues = [copy.deepcopy(impacted)]
        else:
            self.initiative_projects = [copy.deepcopy(self.entities["projects"][0])]
            self.project_initiatives = [copy.deepcopy(self.entities["initiatives"][0])]


class LinearEntityDestructionTests(unittest.TestCase):
    SUPPORTED = (
        ("archive_linear_entity", "issue", {"identifier": "SIS-77"}),
        ("archive_linear_entity", "project", {"name": "Empty project"}),
        ("archive_linear_entity", "initiative", {"name": "Empty initiative"}),
        ("delete_linear_entity", "issue", {"identifier": "SIS-77"}),
        ("delete_linear_entity", "project", {"name": "Empty project"}),
        ("delete_linear_entity", "milestone", {"project": "Empty project", "name": "Empty milestone"}),
        ("delete_linear_entity", "initiative", {"name": "Empty initiative"}),
    )

    def apply(self, raw, client, journal):
        planned = lane.execute_command(client, raw, mode="plan")
        raw, verified = bind(raw, planned["before"])
        with mock.patch("plugins.project_manager_linear.approval.verify_owner_approval", return_value=verified):
            return execute_claimed_task(raw, task_id="t_1234abcd", lane=lane, client=client, journal_path=journal, approval_now=NOW)

    def test_each_supported_matrix_entry_applies_through_public_pm_seam(self):
        for operation, entity_type, selector in self.SUPPORTED:
            with self.subTest(operation=operation, entity_type=entity_type), tempfile.TemporaryDirectory() as tmp:
                client = DestructiveClient()
                result = self.apply(destructive_command(operation, entity_type, selector), client, Path(tmp) / "journal.json")["result"]
                self.assertEqual(result["result"], "applied")
                self.assertTrue(result["verified"])
                self.assertEqual(result["target"], {"type": entity_type, "selector": selector})
                def assert_no_raw_id_keys(value):
                    if isinstance(value, dict):
                        self.assertNotIn("id", value)
                        for item in value.values():
                            assert_no_raw_id_keys(item)
                    elif isinstance(value, list):
                        for item in value:
                            assert_no_raw_id_keys(item)
                assert_no_raw_id_keys(result)
                if operation == "archive_linear_entity":
                    self.assertTrue(result["after"]["archived"])
                    self.assertEqual(result["before"]["entity"]["description"], result["after"]["entity"]["description"])
                else:
                    self.assertEqual(result["after"], {"present": False})

    def test_unsafe_matrix_entries_are_rejected_before_linear_access(self):
        unsupported = (
            ("archive_linear_entity", "milestone", {"project": "P", "name": "M"}),
        )
        for operation, entity_type, selector in unsupported:
            client = mock.Mock()
            with self.subTest(operation=operation, entity_type=entity_type), self.assertRaisesRegex(lane.ContractError, "supported safe matrix"):
                lane.execute_command(client, destructive_command(operation, entity_type, selector), mode="plan")
            client.assert_not_called()

    def test_standard_policy_raw_ids_bulk_and_wrong_selectors_are_blocked(self):
        cases = [
            destructive_command("archive_linear_entity", "issue", {"identifier": "SIS-77"}, policy={"mode": "standard"}),
            destructive_command("archive_linear_entity", "issue", {"id": "raw-id"}),
            destructive_command("archive_linear_entity", "project", {"name": ["A", "B"]}),
            destructive_command("delete_linear_entity", "milestone", {"name": "M"}),
            destructive_command("archive_linear_entity", "team", {"name": "SIS"}),
        ]
        for raw in cases:
            with self.subTest(target=raw["target"]), self.assertRaises(lane.ContractError):
                lane.validate_command(raw)

    def test_nonempty_dependency_inventory_is_bound_into_deterministic_plan(self):
        cases = (
            ("issue", "children", [{"id": "child-id", "identifier": "SIS-78"}]),
            ("project", "project_issues", [{"id": "issue-id", "identifier": "SIS-78"}]),
            ("project", "project_milestones", [{"id": "milestone-id", "name": "M2"}]),
            ("milestone", "milestone_issues", [{"id": "issue-id", "identifier": "SIS-78"}]),
            ("initiative", "initiative_projects", [{"id": "project-id", "name": "P"}]),
        )
        selector = {
            "issue": {"identifier": "SIS-77"}, "project": {"name": "Empty project"},
            "milestone": {"project": "Empty project", "name": "Empty milestone"},
            "initiative": {"name": "Empty initiative"},
        }
        for entity_type, field, impact in cases:
            client = DestructiveClient()
            setattr(client, field, impact)
            operation = "delete_linear_entity"
            with self.subTest(entity_type=entity_type, field=field):
                plan = lane.execute_command(client, destructive_command(operation, entity_type, selector[entity_type]), mode="plan")
            self.assertEqual(plan["before"]["impact_counts"], {
                key: len(items) for key, items in plan["before"]["impact"].items()
            })
            self.assertEqual(plan["plan"][0]["impact_counts"], plan["before"]["impact_counts"])
            self.assertEqual(plan["plan"][0]["affected_entities"], plan["before"]["impact"])
            self.assertEqual(client.writes, [])

    def test_each_supported_operation_applies_with_nonempty_bound_impact(self):
        client = DestructiveClient()
        client.project_issues = [
            {"id": "issue-a", "identifier": "SIS-78", "title": "Same"},
            {"id": "issue-b", "identifier": "SIS-78", "title": "Same"},
        ]
        plan = lane.execute_command(
            client,
            destructive_command(
                "archive_linear_entity", "project", {"name": "Empty project"}
            ),
            mode="plan",
        )
        self.assertEqual(plan["before"]["impact_counts"]["issues"], 2)

        client.project_issues[1]["id"] = "issue-a"
        with self.assertRaisesRegex(
            (RuntimeError, lane.ContractError), "duplicate raw IDs"
        ):
            lane.execute_command(
                client,
                destructive_command(
                    "archive_linear_entity", "project", {"name": "Empty project"}
                ),
                mode="plan",
            )

        for operation, entity_type, selector in self.SUPPORTED:
            with self.subTest(operation=operation, entity_type=entity_type), tempfile.TemporaryDirectory() as tmp:
                client = DestructiveClient()
                client.configure_nonempty_impact(entity_type)
                result = self.apply(
                    destructive_command(operation, entity_type, selector, key=f"linear:destroy:impact:{operation}:{entity_type}"),
                    client,
                    Path(tmp) / "journal.json",
                )["result"]
                self.assertEqual(result["result"], "applied")
                self.assertTrue(result["verified"])
                self.assertGreater(sum(result["before"]["impact_counts"].values()), 0)
                self.assertEqual(result["plan"][0]["affected_entities"], result["before"]["impact"])

    def test_ambiguity_api_failure_and_readback_drift_fail_closed(self):
        required = (
            "get_linear_entity",
            "list_issue_relations",
            "list_project_initiatives",
        )
        for missing in required:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                class MissingCapabilityClient(DestructiveClient):
                    missing_capability = missing

                    def __getattribute__(self, name):
                        if name == object.__getattribute__(
                            self, "missing_capability"
                        ):
                            raise AttributeError(name)
                        return super().__getattribute__(name)

                client = MissingCapabilityClient()
                raw = destructive_command(
                    "archive_linear_entity",
                    "issue",
                    {"identifier": "SIS-77"},
                    key=f"linear:destroy:missing:{missing}",
                )
                planned = lane.execute_command(client, raw, mode="plan")
                raw, consumed = bind(raw, planned["before"])
                auth = approval.ConsumedOwnerApproval(
                    consumed, _marker=approval._CONSUMED_MARKER
                )
                with self.assertRaisesRegex(lane.ContractError, missing):
                    lane.execute_command(
                        client,
                        raw,
                        mode="apply",
                        journal_path=Path(tmp) / "journal.json",
                        owner_approval_authorization=auth,
                    )
                self.assertEqual(client.writes, [])

        ambiguous = DestructiveClient()
        ambiguous.entities["initiatives"].append(copy.deepcopy(ambiguous.entities["initiatives"][0]))
        ambiguous.entities["initiatives"][-1]["id"] = "other"
        with self.assertRaisesRegex(lane.ContractError, "ambiguous"):
            lane.execute_command(ambiguous, destructive_command("archive_linear_entity", "initiative", {"name": "Empty initiative"}), mode="plan")

        class Failure(DestructiveClient):
            def archive_linear_entity(self, entity_type, entity_id):
                raise RuntimeError("api failed")
        class Drift(DestructiveClient):
            def archive_linear_entity(self, entity_type, entity_id):
                super().archive_linear_entity(entity_type, entity_id)
                self.entities["issues"][0]["description"] = "drift"
        for client, message in ((Failure(), "api failed"), (Drift(), "read-back")):
            raw = destructive_command("archive_linear_entity", "issue", {"identifier": "SIS-77"}, key=f"linear:destroy:failure:{type(client).__name__}")
            planned = lane.execute_command(client, raw, mode="plan")
            raw, consumed = bind(raw, planned["before"])
            auth = approval.ConsumedOwnerApproval(consumed, _marker=approval._CONSUMED_MARKER)
            with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex((RuntimeError, lane.ContractError), message):
                lane.execute_command(client, raw, mode="apply", journal_path=Path(tmp)/"j.json", owner_approval_authorization=auth)

    def test_toctou_impact_drift_fails_before_claim_and_write(self):
        class ImpactDrift(DestructiveClient):
            calls = 0
            def list_child_issues(self, identifier):
                if identifier != "SIS-77":
                    return []
                self.calls += 1
                values = copy.deepcopy(self.children)
                if self.calls >= 2:
                    extra = copy.deepcopy(values[0])
                    extra.update({"id": "late-child-id", "identifier": "SIS-79", "title": "Late child"})
                    values.append(extra)
                return values
        client = ImpactDrift()
        client.configure_nonempty_impact("issue")
        raw = destructive_command("delete_linear_entity", "issue", {"identifier": "SIS-77"}, key="linear:destroy:impact-drift")
        approved = lane.execute_command(client, raw, mode="plan")
        raw, verified = bind(raw, approved["before"])
        client.calls = 0
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ), self.assertRaisesRegex(approval.ApprovalError, "drift"):
            execute_claimed_task(
                raw, task_id="t_1234abcd", lane=lane, client=client,
                journal_path=Path(tmp) / "journal.json", approval_now=NOW,
            )
        self.assertEqual(client.writes, [])

    def test_linear_client_uses_only_fixed_safe_matrix_mutations(self):
        calls = []
        class Client(lane.LinearClient):
            def execute(self, query, variables=None):
                calls.append((query, variables))
                field = {
                    lane.ISSUE_ARCHIVE: "issueArchive",
                    lane.PROJECT_ARCHIVE: "projectArchive",
                    lane.INITIATIVE_ARCHIVE: "initiativeArchive",
                    lane.ISSUE_DELETE: "issueDelete",
                    lane.PROJECT_DELETE: "projectDelete",
                    lane.PROJECT_MILESTONE_DELETE: "projectMilestoneDelete",
                    lane.INITIATIVE_DELETE: "initiativeDelete",
                }[query]
                if field in {"issueDelete", "projectDelete"}:
                    return {field: {"success": True, "entity": None}}
                if field in {"projectMilestoneDelete", "initiativeDelete"}:
                    return {field: {"success": True, "entityId": variables["id"]}}
                return {field: {"success": True, "entity": {"id": variables["id"], "archivedAt": "now"}}}
        client = Client("fixture-token")
        for entity_type, document in (
            ("issue", lane.ISSUE_ARCHIVE),
            ("project", lane.PROJECT_ARCHIVE),
            ("initiative", lane.INITIATIVE_ARCHIVE),
        ):
            client.archive_linear_entity(entity_type, "trusted-id")
            self.assertEqual(calls[-1], (document, {"id": "trusted-id"}))
        for entity_type, document in (
            ("issue", lane.ISSUE_DELETE), ("project", lane.PROJECT_DELETE),
            ("milestone", lane.PROJECT_MILESTONE_DELETE),
            ("initiative", lane.INITIATIVE_DELETE),
        ):
            client.delete_linear_entity(entity_type, "trusted-id")
            self.assertEqual(calls[-1], (document, {"id": "trusted-id"}))
        source = Path(lane.__file__).read_text(encoding="utf-8")
        for forbidden in ("teamDelete", "organizationDelete", "accountDelete"):
            self.assertNotIn(forbidden, source)

    def test_every_impact_connection_cursor_paginates_to_exhaustion(self):
        class Client(lane.LinearClient):
            def execute(self, query, variables=None):
                after = variables.get("after")
                node = {"id": f"node-{after or 'first'}"}
                connection = {
                    "nodes": [node],
                    "pageInfo": {
                        "hasNextPage": after is None,
                        "endCursor": "next" if after is None else None,
                    },
                }
                if query == lane.PROJECT_MILESTONES_QUERY:
                    return {"project": {"projectMilestones": connection}}
                if query == lane.PROJECT_ISSUES_QUERY or query == lane.MILESTONE_ISSUES_QUERY:
                    return {"issues": connection}
                if query == lane.INITIATIVE_PROJECTS_QUERY:
                    return {"initiative": {"projects": connection}}
                raise AssertionError("unexpected fixed query")
        client = Client("fixture-token")
        self.assertEqual(len(client.list_project_milestones("p")), 2)
        self.assertEqual(len(client.list_project_issues("p")), 2)
        self.assertEqual(len(client.list_milestone_issues("m")), 2)
        self.assertEqual(len(client.list_initiative_projects("i")), 2)

    def test_public_projection_revalidates_exact_selector_without_raw_ids(self):
        valid = {
            "operation": "archive_linear_entity",
            "target": {"type": "issue", "selector": {"identifier": "SIS-77"}},
            "after": {"archived": True},
        }
        self.assertEqual(_public_target(valid)[0], valid["target"])
        for target in (
            {"type": "issue", "selector": {"id": "raw-id"}},
            {"type": "project", "selector": {"name": "P", "id": "raw"}},
            {"type": "milestone", "selector": {"project": "P", "name": "M", "id": "raw"}},
        ):
            with self.assertRaises(source_route.RouteError):
                _public_target({"operation": "archive_linear_entity", "target": target, "after": {"archived": True}})

    def test_source_and_approval_contracts_are_exact_and_narrow(self):
        reference = owner_policy()["approval"]
        for operation, entity_type, selector in self.SUPPORTED:
            parsed = source_route.parse_linear_request(
                {"operation": operation, "entity_type": entity_type, "selector": selector, "approval": reference},
                source_profile="swe",
                uuid_factory=iter(("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")).__next__,
            )
            self.assertEqual(parsed.command["target"], {"type": entity_type, "selector": selector})
            self.assertEqual(parsed.command["change"], {})
            self.assertEqual(parsed.command["policy"], {"mode": "owner_approved", "approval": reference})
            intent = {key: parsed.command[key] for key in ("operation", "target", "change")}
            plan = owner_approval.build_plan(intent, "b" * 64, "2026-09-01T13:00:00Z", now=NOW)
            self.assertEqual(plan["plannedActions"], [intent])
        operations = LINEAR_SOURCE_REQUEST_SCHEMA["parameters"]["properties"]["operation"]["enum"]
        self.assertIn("archive_linear_entity", operations)
        self.assertIn("delete_linear_entity", operations)
        for malformed in (
            {"operation": "archive_linear_entity", "entity_type": "issue", "selector": {"identifier": "SIS-77"}},
            {"operation": "archive_linear_entity", "entity_type": "milestone", "selector": {"project": "P", "name": "M"}, "approval": reference},
            {"operation": "archive_linear_entity", "entity_type": "issue", "selector": {"identifier": "SIS-77", "id": "raw"}, "approval": reference},
            {"operation": "archive_linear_entity", "entity_type": "issue", "selector": {"identifier": "SIS-77"}, "approval": {**reference, "checksum": "0"}},
        ):
            with self.assertRaises(source_route.RouteError):
                source_route.parse_linear_request(malformed)
        intent = {"operation": "archive_linear_entity", "target": {"type": "issue", "selector": {"identifier": "SIS-77"}}, "change": {}}
        with self.assertRaises(owner_approval.ContractError):
            owner_approval.build_plan(intent, "b" * 64, "2026-09-01T11:00:00Z", now=NOW)

    def test_crash_after_write_recovers_and_completed_replay_is_noop(self):
        class Crash(DestructiveClient):
            crashed = False
            def archive_linear_entity(self, entity_type, entity_id):
                super().archive_linear_entity(entity_type, entity_id)
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("death")
        client = Crash()
        raw = destructive_command("archive_linear_entity", "issue", {"identifier": "SIS-77"}, key="linear:destroy:crash:issue")
        planned = lane.execute_command(client, raw, mode="plan")
        raw, verified = bind(raw, planned["before"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch("plugins.project_manager_linear.approval.verify_owner_approval", return_value=verified) as verifier:
            journal = Path(tmp)/"j.json"
            with self.assertRaises(KeyboardInterrupt):
                execute_claimed_task(raw, task_id="t_1234abcd", lane=lane, client=client, journal_path=journal, approval_now=NOW, approval_lease_seconds=1)
            verifier.reset_mock()
            recovered = execute_claimed_task(raw, task_id="t_1234abcd", lane=lane, client=client, journal_path=journal, approval_now=NOW + timedelta(seconds=2), approval_lease_seconds=1)
            replay = execute_claimed_task(raw, task_id="t_1234abcd", lane=lane, client=client, journal_path=journal, approval_now=NOW + timedelta(seconds=3), approval_lease_seconds=1)
        self.assertEqual(recovered["result"]["result"], "no_op")
        self.assertTrue(recovered["result"]["recovered"])
        self.assertEqual(replay["result"]["result"], "no_op")
        self.assertEqual(client.writes, [("archive", "issue", "issue-id")])
        verifier.assert_not_called()

    def test_direct_issue_lookup_matches_dependency_project_field_depth(self):
        self.assertIn("project { id name }", lane.ISSUE_QUERY)
        self.assertIn("projectMilestone { id name }", lane.ISSUE_QUERY)

    def test_delete_crash_after_trash_recovers_without_second_mutation(self):
        class CrashDelete(DestructiveClient):
            crashed = False
            def delete_linear_entity(self, entity_type, entity_id):
                super().delete_linear_entity(entity_type, entity_id)
                if not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("death")
        client = CrashDelete()
        raw = destructive_command(
            "delete_linear_entity", "project",
            {"name": "Empty project"}, key="linear:destroy:crash:project",
        )
        planned = lane.execute_command(client, raw, mode="plan")
        raw, verified = bind(raw, planned["before"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "plugins.project_manager_linear.approval.verify_owner_approval",
            return_value=verified,
        ) as verifier:
            journal = Path(tmp) / "j.json"
            with self.assertRaises(KeyboardInterrupt):
                execute_claimed_task(
                    raw, task_id="t_1234abcd", lane=lane, client=client,
                    journal_path=journal, approval_now=NOW, approval_lease_seconds=1,
                )
            verifier.reset_mock()
            recovered = execute_claimed_task(
                raw, task_id="t_1234abcd", lane=lane, client=client,
                journal_path=journal, approval_now=NOW + timedelta(seconds=2),
                approval_lease_seconds=1,
            )
        self.assertEqual(recovered["result"]["result"], "no_op")
        self.assertTrue(recovered["result"]["recovered"])
        self.assertEqual(client.writes, [("delete", "project", "project-id")])
        verifier.assert_not_called()

    def test_archive_crash_recovery_rejects_child_and_relation_drift(self):
        for drift in ("child", "relation"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                class CrashArchive(DestructiveClient):
                    crashed = False

                    def archive_linear_entity(self, entity_type, entity_id):
                        super().archive_linear_entity(entity_type, entity_id)
                        if not self.crashed:
                            self.crashed = True
                            raise KeyboardInterrupt("death")

                client = CrashArchive()
                client.configure_nonempty_impact("issue")
                raw = destructive_command(
                    "archive_linear_entity",
                    "issue",
                    {"identifier": "SIS-77"},
                    key=f"linear:destroy:archive-recovery-drift:{drift}",
                )
                planned = lane.execute_command(client, raw, mode="plan")
                raw, verified = bind(raw, planned["before"])
                journal = Path(tmp) / "j.json"
                with mock.patch(
                    "plugins.project_manager_linear.approval.verify_owner_approval",
                    return_value=verified,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        execute_claimed_task(
                            raw, task_id="t_1234abcd", lane=lane, client=client,
                            journal_path=journal, approval_now=NOW,
                            approval_lease_seconds=1,
                        )
                    if drift == "child":
                        client.children[0]["description"] = "dependency drift"
                    else:
                        client.issue_relations[0]["type"] = "blocks"
                    with self.assertRaisesRegex(lane.ContractError, "impact.*drift"):
                        execute_claimed_task(
                            raw, task_id="t_1234abcd", lane=lane, client=client,
                            journal_path=journal,
                            approval_now=NOW + timedelta(seconds=2),
                            approval_lease_seconds=1,
                        )
                recovery_path = journal.with_name(
                    journal.name + ".linear-entity-destruction"
                )
                persisted_text = recovery_path.read_text(encoding="utf-8")
                persisted = json.loads(persisted_text)
                entry = next(iter(persisted["entries"].values()))
                self.assertEqual(entry["phase"], "prepared")
                self.assertEqual(recovery_path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("dependency drift", persisted_text)
                self.assertNotIn('"description"', persisted_text)

    def test_delete_crash_recovery_rejects_dependency_drift(self):
        cases = ("child", "relation", "project", "milestone", "link")
        for drift in cases:
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                class CrashDelete(DestructiveClient):
                    crashed = False

                    def delete_linear_entity(self, entity_type, entity_id):
                        super().delete_linear_entity(entity_type, entity_id)
                        if not self.crashed:
                            self.crashed = True
                            raise KeyboardInterrupt("death")

                client = CrashDelete()
                entity_type = "issue" if drift in {"child", "relation"} else "project"
                selector = (
                    {"identifier": "SIS-77"}
                    if entity_type == "issue"
                    else {"name": "Empty project"}
                )
                client.configure_nonempty_impact(entity_type)
                relation_before = copy.deepcopy(client.issue_relations)
                milestone_before = copy.deepcopy(client.entities["milestones"])
                project_before = copy.deepcopy(client.entities["projects"][0])
                raw = destructive_command(
                    "delete_linear_entity",
                    entity_type,
                    selector,
                    key=f"linear:destroy:delete-recovery-drift:{drift}",
                )
                planned = lane.execute_command(client, raw, mode="plan")
                raw, verified = bind(raw, planned["before"])
                journal = Path(tmp) / "j.json"
                with mock.patch(
                    "plugins.project_manager_linear.approval.verify_owner_approval",
                    return_value=verified,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        execute_claimed_task(
                            raw, task_id="t_1234abcd", lane=lane, client=client,
                            journal_path=journal, approval_now=NOW,
                            approval_lease_seconds=1,
                        )
                    if drift == "child":
                        child = next(
                            item for item in client.entities["issues"]
                            if item["id"] == "child-id"
                        )
                        child["description"] = "dependency drift"
                    elif drift == "relation":
                        client.issue_relations = relation_before
                    elif drift == "project":
                        issue = next(
                            item for item in client.entities["issues"]
                            if item["id"] == "impacted-issue-id"
                        )
                        issue["description"] = "dependency drift"
                    elif drift == "milestone":
                        client.entities["milestones"] = milestone_before
                    else:
                        client.initiative_projects = [project_before]
                    with self.assertRaisesRegex(lane.ContractError, "drift|cascade|remained"):
                        execute_claimed_task(
                            raw, task_id="t_1234abcd", lane=lane, client=client,
                            journal_path=journal,
                            approval_now=NOW + timedelta(seconds=2),
                            approval_lease_seconds=1,
                        )
                recovery_path = journal.with_name(
                    journal.name + ".linear-entity-destruction"
                )
                persisted_text = recovery_path.read_text(encoding="utf-8")
                entry = next(iter(json.loads(persisted_text)["entries"].values()))
                self.assertEqual(entry["phase"], "prepared")
                self.assertEqual(recovery_path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("dependency drift", persisted_text)
                self.assertNotIn('"description"', persisted_text)
