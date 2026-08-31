from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .audit import audit_route as bundled_audit_route
from .route import (
    CREDENTIAL_SHAPES,
    RouteError,
    SourceContext,
    is_source_profile,
    route_request,
)


PUBLIC_ISSUE_IDENTIFIER = re.compile(r"^SIS-[1-9][0-9]*$")
PUBLIC_ISSUE_URL = re.compile(
    r"^https://linear\.app/[A-Za-z0-9_-]+/issue/(SIS-[1-9][0-9]*)/"
    r"[A-Za-z0-9][A-Za-z0-9_-]*$"
)
PUBLIC_INTERNAL_MARKER = re.compile(
    r"(?i)(?:\b(?:task_id|run_id|idempotency|delivery_key|command_id|"
    r"correlation_id)\b|\bt_[a-f0-9]{8,}\b|\blinear(?::|-)"
    r"(?:delivery:)?v[0-9]+(?:[:.]|\b)|\blinear-(?:command|result|kanban-task)\.v[0-9]+\b|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}\b)"
)
PUBLIC_STATES = {"Backlog", "Todo", "Research", "In Progress", "In Review"}
PUBLIC_MISMATCH_FIELDS = (
    r"(?:id/title|description|state|priority|parent|project|milestone|team)"
)
PUBLIC_MISMATCH_LIST = rf"{PUBLIC_MISMATCH_FIELDS}(?:, {PUBLIC_MISMATCH_FIELDS})*"
PUBLIC_BLOCK_REASON_PATTERNS = {
    "create_issue": (
        re.compile(rf"^create_issue read-back mismatched fields: {PUBLIC_MISMATCH_LIST}$"),
        re.compile(r"^create_issue idempotency key conflicts with another request$"),
        re.compile(r"^create_issue parent is not in the SIS team$"),
        re.compile(r"^exact Linear parent not found: SIS-[1-9][0-9]*$"),
        re.compile(
            r"^exact workflow state not found: (?:Backlog|Todo|Research|In Progress|In Review)$"
        ),
    ),
    "converge_hierarchy": (
        re.compile(r"^exact SIS team was not found$"),
        re.compile(
            r"^ambiguous scoped Linear match for (?:team SIS|project id|project name|"
            r"workflow state|milestone id|milestone name|issue id|created issue read-back)$"
        ),
        re.compile(
            r"^(?:project|milestone) supplied description conflicts with live state$"
        ),
        re.compile(
            r"^(?:project|milestone) (?:deterministic id|exact-name match) "
            r"conflicts with live scope or name$"
        ),
        re.compile(r"^issue deterministic id conflicts with live hierarchy$"),
        re.compile(r"^issue title already exists with a different deterministic id$"),
        re.compile(
            r"^(?:project|milestone|issue|hierarchy) exact read-back verification failed$"
        ),
        re.compile(
            rf"^converge_hierarchy read-back mismatched fields: {PUBLIC_MISMATCH_LIST}$"
        ),
    ),
    "create_standalone_issue": (
        re.compile(r"^exact SIS team was not found$"),
        re.compile(r"^exact existing (?:project|milestone) was not found$"),
        re.compile(r"^ambiguous scoped Linear match for (?:team SIS|project name|milestone name|workflow state|issue id|issue title)$"),
        re.compile(r"^(?:project|milestone) supplied description conflicts with live state$"),
        re.compile(r"^(?:project|milestone) exact-name match conflicts with live scope or name$"),
        re.compile(
            rf"^create_standalone_issue read-back mismatched fields: {PUBLIC_MISMATCH_LIST}$"
        ),
    ),
    "converge_issue_tree": (
        re.compile(r"^exact SIS team was not found$"),
        re.compile(r"^exact existing (?:project|milestone) was not found$"),
        re.compile(r"^ambiguous scoped Linear match for (?:team SIS|project name|milestone name|workflow state|issue id|issue title)$"),
        re.compile(r"^(?:project|milestone) supplied description conflicts with live state$"),
        re.compile(r"^(?:project|milestone) exact-name match conflicts with live scope or name$"),
        re.compile(
            rf"^converge_issue_tree read-back mismatched fields: {PUBLIC_MISMATCH_LIST}$"
        ),
    ),
}

