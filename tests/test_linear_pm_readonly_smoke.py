import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "linear_pm_readonly_smoke.py"
SPEC = importlib.util.spec_from_file_location("linear_pm_readonly_smoke", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load PM read-only smoke")
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


class SmokeTests(unittest.TestCase):
    def test_build_command_is_exact_workspace_read_only_shape(self):
        raw = smoke.build_command(
            operation="search_linear",
            entity_types=["issues", "projects"],
            include_archived=False,
            query="Straße",
        )
        self.assertEqual(raw["operation"], "search_linear")
        self.assertEqual(raw["target"], {"type": "workspace", "identifier": "current"})
        self.assertEqual(
            raw["change"],
            {
                "query": "Straße",
                "entity_types": ["issues", "projects"],
                "include_archived": False,
            },
        )
        self.assertEqual(raw["source_profile"], "project-manager")
        self.assertNotIn("graphql", json.dumps(raw).lower())

    def test_live_smoke_requires_pm_profile_before_client_or_network(self):
        factory = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "project-manager"):
            smoke.run_smoke(
                operation="inventory_linear",
                entity_types=["issues"],
                include_archived=False,
                query=None,
                environ={"HERMES_PROFILE": "default", "LINEAR_TOKEN": "fixture"},
                client_factory=factory,
            )
        factory.assert_not_called()

    def test_live_smoke_returns_only_verified_safe_counts(self):
        class Lane:
            class LinearClient:
                pass

            @staticmethod
            def execute_command(client, command, *, mode, journal_path=None):
                self.assertEqual(mode, "apply")
                self.assertIsNone(journal_path)
                self.assertEqual(command["operation"], "inventory_linear")
                self.assertEqual(client, "client")
                facts = {
                    "entity_types": ["issues", "initiatives"],
                    "include_archived": False,
                    "counts": {"issues": 125, "initiatives": 3},
                    "entities": {"issues": [], "initiatives": []},
                }
                return {
                    "schema_version": "linear-result.v2",
                    "operation": "inventory_linear",
                    "result": "read",
                    "verified": True,
                    "after": facts,
                }

        result = smoke.run_smoke(
            operation="inventory_linear",
            entity_types=["issues", "initiatives"],
            include_archived=False,
            query=None,
            environ={"HERMES_PROFILE": "project-manager", "LINEAR_TOKEN": "fixture"},
            lane=Lane,
            client_factory=lambda token: "client" if token == "fixture" else None,
        )
        self.assertEqual(
            result,
            {
                "result": "pass",
                "readOnly": True,
                "operation": "inventory_linear",
                "entityTypes": ["issues", "initiatives"],
                "includeArchived": False,
                "counts": {"issues": 125, "initiatives": 3},
                "verified": True,
            },
        )
        serialized = json.dumps(result).lower()
        for forbidden in ("token", "description", "url", "idempotency", "command_id"):
            self.assertNotIn(forbidden, serialized)
    def test_relation_inventory_smoke_hashes_exact_inventory_without_exposing_ids(self):
        class Client:
            def get_issue(self, identifier):
                self.identifier = identifier
                return {
                    "id": "issue-internal",
                    "identifier": identifier,
                    "team": {"id": "team-internal", "key": "SIS"},
                }

            def list_issue_relations(self, identifier):
                self.listed = identifier
                return [
                    {
                        "id": "relation-internal",
                        "type": "blocks",
                        "issue": {"id": "issue-a", "identifier": "SIS-77"},
                        "relatedIssue": {
                            "id": "issue-b",
                            "identifier": "SIS-94",
                        },
                    }
                ]

        result = smoke.run_relation_inventory_smoke(
            identifier="SIS-77",
            environ={"HERMES_PROFILE": "project-manager", "LINEAR_TOKEN": "fixture"},
            client_factory=lambda token: Client() if token == "fixture" else None,
        )
        self.assertEqual(result["result"], "pass")
        self.assertTrue(result["readOnly"])
        self.assertEqual(result["identifier"], "SIS-77")
        self.assertEqual(result["relationCount"], 1)
        self.assertRegex(result["inventorySha256"], r"^[0-9a-f]{64}$")
        serialized = json.dumps(result)
        self.assertNotIn("relation-internal", serialized)
        self.assertNotIn("issue-internal", serialized)


if __name__ == "__main__":
    unittest.main()
