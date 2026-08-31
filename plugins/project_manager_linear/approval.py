"""Fixed Project Manager verifier for Swamp owner-approval attestations."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from . import approval_contract as contract
except ImportError:  # standalone bundled lane loader
    from plugins.project_manager_linear import approval_contract as contract

SWAMP_WORKSPACE = Path("/Users/hermes/workspaces/swamp-ops-runtime")
APPROVAL_FIELDS = {
    "workflow",
    "model",
    "run_id",
    "artifact_version",
    "checksum",
    "intent_hash",
    "before_state_hash",
    "expires_at",
}


class ApprovalError(RuntimeError):
    """Owner approval is absent, stale, mismatched, or already consumed."""


_VERIFIED_MARKER = object()
_CONSUMED_MARKER = object()


class VerifiedOwnerApproval(Mapping[str, Any]):
    """Opaque result proving fixed Swamp provenance was checked, not consumed."""

    __slots__ = ("_attestation", "_intent", "_before_state_hash", "_checksum", "_marker")

    def __init__(
        self,
        attestation: dict[str, Any],
        intent: dict[str, Any],
        before_state_hash: str,
        checksum: str,
        *,
        _marker: object,
    ) -> None:
        if _marker is not _VERIFIED_MARKER:
            raise ApprovalError("verified owner approval cannot be constructed by callers")
        self._attestation = attestation
        self._intent = intent
        self._before_state_hash = before_state_hash
        self._checksum = checksum
        self._marker = _marker

    def __getitem__(self, key: str) -> Any:
        return self._attestation[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._attestation)

    def __len__(self) -> int:
        return len(self._attestation)


class ConsumedOwnerApproval:
    """Opaque one-use authorization accepted by the mutation lane."""

    __slots__ = ("_intent", "_checksum", "_marker")

    def __init__(self, verified: VerifiedOwnerApproval, *, _marker: object) -> None:
        if (
            _marker is not _CONSUMED_MARKER
            or not isinstance(verified, VerifiedOwnerApproval)
            or verified._marker is not _VERIFIED_MARKER
        ):
            raise ApprovalError("consumed owner approval cannot be constructed by callers")
        self._intent = verified._intent
        self._checksum = verified._checksum
        self._marker = _marker


def require_consumed_owner_approval(
    authorization: Any, *, expected_intent: dict[str, Any]
) -> None:
    """Fail closed unless authorization came from atomic one-time consumption."""
    if (
        not isinstance(authorization, ConsumedOwnerApproval)
        or authorization._marker is not _CONSUMED_MARKER
        or authorization._intent != expected_intent
    ):
        raise ApprovalError("apply requires a consumed owner approval for the exact intent")


def validate_policy(policy: Any) -> dict[str, Any]:
    if policy == {"mode": "standard"}:
        return policy
    if not isinstance(policy, dict) or set(policy) != {"mode", "approval"}:
        raise ApprovalError("policy must be exact standard or owner_approved reference")
    if policy.get("mode") != "owner_approved":
        raise ApprovalError("policy mode is invalid")
    reference = policy.get("approval")
    if not isinstance(reference, dict) or set(reference) != APPROVAL_FIELDS:
        raise ApprovalError("owner_approved policy reference fields are invalid")
    if reference.get("workflow") != contract.ATTEST_WORKFLOW:
        raise ApprovalError("owner_approved workflow is not the fixed workflow")
    if reference.get("model") != contract.ATTEST_MODEL:
        raise ApprovalError("owner_approved model is not the fixed model")
    run_id = reference.get("run_id")
    if not isinstance(run_id, str) or contract.UUID.fullmatch(run_id) is None:
        raise ApprovalError("owner_approved run_id must be a UUID")
    version = reference.get("artifact_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ApprovalError("owner_approved artifact_version must be positive")
    for field in ("checksum", "intent_hash", "before_state_hash"):
        value = reference.get(field)
        if not isinstance(value, str) or contract.SHA256.fullmatch(value) is None:
            raise ApprovalError(f"owner_approved {field} must be SHA-256")
    try:
        contract.parse_expiry(reference.get("expires_at"))
    except contract.ContractError as exc:
        raise ApprovalError(str(exc)) from exc
    return policy


def _default_runner(argv: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _json_command(
    runner: Callable[..., dict[str, Any]],
    argv: list[str],
    *,
    workspace: Path,
    label: str,
) -> dict[str, Any]:
    completed = runner(argv, cwd=workspace, timeout=60)
    if completed.get("returncode") != 0:
        raise ApprovalError(f"{label} failed")
    try:
        payload = json.loads(completed.get("stdout", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ApprovalError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ApprovalError(f"{label} returned non-object JSON")
    return payload


def _approval_step_succeeded(history: dict[str, Any]) -> bool:
    for job in history.get("jobs", []):
        if not isinstance(job, dict) or job.get("name") != "attest":
            continue
        for step in job.get("steps", []):
            if (
                isinstance(step, dict)
                and step.get("name") == "approve-linear-destructive-intent"
            ):
                return step.get("status") == "succeeded"
    return False


def _load_attestation(
    reference: dict[str, Any],
    *,
    runner: Callable[..., dict[str, Any]],
    workspace: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = reference["run_id"]
    history = _json_command(
        runner,
        ["swamp", "workflow", "history", "get", run_id, "--json"],
        workspace=workspace,
        label="owner approval workflow history",
    )
    if (
        history.get("id") != run_id
        or history.get("workflowName") != contract.ATTEST_WORKFLOW
        or history.get("status") != "succeeded"
        or not _approval_step_succeeded(history)
    ):
        raise ApprovalError("owner approval workflow is not explicitly approved and succeeded")

    version = reference["artifact_version"]
    artifact = _json_command(
        runner,
        [
            "swamp",
            "data",
            "get",
            contract.ATTEST_MODEL,
            "result",
            "--version",
            str(version),
            "--json",
        ],
        workspace=workspace,
        label="owner approval attestation retrieval",
    )
    owner = artifact.get("ownerDefinition")
    content = artifact.get("content")
    if (
        artifact.get("modelName") != contract.ATTEST_MODEL
        or artifact.get("name") != "result"
        or artifact.get("version") != version
        or not isinstance(owner, dict)
        or owner.get("workflowRunId") != run_id
        or not isinstance(content, dict)
        or content.get("exitCode") != 0
        or not isinstance(content.get("stdout"), str)
    ):
        raise ApprovalError("owner approval artifact provenance is invalid")
    try:
        attestation = json.loads(content["stdout"])
    except json.JSONDecodeError as exc:
        raise ApprovalError("owner approval attestation content is invalid JSON") from exc
    if not isinstance(attestation, dict):
        raise ApprovalError("owner approval attestation content is invalid")
    return history, attestation


def _consume_once(journal_path: Path, checksum: str) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = journal_path.with_suffix(journal_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        consumed: list[str] = []
        if journal_path.exists():
            try:
                loaded = json.loads(journal_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ApprovalError("owner approval replay journal is invalid") from exc
            if (
                not isinstance(loaded, dict)
                or set(loaded) != {"schema_version", "consumed"}
                or loaded.get("schema_version") != 1
                or not isinstance(loaded.get("consumed"), list)
                or any(
                    not isinstance(value, str)
                    or contract.SHA256.fullmatch(value) is None
                    for value in loaded["consumed"]
                )
            ):
                raise ApprovalError("owner approval replay journal is invalid")
            consumed = loaded["consumed"]
        if checksum in consumed:
            raise ApprovalError("owner approval was already consumed")
        temporary = journal_path.with_suffix(journal_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"schema_version": 1, "consumed": sorted(consumed + [checksum])},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(journal_path)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def verify_owner_approval(
    policy: Any,
    *,
    expected_intent: dict[str, Any],
    expected_before_state_hash: str,
    runner: Callable[..., dict[str, Any]] = _default_runner,
    workspace: Path = SWAMP_WORKSPACE,
    now: datetime | None = None,
) -> VerifiedOwnerApproval:
    """Verify one exact attestation without spending its one-use approval."""
    validate_policy(policy)
    if policy.get("mode") != "owner_approved":
        raise ApprovalError("owner_approved policy is required")
    contract.validate_intent(expected_intent)
    if (
        not isinstance(expected_before_state_hash, str)
        or contract.SHA256.fullmatch(expected_before_state_hash) is None
    ):
        raise ApprovalError("expected before-state hash must be SHA-256")
    reference = policy["approval"]
    current = now or datetime.now(timezone.utc)
    expiry = contract.parse_expiry(reference["expires_at"])
    if expiry <= current.astimezone(timezone.utc):
        raise ApprovalError("owner approval is expired")
    expected_intent_hash = contract.canonical_sha256(expected_intent)
    if reference["intent_hash"] != expected_intent_hash:
        raise ApprovalError("owner approval intent hash does not match command")
    if reference["before_state_hash"] != expected_before_state_hash:
        raise ApprovalError("owner approval before-state hash does not match live plan")

    history, attestation = _load_attestation(
        reference, runner=runner, workspace=workspace
    )
    if (
        attestation.get("schemaVersion") != contract.ATTESTATION_SCHEMA_VERSION
        or attestation.get("mode") != "attestation"
        or attestation.get("decision") != "owner_approved"
        or attestation.get("workflow") != contract.ATTEST_WORKFLOW
        or attestation.get("model") != contract.ATTEST_MODEL
        or attestation.get("checksum") != reference["checksum"]
        or not contract.verify_artifact_checksum(attestation)
        or attestation.get("intent") != expected_intent
        or attestation.get("intentHash") != expected_intent_hash
        or attestation.get("beforeStateHash") != expected_before_state_hash
        or attestation.get("expiresAt") != reference["expires_at"]
    ):
        raise ApprovalError("owner approval attestation binding is invalid")
    plan = attestation.get("plan")
    if not isinstance(plan, dict) or set(plan) != {
        "workflow", "model", "runId", "artifactVersion", "checksum"
    }:
        raise ApprovalError("owner approval plan binding is invalid")
    expected_inputs = {
        "intent": contract.encode_intent(expected_intent),
        "beforeStateHash": expected_before_state_hash,
        "expiresAt": reference["expires_at"],
        "planRunId": plan["runId"],
        "planArtifactVersion": plan["artifactVersion"],
        "planChecksum": plan["checksum"],
    }
    if (
        plan.get("workflow") != contract.PLAN_WORKFLOW
        or plan.get("model") != contract.PLAN_MODEL
        or history.get("inputs") != expected_inputs
    ):
        raise ApprovalError("owner approval workflow inputs do not match attestation")
    return VerifiedOwnerApproval(
        attestation,
        expected_intent,
        expected_before_state_hash,
        reference["checksum"],
        _marker=_VERIFIED_MARKER,
    )


def consume_owner_approval(
    verified: VerifiedOwnerApproval, *, journal_path: Path
) -> ConsumedOwnerApproval:
    """Atomically spend a previously verified approval and mint apply authority."""
    if (
        not isinstance(verified, VerifiedOwnerApproval)
        or verified._marker is not _VERIFIED_MARKER
    ):
        raise ApprovalError("owner approval must be verified before consumption")
    _consume_once(journal_path, verified._checksum)
    return ConsumedOwnerApproval(verified, _marker=_CONSUMED_MARKER)
