"""Test-side Kanban API resolver across Hermes core versions.

Hermes core dropped re-exports (connect, add_notify_sub, heartbeat_worker, ...)
from ``hermes_cli.kanban_db`` in favour of split modules. Tests use this proxy so
they exercise the same resolution order as the plugins.
"""
import importlib

_MODULES = (
    "hermes_cli.kanban_db",
    "hermes_cli.kanban_db_connect",
    "hermes_cli.kanban_db_notify",
    "hermes_cli.kanban_db_dispatch",
    "hermes_cli.kanban_db_boards",
)


class _KanbanCompat:
    def __getattr__(self, name):
        for module_name in _MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(f"Hermes Kanban API has no attribute {name!r}")


kb = _KanbanCompat()


def connect_target() -> str:
    """Dotted path to patch for ``connect`` on the running Hermes core."""
    if hasattr(importlib.import_module("hermes_cli.kanban_db"), "connect"):
        return "hermes_cli.kanban_db.connect"
    return "hermes_cli.kanban_db_connect.connect"
