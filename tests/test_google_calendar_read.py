import importlib.util
import sys
import os
import json
import tempfile
import traceback
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
    def test_list_events_paginates_using_only_profile_local_target(self):
        target_id = "configured-calendar@example.invalid"
        pages = [{"items": [{"id": "e1"}, {"id": "e2"}], "nextPageToken": "t"}]
        calls = {"n": 0}
        captured = []

        class FakeList:
            def execute(self):
                calls["n"] += 1
                return pages[0] if calls["n"] == 1 else {"items": []}

        class FakeService:
            def events(self):
                return self

            def list(self, **kw):
                captured.append(kw)
                return FakeList()

            def list_next(self, req, resp):
                return None if calls["n"] >= 2 else FakeList()

        start = datetime(2026, 6, 1, tzinfo=KYIV)
        end = datetime(2026, 6, 2, tzinfo=KYIV)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles" / "personal-assistant"
            root.mkdir(parents=True)
            (root / "google_calendar_target.json").write_text(
                json.dumps({"calendar_id": target_id})
            )
            os.chmod(root / "google_calendar_target.json", 0o600)
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp}):
                events = list(gcr.list_events_paginated(
                    FakeService(), start, end, profile="personal-assistant"
                ))

        self.assertEqual([event["id"] for event in events], ["e1", "e2"])
        self.assertEqual(captured[0]["calendarId"], target_id)

    def test_list_events_public_seam_rejects_caller_supplied_calendar_id(self):
        start = datetime(2026, 6, 1, tzinfo=KYIV)
        end = datetime(2026, 6, 2, tzinfo=KYIV)
        with self.assertRaises(TypeError):
            list(gcr.list_events_paginated(object(), "attacker@example.invalid", start, end))

