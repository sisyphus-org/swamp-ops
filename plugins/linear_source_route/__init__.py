from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .audit import audit_route as bundled_audit_route
from .route import RouteError, SourceContext, is_source_profile, route_request

LINEAR_SOURCE_REQUEST_SCHEMA = {
    "name": "linear_source_request",
    "description": (
        "Route one bounded Linear request from an allowed user-facing profile "
        "through the project-manager Kanban lane. Accepts an exact comment text, "
        "a structured change_state request, or a structured create_issue request. "
        "The calling profile never mutates Linear directly; the tool creates or "
        "replays one audited wake-only task and returns its state."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "request": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4200,
                "description": "Exact bounded comment request text.",
            },
            "operation": {"type": "string", "enum": ["change_state", "create_issue"]},
            "identifier": {
                "type": "string",
                "pattern": "^SIS-[1-9][0-9]*$",
            },
            "state": {
                "type": "string",
                "enum": ["Backlog", "Todo", "Research", "In Progress", "In Review"],
            },
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "description": {"type": "string", "maxLength": 10000},
            "parent_identifier": {
                "type": "string",
                "pattern": "^SIS-[1-9][0-9]*$",
            },
            "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
        },
        "oneOf": [
            {"required": ["request"]},
            {"required": ["operation", "identifier", "state"]},
            {
                "required": [
                    "operation",
                    "title",
                    "description",
                    "parent_identifier",
                    "state",
                    "priority",
                ]
            },
        ],
    },
}


def _task_dict(task: Any) -> dict[str, Any]:
    if isinstance(task, dict):
        return dict(task)
    fields = ("id", "status", "session_id", "idempotency_key", "result")
    return {field: getattr(task, field, None) for field in fields}


