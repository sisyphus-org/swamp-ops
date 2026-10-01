"""Exact-identifier search reconciles descriptions without exposing bulk content."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import test_linear_command_lane as fixtures
lane = fixtures.lane
workspace_read_command = fixtures.workspace_read_command
from plugins.linear_source_route import _public_result
from plugins.linear_source_route.route import RouteError


class ExactIssueReadTests(unittest.TestCase):
    def read(self, query="SIS-9", description="## Goal\n\n* Integration https://example.com"):
        client = fixtures.ExecutionTests.WorkspaceReadClient()
        client.data["issues"][0]["description"] = description
        client.data["issues"][0]["url"] = "https://linear.app/sisyphusx/issue/SIS-9/fixture"
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.json"
            result = lane.execute_command(
                client, workspace_read_command("search_linear", entity_types=["issues"], query=query),
                mode="apply", journal_path=journal,
            )
            self.assertFalse(journal.exists())
        public = _public_result({"status": "verified_no_op", "linear_result": result})
        return result, public

    def test_exact_search_returns_literal_description_and_canonical_url(self):
        result, public = self.read()
        item = public["context"]["entities"]["issues"][0]
        self.assertEqual(item["description"], "## Goal\n\n* Integration https://example.com")
        self.assertEqual(item["url"], "https://linear.app/sisyphusx/issue/SIS-9/fixture")
        self.assertEqual(result["result"], "read")
        self.assertNotIn("issue-internal", str(public))
        self.assertNotIn("private@example.com", str(public))

    def test_empty_and_null_descriptions_are_preserved(self):
        for description in (None, "", " \n"):
            with self.subTest(description=description):
                _, public = self.read(description=description)
                self.assertEqual(public["context"]["entities"]["issues"][0]["description"], description)

    def test_fuzzy_search_does_not_expose_descriptions(self):
        for query in ("Straße", "SIS-", "sis-9"):
            _, public = self.read(query=query)
            for item in public["context"]["entities"]["issues"]:
                self.assertNotIn("description", item)
                self.assertNotIn("url", item)

    def test_source_rejects_detail_fields_outside_exact_identifier_search(self):
        result, _ = self.read()
        for query in ("Straße", "SIS-", "SIS-10"):
            tampered = copy.deepcopy(result)
            tampered["after"]["query"] = query
            with self.subTest(query=query), self.assertRaises(RouteError):
                _public_result({"status": "verified_no_op", "linear_result": tampered})

    def test_source_rejects_partial_detail_projection(self):
        result, _ = self.read()
        for missing in ("description", "url"):
            tampered = copy.deepcopy(result)
            del tampered["after"]["entities"]["issues"][0][missing]
            with self.subTest(missing=missing), self.assertRaises(RouteError):
                _public_result({"status": "verified_no_op", "linear_result": tampered})

    def test_source_rejects_wrong_canonical_url(self):
        result, _ = self.read()
        result["after"]["entities"]["issues"][0]["url"] = "https://linear.app/sisyphusx/issue/SIS-10/other"
        with self.assertRaises(RouteError):
            _public_result({"status": "verified_no_op", "linear_result": result})

    def test_exact_search_rejects_duplicate_exact_entities(self):
        client = fixtures.ExecutionTests.WorkspaceReadClient()
        client.data["issues"][0]["url"] = "https://linear.app/sisyphusx/issue/SIS-9/fixture"
        client.data["issues"].append(copy.deepcopy(client.data["issues"][0]))
        with self.assertRaises(lane.ContractError):
            lane.execute_command(
                client, workspace_read_command("search_linear", entity_types=["issues"], query="SIS-9"),
                mode="apply",
            )

    def test_exact_search_rejects_missing_description_as_unknown(self):
        client = fixtures.ExecutionTests.WorkspaceReadClient()
        del client.data["issues"][0]["description"]
        client.data["issues"][0]["url"] = "https://linear.app/sisyphusx/issue/SIS-9/fixture"
        with self.assertRaises(lane.ContractError):
            lane.execute_command(
                client, workspace_read_command("search_linear", entity_types=["issues"], query="SIS-9"),
                mode="apply",
            )

    def test_exact_search_rejects_unsafe_description(self):
        for description in ("bad\x00text", "command_id: internal", "ghp_" + "A" * 40):
            with self.subTest(description=description), self.assertRaises((lane.ContractError, RouteError)):
                self.read(description=description)

    def test_exact_search_rejects_oversized_description(self):
        with self.assertRaises((lane.ContractError, RouteError)):
            self.read(description="x" * 10001)


if __name__ == "__main__":
    unittest.main()