class FreebusyTests(unittest.TestCase):
    def test_query_freebusy_passes_kyiv_timezone_for_profile_local_target(self):
        target_id = "configured-calendar@example.invalid"
        captured = {}

        class FakeRequest:
            def __init__(self, body):
                self.body = body
            def execute(self):
                return {"calendars": {target_id: {"busy": [{"start": "x"}]}}}

        class FakeFreebusy:
            def query(self, body=None):
                captured["body"] = body
                return FakeRequest(body)

        class FakeService:
            def freebusy(self):
                return FakeFreebusy()

        start = datetime(2026, 6, 1, tzinfo=KYIV)
        end = datetime(2026, 6, 2, tzinfo=KYIV)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles" / "personal-assistant"
            root.mkdir(parents=True)
            (root / "google_calendar_target.json").write_text(
                json.dumps({"calendar_id": target_id})
            )
            os.chmod(root / "google_calendar_target.json", 0o600)
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp}):
                result = gcr.query_freebusy(FakeService(), start, end)
        self.assertEqual(captured["body"]["timeZone"], "Europe/Kyiv")
        self.assertEqual(captured["body"]["items"], [{"id": target_id}])
        self.assertEqual(len(result["calendars"][target_id]["busy"]), 1)

    def test_query_freebusy_public_seam_rejects_caller_supplied_calendar_ids(self):
        start = datetime(2026, 6, 1, tzinfo=KYIV)
        end = datetime(2026, 6, 2, tzinfo=KYIV)
        with self.assertRaises(TypeError):
            gcr.query_freebusy(object(), start, end, ["attacker@example.invalid"])

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
        self.assertEqual(r["calendar_count"], 1)
        self.assertNotIn("calendars_considered", r)

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

    def test_live_reads_use_only_protected_target_without_calendar_list_or_id_exposure(self):
        target_id = "configured-calendar@example.invalid"
        calls = []

        class Request:
            def __init__(self, payload):
                self.payload = payload
            def execute(self):
                return self.payload

        class Events:
            def list(self, **kwargs):
                calls.append(("events_list", kwargs))
                return Request({"items": []})
            def list_next(self, _request, _response):
                return None

        class CalendarList:
            def get(self, **kwargs):
                calls.append(("calendar_get", kwargs))
                return Request({"accessRole": "writer"})

        class Freebusy:
            def query(self, **kwargs):
                calls.append(("freebusy", kwargs))
                return Request({"calendars": {target_id: {"busy": []}}})

        class Service:
            def calendarList(self):
                return CalendarList()
            def events(self):
                return Events()
            def freebusy(self):
                return Freebusy()

        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles" / "personal-assistant"
            root.mkdir(parents=True)
            (root / "google_service_account.json").write_text("{}")
            os.chmod(root / "google_service_account.json", 0o600)
            (root / "google_calendar_target.json").write_text(
                json.dumps({"calendar_id": target_id})
            )
            os.chmod(root / "google_calendar_target.json", 0o600)
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp}), mock.patch.object(
                gcr, "_write_atomic_payload"
            ):
                for operation in ("inventory", "events", "freebusy"):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        result = gcr.run_read(
                            operation, "today", service=Service(), live=True
                        )
                    self.assertNotIn(target_id, stdout.getvalue())
                    self.assertNotIn(target_id, json.dumps(result))
                    if operation in {"events", "freebusy"}:
                        self.assertNotIn("writable_calendar_count", result)

        self.assertEqual(calls[0][0], "calendar_get")
        self.assertEqual(calls[0][1]["calendarId"], target_id)
        self.assertEqual(calls[1][0], "events_list")
        self.assertEqual(calls[1][1]["calendarId"], target_id)
        self.assertEqual(
            calls[2][1]["body"]["items"], [{"id": target_id}]
        )

    def test_run_read_uses_one_immutable_target_for_freebusy(self):
        queried = []

        class Request:
            def __init__(self, body):
                self.body = body
            def execute(self):
                calendar_id = self.body["items"][0]["id"]
                return {"calendars": {calendar_id: {"busy": [{"start": "x"}]}}}

        class Freebusy:
            def query(self, body):
                queried.append(body["items"][0]["id"])
                return Request(body)

        class Service:
            def freebusy(self):
                return Freebusy()

        with mock.patch.object(
            gcr, "load_calendar_target", side_effect=["calendar-a", "calendar-b"]
        ) as loader, mock.patch.object(gcr, "_write_atomic_payload"), mock.patch.object(
            gcr, "_emit_sanitized_stdout"
        ):
            result = gcr.run_read("freebusy", "today", service=Service(), live=True)

        self.assertEqual(loader.call_count, 1)
        self.assertEqual(queried, ["calendar-a"])
        self.assertEqual(result["busy_intervals"], 1)
        self.assertNotIn("writable_calendar_count", result)


class InventoryAccessTests(unittest.TestCase):
    class HttpFailure(Exception):
        def __init__(self, status, message="provider detail"):
            super().__init__(message)
            self.resp = type("Response", (), {"status": status})()

    def run_inventory(self, *, access_role=None, calendar_error=None):
        target_id = "configured-calendar@example.invalid"
        calls = []

        class Request:
            def __init__(self, payload):
                self.payload = payload
            def execute(self):
                if isinstance(self.payload, BaseException):
                    raise self.payload
                return self.payload

        class CalendarList:
            def get(self, **kwargs):
                calls.append(("calendar_get", kwargs))
                payload = calendar_error or {"accessRole": access_role}
                return Request(payload)
            def list(self, **_kwargs):
                raise AssertionError("CalendarList enumeration is forbidden")

        class Events:
            def list(self, **kwargs):
                calls.append(("events_list", kwargs))
                return Request({"items": []})

        class Service:
            def calendarList(self):
                return CalendarList()
            def events(self):
                return Events()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles" / "personal-assistant"
            root.mkdir(parents=True)
            (root / "google_calendar_target.json").write_text(
                json.dumps({"calendar_id": target_id})
            )
            os.chmod(root / "google_calendar_target.json", 0o600)
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp}), mock.patch.object(
                gcr, "_write_atomic_payload"
            ), mock.patch.object(gcr, "_emit_sanitized_stdout"):
                result = gcr.run_read(
                    "inventory", "today", service=Service(), live=True
                )
        return target_id, calls, result

    def test_reader_calendar_list_entry_reports_zero_writable_targets(self):
        target_id, calls, result = self.run_inventory(access_role="reader")
        self.assertEqual(result["writable_calendar_count"], 0)
        self.assertEqual(calls, [("calendar_get", {"calendarId": target_id})])

    def test_writer_calendar_list_entry_reports_one_writable_target(self):
        target_id, calls, result = self.run_inventory(access_role="writer")
        self.assertEqual(result["writable_calendar_count"], 1)
        self.assertEqual(calls, [("calendar_get", {"calendarId": target_id})])

    def test_missing_calendar_list_entry_falls_back_to_bounded_read_probe(self):
        target_id, calls, result = self.run_inventory(
            calendar_error=self.HttpFailure(404)
        )
        self.assertNotIn("writable_calendar_count", result)
        self.assertEqual(calls[0], ("calendar_get", {"calendarId": target_id}))
        self.assertEqual(calls[1][0], "events_list")
        self.assertEqual(calls[1][1]["calendarId"], target_id)
        self.assertEqual(calls[1][1]["maxResults"], 1)


