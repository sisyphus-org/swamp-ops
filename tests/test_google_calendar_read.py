import importlib.util
import sys
import os
import json
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest import mock

import yaml

SCRIPT = Path(__file__).parents[1] / "scripts" / "google_calendar_read.py"
SPEC = importlib.util.spec_from_file_location("google_calendar_read", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import google_calendar_read script: {SCRIPT}")
gcr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gcr
SPEC.loader.exec_module(gcr)

KYIV = ZoneInfo("Europe/Kyiv")


class WindowTests(unittest.TestCase):
    def test_build_query_bounds_today_starts_at_midnight_kyiv(self):
        start, end = gcr.build_query_bounds("today")
        self.assertEqual(start.tzinfo, KYIV)
        # day_start hour == 0
        self.assertEqual(start.hour, 0)
        self.assertEqual(end.hour, 0)
        # 1-day window
        delta = end - start
        self.assertEqual(delta.days, 1)

    def test_build_query_bounds_next7_is_7_days(self):
        start, end = gcr.build_query_bounds("next-7-days")
        self.assertEqual((end - start).days, 7)

    def test_build_query_bounds_next30_is_30_days(self):
        start, end = gcr.build_query_bounds("next-30-days")
        self.assertEqual((end - start).days, 30)

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            gcr.build_query_bounds("next-14-days")

    def test_kyiv_dynamic_offset_winter_vs_summer(self):
        winter = datetime(2026, 1, 15, 12, 0, 0, tzinfo=KYIV)
        summer = datetime(2026, 7, 15, 12, 0, 0, tzinfo=KYIV)
        self.assertEqual(winter.utcoffset().total_seconds(), 2 * 3600)
        self.assertEqual(summer.utcoffset().total_seconds(), 3 * 3600)
        self.assertEqual(winter.tzname(), "EET")
        self.assertEqual(summer.tzname(), "EEST")


class NormalizeEventTests(unittest.TestCase):
    def test_trailing_z_timestamp_remains_timezone_aware(self):
        rendered = gcr._format_dt("2026-06-01T07:00:00Z")
        parsed = datetime.fromisoformat(rendered)
        self.assertIsNotNone(parsed.utcoffset())
        self.assertEqual(parsed.hour, 10)

    def test_normalize_timed_event(self):
        raw = {
            "id": "evt1",
            "start": {"dateTime": "2026-06-01T10:00:00+02:00"},
            "end": {"dateTime": "2026-06-01T11:00:00+02:00"},
            "status": "confirmed",
        }
        n = gcr.normalize_event(raw)
        self.assertEqual(n["id"], "")
        self.assertFalse(n["all_day"])
        self.assertFalse(n["recurring"])
        self.assertEqual(n["status"], "confirmed")

    def test_normalize_all_day_event(self):
        raw = {
            "id": "evt2",
            "start": {"date": "2026-06-01"},
            "end": {"date": "2026-06-02"},
            "status": "confirmed",
        }
        n = gcr.normalize_event(raw)
        self.assertTrue(n["all_day"])

    def test_normalize_recurring_event(self):
        raw = {
            "id": "evt3",
            "start": {"dateTime": "2026-06-01T09:00:00+03:00"},
            "end": {"dateTime": "2026-06-01T10:00:00+03:00"},
            "recurringEventId": "rec1",
            "status": "confirmed",
        }
        n = gcr.normalize_event(raw)
        self.assertTrue(n["recurring"])


class PaginationTests(unittest.TestCase):
    def test_list_calendars_paginates(self):
        """Fake service that paginates."""
        pages = [{"items": [{"id": "c1", "primary": True}, {"id": "c2", "primary": False}], "nextSyncToken": "x"}]
        calls = {"n": 0}

        class FakeList:
            def __init__(self, **kw):
                self.kw = kw

            def execute(self):
                calls["n"] += 1
                return pages[0] if calls["n"] == 1 else {}

            def list_next(self, prev, resp):
                return None if calls["n"] >= 2 else FakeList()

        class FakeService:
            def calendarList(self):
                return self

            def list(self, **kw):
                return FakeList(**kw)

            def list_next(self, req, resp):
                return None if calls["n"] >= 2 else FakeList()

        items = list(gcr.list_calendars_paginated(FakeService()))
        self.assertEqual(len(items), 2)

    def test_list_events_paginates(self):
        pages = [{"items": [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}], "nextPageToken": "t"}]
        calls = {"n": 0}

        class FakeList:
            def execute(self):
                calls["n"] += 1
                return pages[0] if calls["n"] == 1 else {"items": []}

        class FakeEvents:
            def list(self, **kw):
                return FakeList()

            def list_next(self, req, resp):
                return None if calls["n"] >= 2 else FakeList()

        class FakeService:
            def events(self):
                return self

            def list(self, **kw):
                return FakeList()

            def list_next(self, req, resp):
                return None if calls["n"] >= 2 else FakeList()

        start = datetime(2026, 6, 1, tzinfo=KYIV)
        end = datetime(2026, 6, 2, tzinfo=KYIV)
        # Monkeypatch paginate to use our fake
        original_events = gcr.list_events_paginated

        def fake_paginate(service, calendar_id, time_min, time_max):
            yield {"id": "e1"}
            yield {"id": "e2"}
            yield {"id": "e3"}

        gcr.list_events_paginated = fake_paginate
        try:
            events = list(gcr.list_events_paginated(FakeService(), "primary", start, end))
            self.assertEqual(len(events), 3)
        finally:
            gcr.list_events_paginated = original_events

    def test_list_events_rejects_non_allowlist_calendar(self):
        with self.assertRaises(PermissionError):
            list(gcr.list_events_paginated(object(), "some-other-id", datetime.now(KYIV), datetime.now(KYIV)))


