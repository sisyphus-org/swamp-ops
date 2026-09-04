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
from pathlib import Path
from typing import Iterable, Set

import google.auth
import google.auth.exceptions
import google.oauth2.service_account  # noqa: F401  (ensures availability)
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

def profile_root(profile: str = "personal-assistant", hermes_home: Path | None = None) -> Path:
    """Return the profile-local secrets root."""
    base = hermes_home or Path(os.environ.get("HERMES_HOME", DEFAULT_HERMES_HOME))
    return base / "profiles" / profile

def token_path(profile: str = "personal-assistant", hermes_home: Path | None = None) -> Path:
    return profile_root(profile, hermes_home) / "google_token.json"

def client_secret_path(profile: str = "personal-assistant", hermes_home: Path | None = None) -> Path:
    return profile_root(profile, hermes_home) / "google_client_secret.json"

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

def load_credentials(profile: str = "personal-assistant", hermes_home: Path | None = None):
    """Build a refresh-aware Credentials object from the profile-local token.

    Raises FileNotFoundError if the token is missing.
    Raises google.auth.exceptions.RefreshError if the stored token is invalid.
    """
    import json

    tpath = token_path(profile, hermes_home)
    if not tpath.exists():
        raise FileNotFoundError(f"token not found: {tpath}")
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
