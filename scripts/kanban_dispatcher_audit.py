#!/usr/bin/env python3
"""Read-only audit of the production Hermes Kanban dispatcher topology."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

import yaml


HERMES_ROOT = Path("/Users/hermes/.hermes")
PROFILES = [
    "default",
    "ideas",
    "swe",
    "books",
    "crypto-analyst",
    "broker",
    "project-manager",
]
EXPECTED_OWNER = "broker"


def parse_dispatch_flag(config_text: str) -> bool | None:
    """Return a direct boolean kanban.dispatch_in_gateway value, or None."""
    try:
        parsed = yaml.safe_load(config_text)
    except (yaml.YAMLError, ValueError, KeyError, IndexError):
        return None
    if not isinstance(parsed, dict):
        return None
    kanban = parsed.get("kanban")
    if not isinstance(kanban, dict):
        return None
    value = kanban.get("dispatch_in_gateway")
    return value if type(value) is bool else None


def profile_from_gateway_command(command: str) -> str | None:
    """Extract the Hermes profile identity from a gateway command line."""
    argv = shlex.split(command)
    if "gateway" not in argv or "run" not in argv:
        return None
    for index, token in enumerate(argv[:-1]):
        if token in {"--profile", "-p"}:
            return argv[index + 1]
        if token.startswith("--profile="):
            return token.split("=", 1)[1]
    return "default"


def audit_lock(
    lock_path: Path,
    expected_owner: str,
    runner: Callable = subprocess.run,
) -> dict:
    """Identify the sole process holding the dispatcher lock."""
    lock = runner(
        ["lsof", "-t", str(lock_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    pids = sorted({int(value) for value in lock.stdout.split() if value.isdigit()})
    owners = []
    commands = {}
    for pid in pids:
        proc = runner(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
        command = proc.stdout.strip()
        commands[str(pid)] = command
        owners.append(profile_from_gateway_command(command))
    owner = owners[0] if len(owners) == 1 else None
    return {
        "valid": len(pids) == 1 and owner == expected_owner,
        "expected_owner": expected_owner,
        "owner_profile": owner,
        "pids": pids,
        "commands": commands,
    }


def audit_configs(root: Path, profiles: list[str]) -> dict:
    """Check that every profile is explicit and only broker enables dispatch."""
    values = {}
    for profile in profiles:
        home = root if profile == "default" else root / "profiles" / profile
        path = home / "config.yaml"
        values[profile] = parse_dispatch_flag(path.read_text()) if path.is_file() else None
    enabled = sorted(name for name, value in values.items() if value is True)
    missing = sorted(name for name, value in values.items() if value is None)
    return {
        "valid": enabled == ["broker"] and not missing,
        "enabled": enabled,
        "missing_explicit": missing,
        "profiles": values,
    }


def build_report(
    root: Path,
    profiles: list[str],
    expected_owner: str,
    lock_probe: Callable = audit_lock,
) -> dict:
    """Build one machine-readable config-plus-runtime attestation."""
    config = audit_configs(root, profiles)
    lock = lock_probe(root / "kanban" / ".dispatcher.lock", expected_owner)
    valid = config["valid"] and lock["valid"]
    return {
        "result": "pass" if valid else "drift",
        "readOnly": True,
        "expectedOwner": expected_owner,
        "config": config,
        "lock": lock,
    }


def main(report_builder: Callable = build_report) -> int:
    """Print the fixed production topology audit and fail on drift."""
    report = report_builder(HERMES_ROOT, PROFILES, EXPECTED_OWNER)
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
