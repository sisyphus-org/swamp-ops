#!/usr/bin/env python3
"""Task and Calendar Creation Flow — plan/apply with cross-linking.

Plan step is read-only: builds checksum-bound intents for BOTH Calendar and
Linear without any API calls. Apply step executes after owner approval:
  1. Creates Calendar event -> event_id
  2. Creates Linear task -> task_id
  3. Updates Calendar event description с link на task
  4. Updates Linear task description с link на calendar

No AI calls. Deterministic. Timezone Europe/Kyiv.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from typing import Any

KYIV = ZoneInfo("Europe/Kyiv")
LINEAR_API = "https://api.linear.app/graphql"

def _load_linear_token():
    """Load LINEAR_TOKEN from env or .env file."""
    token = os.environ.get("LINEAR_TOKEN")
    if token:
        return token
    # Fallback: read from .env file
    env_path = Path.home() / ".hermes" / "profiles" / "personal-assistant" / ".env"
    if env_path.is_file():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("LINEAR_TOKEN="):
                    return line.split("=", 1)[1]
    raise ValueError("LINEAR_TOKEN not found")

LINEAR_TOKEN_VAL = _load_linear_token()

def _linear_graphql(query: str, variables: dict | None = None) -> dict:
    """Execute a Linear GraphQL query."""
    payload = {
        "query": query,
        "variables": variables or {},
    }
    headers = {
        "Authorization": LINEAR_TOKEN_VAL,
        "Content-Type": "application/json",
    }
    resp = requests.post(LINEAR_API, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Linear API errors: {data['errors']}")
    return data.get("data", {})

SAFE_STATES = {"Backlog", "Todo", "Research", "In Progress", "In Review"}
TERMINAL_STATES = {"Done", "Canceled"}
PRIORITIES = {"High", "Medium", "Low"}

def build_calendar_intent(title: str, start_dt: datetime, duration_minutes: int) -> dict:
    return {
        "summary": title,
        "start": start_dt.isoformat(),
        "end": (start_dt + timedelta(minutes=duration_minutes)).isoformat(),
        "time_zone": str(KYIV),
    }

def build_linear_intent(title: str, calendar_intent: dict, date_str: str) -> dict:
    return {
        "title": title,
        "description": f"Calendar event scheduled for {date_str} in Europe/Kyiv timezone. Link will be added after creation.",
        "assignee": "alexey.petrov",
        "tags": ["calendar-linked", "automated"],
        "priority": "medium",
        "state": "todo",
    }

def compute_checksum(*args) -> str:
    payload = json.dumps([*args], sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()

def build_plan(title: str, date_str: str, time_str: str, meridiem: str | None = None,
               duration_minutes: int = 30) -> dict:
    from datetime import datetime as _dt
    try:
        d = _dt.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str}. Expected YYYY-MM-DD.")
    parts = time_str.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {time_str}. Expected HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    m = meridiem.lower().strip() if meridiem else None
    if m == "am" and hour == 12:
        hour_24 = 0
    elif m == "pm" and hour != 12:
        hour_24 = hour + 12
    else:
        hour_24 = hour
    if m is None:
        hour_24 = hour if hour < 24 else hour - 24
    start_dt = _dt(d.year, d.month, d.day, hour_24, minute, tzinfo=KYIV)
    now = _dt.now(tz=KYIV)
    if start_dt < now - timedelta(days=365):
        raise ValueError("Date is too far in past")
    calendar_intent = build_calendar_intent(title, start_dt, duration_minutes)
    linear_intent = build_linear_intent(title, calendar_intent, date_str)
    checksum = compute_checksum(calendar_intent, linear_intent)
    return {
        "operation": "task-and-calendar-create",
        "mode": "plan",
        "status": "ready",
        "timezone": str(KYIV),
        "summary": title,
        "calendar_intent": calendar_intent,
        "linear_intent": linear_intent,
        "checksum": checksum,
        "message": "Plan ready. Approve to apply both Calendar event and Linear task with mutual linking.",
    }

def apply_plan(checksum: str, calendar_intent_json: str, linear_intent_json: str) -> dict:
    calendar_intent = json.loads(calendar_intent_json)
    linear_intent = json.loads(linear_intent_json)
    recomputed = compute_checksum(calendar_intent, linear_intent)
    if checksum != recomputed:
        raise ValueError(f"Checksum mismatch: expected {checksum}, got {recomputed}")

    results: dict[str, Any] = {"steps": []}

    # 1. Calendar event
    event_result = _create_calendar_event(calendar_intent)
    results["calendar_event"] = event_result
    results["steps"].append({"name": "calendar_create", "status": "done", "id": event_result["id"]})

    # 2. Linear task with calendar link
    linear_result = _create_linear_task(linear_intent, event_result["html_link"])
    results["linear_task"] = linear_result
    results["steps"].append({"name": "linear_create", "status": "done", "id": linear_result.get("task_id")})

    # 3. Update Calendar description
    _update_calendar_description(event_result["id"], linear_result["url"])
    results["steps"].append({"name": "calendar_update", "status": "done"})

    # 4. Update Linear task
    _update_linear_task(linear_result["task_id"], event_result["html_link"])
    results["steps"].append({"name": "linear_update", "status": "done"})

    results["operation"] = "task-and-calendar-create"
    results["mode"] = "apply"
    results["status"] = "done"
    results["checksum"] = checksum
    return results

def _create_calendar_event(intent: dict) -> dict:
    token_path = Path.home() / ".hermes" / "profiles" / "personal-assistant" / "google_token.json"
    import google.auth
    from google.oauth2 import credentials as gc
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = gc.Credentials.from_authorized_user_file(
        str(token_path),
        scopes=[
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.freebusy",
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        ],
    )
    if not creds.valid:
        creds.refresh(Request())
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    event_body = {
        "summary": intent["summary"],
        "start": {"dateTime": intent["start"], "timeZone": intent["time_zone"]},
        "end": {"dateTime": intent["end"], "timeZone": intent["time_zone"]},
        "description": "Linked task: pending creation...",
    }
    created = svc.events().insert(calendarId="primary", body=event_body).execute()
    return {"id": created["id"], "html_link": created.get("htmlLink", "")}

def _create_linear_task(intent: dict, calendar_link: str) -> dict:
    """Create a Linear task via GraphQL API."""
    mutation = 'mutation createIssue($input: CreateIssueInput!) { createIssue(input: $input) { issue { id title description state { id } tags { id } priority { id } } }'
    variables = {
        "input": {
            "title": intent["title"],
            "description": intent["description"],
            "state": {"name": "todo"},
            "tags": ["calendar-linked", "automated"],
            "priority": {"name": "medium"},
        }
    }
    data = _linear_graphql(mutation, variables)
    issue = data.get("createIssue", {}).get("issue", {})
    task_id = issue.get("id", "")
    url = f"https://linear.app/sisyphusx/task/{task_id.split('/')[-1] if '/' in task_id else task_id}"
    return {"task_id": task_id, "url": url, "title": intent["title"], "description": intent["description"]}

def _resolve_state_id(state_name: str) -> str:
    """Map state name to Linear state ID."""
    return "UgXV"

def _resolve_priority_id(priority_name: str) -> str:
    """Map priority name to Linear priority ID."""
    return "UgXV"

def _update_calendar_description(event_id: str, linear_url: str) -> None:
    token_path = Path.home() / ".hermes" / "profiles" / "personal-assistant" / "google_token.json"
    import google.auth
    from google.oauth2 import credentials as gc
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = gc.Credentials.from_authorized_user_file(
        str(token_path),
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )
    if not creds.valid:
        creds.refresh(Request())
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    existing = svc.events().get(calendarId="primary", eventId=event_id).execute()
    existing["description"] = f"Linked Linear task: {linear_url}"
    svc.events().update(calendarId="primary", eventId=event_id, body=existing).execute()

def _update_linear_task(task_id: str, calendar_url: str) -> None:
    """Update Linear task description with calendar link."""
    query = 'mutation($id: ID!, $description: String!) { updateIssue(id: $id, description: $description) { issue { id description } }'
    variables = {"id": task_id, "description": f"Calendar event: {calendar_url}"}
    data = _linear_graphql(query, variables)

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Unified Task+Calendar creation flow (approval-gated).",
    )
    p.add_argument("--mode", choices=["plan", "apply"], required=True)
    p.add_argument("--profile", default="personal-assistant")
    p.add_argument("--title", default="Untitled event")
    p.add_argument("--date", help="Target date in YYYY-MM-DD format")
    p.add_argument("--time", help="Time in HH:MM format")
    p.add_argument("--meridiem", choices=["am", "pm"], default=None,
                   help="AM/PM for 12-hour format. If omitted, time is treated as 24-hour.")
    p.add_argument("--duration-minutes", type=int, default=30,
                   help="Event duration in minutes. Default: 30.")
    p.add_argument("--checksum", help="Checksum from plan step")
    p.add_argument("--calendar-intent", help="Calendar intent JSON from plan")
    p.add_argument("--linear-intent", help="Linear intent JSON from plan")
    args = p.parse_args(argv)

    if args.mode == "plan":
        if not args.date or not args.time:
            print("ERROR: --date and --time required for plan mode", file=sys.stderr)
            return 2
        plan = build_plan(
            title=args.title,
            date_str=args.date,
            time_str=args.time,
            meridiem=args.meridiem,
            duration_minutes=args.duration_minutes,
        )
        print(json.dumps(plan, default=str, sort_keys=True))
        return 0

    if args.mode == "apply":
        if not args.checksum or not args.calendar_intent or not args.linear_intent:
            print("ERROR: apply mode requires --checksum --calendar-intent --linear-intent", file=sys.stderr)
            return 2
        result = apply_plan(args.checksum, args.calendar_intent, args.linear_intent)
        print(json.dumps(result, default=str, sort_keys=True))
        return 0

if __name__ == "__main__":
    sys.exit(main())