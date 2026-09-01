"""Fixed Project Manager verifier for Swamp owner-approval attestations."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
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
_BULK_CHILD_MARKER = object()


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

    __slots__ = (
        "_intent",
        "_checksum",
        "_command_hash",
        "_task_id",
        "_generation",
        "_marker",
    )

    def __init__(
        self,
        verified: VerifiedOwnerApproval,
        *,
        _marker: object,
        command_hash: str | None = None,
        task_id: str | None = None,
        generation: int = 1,
    ) -> None:
        if (
            _marker is not _CONSUMED_MARKER
            or not isinstance(verified, VerifiedOwnerApproval)
            or verified._marker is not _VERIFIED_MARKER
        ):
            raise ApprovalError("consumed owner approval cannot be constructed by callers")
        self._intent = verified._intent
        self._checksum = verified._checksum
        self._command_hash = command_hash
        self._task_id = task_id
        self._generation = generation
        self._marker = _marker

    @classmethod
    def _from_binding(
        cls,
        *,
        intent: dict[str, Any],
        checksum: str,
        command_hash: str,
        task_id: str,
        generation: int,
    ) -> "ConsumedOwnerApproval":
        value = object.__new__(cls)
        value._intent = intent
        value._checksum = checksum
        value._command_hash = command_hash
        value._task_id = task_id
        value._generation = generation
        value._marker = _CONSUMED_MARKER
        return value


class BulkChildAuthorization:
    """Opaque child capability minted only from an exact consumed parent claim."""

    __slots__ = ("_intent", "_command_hash", "_parent_command_hash", "_marker")

    def __init__(
        self,
        *,
        intent: dict[str, Any],
        command_hash: str,
        parent_command_hash: str,
        _marker: object,
    ) -> None:
        if _marker is not _BULK_CHILD_MARKER:
            raise ApprovalError("bulk child authorization cannot be constructed by callers")
        self._intent = intent
        self._command_hash = command_hash
        self._parent_command_hash = parent_command_hash
        self._marker = _marker


def _mint_bulk_child_authorization(
    authorization: Any,
    *,
    parent_command: dict[str, Any],
    child_command: dict[str, Any],
) -> BulkChildAuthorization:
    """Narrow one exact durable parent claim to one deterministic child."""
    expected_parent_intent = {
        "operation": parent_command.get("operation"),
        "target": parent_command.get("target"),
        "change": parent_command.get("change"),
    }
    require_consumed_owner_approval(
        authorization,
        expected_intent=expected_parent_intent,
        expected_command=parent_command,
    )
    if parent_command.get("operation") != "bulk_linear_operations":
        raise ApprovalError("bulk child authorization requires an exact bulk parent")
    child_intent = {
        "operation": child_command.get("operation"),
        "target": child_command.get("target"),
        "change": child_command.get("change"),
    }
    return BulkChildAuthorization(
        intent=child_intent,
        command_hash=command_binding_hash(child_command),
        parent_command_hash=command_binding_hash(parent_command),
        _marker=_BULK_CHILD_MARKER,
    )


def require_consumed_owner_approval(
    authorization: Any,
    *,
    expected_intent: dict[str, Any],
    expected_command: dict[str, Any] | None = None,
) -> None:
    """Fail closed unless authorization came from atomic one-time consumption."""
    consumed_valid = (
        isinstance(authorization, ConsumedOwnerApproval)
        and authorization._marker is _CONSUMED_MARKER
        and authorization._intent == expected_intent
        and (
            authorization._command_hash is None
            or (
                expected_command is not None
                and authorization._command_hash == command_binding_hash(expected_command)
            )
        )
    )
    child_valid = (
        isinstance(authorization, BulkChildAuthorization)
        and authorization._marker is _BULK_CHILD_MARKER
        and authorization._intent == expected_intent
        and expected_command is not None
        and authorization._command_hash == command_binding_hash(expected_command)
    )
    if not (consumed_valid or child_valid):
        raise ApprovalError("apply requires a consumed owner approval for the exact intent")


def command_binding_hash(command: Any) -> str:
    """Hash the complete persisted command, including delivery identity and policy."""
    if not isinstance(command, dict):
        raise ApprovalError("owner approval command binding is invalid")
    required = {
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
    if set(command) != required:
        raise ApprovalError("owner approval command binding is invalid")
    return contract.canonical_sha256(command)


def _execution_binding(
    *, command: dict[str, Any], task_id: str, checksum: str, before_state_hash: str
) -> dict[str, str]:
    if not isinstance(task_id, str) or re.fullmatch(r"t_[a-f0-9]{8,}", task_id) is None:
        raise ApprovalError("owner approval task binding is invalid")
    reference = command.get("policy", {}).get("approval")
    if not isinstance(reference, dict) or reference.get("checksum") != checksum:
        raise ApprovalError("owner approval command binding is invalid")
    intent = {
        "operation": command.get("operation"),
        "target": command.get("target"),
        "change": command.get("change"),
    }
    intent_hash = contract.canonical_sha256(intent)
    if reference.get("intent_hash") != intent_hash:
        raise ApprovalError("owner approval intent binding is invalid")
    if reference.get("before_state_hash") != before_state_hash:
        raise ApprovalError("owner approval before-state binding is invalid")
    fields = ("command_id", "correlation_id", "idempotency_key", "source_profile")
    if any(not isinstance(command.get(field), str) or not command[field] for field in fields):
        raise ApprovalError("owner approval command identity is invalid")
    return {
        "approval_checksum": checksum,
        "intent_hash": intent_hash,
        "before_state_hash": before_state_hash,
        "command_hash": command_binding_hash(command),
        "command_id": command["command_id"],
        "correlation_id": command["correlation_id"],
        "idempotency_key": command["idempotency_key"],
        "source_profile": command["source_profile"],
        "task_id": task_id,
    }


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


_BINDING_FIELDS = {
    "approval_checksum",
    "intent_hash",
    "before_state_hash",
    "command_hash",
    "command_id",
    "correlation_id",
    "idempotency_key",
    "source_profile",
    "task_id",
}
_ENTRY_FIELDS = {
    "state",
    "binding",
    "lease_expires_at",
    "generation",
    "recovery_evidence_hash",
}
_RECOVERY_FIELDS = {
    "schema_version",
    "approval_checksum",
    "intent_hash",
    "command_hash",
    "before_state_hash",
    "after_state_hash",
    "phase",
}


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalError("owner approval claim time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc_text(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ApprovalError("owner approval apply journal is invalid")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ApprovalError("owner approval apply journal is invalid") from exc


def _load_apply_journal(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError("owner approval apply journal is invalid") from exc
    if (
        not isinstance(loaded, dict)
        or set(loaded) != {"schema_version", "entries"}
        or loaded.get("schema_version") != 2
        or not isinstance(loaded.get("entries"), dict)
    ):
        raise ApprovalError("owner approval apply journal is invalid")
    entries = loaded["entries"]
    for checksum, entry in entries.items():
        binding = entry.get("binding") if isinstance(entry, dict) else None
        if (
            not isinstance(checksum, str)
            or contract.SHA256.fullmatch(checksum) is None
            or not isinstance(entry, dict)
            or set(entry) != _ENTRY_FIELDS
            or entry.get("state") not in {"applying", "completed"}
            or not isinstance(binding, dict)
            or set(binding) != _BINDING_FIELDS
            or binding.get("approval_checksum") != checksum
            or any(not isinstance(value, str) or not value for value in binding.values())
            or any(
                contract.SHA256.fullmatch(binding[field]) is None
                for field in (
                    "approval_checksum",
                    "intent_hash",
                    "before_state_hash",
                    "command_hash",
                )
            )
            or not isinstance(entry.get("generation"), int)
            or isinstance(entry.get("generation"), bool)
            or entry["generation"] < 1
            or (
                entry.get("recovery_evidence_hash") is not None
                and (
                    not isinstance(entry["recovery_evidence_hash"], str)
                    or contract.SHA256.fullmatch(entry["recovery_evidence_hash"])
                    is None
                )
            )
        ):
            raise ApprovalError("owner approval apply journal is invalid")
        _parse_utc_text(entry.get("lease_expires_at"))
        if entry["state"] == "completed" and entry["recovery_evidence_hash"] is None:
            raise ApprovalError("owner approval apply journal is invalid")
    return entries


def _write_apply_journal(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump({"schema_version": 2, "entries": entries}, handle, sort_keys=True)
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


def _validate_recovery_evidence(
    evidence: Any, *, binding: dict[str, str], completed: bool
) -> str:
    if (
        not isinstance(evidence, dict)
        or set(evidence) != _RECOVERY_FIELDS
        or evidence.get("schema_version") != "linear-owner-recovery.v1"
        or evidence.get("approval_checksum") != binding["approval_checksum"]
        or evidence.get("intent_hash") != binding["intent_hash"]
        or evidence.get("command_hash") != binding["command_hash"]
        or evidence.get("before_state_hash") != binding["before_state_hash"]
        or not isinstance(evidence.get("after_state_hash"), str)
        or contract.SHA256.fullmatch(evidence["after_state_hash"]) is None
        or evidence.get("phase")
        not in ({"completed"} if completed else {"prepared", "completed"})
    ):
        raise ApprovalError("owner approval exact recovery evidence is required")
    return contract.canonical_sha256(evidence)


def _with_apply_journal(
    journal_path: Path, action: Callable[[dict[str, dict[str, Any]]], Any]
) -> Any:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = journal_path.with_suffix(journal_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return action(_load_apply_journal(journal_path))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def claim_owner_approval(
    verified: VerifiedOwnerApproval,
    *,
    command: dict[str, Any],
    task_id: str,
    journal_path: Path,
    now: datetime | None = None,
    lease_seconds: int = 30,
) -> ConsumedOwnerApproval:
    """Atomically claim one exact approval for one persisted command/task delivery."""
    if (
        not isinstance(verified, VerifiedOwnerApproval)
        or verified._marker is not _VERIFIED_MARKER
    ):
        raise ApprovalError("owner approval must be verified before claim")
    if (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or not 1 <= lease_seconds <= 300
    ):
        raise ApprovalError("owner approval lease must be between 1 and 300 seconds")
    current = now or datetime.now(timezone.utc)
    binding = _execution_binding(
        command=command,
        task_id=task_id,
        checksum=verified._checksum,
        before_state_hash=verified._before_state_hash,
    )

    def claim(entries: dict[str, dict[str, Any]]) -> ConsumedOwnerApproval:
        if binding["approval_checksum"] in entries:
            existing = entries[binding["approval_checksum"]]
            if existing["binding"] != binding:
                raise ApprovalError("owner approval apply binding conflicts")
            if existing["state"] == "completed":
                raise ApprovalError("owner approval apply is completed and non-reusable")
            raise ApprovalError("owner approval apply is already claimed")
        entries[binding["approval_checksum"]] = {
            "state": "applying",
            "binding": binding,
            "lease_expires_at": _utc_text(current + timedelta(seconds=lease_seconds)),
            "generation": 1,
            "recovery_evidence_hash": None,
        }
        _write_apply_journal(journal_path, entries)
        return ConsumedOwnerApproval(
            verified,
            _marker=_CONSUMED_MARKER,
            command_hash=binding["command_hash"],
            task_id=task_id,
            generation=1,
        )

    return _with_apply_journal(journal_path, claim)


def recover_owner_approval(
    policy: Any,
    *,
    command: dict[str, Any],
    task_id: str,
    journal_path: Path,
    recovery_evidence: Any,
    now: datetime | None = None,
    lease_seconds: int = 30,
) -> ConsumedOwnerApproval:
    """Re-enter a stale exact apply only when its prepared mutation is proven."""
    validate_policy(policy)
    if policy.get("mode") != "owner_approved":
        raise ApprovalError("owner_approved policy is required")
    if (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or not 1 <= lease_seconds <= 300
    ):
        raise ApprovalError("owner approval lease must be between 1 and 300 seconds")
    reference = policy["approval"]
    binding = _execution_binding(
        command=command,
        task_id=task_id,
        checksum=reference["checksum"],
        before_state_hash=reference["before_state_hash"],
    )
    current = now or datetime.now(timezone.utc)

    def recover(entries: dict[str, dict[str, Any]]) -> ConsumedOwnerApproval:
        entry = entries.get(binding["approval_checksum"])
        if entry is None:
            raise ApprovalError("owner approval apply claim does not exist")
        if entry["binding"] != binding:
            raise ApprovalError("owner approval apply binding conflicts")
        if entry["state"] == "completed":
            raise ApprovalError("owner approval apply is completed and non-reusable")
        if _parse_utc_text(entry["lease_expires_at"]) > current.astimezone(timezone.utc):
            raise ApprovalError("owner approval apply is already claimed")
        _validate_recovery_evidence(recovery_evidence, binding=binding, completed=False)
        generation = entry["generation"] + 1
        entry.update(
            {
                "lease_expires_at": _utc_text(
                    current + timedelta(seconds=lease_seconds)
                ),
                "generation": generation,
            }
        )
        _write_apply_journal(journal_path, entries)
        return ConsumedOwnerApproval._from_binding(
            intent={
                "operation": command["operation"],
                "target": command["target"],
                "change": command["change"],
            },
            checksum=binding["approval_checksum"],
            command_hash=binding["command_hash"],
            task_id=task_id,
            generation=generation,
        )

    return _with_apply_journal(journal_path, recover)


def complete_owner_approval(
    authorization: ConsumedOwnerApproval,
    *,
    journal_path: Path,
    recovery_evidence: Any,
) -> None:
    """Permanently complete an exact claimed approval after verified read-back."""
    if (
        not isinstance(authorization, ConsumedOwnerApproval)
        or authorization._marker is not _CONSUMED_MARKER
        or authorization._command_hash is None
        or authorization._task_id is None
    ):
        raise ApprovalError("owner approval completion authorization is invalid")

    def complete(entries: dict[str, dict[str, Any]]) -> None:
        entry = entries.get(authorization._checksum)
        if entry is None or entry["binding"].get("command_hash") != authorization._command_hash:
            raise ApprovalError("owner approval completion binding conflicts")
        if entry["binding"].get("task_id") != authorization._task_id:
            raise ApprovalError("owner approval completion binding conflicts")
        if entry["state"] == "completed":
            raise ApprovalError("owner approval apply is completed and non-reusable")
        if entry["generation"] != authorization._generation:
            raise ApprovalError("owner approval completion lease was superseded")
        evidence_hash = _validate_recovery_evidence(
            recovery_evidence, binding=entry["binding"], completed=True
        )
        entry["state"] = "completed"
        entry["recovery_evidence_hash"] = evidence_hash
        _write_apply_journal(journal_path, entries)

    _with_apply_journal(journal_path, complete)


def completed_owner_approval_matches(
    policy: Any,
    *,
    command: dict[str, Any],
    task_id: str,
    journal_path: Path,
    recovery_evidence: Any,
) -> bool:
    """Validate an exact completed replay without minting mutation authority."""
    validate_policy(policy)
    reference = policy["approval"]
    binding = _execution_binding(
        command=command,
        task_id=task_id,
        checksum=reference["checksum"],
        before_state_hash=reference["before_state_hash"],
    )

    def matches(entries: dict[str, dict[str, Any]]) -> bool:
        entry = entries.get(binding["approval_checksum"])
        if entry is None or entry["binding"] != binding:
            raise ApprovalError("owner approval apply binding conflicts")
        if entry["state"] != "completed":
            return False
        evidence_hash = _validate_recovery_evidence(
            recovery_evidence, binding=binding, completed=True
        )
        if entry["recovery_evidence_hash"] != evidence_hash:
            raise ApprovalError("owner approval completed recovery evidence conflicts")
        return True

    return bool(_with_apply_journal(journal_path, matches))


def consume_owner_approval(
    verified: VerifiedOwnerApproval, *, journal_path: Path
) -> ConsumedOwnerApproval:
    """Legacy atomic one-use authorization for direct lane compatibility tests."""
    if (
        not isinstance(verified, VerifiedOwnerApproval)
        or verified._marker is not _VERIFIED_MARKER
    ):
        raise ApprovalError("owner approval must be verified before consumption")
    _consume_once(journal_path, verified._checksum)
    return ConsumedOwnerApproval(verified, _marker=_CONSUMED_MARKER)
