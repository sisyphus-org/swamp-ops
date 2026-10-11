import importlib
import sys
import unittest
from unittest import mock

try:
    import kanban_compat
except ImportError:  # discovered as package (-t .)
    from tests import kanban_compat

PLUGIN_MODULES = (
    "plugins.linear_source_route",
    "plugins.ops_broker",
    "plugins.ops_broker.broker",
    "plugins.personal_assistant_calendar",
    "plugins.project_manager_linear",
)


def _resolvers():
    yield "tests.kanban_compat", kanban_compat._KanbanCompat()
    for name in PLUGIN_MODULES:
        try:
            module = importlib.import_module(name)
        except Exception:  # plugin import needs Hermes; covered elsewhere
            continue
        yield name, module._KanbanCompat()


class KanbanCompatResolverTests(unittest.TestCase):
    def test_resolves_split_module_names(self):
        for label, resolver in _resolvers():
            with self.subTest(resolver=label):
                self.assertTrue(callable(resolver.connect))
                self.assertTrue(callable(resolver.get_task))

    def test_missing_candidate_module_is_skipped(self):
        real = importlib.import_module

        def fake(name, *args, **kwargs):
            if name == "hermes_cli.kanban_db_boards":
                raise ModuleNotFoundError(name=name)
            return real(name, *args, **kwargs)

        for label, resolver in _resolvers():
            with self.subTest(resolver=label), mock.patch("importlib.import_module", fake):
                self.assertTrue(callable(resolver.connect))

    def test_nested_dependency_failure_is_reraised(self):
        real = importlib.import_module

        def fake(name, *args, **kwargs):
            if name == "hermes_cli.kanban_db_connect":
                raise ModuleNotFoundError("No module named 'sqlite_vendor'", name="sqlite_vendor")
            return real(name, *args, **kwargs)

        for label, resolver in _resolvers():
            with self.subTest(resolver=label), mock.patch("importlib.import_module", fake):
                with self.assertRaises(ModuleNotFoundError):
                    resolver.connect

    def test_other_import_errors_are_reraised(self):
        real = importlib.import_module

        def fake(name, *args, **kwargs):
            if name == "hermes_cli.kanban_db_connect":
                raise ImportError("cannot import name 'x'")
            return real(name, *args, **kwargs)

        for label, resolver in _resolvers():
            with self.subTest(resolver=label), mock.patch("importlib.import_module", fake):
                with self.assertRaises(ImportError):
                    resolver.connect


if __name__ == "__main__":
    unittest.main()
