#!/usr/bin/env python3
"""Local no-network SIS-61 smoke using the shipped Hermes Kanban DB."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import kanban_db as kb  # noqa: E402
from plugins.swe_linear_route import HermesKanbanBoard  # noqa: E402
from plugins.swe_linear_route.route import SourceContext, route_request  # noqa: E402


def load_local_audit():
    path = ROOT / "scripts" / "kanban_source_route_audit.py"
    spec = importlib.util.spec_from_file_location("sis61_local_route_audit", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load local route audit")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.audit_route


def main() -> int:
    temp_root = ROOT / ".tmp"
    temp_root.mkdir(exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sis61-route-", dir=temp_root))
    db_path = workdir / "kanban.db"
    old_db = os.environ.get("HERMES_KANBAN_DB")
    os.environ["HERMES_KANBAN_DB"] = str(db_path)
    try:
        kb.init_db(db_path=db_path)
        board = HermesKanbanBoard(
            board="default",
            kb=kb,
            audit_func=load_local_audit(),
        )
        source = SourceContext(
            session_id="20260828_120000_abcdef12",
            profile="swe",
            platform="telegram",
            chat_id="442308262",
            user_id="442308262",
            chat_type="dm",
            thread_id="",
        )
        request = "Добавь к SIS-61 комментарий: SIS-61 E2E proof A."
        first = route_request(request, source=source, board=board)
        replay = route_request(request, source=source, board=board)

        conn = kb.connect(db_path=db_path)
        try:
            tasks = conn.execute(
                "SELECT id, status, assignee, session_id, idempotency_key FROM tasks"
            ).fetchall()
            subs = conn.execute(
                "SELECT task_id, platform, chat_id, thread_id, user_id, chat_type, "
                "notifier_profile, delivery_mode, delivery_metadata "
                "FROM kanban_notify_subs"
            ).fetchall()
        finally:
            conn.close()

        task_rows = [dict(row) for row in tasks]
        sub_rows = [dict(row) for row in subs]
        if len(task_rows) != 1 or len(sub_rows) != 1:
            raise RuntimeError("smoke expected exactly one task and subscription")
        task = task_rows[0]
        sub = sub_rows[0]
        if task["status"] != "ready" or task["assignee"] != "project-manager":
            raise RuntimeError("task was not promoted to the PM ready lane")
        if task["session_id"] != source.session_id:
            raise RuntimeError("task did not persist the exact source session")
        if (
            sub["platform"] != "telegram"
            or sub["chat_id"] != source.chat_id
            or (sub["thread_id"] or None) is not None
            or sub["user_id"] != source.user_id
            or sub["chat_type"] != "dm"
            or sub["notifier_profile"] != "swe"
            or sub["delivery_mode"] != "wake"
            or json.loads(sub["delivery_metadata"]) != {"chat_type": "dm"}
        ):
            raise RuntimeError("subscription is not the exact source root-DM wake route")
        if first["status"] != "queued" or replay["status"] != "already_in_flight":
            raise RuntimeError("new/replay statuses violated the route contract")
        print(
            json.dumps(
                {
                    "result": "pass",
                    "readOnlyExternal": True,
                    "taskCount": len(task_rows),
                    "subscriptionCount": len(sub_rows),
                    "taskStatus": task["status"],
                    "sourceSessionExact": True,
                    "deliveryMode": sub["delivery_mode"],
                    "threadId": sub["thread_id"] or None,
                    "replay": replay["status"],
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if old_db is None:
            os.environ.pop("HERMES_KANBAN_DB", None)
        else:
            os.environ["HERMES_KANBAN_DB"] = old_db
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            temp_root.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
