#!/usr/bin/env python3
"""Build and attest exact, expiring approvals for bounded Linear changes.

This module never imports a Linear client and never mutates Linear.  It produces
checksum-bound Swamp artifacts which the credential-holding Project Manager can
independently verify before the bounded destructive parent mutation.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

PLAN_SCHEMA_VERSION = "linear-destructive-owner-approval-plan.v1"
ATTESTATION_SCHEMA_VERSION = "linear-destructive-owner-approval-attestation.v1"
PLAN_MODEL = "linear-destructive-owner-approval-plan"
PLAN_WORKFLOW = "linear-destructive-owner-approval-plan"
ATTEST_MODEL = "linear-destructive-owner-approval-attest"
ATTEST_WORKFLOW = "linear-destructive-owner-approval-attest"
ISSUE_IDENTIFIER = re.compile(r"^SIS-[1-9][0-9]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
ENCODED_INTENT = re.compile(r"^[A-Za-z0-9_-]{16,4096}$")
MAX_APPROVAL_LIFETIME = timedelta(hours=24)


class ContractError(RuntimeError):
    """The fixed approval contract or immutable artifact failed validation."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def artifact_checksum(value: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(value)
    unsigned.pop("checksum", None)
    return canonical_sha256(unsigned)


def verify_artifact_checksum(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("checksum"), str)
        and SHA256.fullmatch(value["checksum"]) is not None
        and value["checksum"] == artifact_checksum(value)
    )


def encode_intent(value: Any) -> str:
    validate_intent(value)
    return base64.urlsafe_b64encode(canonical_json(value)).decode("ascii").rstrip("=")


