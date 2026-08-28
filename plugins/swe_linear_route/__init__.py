from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .audit import audit_route as bundled_audit_route
from .route import RouteError, SourceContext, route_request

SWE_LINEAR_REQUEST_SCHEMA = {
    "name": "swe_linear_request",
    "description": (
        "Route one bounded owner request about an exact SIS-N Linear issue through "
        "the project-manager Kanban lane. Use this for an exact request such as "
        "'Добавь к SIS-61 комментарий: ...'. The tool never mutates Linear from SWE; "
        "it creates or replays one audited wake-only task and returns its state."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "request": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4200,
                "description": "The owner's original request text, preserved exactly.",
            }
        },
        "required": ["request"],
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
        kb: Any | None = None,
        audit_func: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        if kb is None:
            from hermes_cli import kanban_db as kb_module

            kb = kb_module
        self.board = board
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
                created_by="swe",
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
                notifier_profile="swe",
                delivery_mode="wake",
                delivery_metadata={"chat_type": "dm"},
            )
        finally:
            conn.close()

    def audit_route(self, task_id: str, source: SourceContext) -> dict[str, Any]:
        return self.audit_func(
            self.kb.kanban_db_path(self.board),
            task_id=task_id,
            source_profile="swe",
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
                    author="swe-linear-route",
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
                actor="swe-linear-route",
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
    if runtime_profile != "swe":
        raise RouteError("runtime profile must be swe")
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


def handle_swe_linear_request(args: dict[str, Any], **kwargs: Any) -> str:
    """Validate the live SWE route and create/replay one PM Kanban task."""
    try:
        if not isinstance(args, dict) or set(args) != {"request"}:
            raise RouteError("tool input must contain exactly request")
        request = args["request"]
        if not isinstance(request, str):
            raise RouteError("request must be text")
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
        board = board_factory()
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
        name="swe_linear_request",
        toolset="swe-linear-route",
        schema=SWE_LINEAR_REQUEST_SCHEMA,
        handler=handle_swe_linear_request,
        description=SWE_LINEAR_REQUEST_SCHEMA["description"],
        emoji="🔁",
    )
