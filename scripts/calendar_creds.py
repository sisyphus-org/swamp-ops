#!/usr/bin/env python3
"""Secure, profile-local Google OAuth credential handling for the
Personal Assistant calendar read lane.

This module NEVER performs live OAuth and NEVER reads real calendar data.
It only:
  - declares the exact Calendar-only scopes,
  - validates that the persisted token contains no non-Calendar scope,
  - constructs a google.auth Credentials object from a profile-local token.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Iterable

import google.auth
import google.auth.exceptions
import google.oauth2.service_account
from google.auth.transport import requests as auth_requests

#: Exact scopes granted to the Personal Assistant calendar lane.
#: NOTE: do NOT add Gmail / Drive / Docs / Sheets / Contacts here.
ALLOWED_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
)

#: Scopes explicitly forbidden in the persisted token.
FORBIDDEN_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/contacts.readonly",
)

DEFAULT_HERMES_HOME = Path.home() / ".hermes"
SUPPORTED_PROFILE = "personal-assistant"
MAX_CALENDAR_ID_LENGTH = 1024

def profile_root(profile: str = "personal-assistant", hermes_home: Path | None = None) -> Path:
    """Return the profile-local secrets root."""
    if profile != SUPPORTED_PROFILE:
        raise ValueError(f"unsupported Calendar profile: {profile!r}")
    base = hermes_home or Path(os.environ.get("HERMES_HOME", DEFAULT_HERMES_HOME))
    if base.name == profile and base.parent.name == "profiles":
        return base
    return base / "profiles" / profile

def token_path(profile: str = "personal-assistant", hermes_home: Path | None = None) -> Path:
    return profile_root(profile, hermes_home) / "google_token.json"

def client_secret_path(profile: str = "personal-assistant", hermes_home: Path | None = None) -> Path:
    return profile_root(profile, hermes_home) / "google_client_secret.json"

def service_account_path(profile: str = "personal-assistant", hermes_home: Path | None = None) -> Path:
    return profile_root(profile, hermes_home) / "google_service_account.json"

def calendar_target_path(profile: str = "personal-assistant", hermes_home: Path | None = None) -> Path:
    return profile_root(profile, hermes_home) / "google_calendar_target.json"

def validate_scopes(scopes: Iterable[str]) -> None:
    """Ensure only ALLOWED_SCOPES are present and none are FORBIDDEN."""
    granted = set(scopes)
    extra = granted - set(ALLOWED_SCOPES)
    forbidden_hit = granted & set(FORBIDDEN_SCOPES)
    if extra or forbidden_hit:
        msg_parts = []
        if extra:
            msg_parts.append(f"extra scopes not allowed: {sorted(extra)}")
        if forbidden_hit:
            msg_parts.append(f"forbidden scopes present: {sorted(forbidden_hit)}")
        raise ValueError("; ".join(msg_parts))

def _require_secure_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise PermissionError(f"{label} must be a regular file: {path}")
    if secure_file_mode(path) != 0o600:
        raise PermissionError(f"{label} must have mode 0600: {path}")

def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()

def _closed_json_object(raw: str) -> dict:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=reject_duplicates)
    if not isinstance(value, dict):
        raise ValueError("target config must be a JSON object")
    return value

def load_calendar_target(
    profile: str = "personal-assistant",
    hermes_home: Path | None = None,
    *,
    service_account_selected: bool | None = None,
) -> str:
    """Resolve the protected target without accepting caller-supplied IDs."""
    service_path = service_account_path(profile, hermes_home)
    if service_account_selected is None:
        service_account_selected = _path_present(service_path)
    path = calendar_target_path(profile, hermes_home)
    if not _path_present(path):
        if service_account_selected:
            raise FileNotFoundError("service-account Calendar target is not configured")
        return "primary"
    _require_secure_regular_file(path, "Calendar target file")
    try:
        document = _closed_json_object(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Calendar target config is invalid") from exc
    if set(document) != {"calendar_id"}:
        raise ValueError("Calendar target config must contain exactly calendar_id")
    calendar_id = document["calendar_id"]
    if (
        not isinstance(calendar_id, str)
        or not calendar_id.strip()
        or calendar_id != calendar_id.strip()
        or len(calendar_id) > MAX_CALENDAR_ID_LENGTH
        or any(not char.isprintable() for char in calendar_id)
    ):
        raise ValueError("Calendar target ID is invalid")
    if service_account_selected and calendar_id == "primary":
        raise ValueError("service-account Calendar target cannot be primary")
    return calendar_id

def load_credentials(profile: str = "personal-assistant", hermes_home: Path | None = None):
    """Build credentials from profile-local service-account or OAuth data.

    Service-account credentials are preferred whenever their file exists.
    Raises FileNotFoundError if neither credential file exists.
    Raises google.auth.exceptions.RefreshError if the stored token is invalid.
    """
    service_path = service_account_path(profile, hermes_home)
    if _path_present(service_path):
        _require_secure_regular_file(service_path, "service-account file")
        load_calendar_target(
            profile, hermes_home, service_account_selected=True
        )
        return google.oauth2.service_account.Credentials.from_service_account_file(
            str(service_path), scopes=list(ALLOWED_SCOPES)
        )

    tpath = token_path(profile, hermes_home)
    if not tpath.exists():
        raise FileNotFoundError(f"token not found: {tpath}")
    _require_secure_regular_file(tpath, "token file owner-only")
    info = json.loads(tpath.read_text())
    scopes = info.get("scopes", [])
    validate_scopes(scopes)
    creds, _ = google.auth.load_credentials_from_file(
        tpath,
        scopes=list(scopes) if scopes else list(ALLOWED_SCOPES),
    )
    return creds

def secure_file_mode(path: Path) -> int:
    """Return the numeric permission bits of a file (e.g. 0o600)."""
    return path.stat().st_mode & 0o777