class FreebusyTests(unittest.TestCase):
    def test_query_freebusy_passes_kyiv_timezone(self):
        captured = {}

        class FakeRequest:
            def __init__(self, body):
                self.body = body
            def execute(self):
                return {"calendars": {"primary": {"busy": [{"start": "x"}]}}}

        class FakeFreebusy:
            def query(self, body=None):
                captured["body"] = body
                return FakeRequest(body)

        class FakeService:
            def freebusy(self):
                return FakeFreebusy()

        start = datetime(2026, 6, 1, tzinfo=KYIV)
        end = datetime(2026, 6, 2, tzinfo=KYIV)
        result = gcr.query_freebusy(FakeService(), start, end)
        self.assertEqual(captured["body"]["timeZone"], "Europe/Kyiv")
        self.assertEqual(len(result["calendars"]["primary"]["busy"]), 1)

    def test_query_freebusy_rejects_non_allowlisted_calendar(self):
        with self.assertRaises(PermissionError):
            gcr.query_freebusy(
                object(),
                datetime(2026, 6, 1, tzinfo=KYIV),
                datetime(2026, 6, 2, tzinfo=KYIV),
                ["primary", "unauthorized"],
            )


class RunReadTests(unittest.TestCase):
    def test_smoke_returns_plan_skeleton(self):
        r = gcr.run_read("smoke", "next-7-days")
        self.assertEqual(r["operation"], "smoke")
        self.assertEqual(r["timezone"], "Europe/Kyiv")
        self.assertEqual(r["status"], "ok")
        self.assertIn("bounds", r)

    def test_events_mode_non_live_returns_plan(self):
        r = gcr.run_read("events", "today")
        self.assertEqual(r["status"], "planned")
        self.assertEqual(r["operation"], "events")
        self.assertEqual(r["calendars_considered"], ["primary"])

    def test_unknown_operation_rejected(self):
        # Unknown ops should raise before credentials are touched.
        with self.assertRaises(ValueError):
            gcr.run_read("nope", "today", live=False)

    def test_include_summary_preserves_summary_only_in_detailed_payload(self):
        class Calendars:
            def list(self, **_kwargs):
                return self
            def execute(self):
                return {"items": [{"id": "primary", "primary": True}]}
            def list_next(self, _req, _resp):
                return None

        class Events:
            def list(self, **_kwargs):
                return self
            def execute(self):
                return {"items": [{
                    "id": "evt",
                    "summary": "Private title",
                    "start": {"dateTime": "2026-06-01T07:00:00Z"},
                    "end": {"dateTime": "2026-06-01T08:00:00Z"},
                }]}
            def list_next(self, _req, _resp):
                return None

        class Service:
            def calendarList(self):
                return Calendars()
            def events(self):
                return Events()

        import io
        from contextlib import redirect_stdout

        stdout = io.StringIO()
        with mock.patch.object(gcr, "_write_atomic_payload") as writer:
            with redirect_stdout(stdout):
                result = gcr.run_read(
                    "events",
                    "today",
                    service=Service(),
                    include_summary=True,
                    live=True,
                )
        self.assertEqual(result["events"][0]["summary"], "Private title")
        self.assertEqual(result["events"][0]["id"], "evt")
        writer.assert_called_once()
        published = writer.call_args.args[0]
        self.assertEqual(published["events"][0]["summary"], "Private title")
        self.assertEqual(published["events"][0]["id"], "evt")
        self.assertNotIn("Private title", stdout.getvalue())
        self.assertNotIn("evt", stdout.getvalue())


