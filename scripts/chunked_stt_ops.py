#!/usr/bin/env python3
"""Read-only Swamp plan and bounded smoke test for chunked Qwen STT."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCRIPT = REPOSITORY_ROOT / "scripts" / "chunked_qwen_stt.py"
NUMERIC_PROMPT_FILE = REPOSITORY_ROOT / "config" / "qwen-stt-numeric-prompt.txt"
DEPLOYED_SCRIPT = Path("/Users/hermes/workspaces/runtime/hermes-stt/chunked_qwen_stt.py")
QWEN_LAUNCHDAEMON = Path("/Library/LaunchDaemons/local.qwen-stt.plist")
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


def read_desired_prompt(path: Path = NUMERIC_PROMPT_FILE) -> str:
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError(f"Qwen numeric prompt is empty: {path}")
    return prompt


def read_launchdaemon_prompt(path: Path = QWEN_LAUNCHDAEMON) -> str | None:
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    environment = payload.get("EnvironmentVariables", {})
    if not isinstance(environment, dict):
        raise RuntimeError(f"invalid EnvironmentVariables in {path}")
    prompt = environment.get("HERMES_STT_VOCAB")
    if prompt is None:
        return None
    if not isinstance(prompt, str):
        raise RuntimeError(f"HERMES_STT_VOCAB must be a string in {path}")
    return prompt.strip()


def read_profile_command(profile_home: Path) -> str | None:
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
        return None
    return " ".join(proc.stdout.split())


def build_plan(
    *,
    profile_commands: dict[str, str | None],
    deployed_script: Path,
    source_script: Path,
    deployed_hash: str | None,
    source_hash: str,
    desired_prompt: str | None = None,
    live_prompt: str | None = None,
) -> dict:
    expected = desired_command(deployed_script)
    unreadable = sorted(
        profile for profile, command in profile_commands.items() if command is None
    )
    noncompliant = sorted(
        profile
        for profile, command in profile_commands.items()
        if command is not None and command != expected
    )
    deployed_matches = deployed_hash == source_hash and deployed_hash is not None
    prompt_audited = desired_prompt is not None
    prompt_compliant = live_prompt == desired_prompt if prompt_audited else None
    if unreadable:
        result = "error"
    elif noncompliant or not deployed_matches or prompt_compliant is False:
        result = "changes_required"
    else:
        result = "compliant"
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
    if prompt_compliant is False:
        actions.append(
            {
                "type": "set-qwen-prompt",
                "launchDaemon": str(QWEN_LAUNCHDAEMON),
                "environmentVariable": "HERMES_STT_VOCAB",
                "ownerRequired": True,
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
        "numericPromptFile": str(NUMERIC_PROMPT_FILE),
        "numericPromptSha256": (
            hashlib.sha256(desired_prompt.encode("utf-8")).hexdigest()
            if desired_prompt is not None
            else None
        ),
        "livePromptSha256": (
            hashlib.sha256(live_prompt.encode("utf-8")).hexdigest()
            if live_prompt is not None
            else None
        ),
        "qwenPromptCompliant": prompt_compliant,
        "profileCommands": profile_commands,
        "unreadableProfiles": unreadable,
        "noncompliantProfiles": noncompliant,
        "plannedActions": actions,
        "approvalRequiredBeforeApply": bool(actions) or bool(unreadable),
    }


def run_plan() -> dict:
    if not SOURCE_SCRIPT.is_file():
        raise RuntimeError(f"source script missing: {SOURCE_SCRIPT}")
    source_hash = sha256_file(SOURCE_SCRIPT)
    deployed_hash = sha256_file(DEPLOYED_SCRIPT) if DEPLOYED_SCRIPT.is_file() else None
    commands = {
        profile: read_profile_command(home) for profile, home in PROFILE_HOMES.items()
    }
    desired_prompt = read_desired_prompt()
    live_prompt = read_launchdaemon_prompt()
    return build_plan(
        profile_commands=commands,
        deployed_script=DEPLOYED_SCRIPT,
        source_script=SOURCE_SCRIPT,
        deployed_hash=deployed_hash,
        source_hash=source_hash,
        desired_prompt=desired_prompt,
        live_prompt=live_prompt,
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
    for suffix in (".ogg", ".wav", ".aiff", ".m4a", ".mp3"):
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


def parse_metrics(stderr: str) -> dict:
    for line in reversed(stderr.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(parsed, dict)
            and "durationSeconds" in parsed
            and "chunkCount" in parsed
        ):
            return parsed
    raise RuntimeError("chunked STT smoke produced no metrics line")


def validate_numeric_transcript(transcript: str) -> list[str]:
    checks = {
        "quantity-25": r"(?<!\d)25(?!\d)",
        "negative-7": r"(?<!\d)-7(?!\d)",
        "decimal-3-5": r"(?<!\d)3,5(?!\d)",
        "percent-12": r"(?<!\d)12\s*%",
        "date-28": r"(?<!\d)28\s+августа",
        "year-2026": r"(?<!\d)2026(?!\d)",
        "time-14-30": r"(?<!\d)14:30(?!\d)",
        "version-3-2": r"(?<!\d)3[.,]2(?!\d)",
        "amount-1200": r"(?<!\d)(?:1200|1\s*200)(?!\d)",
    }
    issues = [
        f"missing:{label}"
        for label, pattern in checks.items()
        if re.search(pattern, transcript, flags=re.IGNORECASE) is None
    ]
    number_words = re.compile(
        r"\b(?:двадцать|пять|минус\s+семь|двенадцать|две\s+тысячи|"
        r"три\s+целых\s+пять\s+десятых|четырнадцать|тридцать|"
        r"три\s+точка\s+два|тысяча\s+двести)\b",
        flags=re.IGNORECASE,
    )
    if number_words.search(transcript):
        issues.append("number-words-remain")
    return issues


def run_smoke(slug: str, *, require_numeric_formats: bool = False) -> dict:
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
    metrics = parse_metrics(proc.stderr)
    transcript = output.read_text(encoding="utf-8")
    numeric_issues = (
        validate_numeric_transcript(transcript) if require_numeric_formats else []
    )
    if numeric_issues:
        raise RuntimeError(
            "numeric STT assertions failed: " + ", ".join(numeric_issues)
        )
    return {
        "mode": "numeric-smoke" if require_numeric_formats else "smoke",
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
        "numericAssertionsPassed": require_numeric_formats,
        "runtimeOutput": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["plan", "smoke", "numeric-smoke"], default="plan"
    )
    parser.add_argument("--sample", default="books-7m")
    parser.add_argument("--numeric-sample", default="sis69-numbers")
    args = parser.parse_args()
    try:
        if args.mode == "plan":
            payload = run_plan()
        elif args.mode == "numeric-smoke":
            payload = run_smoke(args.numeric_sample, require_numeric_formats=True)
        else:
            payload = run_smoke(args.sample)
        emit(payload)
        return 0
    except Exception as exc:
        emit({"mode": args.mode, "result": "error", "issues": [str(exc)]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
