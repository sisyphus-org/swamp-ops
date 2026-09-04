#!/usr/bin/env python3
"""Approval-gated Google Calendar create/update/delete lane.

The caller supplies a canonical Linear issue URL obtained through the existing
Linear specialist route. This module never reads Linear credentials or calls
Linear. It mutates only stable Linear-linked blocks in the primary calendar and
verifies each outcome by deterministic read-back.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
WRITE_CALENDAR_ID = "primary"
PUBLIC_ISSUE_URL = re.compile(
    r"^https://linear\.app/[A-Za-z0-9_-]+/issue/(SIS-[1-9][0-9]*)/"
    r"[A-Za-z0-9][A-Za-z0-9_-]*$"
)
LOCAL_DATETIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?$")
BLOCK_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
OPERATIONS = ("create", "update", "delete")
EVENT_ID_DOMAIN = b"google-calendar-linear-block:v1\0"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
PLAN_WORKFLOW = "google-calendar-write-plan"
PLAN_MODEL = "google-calendar-write"
APPROVAL_WORKFLOW = "google-calendar-write-approval"
APPROVAL_MODEL = "google-calendar-write-approval"

_spec = importlib.util.spec_from_file_location(
    "calendar_write_creds", Path(__file__).parent / "calendar_creds.py"
)
creds_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec and _spec.loader is not None
_spec.loader.exec_module(creds_mod)


class CalendarWriteError(ValueError):
    """The requested Calendar mutation does not satisfy the write contract."""


_VERIFIED_APPROVAL_MARKER = object()


class VerifiedApproval:
    """Opaque capability minted only after Swamp approval provenance verification."""

    __slots__ = ("_plan_checksum", "_marker")

    def __init__(self, plan_checksum: str, *, _marker: object) -> None:
        if _marker is not _VERIFIED_APPROVAL_MARKER:
            raise CalendarWriteError("verified approval cannot be constructed by callers")
        self._plan_checksum = plan_checksum
        self._marker = _marker


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan_checksum(plan: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(plan)
    unsigned.pop("checksum", None)
    return _canonical_sha256(unsigned)


def verify_plan_checksum(plan: dict[str, Any]) -> bool:
    checksum = plan.get("checksum")
    return (
        isinstance(checksum, str)
        and SHA256.fullmatch(checksum) is not None
        and checksum == _plan_checksum(plan)
    )


def _default_runner(argv: list[str], **kwargs: Any) -> dict[str, Any]:
    completed = subprocess.run(
        argv, capture_output=True, text=True, check=False, shell=False, timeout=60,
        **kwargs,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _run_json(runner: Any, argv: list[str], *, label: str) -> dict[str, Any]:
    completed = runner(argv, cwd=Path(__file__).parents[1])
    if completed.get("returncode") != 0:
        raise CalendarWriteError(f"{label} failed")
    try:
        value = json.loads(completed.get("stdout", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CalendarWriteError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise CalendarWriteError(f"{label} returned non-object JSON")
    return value


def _load_artifact(
    *,
    model: str,
    run_id: str,
    version: int,
    runner: Any,
    label: str,
    workflow: str,
) -> dict[str, Any]:
    artifact = _run_json(
        runner,
        ["swamp", "data", "get", model, "result", "--version", str(version), "--json"],
        label=label,
    )
    owner = artifact.get("ownerDefinition")
    content = artifact.get("content")
    if (
        artifact.get("modelName") != model
        or artifact.get("name") != "result"
        or artifact.get("version") != version
        or not isinstance(owner, dict)
        or owner.get("workflowRunId") != run_id
        or ("workflowName" in owner and owner.get("workflowName") != workflow)
        or not isinstance(content, dict)
        or content.get("exitCode") != 0
        or not isinstance(content.get("stdout"), str)
    ):
        raise CalendarWriteError(f"{label} provenance is invalid")
    try:
        value = json.loads(content["stdout"])
    except json.JSONDecodeError as exc:
        raise CalendarWriteError(f"{label} content is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CalendarWriteError(f"{label} content is invalid")
    return value


def _load_verified_plan(
    *, run_id: str, version: int, checksum: str, runner: Any
) -> dict[str, Any]:
    plan = _load_artifact(
        model=PLAN_MODEL,
        run_id=run_id,
        version=version,
        runner=runner,
        label="Calendar plan artifact",
        workflow=PLAN_WORKFLOW,
    )
    _validate_plan_schema(plan)
    history = _run_json(
        runner,
        ["swamp", "workflow", "history", "get", run_id, "--json"],
        label="Calendar plan workflow history",
    )
    inputs = history.get("inputs")
    rebuilt = None
    if isinstance(inputs, dict) and set(inputs) == {
        "operation", "blockKey", "summary", "start", "end", "linearUrl", "details"
    }:
        try:
            rebuilt = build_plan(
                operation=inputs["operation"],
                block_key=inputs["blockKey"],
                summary=inputs["summary"],
                start=inputs["start"],
                end=inputs["end"],
                linear_url=inputs["linearUrl"],
                details=inputs["details"],
            )
        except (CalendarWriteError, KeyError, TypeError):
            pass
    if (
        history.get("id") != run_id
        or history.get("workflowName") != PLAN_WORKFLOW
        or history.get("status") != "succeeded"
        or rebuilt != plan
        or plan.get("checksum") != checksum
        or not verify_plan_checksum(plan)
    ):
        raise CalendarWriteError("Calendar plan workflow provenance is invalid")
    return plan


def _approval_step_succeeded(history: dict[str, Any]) -> bool:
    return any(
        isinstance(step, dict)
        and step.get("name") == "approve-calendar-write"
        and step.get("status") == "succeeded"
        for job in history.get("jobs", [])
        if isinstance(job, dict) and job.get("name") == "attest"
        for step in job.get("steps", [])
    )


def build_approval_attestation(
    *,
    plan_run_id: str,
    plan_artifact_version: int,
    plan_checksum: str,
    runner: Any = _default_runner,
) -> dict[str, Any]:
    if not isinstance(plan_run_id, str) or UUID.fullmatch(plan_run_id) is None:
        raise CalendarWriteError("plan run id must be a UUID")
    if (
        not isinstance(plan_artifact_version, int)
        or isinstance(plan_artifact_version, bool)
        or plan_artifact_version < 1
    ):
        raise CalendarWriteError("plan artifact version must be positive")
    if not isinstance(plan_checksum, str) or SHA256.fullmatch(plan_checksum) is None:
        raise CalendarWriteError("plan checksum must be SHA-256")
    plan = _load_verified_plan(
        run_id=plan_run_id,
        version=plan_artifact_version,
        checksum=plan_checksum,
        runner=runner,
    )
    attestation: dict[str, Any] = {
        "schemaVersion": 1,
        "mode": "attestation",
        "decision": "owner_approved",
        "workflow": APPROVAL_WORKFLOW,
        "model": APPROVAL_MODEL,
        "plan": {
            "workflow": PLAN_WORKFLOW,
            "model": PLAN_MODEL,
            "runId": plan_run_id,
            "artifactVersion": plan_artifact_version,
            "checksum": plan_checksum,
        },
    }
    attestation["checksum"] = _plan_checksum(attestation)
    return attestation


def verify_calendar_approval(
    *,
    plan_run_id: str,
    plan_artifact_version: int,
    plan_checksum: str,
    approval_run_id: str,
    approval_artifact_version: int,
    approval_checksum: str,
    runner: Any = _default_runner,
) -> tuple[dict[str, Any], VerifiedApproval]:
    for label, value in (("plan", plan_run_id), ("approval", approval_run_id)):
        if not isinstance(value, str) or UUID.fullmatch(value) is None:
            raise CalendarWriteError(f"{label} run id must be a UUID")
    for label, value in (
        ("plan", plan_artifact_version), ("approval", approval_artifact_version)
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise CalendarWriteError(f"{label} artifact version must be positive")
    for label, value in (("plan", plan_checksum), ("approval", approval_checksum)):
        if not isinstance(value, str) or SHA256.fullmatch(value) is None:
            raise CalendarWriteError(f"{label} checksum must be SHA-256")

    plan = _load_verified_plan(
        run_id=plan_run_id,
        version=plan_artifact_version,
        checksum=plan_checksum,
        runner=runner,
    )

    history = _run_json(
        runner,
        ["swamp", "workflow", "history", "get", approval_run_id, "--json"],
        label="Calendar approval workflow history",
    )
    expected_inputs = {
        "planRunId": plan_run_id,
        "planArtifactVersion": plan_artifact_version,
        "planChecksum": plan_checksum,
    }
    if (
        history.get("id") != approval_run_id
        or history.get("workflowName") != APPROVAL_WORKFLOW
        or history.get("status") != "succeeded"
        or history.get("inputs") != expected_inputs
        or not _approval_step_succeeded(history)
    ):
        raise CalendarWriteError(
            "Calendar approval workflow is not explicitly approved and succeeded"
        )

    attestation = _load_artifact(
        model=APPROVAL_MODEL, run_id=approval_run_id,
        version=approval_artifact_version, runner=runner,
        label="Calendar approval attestation",
        workflow=APPROVAL_WORKFLOW,
    )
    expected_plan_ref = {
        "workflow": PLAN_WORKFLOW,
        "model": PLAN_MODEL,
        "runId": plan_run_id,
        "artifactVersion": plan_artifact_version,
        "checksum": plan_checksum,
    }
    if (
        attestation.get("schemaVersion") != 1
        or attestation.get("mode") != "attestation"
        or attestation.get("decision") != "owner_approved"
        or attestation.get("workflow") != APPROVAL_WORKFLOW
        or attestation.get("model") != APPROVAL_MODEL
        or attestation.get("plan") != expected_plan_ref
        or attestation.get("checksum") != approval_checksum
        or not verify_plan_checksum(attestation)
    ):
        raise CalendarWriteError("Calendar approval attestation binding is invalid")
    return plan, VerifiedApproval(plan_checksum, _marker=_VERIFIED_APPROVAL_MARKER)


def validate_linear_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value:
        raise CalendarWriteError("linear_url is required")
    match = PUBLIC_ISSUE_URL.fullmatch(value)
    if match is None:
        raise CalendarWriteError("linear_url must be one canonical SIS issue URL")
    return value, match.group(1)


def parse_kyiv_local(value: str) -> datetime:
    if not isinstance(value, str) or LOCAL_DATETIME.fullmatch(value) is None:
        raise CalendarWriteError("datetime must be local YYYY-MM-DDTHH:MM[:SS]")
    try:
        naive = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CalendarWriteError("datetime is invalid") from exc
    first = naive.replace(tzinfo=KYIV, fold=0)
    second = naive.replace(tzinfo=KYIV, fold=1)
    if first.utcoffset() != second.utcoffset():
        first_valid = first.astimezone(timezone.utc).astimezone(KYIV).replace(tzinfo=None) == naive
        second_valid = second.astimezone(timezone.utc).astimezone(KYIV).replace(tzinfo=None) == naive
        if first_valid and second_valid:
            raise CalendarWriteError("datetime is ambiguous in Europe/Kyiv")
    roundtrip = first.astimezone(timezone.utc).astimezone(KYIV).replace(tzinfo=None)
    if roundtrip != naive:
        raise CalendarWriteError("datetime does not exist in Europe/Kyiv")
    return first


def build_plan(
    *,
    summary: str,
    start: str,
    end: str,
    linear_url: str,
    details: str = "",
    operation: str = "create",
    block_key: str = "primary",
) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise CalendarWriteError("operation must be create, update, or delete")
    if (
        not isinstance(block_key, str)
        or len(block_key) > 64
        or BLOCK_KEY.fullmatch(block_key) is None
    ):
        raise CalendarWriteError("block_key must be a safe slug of at most 64 characters")
    if not all(isinstance(value, str) for value in (summary, start, end, details)):
        raise CalendarWriteError("event fields must be strings")
    canonical_url, identifier = validate_linear_url(linear_url)
    event_id = "sis" + hashlib.sha256(
        EVENT_ID_DOMAIN + identifier.encode("ascii") + b"\0" + block_key.encode("ascii")
    ).hexdigest()
    if operation == "delete":
        if any((summary, start, end, details)):
            raise CalendarWriteError("delete requires empty summary, start, end, and details")
        event: dict[str, Any] = {}
    else:
        if not summary.strip() or len(summary) > 200:
            raise CalendarWriteError("summary must contain 1-200 characters")
        if len(details) > 4000:
            raise CalendarWriteError("details must contain at most 4000 characters")
        start_dt = parse_kyiv_local(start)
        end_dt = parse_kyiv_local(end)
        if end_dt <= start_dt:
            raise CalendarWriteError("end must be after start")

        description = f"Linear: {canonical_url}"
        if details.strip():
            description = f"{details.strip()}\n\n{description}"
        event = {
            "summary": summary.strip(),
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": str(KYIV)},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": str(KYIV)},
        }
    plan: dict[str, Any] = {
        "schemaVersion": 1,
        "mode": "plan",
        "readOnly": True,
        "ready": True,
        "calendarId": WRITE_CALENDAR_ID,
        "operation": operation,
        "blockKey": block_key,
        "eventId": event_id,
        "linearIssue": {"identifier": identifier, "url": canonical_url},
        "event": event,
        "requiredApproval": f"{operation.title()} this exact checksum-bound primary-calendar event",
        "blockers": [],
    }
    plan["checksum"] = _plan_checksum(plan)
    return plan


def _validate_plan_schema(plan: Any) -> None:
    top_level_keys = {
        "schemaVersion",
        "mode",
        "readOnly",
        "ready",
        "calendarId",
        "operation",
        "blockKey",
        "eventId",
        "linearIssue",
        "event",
        "requiredApproval",
        "blockers",
        "checksum",
    }
    if not isinstance(plan, dict) or set(plan) != top_level_keys:
        raise CalendarWriteError("approved plan schema is invalid")
    linear = plan.get("linearIssue")
    event = plan.get("event")
    operation = plan.get("operation")
    block_key = plan.get("blockKey")
    event_id = plan.get("eventId")
    if (
        operation not in OPERATIONS
        or not isinstance(block_key, str)
        or len(block_key) > 64
        or BLOCK_KEY.fullmatch(block_key) is None
        or not isinstance(event_id, str)
        or not isinstance(linear, dict)
        or set(linear) != {"identifier", "url"}
        or not isinstance(event, dict)
    ):
        raise CalendarWriteError("approved plan schema is invalid")
    if operation == "delete":
        if event != {}:
            raise CalendarWriteError("approved plan schema is invalid")
        return
    if set(event) != {"summary", "description", "start", "end"}:
        raise CalendarWriteError("approved plan schema is invalid")
    for boundary in (event.get("start"), event.get("end")):
        if (
            not isinstance(boundary, dict)
            or set(boundary) != {"dateTime", "timeZone"}
            or boundary.get("timeZone") != str(KYIV)
            or not isinstance(boundary.get("dateTime"), str)
        ):
            raise CalendarWriteError("approved plan schema is invalid")
        try:
            instant = datetime.fromisoformat(boundary["dateTime"])
        except ValueError as exc:
            raise CalendarWriteError("approved plan schema is invalid") from exc
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise CalendarWriteError("approved plan schema is invalid")
        if instant.astimezone(KYIV).utcoffset() != instant.utcoffset():
            raise CalendarWriteError("approved plan schema is invalid")


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "resp", None)
    return getattr(response, "status", None)


def _get_event(service: Any, event_id: str) -> dict[str, Any] | None:
    try:
        payload = service.events().get(
            calendarId=WRITE_CALENDAR_ID, eventId=event_id
        ).execute()
    except Exception as exc:
        if _http_status(exc) == 404:
            return None
        raise CalendarWriteError("Calendar event lookup failed") from None
    if isinstance(payload, dict) and payload:
        return payload
    raise CalendarWriteError("Calendar event lookup returned malformed payload")


def _verify_event(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    for field in ("summary", "description", "start", "end"):
        if actual.get(field) != expected.get(field):
            raise CalendarWriteError(f"Calendar read-back mismatch for {field}")


def _is_exact_linear_link(description: Any, canonical_url: str) -> bool:
    marker = f"Linear: {canonical_url}"
    return isinstance(description, str) and (
        description == marker or description.endswith(f"\n\n{marker}")
    )


def _sanitized_result(
    *, operation: str, identifier: str, block_key: str, reused: bool
) -> dict[str, Any]:
    return {
        "operation": operation,
        "status": "verified",
        "reused": reused,
        "linearIssue": identifier,
        "blockKey": block_key,
    }


def apply_plan(
    plan: dict[str, Any],
    *,
    approved_checksum: str,
    service: Any,
    authorization: VerifiedApproval | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(authorization, VerifiedApproval)
        or authorization._marker is not _VERIFIED_APPROVAL_MARKER
        or authorization._plan_checksum != approved_checksum
    ):
        raise CalendarWriteError("apply requires verified manual approval for the exact plan")
    if not isinstance(approved_checksum, str) or SHA256.fullmatch(approved_checksum) is None:
        raise CalendarWriteError("approved checksum must be a SHA-256 digest")
    _validate_plan_schema(plan)
    if (
        plan.get("schemaVersion") != 1
        or plan.get("mode") != "plan"
        or plan.get("readOnly") is not True
        or plan.get("ready") is not True
        or plan.get("calendarId") != WRITE_CALENDAR_ID
        or plan.get("blockers") != []
        or plan.get("checksum") != approved_checksum
        or not verify_plan_checksum(plan)
    ):
        raise CalendarWriteError("approved plan is not ready or checksum-bound")

    linear = plan.get("linearIssue")
    event = plan.get("event")
    operation = plan.get("operation")
    block_key = plan.get("blockKey")
    if not isinstance(linear, dict) or not isinstance(event, dict):
        raise CalendarWriteError("approved plan is incomplete")
    canonical_url, identifier = validate_linear_url(linear.get("url"))
    if linear.get("identifier") != identifier:
        raise CalendarWriteError("approved Linear issue identifier is inconsistent")
    if not isinstance(operation, str) or not isinstance(block_key, str):
        raise CalendarWriteError("approved plan operation identity is invalid")
    expected_event_id = "sis" + hashlib.sha256(
        EVENT_ID_DOMAIN + identifier.encode("ascii") + b"\0" + block_key.encode("ascii")
    ).hexdigest()
    if plan.get("eventId") != expected_event_id:
        raise CalendarWriteError("approved event identity is inconsistent")
    if operation != "delete" and not _is_exact_linear_link(
        event.get("description"), canonical_url
    ):
        raise CalendarWriteError("approved event description is not linked to Linear")

    event_id = expected_event_id
    existing = _get_event(service, event_id)

    if operation == "delete":
        if existing is None:
            return _sanitized_result(
                operation=operation, identifier=identifier, block_key=block_key, reused=True
            )
        if not _is_exact_linear_link(existing.get("description"), canonical_url):
            raise CalendarWriteError("Calendar target is linked to a different Linear issue")
        try:
            service.events().delete(
                calendarId=WRITE_CALENDAR_ID,
                eventId=event_id,
                sendUpdates="none",
            ).execute()
        except Exception:
            pass
        if _get_event(service, event_id) is not None:
            raise CalendarWriteError("Calendar delete outcome is ambiguous and event remains")
        return _sanitized_result(
            operation=operation, identifier=identifier, block_key=block_key, reused=False
        )

    expected = copy.deepcopy(event)
    body = copy.deepcopy(event)
    body["id"] = event_id

    if operation == "update":
        if existing is None:
            raise CalendarWriteError("Calendar update target does not exist")
        if not _is_exact_linear_link(existing.get("description"), canonical_url):
            raise CalendarWriteError("Calendar target is linked to a different Linear issue")
        try:
            _verify_event(existing, expected)
        except CalendarWriteError:
            try:
                service.events().update(
                    calendarId=WRITE_CALENDAR_ID,
                    eventId=event_id,
                    body=body,
                    sendUpdates="none",
                ).execute()
            except Exception:
                pass
            read_back = _get_event(service, event_id)
            if read_back is None:
                raise CalendarWriteError(
                    "Calendar update outcome is ambiguous and event is absent"
                )
            _verify_event(read_back, expected)
            reused = False
        else:
            reused = True
        return _sanitized_result(
            operation=operation, identifier=identifier, block_key=block_key, reused=reused
        )

    reused = existing is not None
    if existing is None:
        try:
            created = service.events().insert(
                calendarId=WRITE_CALENDAR_ID,
                body=body,
                sendUpdates="none",
            ).execute()
        except Exception:
            existing = _get_event(service, event_id)
            if existing is None:
                raise CalendarWriteError(
                    "Calendar create outcome is ambiguous and exact event is absent"
                ) from None
            reused = True
        else:
            if not isinstance(created, dict) or created.get("id") != event_id:
                existing = _get_event(service, event_id)
                if existing is None:
                    raise CalendarWriteError(
                        "Calendar create outcome is ambiguous and exact event is absent"
                    )
                reused = True
            else:
                existing = _get_event(service, event_id)
        if existing is None:
            raise CalendarWriteError("created Calendar event could not be read back")

    _verify_event(existing, expected)
    return _sanitized_result(
        operation=operation, identifier=identifier, block_key=block_key, reused=reused
    )


def _build_service(profile: str) -> Any:
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    credentials = creds_mod.load_credentials(profile)
    if not credentials.valid:
        credentials.refresh(Request())
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    mode_parser = argparse.ArgumentParser(add_help=False)
    mode_parser.add_argument("--mode", choices=("plan", "attest", "apply"), default="plan")
    selected, _ = mode_parser.parse_known_args(argv)
    parser = argparse.ArgumentParser(
        description="Approval-gated Google Calendar create/update/delete lane."
    )
    parser.add_argument("--mode", choices=("plan", "attest", "apply"), default="plan")
    if selected.mode == "plan":
        parser.add_argument("--operation", choices=OPERATIONS, default="create")
        parser.add_argument("--block-key", default="primary")
        parser.add_argument("--summary", default="")
        parser.add_argument("--start", default="")
        parser.add_argument("--end", default="")
        parser.add_argument("--linear-url", required=True)
        parser.add_argument("--details", default="")
        parser.add_argument("--profile", default="personal-assistant")
    else:
        parser.add_argument("--plan-run-id", required=True)
        parser.add_argument("--plan-artifact-version", required=True, type=int)
        parser.add_argument("--plan-checksum", required=True)
        if selected.mode == "apply":
            parser.add_argument("--approval-run-id", required=True)
            parser.add_argument("--approval-artifact-version", required=True, type=int)
            parser.add_argument("--approval-checksum", required=True)
            parser.add_argument("--profile", default="personal-assistant")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode == "plan":
        result = build_plan(
            operation=args.operation,
            block_key=args.block_key,
            summary=args.summary,
            start=args.start,
            end=args.end,
            linear_url=args.linear_url,
            details=args.details,
        )
    elif args.mode == "attest":
        result = build_approval_attestation(
            plan_run_id=args.plan_run_id,
            plan_artifact_version=args.plan_artifact_version,
            plan_checksum=args.plan_checksum,
        )
    else:
        plan, authorization = verify_calendar_approval(
            plan_run_id=args.plan_run_id,
            plan_artifact_version=args.plan_artifact_version,
            plan_checksum=args.plan_checksum,
            approval_run_id=args.approval_run_id,
            approval_artifact_version=args.approval_artifact_version,
            approval_checksum=args.approval_checksum,
        )
        result = apply_plan(
            plan,
            approved_checksum=args.plan_checksum,
            authorization=authorization,
            service=_build_service(args.profile),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