class HermesKanbanBoard:
    """Small adapter over the shipped Hermes Kanban DB primitives."""

    def __init__(
        self,
        *,
        board: str = "default",
        source_profile: str = "swe",
        kb: Any | None = None,
        audit_func: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        if not is_source_profile(source_profile):
            raise RouteError("source profile is not an allowed user-facing profile")
        if kb is None:
            from hermes_cli import kanban_db as kb_module

            kb = kb_module
        self.board = board
        self.source_profile = source_profile
        self.kb = kb
        self.audit_func = audit_func or bundled_audit_route

    def _connect(self):
        return self.kb.connect(board=self.board)

    def find_task(self, idempotency_key: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? "
                "AND status != 'archived' ORDER BY created_at DESC",
                (idempotency_key,),
            ).fetchall()
            if len(rows) > 1:
                raise RouteError("duplicate active tasks share the idempotency key")
            if not rows:
                return None
            task = self.kb.get_task(conn, rows[0]["id"])
            if task is None:
                raise RouteError("idempotency lookup returned a missing task")
            return _task_dict(task)
        finally:
            conn.close()

    def create_task(self, **kwargs: Any) -> dict[str, Any]:
        conn = self._connect()
        try:
            task_id = self.kb.create_task(
                conn,
                created_by=self.source_profile,
                workspace_kind="scratch",
                board=self.board,
                **kwargs,
            )
            task = self.kb.get_task(conn, task_id)
            if task is None:
                raise RouteError("Kanban create returned a missing task")
            return _task_dict(task)
        finally:
            conn.close()

    def set_wake_route(self, task_id: str, source: SourceContext) -> None:
        conn = self._connect()
        try:
            self.kb.add_notify_sub(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id=source.chat_id,
                thread_id=source.thread_id,
                user_id=source.user_id,
                chat_type="dm",
                notifier_profile=source.profile,
                delivery_mode="wake",
                delivery_metadata={"chat_type": "dm"},
            )
        finally:
            conn.close()

    def audit_route(self, task_id: str, source: SourceContext) -> dict[str, Any]:
        return self.audit_func(
            self.kb.kanban_db_path(self.board),
            task_id=task_id,
            source_profile=source.profile,
            chat_id=source.chat_id,
            user_id=source.user_id,
            source_thread_id=source.thread_id,
            source_session_id=source.session_id,
        )

    def release(self, task_id: str, reason: str) -> None:
        conn = self._connect()
        try:
            task = self.kb.get_task(conn, task_id)
            if task is None:
                raise RouteError("Kanban release target is missing")
            status = getattr(task, "status", None)
            if status == "triage":
                ok = self.kb.specify_triage_task(
                    conn,
                    task_id,
                    author="linear-source-route",
                )
                if not ok:
                    raise RouteError("triage release failed")
                released = self.kb.get_task(conn, task_id)
                if released is None or getattr(released, "status", None) != "ready":
                    raise RouteError("audited triage task did not reach ready")
                return
            ok, error = self.kb.promote_task(
                conn,
                task_id,
                actor="linear-source-route",
                reason=reason,
            )
            if not ok:
                raise RouteError(error or "Kanban promotion failed")
        finally:
            conn.close()


def _default_session_getter(name: str, default: str = "") -> str:
    from gateway.session_context import get_session_env

    return get_session_env(name, default)


def _default_runtime_profile_getter() -> str:
    """Return the profile selected by Hermes' resolved runtime home."""
    from hermes_cli.profiles import get_active_profile_name

    return get_active_profile_name()


def _source_context(
    *,
    handler_session_id: str,
    runtime_profile: str,
    session_getter: Callable[[str, str], str],
) -> SourceContext:
    if not is_source_profile(runtime_profile):
        raise RouteError("runtime profile is not an allowed user-facing profile")
    contextual_session_id = session_getter("HERMES_SESSION_ID", "")
    if (
        handler_session_id
        and contextual_session_id
        and handler_session_id != contextual_session_id
    ):
        raise RouteError("handler session id does not match gateway source session")
    session_id = handler_session_id or contextual_session_id
    contextual_profile = session_getter("HERMES_SESSION_PROFILE", "")
    if contextual_profile and contextual_profile != runtime_profile:
        raise RouteError("gateway source profile conflicts with runtime profile")
    return SourceContext(
        session_id=session_id,
        profile=runtime_profile,
        platform=session_getter("HERMES_SESSION_PLATFORM", ""),
        chat_id=session_getter("HERMES_SESSION_CHAT_ID", ""),
        user_id=session_getter("HERMES_SESSION_USER_ID", ""),
        chat_type=session_getter("HERMES_SESSION_CHAT_TYPE", ""),
        thread_id=session_getter("HERMES_SESSION_THREAD_ID", ""),
    )


def handle_linear_source_request(args: dict[str, Any], **kwargs: Any) -> str:
    """Validate one live user-facing source route and create or replay its PM task."""
    try:
        if not isinstance(args, dict):
            raise RouteError("tool input must be an object")
        if set(args) == {"request"}:
            request: Any = args["request"]
            if not isinstance(request, str):
                raise RouteError("request must be text")
        elif set(args) == {"operation", "identifier", "state"}:
            request = dict(args)
        elif set(args) == {
            "operation",
            "title",
            "description",
            "parent_identifier",
            "state",
            "priority",
        }:
            request = dict(args)
        else:
            raise RouteError("tool input does not match a bounded request shape")
        session_getter = kwargs.get("session_getter") or _default_session_getter
        runtime_profile_getter = (
            kwargs.get("runtime_profile_getter") or _default_runtime_profile_getter
        )
        source = _source_context(
            handler_session_id=str(kwargs.get("session_id") or ""),
            runtime_profile=str(runtime_profile_getter() or ""),
            session_getter=session_getter,
        )
        board_factory = kwargs.get("board_factory") or HermesKanbanBoard
        board = board_factory(source_profile=source.profile)
        result = route_request(request, source=source, board=board)
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (RouteError, KeyError, TypeError, ValueError, OSError) as exc:
        return json.dumps(
            {"status": "rejected", "error": str(exc)},
            ensure_ascii=False,
            sort_keys=True,
        )


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="linear_source_request",
        toolset="linear-source-route",
        schema=LINEAR_SOURCE_REQUEST_SCHEMA,
        handler=handle_linear_source_request,
        description=LINEAR_SOURCE_REQUEST_SCHEMA["description"],
        emoji="🔁",
    )
