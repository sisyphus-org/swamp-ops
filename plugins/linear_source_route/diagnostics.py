"""Fixed source diagnostics: exception text is lookup input, never public data."""
from typing import Any


DIAGNOSTICS = {
    "invalid_request_shape": (
        "Request must match one bounded operation shape; check required fields and omit unsupported fields.",
        "correct_request",
    ),
    "invalid_text": (
        "Text must be non-empty where required, within field size limits, and free of credentials, control characters and reserved markers.", "correct_request",
    ),
    "invalid_priority": ("Priority must be High, Medium or Low.", "correct_request"),
    "unsafe_text": ("Text contains credential-shaped data, control characters or a reserved marker; remove unsafe content without echoing it.", "correct_request"),
    "request_too_large": ("Request exceeds the bounded serialized size; reduce or split the requested work into supported operations.", "correct_request"),
    "invalid_date": (
        "Date must be a valid calendar date in YYYY-MM-DD format or null.", "correct_request",
    ),
    "invalid_scope": (
        "Project and milestone must be objects containing an exact name and only an optional description; both scopes are mandatory.", "correct_request",
    ),
    "invalid_issue_spec": (
        "Issue must contain exactly title, description, state and priority.", "correct_request",
    ),
    "invalid_state": (
        "State for creation must be Backlog, Todo, Research, In Progress or In Review.", "correct_request",
    ),
    "source_session_mismatch": (
        "Handler session does not match the gateway source session.", "restore_source_context",
    ),
    "source_profile_mismatch": (
        "Gateway source profile does not match the runtime profile.", "restore_source_context",
    ),
    "source_profile_invalid": (
        "Runtime profile is not an allowed user-facing source profile.", "restore_source_context",
    ),
    "source_context_invalid": (
        "Source requires an exact persisted session and numeric chat/user/thread in a Telegram DM.", "restore_source_context",
    ),
    "result_unverified": (
        "Completed result did not pass safe public verification; reconcile the external outcome before retrying.", "reconcile_read_only",
    ),
    "os_error": ("Source storage or operating-system access failed.", "inspect_source_route"),
    "key_error": ("Required source routing data is unavailable.", "inspect_source_route"),
    "type_error": ("Source routing data has an unexpected type.", "inspect_source_route"),
    "value_error": ("Source routing data has an invalid value.", "inspect_source_route"),
    "route_unavailable": (
        "Source routing could not be safely verified.",
        "inspect_source_route",
    ),
}
REASONS = {
    "handler session id does not match gateway source session": "source_session_mismatch",
    "gateway source profile conflicts with runtime profile": "source_profile_mismatch",
    "runtime profile is not an allowed user-facing profile": "source_profile_invalid",
    "source profile is not an allowed user-facing profile": "source_profile_invalid",
    "source session id is invalid": "source_context_invalid",
    "source platform must be telegram": "source_context_invalid",
    "source chat must be a DM": "source_context_invalid",
    "source thread id must be a positive numeric id": "source_context_invalid",
    "source chat id must be a positive numeric id": "source_context_invalid",
    "source user id must be a positive numeric id": "source_context_invalid",
    "structured hierarchy request exceeds the bounded size limit": "request_too_large",
    "structured scoped issue request exceeds the bounded size limit": "request_too_large",
    "priority is not in the bounded allowlist": "invalid_priority",
    "structured project management request has invalid fields": "invalid_request_shape",
    "structured hierarchy request has invalid fields": "invalid_request_shape",
    "structured scoped issue request has invalid fields": "invalid_request_shape",
    "project contains unsupported fields": "invalid_request_shape",
    "milestone contains unsupported fields": "invalid_request_shape",
    "issue contains unsupported fields": "invalid_request_shape",
    "tool input must be an object": "invalid_request_shape",
    "request must be text": "invalid_request_shape",
    "tool input does not match a bounded request shape": "invalid_request_shape",
    "target_date must be ISO YYYY-MM-DD or null": "invalid_date",
    "target_date must be a valid calendar date": "invalid_date",
    "state is not in the safe-state allowlist": "invalid_state",
    "project must contain name": "invalid_scope",
    "milestone must contain name": "invalid_scope",
    "project must contain name and optional description": "invalid_scope",
    "milestone must contain name and optional description": "invalid_scope",
}
# Finite validator-generated labels only, never arbitrary path/payload captures.
for field in ("name", "new_name", "project", "description", "project.name", "milestone.name", "project.description", "milestone.description", "issue.title", "issue.description"):
    for maximum in (200, 10_000):
        REASONS[f"{field} must be a string of at most {maximum} characters"] = "invalid_text"
    REASONS[f"{field} must be non-empty"] = "invalid_text"
    for suffix in ("contains control characters", "contains the reserved marker", "contains credential-shaped data"):
        REASONS[f"{field} {suffix}"] = "unsafe_text"
for field in ("issue", *(f"sub_issues[{index}]" for index in range(10))):
    REASONS[f"{field} must contain exactly title, description, state, and priority"] = "invalid_issue_spec"
    REASONS[f"{field}.state is not in the safe-state allowlist"] = "invalid_state"
    REASONS[f"{field}.priority is not in the bounded allowlist"] = "invalid_priority"



SHAPE_MESSAGES = {
    "create_project": "The bounded create_project shape requires name and allows only description and target_date; omit project and unsupported fields.",
    "converge_hierarchy": "The bounded converge_hierarchy shape requires project and milestone objects with name, and an issue object with title; descriptions and a safe issue state are optional.",
    "create_standalone_issue": "The bounded create_standalone_issue shape requires project and milestone objects with name, and issue with exactly title, description, state and priority.",
    "converge_issue_tree": "The bounded converge_issue_tree shape requires named project/milestone objects, exact title/description/state/priority issue objects and 1-10 sub_issues.",
}


def rejection(exc: Exception, *, stage: str, operation: Any = None) -> dict[str, Any]:
    if isinstance(exc, OSError):
        code = "os_error"
    elif isinstance(exc, KeyError):
        code = "key_error"
    elif isinstance(exc, TypeError):
        code = "type_error"
    elif isinstance(exc, ValueError):
        code = "value_error"
    elif stage == "public_result":
        code = "result_unverified"
    elif stage == "validation":
        code = REASONS.get(str(exc), "route_unavailable")
    else:
        code = "route_unavailable"
    message, action = DIAGNOSTICS[code]
    if code == "invalid_request_shape" and isinstance(operation, str):
        message = SHAPE_MESSAGES.get(operation, message)
    return {
        "status": "rejected",
        "message": message,
        "error_code": code,
        "outcome": "not_attempted" if stage == "validation" else "unknown",
        "recovery": {"action": action if stage == "validation" else "reconcile_read_only"},
    }
