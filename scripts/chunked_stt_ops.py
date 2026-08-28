#!/usr/bin/env python3
"""Read-only Swamp plan and bounded smoke test for chunked Qwen STT."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCRIPT = REPOSITORY_ROOT / "scripts" / "chunked_qwen_stt.py"
DEPLOYED_SCRIPT = Path("/Users/hermes/workspaces/runtime/hermes-stt/chunked_qwen_stt.py")
QWEN_PYTHON = Path("/Users/hermes/.local/share/hermes-stt/.venv/bin/python")
SAMPLE_ROOT = REPOSITORY_ROOT / ".swamp" / "stt-samples"
SMOKE_OUTPUT_ROOT = REPOSITORY_ROOT / ".swamp" / "stt-smoke"
PROFILE_HOMES = {
    "default": Path("/Users/hermes/.hermes"),
    "ideas": Path("/Users/hermes/.hermes/profiles/ideas"),
    "swe": Path("/Users/hermes/.hermes/profiles/swe"),
    "books": Path("/Users/hermes/.hermes/profiles/books"),
    "crypto-analyst": Path("/Users/hermes/.hermes/profiles/crypto-analyst"),
}


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def validate_sample_slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
        raise ValueError("sample must match [a-z0-9][a-z0-9-]{0,63}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def desired_command(deployed_script: Path = DEPLOYED_SCRIPT) -> str:
    return f"{QWEN_PYTHON} {deployed_script} {{input_path}} {{output_path}}"


def read_profile_command(profile_home: Path) -> str:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile_home)
    proc = subprocess.run(
        ["hermes", "config", "get", "stt.providers.qwen3.command"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if proc.returncode != 0:
        return f"<unreadable: {proc.stderr.strip() or proc.stdout.strip()}>"
    return " ".join(proc.stdout.split())


def build_plan(
    *,
    profile_commands: dict[str, str],
    deployed_script: Path,
    source_script: Path,
    deployed_hash: str | None,
    source_hash: str,
) -> dict:
    expected = desired_command(deployed_script)
    noncompliant = sorted(
        profile for profile, command in profile_commands.items() if command != expected
    )
    deployed_matches = deployed_hash == source_hash and deployed_hash is not None
    result = "compliant" if not noncompliant and deployed_matches else "changes_required"
    actions = []
    if not deployed_matches:
        actions.append(
            {
                "type": "deploy-script",
                "source": str(source_script),
                "target": str(deployed_script),
                "expectedSha256": source_hash,
            }
        )
    for profile in noncompliant:
        actions.append(
            {
                "type": "set-profile-command",
                "profile": profile,
                "command": expected,
            }
        )
    return {
        "mode": "plan",
        "result": result,
        "readOnly": True,
        "sourceScript": str(source_script),
        "sourceSha256": source_hash,
        "deployedScript": str(deployed_script),
        "deployedSha256": deployed_hash,
        "desiredCommand": expected,
        "profileCommands": profile_commands,
        "noncompliantProfiles": noncompliant,
        "plannedActions": actions,
        "approvalRequiredBeforeApply": bool(actions),
    }


def run_plan() -> dict:
    if not SOURCE_SCRIPT.is_file():
        raise RuntimeError(f"source script missing: {SOURCE_SCRIPT}")
    source_hash = sha256_file(SOURCE_SCRIPT)
    deployed_hash = sha256_file(DEPLOYED_SCRIPT) if DEPLOYED_SCRIPT.is_file() else None
    commands = {
        profile: read_profile_command(home) for profile, home in PROFILE_HOMES.items()
    }
    return build_plan(
        profile_commands=commands,
        deployed_script=DEPLOYED_SCRIPT,
        source_script=SOURCE_SCRIPT,
        deployed_hash=deployed_hash,
        source_hash=source_hash,
    )


def validated_runtime_directory(
    path: Path,
    *,
    label: str,
    create: bool,
) -> Path:
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeError(f"{label} and its runtime parent must not be symlinks: {path}")
    parent = path.parent.resolve(strict=True)
    if create:
        path.mkdir(parents=False, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"{label} must not be a symlink: {path}")
    resolved = path.resolve(strict=True)
    if resolved.parent != parent:
        raise RuntimeError(f"{label} resolves outside its fixed runtime parent: {path}")
    return resolved


def resolve_sample(slug: str) -> Path:
    root = validated_runtime_directory(
        SAMPLE_ROOT,
        label="sample root",
        create=False,
    )
    matches: list[Path] = []
    for suffix in (".ogg", ".wav", ".m4a", ".mp3"):
        candidate = SAMPLE_ROOT / f"{slug}{suffix}"
        if candidate.is_symlink():
            raise RuntimeError(f"sample must not be a symlink: {candidate}")
        if not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"sample resolves outside runtime root: {candidate}") from exc
        matches.append(resolved)
    if len(matches) != 1:
        raise RuntimeError(
            f"sample '{slug}' must resolve to exactly one audio file under {SAMPLE_ROOT}"
        )
    return matches[0]


def run_smoke(slug: str) -> dict:
    sample = resolve_sample(validate_sample_slug(slug))
    output_root = validated_runtime_directory(
        SMOKE_OUTPUT_ROOT,
        label="smoke output root",
        create=True,
    )
    output = output_root / f"{slug}.txt"
    if output.exists():
        output.unlink()
    proc = subprocess.run(
        [sys.executable, str(SOURCE_SCRIPT), str(sample), str(output)],
        capture_output=True,
        text=True,
        timeout=840,
    )
    if proc.returncode != 0 or not output.is_file():
        raise RuntimeError(f"chunked STT smoke failed: {proc.stderr.strip()}")
    stderr_lines = [line for line in proc.stderr.splitlines() if line.strip()]
    metrics = json.loads(stderr_lines[-1])
    transcript = output.read_text(encoding="utf-8")
    return {
        "mode": "smoke",
        "result": "passed",
        "readOnlyExternalState": True,
        "sample": slug,
        "sampleSha256": sha256_file(sample),
        "transcriptSha256": sha256_file(output),
        "transcriptChars": len(transcript),
        "transcriptWords": len(transcript.split()),
        "durationSeconds": metrics["durationSeconds"],
        "chunkCount": metrics["chunkCount"],
        "transcriptContentIncluded": False,
        "runtimeOutput": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["plan", "smoke"], default="plan")
    parser.add_argument("--sample", default="books-7m")
    args = parser.parse_args()
    try:
        payload = run_plan() if args.mode == "plan" else run_smoke(args.sample)
        emit(payload)
        return 0
    except Exception as exc:
        emit({"mode": args.mode, "result": "error", "issues": [str(exc)]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