LINEAR_SOURCE_REQUEST_SCHEMA = {
    "name": "linear_source_request",
    "description": (
        "Route one bounded Linear request from an allowed user-facing profile "
        "through the project-manager Kanban lane. Accepts an exact comment text, "
        "a structured state/child request, one bounded hierarchy request, one "
        "standalone issue in an exact existing scope, or one top-level issue "
        "plus 1-10 explicit sub-issues. The calling profile never mutates Linear "
        "directly; the tool creates or replays one audited wake-only task and "
        "returns its state."
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
            "operation": {
                "type": "string",
                "enum": [
                    "change_state",
                    "create_issue",
                    "converge_hierarchy",
                    "create_standalone_issue",
                    "converge_issue_tree",
                ],
            },
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
            "project": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": {"type": "string", "maxLength": 10000},
                },
                "required": ["name"],
            },
            "milestone": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": {"type": "string", "maxLength": 10000},
                },
                "required": ["name"],
            },
            "issue": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": {"type": "string", "maxLength": 10000},
                    "state": {
                        "type": "string",
                        "enum": ["Backlog", "Todo", "Research", "In Progress", "In Review"],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["High", "Medium", "Low"],
                    },
                },
                "required": ["title"],
            },
            "sub_issues": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": {"type": "string", "maxLength": 10000},
                        "state": {
                            "type": "string",
                            "enum": ["Backlog", "Todo", "Research", "In Progress", "In Review"],
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["High", "Medium", "Low"],
                        },
                    },
                    "required": ["title", "description", "state", "priority"],
                },
            },
        },
        "oneOf": [
            {"required": ["request"]},
            {
                "required": ["operation", "identifier", "state"],
                "properties": {"operation": {"const": "change_state"}},
            },
            {
                "required": [
                    "operation",
                    "title",
                    "description",
                    "parent_identifier",
                    "state",
                    "priority",
                ],
                "properties": {"operation": {"const": "create_issue"}},
            },
            {
                "required": ["operation", "project", "milestone", "issue"],
                "properties": {"operation": {"const": "converge_hierarchy"}},
            },
            {
                "required": ["operation", "project", "milestone", "issue"],
                "properties": {"operation": {"const": "create_standalone_issue"}},
            },
            {
                "required": [
                    "operation",
                    "project",
                    "milestone",
                    "issue",
                    "sub_issues",
                ],
                "properties": {"operation": {"const": "converge_issue_tree"}},
            },
        ],
    },
}