class ProviderErrorSanitizationTests(unittest.TestCase):
    class HttpFailure(Exception):
        def __init__(self, status, message):
            super().__init__(message)
            self.resp = type("Response", (), {"status": status})()

    def test_target_specific_provider_errors_never_render_target_id(self):
        target_id = "configured-calendar@example.invalid"

        class Request:
            def __init__(self, status):
                self.status = status
            def execute(self):
                raise ProviderErrorSanitizationTests.HttpFailure(
                    self.status,
                    f"GET /calendar/v3/calendars/{target_id}/events provider-secret",
                )

        class CalendarList:
            def __init__(self, status):
                self.status = status
            def get(self, **_kwargs):
                return Request(self.status)

        class Events:
            def list(self, **_kwargs):
                return Request(500)
            def list_next(self, _request, _response):
                return None

        class Freebusy:
            def query(self, **_kwargs):
                return Request(500)

        class Service:
            def __init__(self, inventory_status=500):
                self.inventory_status = inventory_status
            def calendarList(self):
                return CalendarList(self.inventory_status)
            def events(self):
                return Events()
            def freebusy(self):
                return Freebusy()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / "profiles" / "personal-assistant"
            root.mkdir(parents=True)
            (root / "google_calendar_target.json").write_text(
                json.dumps({"calendar_id": target_id})
            )
            os.chmod(root / "google_calendar_target.json", 0o600)
            start = datetime(2026, 6, 1, tzinfo=KYIV)
            end = datetime(2026, 6, 2, tzinfo=KYIV)
            calls = (
                lambda: gcr.run_read(
                    "inventory", "today", service=Service(500), live=True
                ),
                lambda: gcr.run_read(
                    "inventory", "today", service=Service(404), live=True
                ),
                lambda: list(gcr.list_events_paginated(
                    Service(), start, end
                )),
                lambda: gcr.query_freebusy(
                    Service(), start, end
                ),
            )
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp}):
                for call in calls:
                    with self.subTest(call=call):
                        try:
                            call()
                        except RuntimeError as exc:
                            rendered = "".join(traceback.format_exception(exc))
                            self.assertNotIn(target_id, str(exc))
                            self.assertNotIn(target_id, rendered)
                            self.assertNotIn("provider-secret", rendered)
                            self.assertIsNone(exc.__cause__)
                        else:
                            self.fail("target-specific provider failure was not raised")


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
    def test_read_target_is_not_exposed_as_a_caller_allowlist(self):
        self.assertFalse(hasattr(gcr, "READ_CALENDAR_IDS"))

    def test_read_slice_has_no_calendar_write_entrypoint(self):
        self.assertFalse(hasattr(gcr, "run_write"))
        self.assertFalse(hasattr(gcr, "apply_plan"))


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
        self.assertEqual(set(workflow["inputs"]), {"operation", "window"})
        self.assertEqual(workflow["inputs"]["operation"]["enum"], ["inventory", "events", "freebusy"])
        command = workflow["jobs"][0]["steps"][0]["task"]["inputs"]["run"]
        self.assertIn("${{ inputs.operation }}", command)
        self.assertIn("${{ inputs.window }}", command)
        self.assertIn("--live", command)


if __name__ == "__main__":
    unittest.main()
