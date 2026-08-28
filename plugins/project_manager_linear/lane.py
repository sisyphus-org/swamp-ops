#!/usr/bin/env python3
"""Policy-bounded Linear command lane for the project-manager profile."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ISSUE_IDENTIFIER = re.compile(r"^SIS-[1-9][0-9]*$")
PROFILE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{7,199}$")
OPERATIONS = {"read_issue", "change_state", "add_comment"}
SAFE_STATES = {"Backlog", "Todo", "Research", "In Progress", "In Review"}
OWNER_CONTROLLED_STATES = {"Done", "Canceled", "Duplicate"}
RESERVED_COMMENT_MARKER = "<!-- linear-command:v1"
MAX_COMMENT_LENGTH = 4000
API_URL = "https://api.linear.app/graphql"
COMMAND_ROOT = Path(__file__).parents[2] / "commands" / "linear"
ISSUE_QUERY = """
query LaneIssue($id: String!) {
  issue(id: $id) {
    id identifier title url
    state { id name type }
    team { id key }
  }
}
"""
TEAM_STATES_QUERY = """
query LaneStates($teamId: String!) {
  team(id: $teamId) {
    states(first: 100) { nodes { id name type } pageInfo { hasNextPage } }
  }
}
"""
COMMENTS_QUERY = """
query LaneComments($issueId: String!) {
  issue(id: $issueId) {
    comments(first: 100) { nodes { id body } pageInfo { hasNextPage } }
  }
}
"""
ISSUE_UPDATE = """
mutation LaneState($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success }
}
"""
COMMENT_CREATE = """
mutation LaneComment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success }
}
"""
ROOT_FIELDS = {
    "schema_version",
    "command_id",
    "correlation_id",
    "idempotency_key",
    "source_profile",
    "operation",
    "target",
    "change",
    "policy",
}


class ContractError(RuntimeError):
    """The command or live state violates the bounded lane contract."""


class LinearClient:
    """Minimal fixed-query Linear GraphQL client for the MVP command lane."""

    def __init__(self, token: str, endpoint: str = API_URL) -> None:
        token = token.strip()
        if not token:
            raise ContractError("LINEAR_TOKEN is empty")
        self.authorization = token
        self.endpoint = endpoint

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a fixed query with bounded timeout and normalized errors."""
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={
                "Authorization": self.authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            raise ContractError(f"Linear API HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ContractError(f"Linear API request failed: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContractError("Linear API response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ContractError("Linear API response root was not an object")
        if payload.get("errors"):
            messages = [item.get("message", "unknown GraphQL error") for item in payload["errors"]]
            raise ContractError("Linear GraphQL error: " + "; ".join(messages))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ContractError("Linear API response did not contain data")
        return data

    def get_issue(self, identifier: str) -> dict[str, Any] | None:
        """Resolve one issue by its exact identifier."""
        return self.execute(ISSUE_QUERY, {"id": identifier}).get("issue")

    def list_states(self, team_id: str) -> list[dict[str, Any]]:
        """Return all supported workflow states or fail on pagination."""
        connection = self.execute(TEAM_STATES_QUERY, {"teamId": team_id})["team"]["states"]
        if connection["pageInfo"]["hasNextPage"]:
            raise ContractError("team exceeds the supported 100-state limit")
        return connection["nodes"]

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        """Return comments needed for marker-based replay detection."""
        connection = self.execute(COMMENTS_QUERY, {"issueId": issue_id})["issue"]["comments"]
        if connection["pageInfo"]["hasNextPage"]:
            raise ContractError("issue exceeds the supported 100-comment idempotency limit")
        return connection["nodes"]

    def update_issue_state(self, issue_id: str, state_id: str) -> None:
        """Apply only an issue stateId mutation."""
        result = self.execute(ISSUE_UPDATE, {"id": issue_id, "input": {"stateId": state_id}})["issueUpdate"]
        if result.get("success") is not True:
            raise ContractError("Linear state mutation did not succeed")

    def create_comment(self, issue_id: str, body: str) -> None:
        """Create one comment with the internally generated replay marker."""
        result = self.execute(COMMENT_CREATE, {"input": {"issueId": issue_id, "body": body}})["commentCreate"]
        if result.get("success") is not True:
            raise ContractError("Linear comment mutation did not succeed")


def validate_command(raw: Any) -> dict[str, Any]:
    """Validate the exact linear-command.v1 envelope and return it unchanged."""
    if not isinstance(raw, dict) or set(raw) != ROOT_FIELDS:
        raise ContractError("command must contain exactly the linear-command.v1 fields")
    if raw["schema_version"] != "linear-command.v1":
        raise ContractError("schema_version must equal linear-command.v1")
    for field in ("command_id", "correlation_id"):
        try:
            value = uuid.UUID(str(raw[field]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContractError(f"{field} must be a UUID") from exc
        if value.version != 4:
            raise ContractError(f"{field} must be a UUIDv4")
    key = raw["idempotency_key"]
    if not isinstance(key, str) or not IDEMPOTENCY_KEY.fullmatch(key):
        raise ContractError(
            "idempotency_key must be 8-200 allowlisted printable characters"
        )
    if not PROFILE_NAME.fullmatch(str(raw["source_profile"])):
        raise ContractError("source_profile is invalid")
    if raw["operation"] not in OPERATIONS:
        raise ContractError("operation is not allowed")
    target = raw["target"]
    if not isinstance(target, dict) or set(target) != {"type", "identifier"}:
        raise ContractError("target must contain exactly type and identifier")
    if target["type"] != "issue" or not ISSUE_IDENTIFIER.fullmatch(
        str(target["identifier"])
    ):
        raise ContractError("target must be an exact SIS-N issue identifier")
    change = raw["change"]
    if not isinstance(change, dict):
        raise ContractError("change must be an object")
    operation = raw["operation"]
    if operation == "read_issue":
        if change:
            raise ContractError("read_issue change must be empty")
    elif operation == "change_state":
        if set(change) != {"state"} or not isinstance(change.get("state"), str):
            raise ContractError("change_state supports exactly one state field")
        state = change["state"]
        if state in OWNER_CONTROLLED_STATES:
            raise ContractError(f"state {state} is owner-controlled and unavailable in MVP")
        if state not in SAFE_STATES:
            raise ContractError("requested state is not in the exact safe-state allowlist")
    elif operation == "add_comment":
        if set(change) != {"body"} or not isinstance(change.get("body"), str):
            raise ContractError("add_comment supports exactly one comment body")
        body = change["body"]
        if not body.strip() or len(body) > MAX_COMMENT_LENGTH:
            raise ContractError("comment body must be 1-4000 characters")
        if RESERVED_COMMENT_MARKER in body:
            raise ContractError("comment body contains the reserved marker")
    if raw["policy"] != {"mode": "standard"}:
        raise ContractError("policy must be the standard fail-closed lane")
    return raw


def issue_snapshot(issue: dict[str, Any]) -> dict[str, Any]:
    """Return validated bounded issue fields allowed in a typed result."""
    state = issue.get("state")
    required_strings = {
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "url": issue.get("url"),
        "state": state.get("name") if isinstance(state, dict) else None,
    }
    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in required_strings.values()
    ):
        raise ContractError("Linear issue payload is missing required bounded fields")
    return required_strings


def result_base(command: dict[str, Any], issue: dict[str, Any], mode: str) -> dict[str, Any]:
    """Build the common linear-result.v1 envelope without raw API payloads."""
    return {
        "schema_version": "linear-result.v1",
        "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "operation": command["operation"],
        "mode": mode,
        "target": {
            "type": "issue",
            "identifier": issue["identifier"],
            "url": issue["url"],
        },
    }


def command_fingerprint(command: dict[str, Any]) -> tuple[str, str, str]:
    """Return key hash, semantic request hash, and durable comment marker."""
    semantic = {
        field: command[field]
        for field in ("source_profile", "operation", "target", "change", "policy")
    }
    key_hash = hashlib.sha256(command["idempotency_key"].encode()).hexdigest()
    request_hash = hashlib.sha256(
        json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    marker = f"<!-- linear-command:v1 key={key_hash} request={request_hash} -->"
    return key_hash, request_hash, marker


def load_journal(path: Path) -> dict[str, str]:
    """Load a hash-only idempotency journal and fail closed on corruption."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"idempotency journal is unreadable: {path}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise ContractError("idempotency journal has an invalid shape")
    return raw


def write_journal(path: Path, entries: dict[str, str]) -> None:
    """Atomically persist only hashed idempotency keys and request hashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(entries, sort_keys=True) + "\n")
    temporary.replace(path)


@contextmanager
def command_lock(journal_path: Path):
    """Serialize all local mutation applies sharing one journal."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = journal_path.with_suffix(journal_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def execute_command(
    client: Any,
    raw: Any,
    *,
    mode: str,
    journal_path: Path | None = None,
    _lock_held: bool = False,
) -> dict[str, Any]:
    """Plan or execute one validated exact-target command."""
    if mode not in {"plan", "apply"}:
        raise ContractError("mode must be plan or apply")
    command = validate_command(raw)
    key_hash, request_hash, _ = command_fingerprint(command)
    mutation_apply = mode == "apply" and command["operation"] != "read_issue"
    if mutation_apply and not _lock_held:
        if journal_path is None:
            raise ContractError("apply mutations require an idempotency journal")
        with command_lock(journal_path):
            return execute_command(
                client,
                command,
                mode=mode,
                journal_path=journal_path,
                _lock_held=True,
            )
    journal: dict[str, str] = {}
    if mutation_apply and journal_path:
        journal = load_journal(journal_path)
        existing_hash = journal.get(key_hash)
        if existing_hash is not None and existing_hash != request_hash:
            raise ContractError("idempotency key conflict with a different request")

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        """Record a verified apply result without retaining request content."""
        if (
            mode == "apply"
            and command["operation"] != "read_issue"
            and journal_path is not None
            and result.get("verified") is True
        ):
            journal[key_hash] = request_hash
            write_journal(journal_path, journal)
        return result

    identifier = command["target"]["identifier"]
    issue = client.get_issue(identifier)
    if not isinstance(issue, dict) or issue.get("identifier") != identifier:
        raise ContractError(f"exact Linear issue not found: {identifier}")
    if issue.get("team", {}).get("key") != "SIS":
        raise ContractError(f"exact target is not in the SIS team: {identifier}")
    before = issue_snapshot(issue)
    base = result_base(command, issue, mode)
    if command["operation"] == "read_issue":
        return {
            **base,
            "result": "read",
            "before": before,
            "after": before,
            "plan": [],
            "no_op": True,
            "verified": True,
        }
    if command["operation"] == "change_state":
        requested = command["change"]["state"]
        states = [item for item in client.list_states(issue["team"]["id"]) if item["name"] == requested]
        if len(states) != 1:
            raise ContractError(f"exact workflow state not found: {requested}")
        after = {**before, "state": requested}
        if before["state"] == requested:
            return finish({
                **base,
                "result": "no_op",
                "before": before,
                "after": after,
                "plan": [],
                "no_op": True,
                "verified": True,
            })
        plan = [{"action": "change_state", "from": before["state"], "to": requested}]
        if mode == "plan":
            return {
                **base,
                "result": "planned",
                "before": before,
                "after": after,
                "plan": plan,
                "no_op": False,
                "verified": False,
            }
        client.update_issue_state(issue["id"], states[0]["id"])
        verified_issue = client.get_issue(identifier)
        if (
            not isinstance(verified_issue, dict)
            or verified_issue.get("identifier") != identifier
            or verified_issue.get("state", {}).get("name") != requested
        ):
            raise ContractError("state read-back verification failed")
        return finish({
            **base,
            "result": "applied",
            "before": before,
            "after": issue_snapshot(verified_issue),
            "plan": plan,
            "no_op": False,
            "verified": True,
        })
    if command["operation"] == "add_comment":
        body = command["change"]["body"].strip()
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        key_hash, request_hash, marker = command_fingerprint(command)
        comments = client.list_comments(issue["id"])
        key_marker = f"<!-- linear-command:v1 key={key_hash} "
        same_key = [item for item in comments if key_marker in item.get("body", "")]
        exact = [item for item in same_key if marker in item.get("body", "")]
        before_with_comments = {**before, "comment_count": len(comments)}
        plan = [
            {
                "action": "add_comment",
                "body_sha256": body_hash,
                "body_length": len(body),
            }
        ]
        if same_key and not exact:
            raise ContractError(
                f"idempotency key conflicts with a different request: {request_hash}"
            )
        if exact:
            return finish({
                **base,
                "result": "no_op",
                "before": before_with_comments,
                "after": before_with_comments,
                "plan": [],
                "no_op": True,
                "verified": True,
            })
        after_with_comments = {
            **before,
            "comment_count": len(comments) + 1,
            "comment_body_sha256": body_hash,
        }
        if mode == "plan":
            return {
                **base,
                "result": "planned",
                "before": before_with_comments,
                "after": after_with_comments,
                "plan": plan,
                "no_op": False,
                "verified": False,
            }
        client.create_comment(issue["id"], f"{body}\n\n{marker}")
        verified_comments = client.list_comments(issue["id"])
        if not any(marker in item.get("body", "") for item in verified_comments):
            raise ContractError("comment read-back verification failed")
        return finish({
            **base,
            "result": "applied",
            "before": before_with_comments,
            "after": {
                **after_with_comments,
                "comment_count": len(verified_comments),
            },
            "plan": plan,
            "no_op": False,
            "verified": True,
        })
    raise ContractError("validated operation has no execution path")


def load_command(
    path: Path,
    *,
    allowed_root: Path = COMMAND_ROOT,
) -> dict[str, Any]:
    """Load one JSON command confined to the repository command root."""
    resolved = path.resolve()
    root = allowed_root.resolve()
    if not resolved.is_relative_to(root):
        raise ContractError("command path is outside the allowlisted command root")
    if not resolved.is_file() or resolved.suffix.lower() != ".json":
        raise ContractError("command must be an existing .json file")
    try:
        raw = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("command file is not valid JSON") from exc
    return validate_command(raw)


def emit(payload: dict[str, Any]) -> None:
    """Emit one stable machine-readable result line."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    """Run one command in plan or apply mode and emit linear-result.v1."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True, type=Path)
    parser.add_argument("--mode", choices=["plan", "apply"], default="plan")
    default_home = Path(
        os.environ.get(
            "HERMES_HOME", "/Users/hermes/.hermes/profiles/project-manager"
        )
    )
    parser.add_argument(
        "--journal",
        type=Path,
        default=default_home / "linear-command-lane" / "journal.json",
    )
    args = parser.parse_args()
    try:
        command = load_command(args.command.resolve())
        client = LinearClient(os.environ.get("LINEAR_TOKEN", ""))
        result = execute_command(
            client,
            command,
            mode=args.mode,
            journal_path=args.journal.resolve(),
        )
        emit(result)
        return 0
    except ContractError as exc:
        emit(
            {
                "schema_version": "linear-result.v1",
                "mode": args.mode,
                "result": "error",
                "verified": False,
                "issues": [str(exc)],
            }
        )
        return 1
    except Exception as exc:
        emit(
            {
                "schema_version": "linear-result.v1",
                "mode": args.mode,
                "result": "error",
                "verified": False,
                "issues": [f"unexpected execution error: {type(exc).__name__}"],
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
