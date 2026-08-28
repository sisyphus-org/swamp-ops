#!/usr/bin/env python3
"""Read-only audit for one source-profile Kanban notification route."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


HERMES_ROOT = Path("/Users/hermes/.hermes")
BOARD_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TASK_ID = re.compile(r"^t_[a-f0-9]{8}$")
PROFILE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
NUMERIC_ID = re.compile(r"^[1-9][0-9]*$")

TERMINAL_KINDS = {
    "completed",
    "blocked",
    "gave_up",
    "crashed",
    "timed_out",
    "review_requested",
    "block_loop_detected",
}


class AuditError(RuntimeError):
    """The route database or expected source route is invalid."""


def board_db_path(slug: str, *, root: Path = HERMES_ROOT) -> Path:
    """Resolve a validated board slug to its fixed production database path."""
    if not BOARD_SLUG.fullmatch(slug):
        raise AuditError("board slug is invalid")
    if slug == "default":
        return root / "kanban.db"
    return root / "kanban" / "boards" / slug / "kanban.db"


def audit_route(
    db_path: Path,
    *,
    task_id: str,
    source_profile: str,
    chat_id: str,
    thread_id: str,
) -> dict[str, Any]:
    """Verify one exact source-owned Telegram thread subscription."""
    if not TASK_ID.fullmatch(task_id):
        raise AuditError("task_id must be exact t_<8 hex>")
    if not PROFILE.fullmatch(source_profile):
        raise AuditError("source_profile is invalid")
    if source_profile == "broker":
        raise AuditError("broker cannot own source-profile Telegram delivery")
    if not NUMERIC_ID.fullmatch(chat_id) or not NUMERIC_ID.fullmatch(thread_id):
        raise AuditError("chat_id and thread_id must be exact positive numeric IDs")
    if not db_path.is_file():
        raise AuditError(f"board database does not exist: {db_path}")
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        if len(rows) != 1:
            raise AuditError(f"expected exactly one subscription for {task_id}")
        route = dict(rows[0])
        raw_metadata = route.get("delivery_metadata")
        try:
            metadata = json.loads(raw_metadata) if raw_metadata else {}
        except (TypeError, json.JSONDecodeError):
            metadata = None
        expected = {
            "platform": "telegram",
            "chat_id": chat_id,
            "thread_id": thread_id,
            "chat_type": "thread",
            "notifier_profile": source_profile,
            "delivery_mode": "notify+wake",
        }
        mismatches = {
            key: {"expected": value, "actual": route.get(key)}
            for key, value in expected.items()
            if route.get(key) != value
        }
        expected_metadata: dict[str, Any] = {
            "thread_id": thread_id,
            "telegram_dm_topic_created_for_send": True,
        }
        if metadata != expected_metadata:
            mismatches["delivery_metadata"] = {
                "expected": expected_metadata,
                "actual": metadata,
            }
        pending = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? AND id > ? ORDER BY id",
                (task_id, int(route.get("last_event_id") or 0)),
            ).fetchall()
            if row["kind"] in TERMINAL_KINDS
        ]
        return {
            "result": "pass" if not mismatches else "drift",
            "readOnly": True,
            "task_id": task_id,
            "source_profile": source_profile,
            "route": {
                **{
                    key: route.get(key)
                    for key in (
                        "platform",
                        "chat_id",
                        "thread_id",
                        "chat_type",
                        "notifier_profile",
                        "delivery_mode",
                        "last_event_id",
                    )
                },
                "delivery_metadata": metadata,
            },
            "mismatches": mismatches,
            "pending_terminal_events": pending,
        }
    finally:
        conn.close()


def emit(payload: dict[str, Any]) -> None:
    """Print one stable JSON audit result."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    """Audit one fixed source-profile route without writes."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--source-profile", required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--thread-id", required=True)
    args = parser.parse_args()
    try:
        report = audit_route(
            board_db_path(args.board),
            task_id=args.task_id,
            source_profile=args.source_profile,
            chat_id=args.chat_id,
            thread_id=args.thread_id,
        )
        emit(report)
        return 0 if report["result"] == "pass" else 1
    except (AuditError, sqlite3.Error) as exc:
        emit(
            {
                "result": "error",
                "readOnly": True,
                "issues": [str(exc)],
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