def decode_intent(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or ENCODED_INTENT.fullmatch(value) is None:
        raise ContractError("intent must be bounded canonical base64url")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(value + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ContractError("intent encoding is invalid") from exc
    validate_intent(decoded)
    if encode_intent(decoded) != value:
        raise ContractError("intent encoding is not canonical")
    return decoded


def validate_intent(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"operation", "target", "change"}:
        raise ContractError("intent must contain exactly operation, target and change")
    operation = value.get("operation")
    if operation not in {
        "change_state",
        "update_issue",
        "remove_issue_relation",
        "replace_issue_relation",
        "archive_linear_entity",
        "delete_linear_entity",
    }:
        raise ContractError("intent operation is not owner-approvable")
    target = value.get("target")
    change = value.get("change")
    if operation in {"archive_linear_entity", "delete_linear_entity"}:
        if not isinstance(target, dict) or set(target) != {"type", "selector"}:
            raise ContractError("archive/delete target must contain exactly type and selector")
        entity_type = target.get("type")
        selector = target.get("selector")
        safe_matrix = {
            ("archive_linear_entity", "issue"),
            ("archive_linear_entity", "project"),
            ("archive_linear_entity", "initiative"),
            ("delete_linear_entity", "issue"),
            ("delete_linear_entity", "project"),
            ("delete_linear_entity", "milestone"),
            ("delete_linear_entity", "initiative"),
        }
        if (operation, entity_type) not in safe_matrix or not isinstance(selector, dict):
            raise ContractError("archive/delete combination is outside the safe matrix")
        if entity_type == "issue":
            valid_selector = set(selector) == {"identifier"} and isinstance(
                selector.get("identifier"), str
            ) and ISSUE_IDENTIFIER.fullmatch(selector["identifier"]) is not None
        elif entity_type in {"project", "initiative"}:
            valid_selector = set(selector) == {"name"} and isinstance(
                selector.get("name"), str
            ) and 0 < len(selector["name"].strip()) <= 200
        else:
            valid_selector = set(selector) == {"project", "name"} and all(
                isinstance(selector.get(field), str)
                and 0 < len(selector[field].strip()) <= 200
                for field in ("project", "name")
            )
        if not valid_selector or any(
            ord(char) < 32 for item in selector.values() for char in item
        ):
            raise ContractError("archive/delete exact selector is invalid")
        if change != {}:
            raise ContractError("archive/delete intent change must be empty")
        return value
    if (
        not isinstance(target, dict)
        or set(target) != {"type", "identifier"}
        or target.get("type") != "issue"
        or not isinstance(target.get("identifier"), str)
        or ISSUE_IDENTIFIER.fullmatch(target["identifier"]) is None
    ):
        raise ContractError("intent target must be one exact SIS-N issue")
    change = value.get("change")
    if not isinstance(change, dict):
        raise ContractError("intent change must be an object")
    if operation == "change_state":
        if set(change) != {"state"} or change.get("state") not in {
            "Done",
            "Canceled",
            "Duplicate",
        }:
            raise ContractError(
                "change_state requires exactly one owner-controlled terminal state"
            )
        return value
    if operation == "update_issue":
        if set(change) != {"parent_identifier"}:
            raise ContractError("update_issue requires exactly parent_identifier")
        parent = change.get("parent_identifier")
        if parent is not None and (
            not isinstance(parent, str) or ISSUE_IDENTIFIER.fullmatch(parent) is None
        ):
            raise ContractError("update_issue parent_identifier must be exact SIS-N or null")
        if parent == target["identifier"]:
            raise ContractError("update_issue cannot parent an issue to itself")
        return value
    fields = (
        {"related_identifier", "relation_type"}
        if operation == "remove_issue_relation"
        else {
            "old_related_identifier",
            "old_relation_type",
            "new_related_identifier",
            "new_relation_type",
        }
    )
    if set(change) != fields:
        raise ContractError(f"{operation} change fields are invalid")
    endpoint_fields = (
        ("related_identifier",)
        if operation == "remove_issue_relation"
        else ("old_related_identifier", "new_related_identifier")
    )
    type_fields = (
        ("relation_type",)
        if operation == "remove_issue_relation"
        else ("old_relation_type", "new_relation_type")
    )
    for field in endpoint_fields:
        endpoint = change.get(field)
        if (
            not isinstance(endpoint, str)
            or ISSUE_IDENTIFIER.fullmatch(endpoint) is None
            or endpoint == target["identifier"]
        ):
            raise ContractError(f"{operation} {field} must be a distinct exact SIS-N")
    for field in type_fields:
        if change.get(field) not in {"blocks", "blocked_by", "related"}:
            raise ContractError(f"{operation} {field} is invalid")
    return value


def parse_expiry(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        raise ContractError("expires_at must be UTC RFC3339 seconds")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError("expires_at is not a valid timestamp") from exc
    return parsed


def validate_expiry_window(expires_at: str, now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ContractError("current time must be timezone-aware")
    expires = parse_expiry(expires_at)
    current = now.astimezone(timezone.utc)
    if expires <= current:
        raise ContractError("approval plan is expired")
    if expires - current > MAX_APPROVAL_LIFETIME:
        raise ContractError("approval plan expiry exceeds 24 hours")


def build_plan(
    intent: dict[str, Any],
    before_state_hash: str,
    expires_at: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    validate_intent(intent)
    if not isinstance(before_state_hash, str) or SHA256.fullmatch(before_state_hash) is None:
        raise ContractError("before_state_hash must be a SHA-256 hex digest")
    current = now or datetime.now(timezone.utc)
    validate_expiry_window(expires_at, current)
    result: dict[str, Any] = {
        "schemaVersion": PLAN_SCHEMA_VERSION,
        "mode": "plan",
        "readOnly": True,
        "intent": copy.deepcopy(intent),
        "intentHash": canonical_sha256(intent),
        "beforeStateHash": before_state_hash,
        "expiresAt": expires_at,
        "plannedActions": [copy.deepcopy(intent)],
        "requiredApproval": "Approve only this exact intent against this exact before-state hash before expiry",
    }
    result["checksum"] = artifact_checksum(result)
    return result


def validate_plan(
    value: Any,
    *,
    intent: dict[str, Any],
    before_state_hash: str,
    expires_at: str,
    checksum: str,
    now: datetime,
) -> dict[str, Any]:
    expected = build_plan(intent, before_state_hash, expires_at, now=now)
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != PLAN_SCHEMA_VERSION
        or value.get("mode") != "plan"
        or value.get("readOnly") is not True
        or value.get("checksum") != checksum
        or not verify_artifact_checksum(value)
        or value != expected
    ):
        raise ContractError("plan artifact does not match the exact requested binding")
    return value


def load_plan_artifact(
    *, artifact_version: int, plan_run_id: str, plan_checksum: str
) -> dict[str, Any]:
    if not isinstance(artifact_version, int) or isinstance(artifact_version, bool) or artifact_version < 1:
        raise ContractError("plan artifact version must be a positive integer")
    if not isinstance(plan_run_id, str) or UUID.fullmatch(plan_run_id) is None:
        raise ContractError("plan run id must be a UUID")
    if not isinstance(plan_checksum, str) or SHA256.fullmatch(plan_checksum) is None:
        raise ContractError("plan checksum must be SHA-256")
    completed = subprocess.run(
        [
            "swamp", "data", "get", PLAN_MODEL, "result", "--version",
            str(artifact_version), "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise ContractError("plan artifact retrieval failed")
    try:
        artifact = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("plan artifact retrieval returned invalid JSON") from exc
    owner = artifact.get("ownerDefinition") if isinstance(artifact, dict) else None
    content = artifact.get("content") if isinstance(artifact, dict) else None
    if (
        not isinstance(artifact, dict)
        or artifact.get("modelName") != PLAN_MODEL
        or artifact.get("name") != "result"
        or artifact.get("version") != artifact_version
        or not isinstance(owner, dict)
        or owner.get("workflowRunId") != plan_run_id
        or not isinstance(content, dict)
        or content.get("exitCode") != 0
        or not isinstance(content.get("stdout"), str)
    ):
        raise ContractError("plan artifact provenance is invalid")
    try:
        plan = json.loads(content["stdout"])
    except json.JSONDecodeError as exc:
        raise ContractError("plan artifact content is invalid JSON") from exc
    if not isinstance(plan, dict) or plan.get("checksum") != plan_checksum:
        raise ContractError("plan artifact checksum binding is invalid")
    return plan


def build_attestation(
    *,
    intent: dict[str, Any],
    before_state_hash: str,
    expires_at: str,
    plan_run_id: str,
    plan_artifact_version: int,
    plan_checksum: str,
    plan_loader: Callable[..., dict[str, Any]] = load_plan_artifact,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    loaded = plan_loader(
        artifact_version=plan_artifact_version,
        plan_run_id=plan_run_id,
        plan_checksum=plan_checksum,
    )
    validate_plan(
        loaded,
        intent=intent,
        before_state_hash=before_state_hash,
        expires_at=expires_at,
        checksum=plan_checksum,
        now=current,
    )
    result: dict[str, Any] = {
        "schemaVersion": ATTESTATION_SCHEMA_VERSION,
        "mode": "attestation",
        "decision": "owner_approved",
        "workflow": ATTEST_WORKFLOW,
        "model": ATTEST_MODEL,
        "plan": {
            "workflow": PLAN_WORKFLOW,
            "model": PLAN_MODEL,
            "runId": plan_run_id,
            "artifactVersion": plan_artifact_version,
            "checksum": plan_checksum,
        },
        "intent": copy.deepcopy(intent),
        "intentHash": canonical_sha256(intent),
        "beforeStateHash": before_state_hash,
        "expiresAt": expires_at,
    }
    result["checksum"] = artifact_checksum(result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("plan", "attest"), default="plan")
    parser.add_argument("--intent", required=True)
    parser.add_argument("--before-state-hash", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--plan-run-id")
    parser.add_argument("--plan-artifact-version", type=int)
    parser.add_argument("--plan-checksum")
    args = parser.parse_args(argv)
    attest_fields = (args.plan_run_id, args.plan_artifact_version, args.plan_checksum)
    if args.mode == "plan" and any(value is not None for value in attest_fields):
        parser.error("plan mode does not accept attestation references")
    if args.mode == "attest" and any(value is None for value in attest_fields):
        parser.error("attest mode requires exact plan run/version/checksum")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        decoded = decode_intent(args.intent)
        if args.mode == "plan":
            result = build_plan(decoded, args.before_state_hash, args.expires_at)
        else:
            result = build_attestation(
                intent=decoded,
                before_state_hash=args.before_state_hash,
                expires_at=args.expires_at,
                plan_run_id=args.plan_run_id,
                plan_artifact_version=args.plan_artifact_version,
                plan_checksum=args.plan_checksum,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except ContractError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