class SanitizationTests(unittest.TestCase):
    def test_stdout_emits_no_sensitive_data(self):
        r = {
            "operation": "events",
            "status": "ok",
            "timezone": "Europe/Kyiv",
            "window": "next-30-days",
            "bounds": {"start": "x", "end": "y"},
            "calendar_count": 1,
            "writable_calendar_count": 1,
            "event_count": 2,
            "all_day_events": 0,
            "recurring_events": 0,
            "busy_intervals": 0,
            "events": [{"id": "secret evt id", "summary": "SECRET TITLE"}],
        }
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            gcr._emit_sanitized_stdout(r)
        out = buf.getvalue()
        self.assertNotIn("SECRET TITLE", out)
        self.assertNotIn("secret evt id", out)
        self.assertIn("operation", json.loads(out))

    def test_atomic_payload_written_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERMES_HOME"] = tmp
            try:
                r = gcr.run_read("smoke", "today")
                payload_path = gcr._output_path("personal-assistant")
                # smoke mode doesn't write payload; call _write_atomic_payload directly
                gcr._write_atomic_payload(r, "personal-assistant")
                self.assertTrue(payload_path.exists())
                mode = payload_path.stat().st_mode & 0o777
                self.assertEqual(mode, 0o600)
                data = json.loads(payload_path.read_text())
                self.assertEqual(data["operation"], "smoke")
                self.assertEqual(
                    list(payload_path.parent.glob(f".{payload_path.name}.*.tmp")),
                    [],
                )
            finally:
                del os.environ["HERMES_HOME"]


class ReadWriteAllowlistTests(unittest.TestCase):
    def test_read_allowlist_is_primary_only(self):
        self.assertEqual(gcr.READ_CALENDAR_IDS, ("primary",))

    def test_write_allowlist_is_empty(self):
        self.assertEqual(gcr.WRITE_CALENDAR_IDS, ())

    def test_read_only_slice_has_no_calendar_write_entrypoint(self):
        self.assertFalse((SCRIPT.parent / "google_calendar_write.py").exists())


class SwampContractTests(unittest.TestCase):
    def test_model_definition_uses_a_valid_rfc4122_uuid(self):
        model_path = SCRIPT.parents[1] / "models" / "command" / "shell" / "google-calendar-read.yaml"
        model = yaml.safe_load(model_path.read_text())
        parsed = uuid.UUID(model["id"])
        self.assertIn(parsed.version, range(1, 9))
        self.assertEqual(parsed.variant, uuid.RFC_4122)

    def test_workflow_declares_only_inputs_it_honors(self):
        workflow_path = SCRIPT.parents[1] / "workflows" / "workflow-google-calendar-read.yaml"
        workflow = yaml.safe_load(workflow_path.read_text())
        self.assertEqual(set(workflow["inputs"]), {"window"})
        command = workflow["jobs"][0]["steps"][0]["task"]["inputs"]["run"]
        self.assertIn("${{ inputs.window }}", command)


if __name__ == "__main__":
    unittest.main()