def _task_dict(task: Any) -> dict[str, Any]:
    if isinstance(task, dict):
        return dict(task)
    fields = ("id", "status", "session_id", "idempotency_key", "body", "result")
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

    def get_or_create_task(
        self, delivery_key: str, **kwargs: Any
    ) -> tuple[dict[str, Any], bool]:
        """Atomically return the active delivery task or create it once."""
        if kwargs.get("idempotency_key") != delivery_key:
            raise RouteError("delivery key does not match task idempotency key")
        conn = self._connect()
        try:
            with self.kb.write_txn(conn):
                rows = conn.execute(
                    "SELECT id FROM tasks WHERE idempotency_key = ? "
                    "AND status != 'archived' ORDER BY created_at DESC",
                    (delivery_key,),
                ).fetchall()
                if len(rows) > 1:
                    raise RouteError("duplicate active tasks share the delivery key")
                if rows:
                    task = self.kb.get_task(conn, rows[0]["id"])
                    if task is None:
                        raise RouteError("delivery lookup returned a missing task")
                    return _task_dict(task), False
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
                return _task_dict(task), True
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

    def block_reason(self, task_id: str) -> str | None:
        """Return the latest persisted blocker reason for one exact task."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            raw = row["payload"]
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                return None
            reason = payload.get("reason") if isinstance(payload, dict) else None
            return reason if isinstance(reason, str) else None
        finally:
            conn.close()

    def release(self, task_id: str, reason: str) -> None:
        conn = self._connect()
        try:
            task = self.kb.get_task(conn, task_id)
            if task is None:
                raise RouteError("Kanban release target is missing")
            status = getattr(task, "status", None)
            if status == "ready":
                return
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


def _public_text(value: Any, label: str, *, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
        or PUBLIC_INTERNAL_MARKER.search(value)
    ):
        raise RouteError(f"verified result has an invalid public {label}")
    return value


def _public_issue_target(target: Any) -> dict[str, Any]:
    if not isinstance(target, dict) or target.get("type") != "issue":
        raise RouteError("verified result lacks a public issue target")
    identifier = target.get("identifier")
    url = target.get("url")
    if not isinstance(identifier, str) or not PUBLIC_ISSUE_IDENTIFIER.fullmatch(identifier):
        raise RouteError("verified result has an invalid public issue identifier")
    match = PUBLIC_ISSUE_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None or match.group(1) != identifier:
        raise RouteError("verified result has an invalid canonical Linear URL")
    return {"type": "issue", "identifier": identifier, "url": url}


def _public_target(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return only validated user-relevant target facts from one PM result."""
    operation = result.get("operation")
    after = result.get("after")
    if operation == "converge_hierarchy":
        if not isinstance(after, dict):
            raise RouteError("verified hierarchy result lacks public completion facts")
        issue = after.get("issue")
        project = after.get("project")
        milestone = after.get("milestone")
        if (
            not isinstance(issue, dict)
            or not isinstance(project, dict)
            or not isinstance(milestone, dict)
        ):
            raise RouteError("verified hierarchy result lacks public completion facts")
        public_target = _public_issue_target({"type": "issue", **issue})
        public_target["title"] = _public_text(issue.get("title"), "issue title")
        state = issue.get("state")
        if state is not None:
            if state not in PUBLIC_STATES:
                raise RouteError("verified hierarchy result has an invalid public state")
            public_target["state"] = state
        context = {
            "project": _public_text(project.get("name"), "project name"),
            "milestone": _public_text(milestone.get("name"), "milestone name"),
        }
        return public_target, context

    if operation in {"create_standalone_issue", "converge_issue_tree"}:
        if not isinstance(after, dict):
            raise RouteError("verified scoped issue result lacks public completion facts")
        issue = after.get("issue")
        project = after.get("project")
        milestone = after.get("milestone")
        if (
            not isinstance(issue, dict)
            or not isinstance(project, dict)
            or not isinstance(milestone, dict)
        ):
            raise RouteError("verified scoped issue result lacks public completion facts")
        public_target = _public_issue_target(result.get("target"))
        if (
            issue.get("identifier") != public_target["identifier"]
            or issue.get("url") != public_target["url"]
        ):
            raise RouteError("verified scoped issue result conflicts with its public target")
        public_target["title"] = _public_text(issue.get("title"), "issue title")
        state = issue.get("state")
        if state not in PUBLIC_STATES:
            raise RouteError("verified scoped issue result lacks a public state")
        public_target["state"] = state
        context: dict[str, Any] = {
            "project": _public_text(project.get("name"), "project name"),
            "milestone": _public_text(milestone.get("name"), "milestone name"),
        }
        if operation == "converge_issue_tree":
            children = after.get("sub_issues")
            if not isinstance(children, list) or not 1 <= len(children) <= 10:
                raise RouteError("verified issue tree lacks bounded sub-issue facts")
            public_children = []
            for child in children:
                if not isinstance(child, dict):
                    raise RouteError("verified issue tree has invalid sub-issue facts")
                target = _public_issue_target({"type": "issue", **child})
                target["title"] = _public_text(child.get("title"), "sub-issue title")
                public_children.append(target)
            context["sub_issues"] = public_children
        return public_target, context

    public_target = _public_issue_target(result.get("target"))
    if not isinstance(after, dict):
        raise RouteError("verified result lacks public completion facts")
    if operation == "change_state":
        state = after.get("state")
        if state not in PUBLIC_STATES:
            raise RouteError("verified state result lacks a public state")
        public_target["state"] = state
    elif operation == "create_issue":
        if (
            after.get("identifier") != public_target["identifier"]
            or after.get("url") != public_target["url"]
        ):
            raise RouteError("verified create result conflicts with its public target")
        public_target["title"] = _public_text(after.get("title"), "issue title")
        state = after.get("state")
        if state not in PUBLIC_STATES:
            raise RouteError("verified create result lacks a public state")
        public_target["state"] = state
    elif operation not in {"add_comment", "read_issue"}:
        raise RouteError("verified result has an unsupported public operation")
    return public_target, None


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Hide routing/protocol metadata from the user-facing model tool result."""
    status = result.get("status")
    if status in {"queued", "already_in_flight"}:
        return {"status": "queued"}
    if status == "blocked":
        operation = result.get("operation")
        reason = result.get("reason")
        prefix = "Linear command failed: "
        if isinstance(reason, str) and reason.startswith(prefix):
            reason = reason[len(prefix) :]
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 500
            or any(ord(char) < 32 for char in reason)
            or PUBLIC_INTERNAL_MARKER.search(reason)
            or any(pattern.search(reason) for pattern in CREDENTIAL_SHAPES)
            or "[credential-redacted]" in reason
            or not isinstance(operation, str)
            or not any(
                pattern.fullmatch(reason)
                for pattern in PUBLIC_BLOCK_REASON_PATTERNS.get(operation, ())
            )
        ):
            message = "Не удалось выполнить запрос: безопасная причина недоступна."
        else:
            message = f"Не удалось выполнить: {reason.rstrip('.')}."
        return {
            "status": "blocked",
            "message": message,
        }
    if status == "verified_no_op":
        verified = result.get("linear_result")
        if not isinstance(verified, dict) or verified.get("verified") is not True:
            raise RouteError("completed replay lacks a verified result")
        outcome = verified.get("result")
        if outcome not in {"applied", "no_op", "read"}:
            raise RouteError("completed replay has an invalid public outcome")
        public: dict[str, Any] = {
            "status": "completed",
            "changed": outcome == "applied",
        }
        target, context = _public_target(verified)
        public["target"] = target
        if context is not None:
            public["context"] = context
        return public
    raise RouteError("routing returned an unsupported public status")


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
        elif set(args) == {"operation", "project", "milestone", "issue"}:
            request = dict(args)
        elif set(args) == {
            "operation",
            "project",
            "milestone",
            "issue",
            "sub_issues",
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
        internal_result = route_request(request, source=source, board=board)
        return json.dumps(
            _public_result(internal_result), ensure_ascii=False, sort_keys=True
        )
    except (RouteError, KeyError, TypeError, ValueError, OSError):
        return json.dumps(
            {
                "status": "rejected",
                "message": "Не удалось безопасно обработать запрос.",
            },
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
