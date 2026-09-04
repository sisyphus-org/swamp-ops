#!/usr/bin/env python3
"""Deterministic Google Calendar read CLI for the Personal Assistant.

Public seams:
  - WindowSpec: bounded read windows (today, next-7-days, next-30-days).
  - normalize_event: all-day / timed / recurring normalization.
  - build_query_bounds: Europe/Kyiv-aware datetime bounds.
  - list_calendars_paginated, list_events_paginated, query_freebusy: pagination + Kyiv.
  - run_read: atomic payload write + sanitized stdout (no sensitive data).

NO live network calls are made unless --live is given and the profile-local
token exists. This module is import-safe for tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, date, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

try:
    from google.oauth2 import service_account  # noqa: F401
except Exception:  # pragma: no cover
    service_account = None  # type: ignore

# Reuse the OAuth helper (loaded by spec to avoid path issues)
import importlib.util
_spec = importlib.util.spec_from_file_location("calendar_creds", Path(__file__).parent / "calendar_creds.py")
creds_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec and _spec.loader is not None
_spec.loader.exec_module(creds_mod)
sys.modules["calendar_creds"] = creds_mod

ALLOWED_SCOPES = creds_mod.ALLOWED_SCOPES
validate_scopes = creds_mod.validate_scopes
profile_root = creds_mod.profile_root
token_path = creds_mod.token_path

KYIV = ZoneInfo("Europe/Kyiv")
VALID_WINDOWS = ("today", "next-7-days", "next-30-days")

# Read allowlist: only the primary calendar is read in this slice.
READ_CALENDAR_IDS: tuple[str, ...] = ("primary",)
# Write allowlist: empty in this slice.
WRITE_CALENDAR_IDS: tuple[str, ...] = ()


class Window(Enum):
    TODAY = "today"
    NEXT_7 = "next-7-days"
    NEXT_30 = "next-30-days"


def build_query_bounds(window: str, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """Return (start, end) datetimes in the given tz (default Europe/Kyiv)."""
    if window not in VALID_WINDOWS:
        raise ValueError(f"invalid window: {window!r}; expected one of {VALID_WINDOWS}")
    tz = tz or KYIV
    now = datetime.now(tz=tz)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "today":
        end = day_start + timedelta(days=1)
    elif window == "next-7-days":
        end = day_start + timedelta(days=7)
    else:
        end = day_start + timedelta(days=30)
    return day_start, end


def _format_dt(dt: Any) -> str:
    """Render an API datetime or date-only string in Kyiv."""
    if not dt:
        return ""
    if "T" in dt:  # timed
        val = datetime.fromisoformat(dt[:-1] + "+00:00" if dt.endswith("Z") else dt)
        return val.astimezone(KYIV).isoformat()
    else:  # date-only (all-day)
        d = date.fromisoformat(dt)
        return d.isoformat()


def normalize_event(raw: dict, *, include_summary: bool = False) -> dict:
    """Normalize a raw API event into a compact, timezone-safe form.

    No PII such as title, location, description, or calendar ID is included
    in the *default* output; callers can opt in via include_summary.
    """
    start = raw.get("start", {}) or {}
    end = raw.get("end", {}) or {}
    out: dict[str, Any] = {
        "id": raw.get("id", ""),
        "summary": raw.get("summary", "") if include_summary else "",
        "start": _format_dt(start.get("dateTime") or start.get("date")),
        "end": _format_dt(end.get("dateTime") or end.get("date")),
        "all_day": "date" in start,
        "recurring": bool(raw.get("recurringEventId") or raw.get("recurrence")),
        "status": raw.get("status", ""),
    }
    return out


def list_calendars_paginated(service) -> Iterator[dict]:
    """Yield each calendar in the account, exhausting pagination."""
    req = service.calendarList().list(pageToken=None, maxResults=250, showHidden=True)
    while req is not None:
        resp = req.execute()
        for item in resp.get("items", []):
            yield item
        req = service.calendarList().list_next(req, resp)


def list_events_paginated(service, calendar_id: str, time_min: datetime, time_max: datetime) -> Iterator[dict]:
    """Yield events for calendar_id in [time_min, time_max), exhausting pagination."""
    if calendar_id not in READ_CALENDAR_IDS:
        raise PermissionError(f"calendar_id {calendar_id!r} is not in the read allowlist")
    req = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=250,
        timeZone=str(KYIV),
    )
    while req is not None:
        resp = req.execute()
        for item in resp.get("items", []):
            yield item
        req = service.events().list_next(req, resp)


def query_freebusy(service, time_min: datetime, time_max: datetime, calendar_ids: list[str] | None = None) -> dict:
    """Return free/busy info for calendar_ids (defaults to READ_CALENDAR_IDS)."""
    selected = calendar_ids or list(READ_CALENDAR_IDS)
    if any(cid not in READ_CALENDAR_IDS for cid in selected):
        raise PermissionError("free/busy calendar is not in the read allowlist")
    items = [{"id": cid} for cid in selected]
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "timeZone": str(KYIV),
        "items": items,
    }
    return service.freebusy().query(body=body).execute()


def run_read(operation: str, window: str, *, service=None, profile: str = "personal-assistant",
             include_summary: bool = False, live: bool = False) -> dict:
    """Execute a read operation.

    operation: inventory | events | freebusy | smoke
    Returns a sanitized result dict. Writes detailed payload atomically when live.
    """
    if window not in VALID_WINDOWS:
        raise ValueError(f"invalid window: {window!r}")

    start, end = build_query_bounds(window)

    if operation == "smoke":
        return {
            "operation": "smoke",
            "status": "ok",
            "timezone": str(KYIV),
            "window": window,
            "bounds": {"start": start.isoformat(), "end": end.isoformat()},
            "checks": ["timezone-resolved", "bounds-valid"],
        }

    # Reject unknown operations BEFORE any network/credentials access.
    valid_ops = ("inventory", "events", "freebusy", "smoke")
    if operation not in valid_ops:
        raise ValueError(f"unknown operation: {operation!r}")

    if not live:
        # Non-live mode: return a plan/skeleton without network.
        return {
            "operation": operation,
            "status": "planned",
            "timezone": str(KYIV),
            "window": window,
            "bounds": {"start": start.isoformat(), "end": end.isoformat()},
            "calendars_considered": list(READ_CALENDAR_IDS),
        }

    if service is None:
        import importlib
        from google.auth.transport.requests import Request

        from google.oauth2 import credentials as creds_mod_g

        # Load via our helper
        google_creds = creds_mod.load_credentials(profile)
        if not google_creds.valid:
            google_creds.refresh(Request())
        from googleapiclient.discovery import build

        service = build("calendar", "v3", credentials=google_creds, cache_discovery=False)

    if operation == "inventory":
        items = [
            {
                "id": c.get("id", ""),
                "summary": "",
                "access_role": c.get("accessRole", ""),
                "primary": bool(c.get("primary", False)),
            }
            for c in list_calendars_paginated(service)
        ]
        result = {
            "operation": "inventory",
            "status": "ok",
            "timezone": str(KYIV),
            "window": window,
            "bounds": {"start": start.isoformat(), "end": end.isoformat()},
            "calendar_count": len(items),
            "writable_calendar_count": sum(1 for c in items if c["access_role"] in ("writer", "owner")),
            "calendars": items,
        }
    elif operation == "events":
        primary = next(
            (c for c in list_calendars_paginated(service) if c.get("primary")),
            None,
        )
        if primary is None:
            raise RuntimeError("primary calendar not found")
        raw_events = list(list_events_paginated(service, "primary", start, end))
        events = [normalize_event(e, include_summary=include_summary) for e in raw_events]
        result = {
            "operation": "events",
            "status": "ok",
            "timezone": str(KYIV),
            "window": window,
            "bounds": {"start": start.isoformat(), "end": end.isoformat()},
            "calendar_count": 1,
            "writable_calendar_count": 1,
            "event_count": len(events),
            "all_day_events": sum(1 for e in events if e["all_day"]),
            "recurring_events": sum(1 for e in events if e["recurring"]),
            "events": events,
        }
    elif operation == "freebusy":
        fb = query_freebusy(service, start, end)
        busy = fb.get("calendars", {}).get("primary", {}).get("busy", [])
        result = {
            "operation": "freebusy",
            "status": "ok",
            "timezone": str(KYIV),
            "window": window,
            "bounds": {"start": start.isoformat(), "end": end.isoformat()},
            "calendar_count": 1,
            "writable_calendar_count": 1,
            "busy_intervals": len(busy),
        }
    else:
        raise ValueError(f"unknown operation: {operation!r}")

    _write_atomic_payload(result, profile)
    _emit_sanitized_stdout(result)
    return result


def _output_path(profile: str = "personal-assistant") -> Path:
    root = profile_root(profile)
    out_dir = root / "calendar_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / "latest-read.json"


def _write_atomic_payload(result: dict, profile: str = "personal-assistant") -> None:
    """Atomically write the detailed (but redacted) payload to 0600 file."""
    out = _output_path(profile)
    redacted = dict(result)
    # The events payload already excludes titles unless include_summary was explicit.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{out.name}.", suffix=".tmp", dir=out.parent
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(redacted, handle, default=str, indent=2, sort_keys=True)
        tmp.replace(out)
        os.chmod(out, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def _emit_sanitized_stdout(result: dict) -> None:
    """Print only safe counts/metadata — never event titles, locations, IDs, emails."""
    safe = {
        "operation": result.get("operation"),
        "status": result.get("status"),
        "timezone": result.get("timezone"),
        "window": result.get("window"),
        "bounds": result.get("bounds"),
    }
    for k in ("calendar_count", "writable_calendar_count", "event_count",
              "all_day_events", "recurring_events", "busy_intervals"):
        if k in result:
            safe[k] = result[k]
    print(json.dumps(safe, default=str, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic Google Calendar read lane for Personal Assistant.",
    )
    parser.add_argument("--operation", choices=["inventory", "events", "freebusy", "smoke"], default="smoke")
    parser.add_argument("--window", choices=VALID_WINDOWS, default="today")
    parser.add_argument("--include-summary", action="store_true",
                        help="Include event summary in payload (off by default; stdout never has it).")
    parser.add_argument("--profile", default="personal-assistant")
    parser.add_argument("--live", action="store_true", help="Actually hit the Calendar API.")
    args = parser.parse_args(argv)

    result = run_read(
        operation=args.operation,
        window=args.window,
        profile=args.profile,
        include_summary=args.include_summary,
        live=args.live,
    )
    # Print full result for --live, sanitized for non-live
    if not args.live:
        print(json.dumps(result, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
