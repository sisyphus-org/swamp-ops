"""Bounded ordered orchestration for mutating Linear commands.

This module deliberately has no Linear queries or mutations.  It validates and
orders calls back through the shipped single-item lane and persists only hashes
and safe per-item phases needed to resume an exact parent command.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


MAX_ITEMS = 50
MUTATING_OPERATIONS = {
    "change_state",
    "update_issue",
    "update_sub_issues",
    "add_comment",
    "create_issue",
    "converge_hierarchy",
    "create_standalone_issue",
    "converge_issue_tree",
    "create_issue_relation",
    "remove_issue_relation",
    "replace_issue_relation",
    "create_project",
    "create_milestone",
    "update_project",
    "update_milestone",
    "create_initiative",
    "update_initiative",
    "link_project_to_initiative",
    "archive_linear_entity",
    "delete_linear_entity",
}
OWNER_OPERATIONS = {
    "remove_issue_relation",
    "replace_issue_relation",
    "archive_linear_entity",
    "delete_linear_entity",
}
OWNER_STATES = {"Done", "Canceled", "Duplicate"}


class PartialFailure(RuntimeError):
    """An ordered parent stopped after a verified completed prefix."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _uuid4(domain: str, semantic: str) -> str:
    raw = bytearray(hashlib.sha256(f"{domain}:{semantic}".encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def requires_owner(item: dict[str, Any]) -> bool:
    operation = item.get("operation")
    change = item.get("change")
    return bool(
        operation in OWNER_OPERATIONS
        or (
            operation == "change_state"
            and isinstance(change, dict)
            and change.get("state") in OWNER_STATES
        )
        or (
            operation == "update_issue"
            and isinstance(change, dict)
            and set(change) == {"parent_identifier"}
        )
    )


def _conflict_selector(item: dict[str, Any]) -> dict[str, Any]:
    """Identify the complete semantic entity selected by one child operation."""
    operation = item["operation"]
    change = item["change"]
    target = item["target"]
    relation_type = (
        change.get("old_relation_type")
        if operation == "replace_issue_relation"
        else change.get("relation_type")
    )
    related_identifier = (
        change.get("old_related_identifier")
        if operation == "replace_issue_relation"
        else change.get("related_identifier")
    )
    if (
        operation in {
            "create_issue_relation",
            "remove_issue_relation",
            "replace_issue_relation",
        }
        and relation_type == "related"
        and isinstance(target.get("identifier"), str)
        and isinstance(related_identifier, str)
    ):
        return {
            "operation": operation,
            "relation": {
                "type": "related",
                "endpoints": sorted(
                    (target.get("identifier"), related_identifier)
                ),
            },
        }
    selector: dict[str, Any] = {
        "operation": operation,
        "target": target,
    }
    fields = {
        "add_comment": ("body",),
        "create_issue": ("parent_identifier", "title"),
        "create_issue_relation": ("related_identifier", "relation_type"),
        "remove_issue_relation": ("related_identifier", "relation_type"),
        "replace_issue_relation": ("old_related_identifier", "old_relation_type"),
        "create_project": ("name",),
        "update_project": ("name",),
        "create_milestone": ("project", "name"),
        "update_milestone": ("project", "name"),
        "create_initiative": ("name",),
        "update_initiative": ("name",),
        "link_project_to_initiative": ("project", "initiative"),
    }.get(operation, ())
    if fields:
        selector["selector"] = {field: change.get(field) for field in fields}
    elif operation in {
        "converge_hierarchy",
        "create_standalone_issue",
        "converge_issue_tree",
    }:
        selector["selector"] = {
            "project": (
                change.get("project", {}).get("name")
                if isinstance(change.get("project"), dict)
                else None
            ),
            "milestone": (
                change.get("milestone", {}).get("name")
                if isinstance(change.get("milestone"), dict)
                else None
            ),
            "issue": (
                change.get("issue", {}).get("title")
                if isinstance(change.get("issue"), dict)
                else None
            ),
        }
    return selector


def validate_items(
    items: Any,
    *,
    parent_policy: dict[str, Any],
    error_cls: type[Exception],
) -> list[dict[str, Any]]:
    """Validate only the batch-specific shape and cross-item invariants."""
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise error_cls("bulk_linear_operations items must contain 1-50 entries")
    if len(_canonical(items)) > 24_576:
        raise error_cls("bulk_linear_operations exceeds the bounded serialized size")
    semantic_hashes: set[str] = set()
    target_hashes: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, value in enumerate(items):
        if not isinstance(value, dict) or set(value) != {"operation", "target", "change"}:
            raise error_cls(
                f"bulk_linear_operations item {index} must contain exactly operation, target, and change"
            )
        operation = value.get("operation")
        if operation not in MUTATING_OPERATIONS:
            raise error_cls(
                f"bulk_linear_operations item {index} is not an allowed mutating operation"
            )
        if not isinstance(value.get("target"), dict) or not isinstance(value.get("change"), dict):
            raise error_cls(f"bulk_linear_operations item {index} target/change must be objects")
        semantic_hash = _hash(value)
        if semantic_hash in semantic_hashes:
            raise error_cls("bulk_linear_operations contains a duplicate semantic item")
        semantic_hashes.add(semantic_hash)
        target_hash = _hash(_conflict_selector(value))
        if target_hash in target_hashes:
            raise error_cls("bulk_linear_operations contains conflicting writes to the same exact target")
        target_hashes.add(target_hash)
        validated.append(value)
    owner_children = [requires_owner(value) for value in validated]
    if parent_policy.get("mode") == "standard" and any(owner_children):
        raise error_cls("bulk_linear_operations requires owner_approved policy for every owner-controlled child")
    if parent_policy.get("mode") == "owner_approved" and not any(owner_children):
        raise error_cls("bulk_linear_operations owner_approved policy requires an owner-controlled child")
    return validated


def parent_intent(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": command["operation"],
        "target": command["target"],
        "change": command["change"],
    }


def derive_child_command(parent: dict[str, Any], index: int) -> dict[str, Any]:
    """Derive all child identities from exact ordered parent semantics and index."""
    items = parent["change"]["items"]
    item = items[index]
    ordered_semantic = {
        "parent_key": parent["idempotency_key"],
        "intent": parent_intent(parent),
        "index": index,
    }
    semantic = _hash(ordered_semantic)
    policy = parent["policy"] if requires_owner(item) else {"mode": "standard"}
    return {
        "schema_version": "linear-command.v2",
        "command_id": _uuid4("linear-bulk-child-command:v1", semantic),
        "correlation_id": _uuid4("linear-bulk-child-correlation:v1", semantic),
        "idempotency_key": f"linear:v2:{hashlib.sha256(('linear-bulk-child-idempotency:v1:' + semantic).encode()).hexdigest()[:32]}",
        "source_profile": parent["source_profile"],
        "operation": item["operation"],
        "target": item["target"],
        "change": item["change"],
        "policy": policy,
    }


def _plan_binding(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": plan.get("operation"),
        "target": plan.get("target"),
        "before": plan.get("before"),
        "after": plan.get("after"),
        "plan": plan.get("plan"),
    }


def _plan_hashes(plan: dict[str, Any]) -> dict[str, str]:
    return {
        "operation_hash": _hash(plan.get("operation")),
        "target_hash": _hash(plan.get("target")),
        "plan_hash": _hash(plan.get("plan")),
        "before_hash": _hash(plan.get("before")),
        "desired_after_hash": _hash(plan.get("after")),
    }


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _load_state(path: Path, binding: str, count: int, error_cls: type[Exception]) -> dict[str, Any]:
    hash_fields = {
        "operation_hash",
        "target_hash",
        "plan_hash",
        "before_hash",
        "desired_after_hash",
    }
    if not path.is_file():
        return {
            "schema_version": 2,
            "binding": binding,
            "aggregate_plan_hash": None,
            "before_state_hash": None,
            "after_state_hash": None,
            "items": [
                {
                    "phase": "pending",
                    "outcome": None,
                    **{field: None for field in hash_fields},
                }
                for _ in range(count)
            ],
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise error_cls("bulk recovery journal is unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "binding",
            "aggregate_plan_hash",
            "before_state_hash",
            "after_state_hash",
            "items",
        }
        or value.get("schema_version") != 2
        or value.get("binding") != binding
        or not isinstance(value.get("items"), list)
        or len(value["items"]) != count
        or any(
            value.get(field) is not None
            and (not isinstance(value[field], str) or len(value[field]) != 64)
            for field in (
                "aggregate_plan_hash",
                "before_state_hash",
                "after_state_hash",
            )
        )
        or any(
            not isinstance(item, dict)
            or set(item) != {"phase", "outcome", *hash_fields}
            or item.get("phase") not in {"pending", "prepared", "completed"}
            or (
                item.get("outcome") not in {None, "applied", "no_op"}
                or (item.get("phase") == "completed")
                != (item.get("outcome") is not None)
            )
            or any(
                item.get(field) is not None
                and (not isinstance(item[field], str) or len(item[field]) != 64)
                for field in hash_fields
            )
            or (
                any(item.get(field) is None for field in hash_fields)
                and any(item.get(field) is not None for field in hash_fields)
            )
            for item in value["items"]
        )
    ):
        raise error_cls("bulk recovery binding conflicts with the exact parent intent/order")
    return value


@contextmanager
def _claim(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_item(index: int, child: dict[str, Any], outcome: str) -> dict[str, Any]:
    return {
        "index": index,
        "operation": child["operation"],
        "outcome": outcome,
        "verified": True,
    }


def fake_result(child: dict[str, Any], mode: str, *, no_op: bool) -> dict[str, Any]:
    """Small deterministic result fixture used by orchestration unit tests."""
    return {
        "schema_version": "linear-result.v2",
        "operation": child["operation"],
        "mode": mode,
        "result": "planned" if mode == "plan" else ("no_op" if no_op else "applied"),
        "before": {},
        "after": {},
        "plan": [] if no_op else [{"action": child["operation"]}],
        "no_op": no_op,
        "verified": mode == "apply",
    }


def _valid_plan(plan: Any) -> bool:
    return isinstance(plan, dict) and plan.get("schema_version") == "linear-result.v2"


def _matches_original_plan(plan: dict[str, Any], item_state: dict[str, Any]) -> bool:
    return all(item_state[field] == digest for field, digest in _plan_hashes(plan).items())


def _matches_exact_recovered_after(
    plan: dict[str, Any],
    child: dict[str, Any],
    item_state: dict[str, Any],
) -> bool:
    evidence = plan.get("recovery_evidence")
    reference = child.get("policy", {}).get("approval")
    return bool(
        plan.get("operation") == child.get("operation")
        and _hash(plan.get("operation")) == item_state["operation_hash"]
        and _hash(plan.get("target")) == item_state["target_hash"]
        and _hash(plan.get("after")) == item_state["desired_after_hash"]
        and plan.get("result") == "no_op"
        and plan.get("no_op") is True
        and plan.get("verified") is True
        and plan.get("recovered") is True
        and plan.get("plan") == []
        and isinstance(evidence, dict)
        and evidence.get("schema_version") == "linear-owner-recovery.v1"
        and evidence.get("command_hash") == _hash(child)
        and evidence.get("phase") in {"prepared", "completed"}
        and isinstance(evidence.get("before_state_hash"), str)
        and len(evidence["before_state_hash"]) == 64
        and isinstance(evidence.get("after_state_hash"), str)
        and len(evidence["after_state_hash"]) == 64
        and isinstance(reference, dict)
        and evidence.get("approval_checksum") == reference.get("checksum")
        and evidence.get("intent_hash") == reference.get("intent_hash")
    )


def _partial_failure(completed: int, total: int, cause: BaseException | None = None) -> PartialFailure:
    failure = PartialFailure(
        f"bulk_linear_operations partial failure after {completed} of {total} verified items"
    )
    if cause is not None:
        failure.__cause__ = cause
    return failure


def execute_parent(
    command: dict[str, Any],
    *,
    validate_child: Callable[[dict[str, Any]], dict[str, Any]],
    execute_child: Callable[[dict[str, Any], str, Any], dict[str, Any]],
    recovery_path: Path | None = None,
    authorization_factory: Callable[[dict[str, Any]], Any] | None = None,
    error_cls: type[Exception] | None = None,
    mode: str = "apply",
) -> dict[str, Any]:
    """Preflight the full batch, then apply its exact unfinished suffix in order."""
    if error_cls is None:
        from .lane import ContractError

        error_cls = ContractError
    children = [
        validate_child(derive_child_command(command, index))
        for index in range(len(command["change"]["items"]))
    ]
    plans = [execute_child(child, "plan", None) for child in children]
    if any(not _valid_plan(plan) for plan in plans):
        raise error_cls("bulk child preflight returned an invalid result")
    aggregate_plan_hash = _hash([_plan_binding(plan) for plan in plans])
    base = {
        "schema_version": "linear-result.v2",
        "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "operation": "bulk_linear_operations",
        "mode": mode,
        "target": {"type": "workspace", "identifier": "current"},
    }
    before_values = [plan.get("before") for plan in plans]
    after_values = [plan.get("after") for plan in plans]

    def recovery_evidence(
        phase: str,
        *,
        before_hash: str | None = None,
        after_hash: str | None = None,
    ) -> dict[str, Any] | None:
        reference = command.get("policy", {}).get("approval")
        if not isinstance(reference, dict):
            return None
        return {
            "schema_version": "linear-owner-recovery.v1",
            "approval_checksum": reference["checksum"],
            "intent_hash": reference["intent_hash"],
            "command_hash": _hash(command),
            "before_state_hash": before_hash or _hash(before_values),
            "after_state_hash": after_hash or _hash(after_values),
            "phase": phase,
        }

    binding = _hash({"command": command, "children": children})

    def unfinished_plan_status(
        plan: dict[str, Any],
        child: dict[str, Any],
        item_state: dict[str, Any],
    ) -> str | None:
        if _matches_original_plan(plan, item_state):
            return "original"
        if (
            item_state["phase"] == "prepared"
            and _matches_exact_recovered_after(plan, child, item_state)
        ):
            return "recovered"
        return None

    if mode == "plan":
        recovered_state = None
        if recovery_path is not None and recovery_path.is_file():
            with _claim(recovery_path):
                recovered_state = _load_state(
                    recovery_path, binding, len(children), error_cls
                )
                completed = sum(
                    item["phase"] == "completed"
                    for item in recovered_state["items"]
                )
                for index, child in enumerate(children):
                    item_state = recovered_state["items"][index]
                    if item_state["phase"] == "completed":
                        continue
                    if unfinished_plan_status(plans[index], child, item_state) is None:
                        raise _partial_failure(completed, len(children))
        completed_replay = recovered_state is not None and all(
            item["phase"] == "completed" for item in recovered_state["items"]
        )
        recovered = recovered_state is not None and any(
            item["phase"] != "pending" for item in recovered_state["items"]
        )
        result = {
            **base,
            "mode": "apply" if completed_replay else mode,
            "result": "no_op" if completed_replay else "planned",
            "before": before_values,
            "after": after_values,
            "plan": [] if completed_replay else [plan.get("plan") for plan in plans],
            "items": [
                {
                    "index": index,
                    "operation": child["operation"],
                    "outcome": "no_op" if completed_replay else "planned",
                    "verified": completed_replay,
                }
                for index, child in enumerate(children)
            ],
            "counts": {
                "total": len(children),
                "applied": 0,
                "no_op": len(children) if completed_replay else 0,
            },
            "no_op": completed_replay,
            "verified": completed_replay,
        }
        if recovered:
            assert recovered_state is not None
            result["recovered"] = True
            result["recovery_evidence"] = recovery_evidence(
                "completed" if completed_replay else "prepared",
                before_hash=recovered_state["before_state_hash"],
                after_hash=recovered_state["after_state_hash"],
            )
        return result
    if recovery_path is None:
        raise error_cls("bulk apply requires a recovery journal")
    with _claim(recovery_path):
        state = _load_state(recovery_path, binding, len(children), error_cls)
        if state["aggregate_plan_hash"] is None:
            state["aggregate_plan_hash"] = aggregate_plan_hash
            state["before_state_hash"] = _hash(before_values)
            state["after_state_hash"] = _hash(after_values)
            for item_state, plan in zip(state["items"], plans):
                item_state.update(_plan_hashes(plan))
            _write_state(recovery_path, state)
        was_complete = all(item["phase"] == "completed" for item in state["items"])
        outcomes: list[dict[str, Any]] = []
        completed_before = sum(item["phase"] == "completed" for item in state["items"])
        for index, child in enumerate(children):
            item_state = state["items"][index]
            if item_state["phase"] == "completed":
                outcomes.append(
                    _safe_item(
                        index,
                        child,
                        "no_op" if was_complete else item_state["outcome"],
                    )
                )
                continue
            completed = sum(
                item["phase"] == "completed" for item in state["items"]
            )
            try:
                fresh_plan = execute_child(child, "plan", None)
                if not _valid_plan(fresh_plan):
                    raise error_cls("bulk child fresh preflight returned an invalid result")
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise _partial_failure(completed, len(children), exc)
            plan_status = unfinished_plan_status(fresh_plan, child, item_state)
            if plan_status is None:
                raise _partial_failure(completed, len(children))
            if plan_status == "recovered":
                authorization = (
                    authorization_factory(child) if authorization_factory else None
                )
                try:
                    result = execute_child(child, "apply", authorization)
                    if (
                        not _valid_plan(result)
                        or not _matches_exact_recovered_after(
                            result, child, item_state
                        )
                    ):
                        raise error_cls(
                            "bulk prepared child did not converge to an exact recovered no-op"
                        )
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    raise _partial_failure(completed, len(children), exc)
                item_state["phase"] = "completed"
                item_state["outcome"] = "no_op"
                _write_state(recovery_path, state)
                outcomes.append(_safe_item(index, child, "no_op"))
                continue
            if item_state["phase"] == "pending":
                item_state["phase"] = "prepared"
                item_state["outcome"] = None
                _write_state(recovery_path, state)
            authorization = authorization_factory(child) if authorization_factory else None
            try:
                result = execute_child(child, "apply", authorization)
                if (
                    not isinstance(result, dict)
                    or result.get("verified") is not True
                    or result.get("result") not in {"applied", "no_op"}
                ):
                    raise error_cls(
                        "bulk child apply did not return a verified terminal result"
                    )
            except BaseException as exc:
                completed = sum(
                    item["phase"] == "completed" for item in state["items"]
                )
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise _partial_failure(completed, len(children), exc)
            item_state["phase"] = "completed"
            item_state["outcome"] = result["result"]
            _write_state(recovery_path, state)
            outcomes.append(_safe_item(index, child, result["result"]))
        applied = sum(value["outcome"] == "applied" for value in outcomes)
        no_op = len(outcomes) - applied
        replay = was_complete and completed_before == len(children)
        aggregate_result = {
            **base,
            "result": "no_op" if replay else "applied",
            "before": before_values,
            "after": after_values,
            "plan": [] if replay else [plan.get("plan") for plan in plans],
            "items": outcomes,
            "counts": {"total": len(children), "applied": applied, "no_op": no_op},
            "no_op": replay,
            "verified": True,
        }
        evidence = recovery_evidence(
            "completed",
            before_hash=state["before_state_hash"],
            after_hash=state["after_state_hash"],
        )
        if evidence is not None:
            aggregate_result["recovery_evidence"] = evidence
        return aggregate_result
