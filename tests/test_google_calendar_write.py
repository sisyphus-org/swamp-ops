import importlib.util
import json
import re
import sys
import unittest
import uuid
from pathlib import Path

import yaml

SCRIPT = Path(__file__).parents[1] / "scripts" / "google_calendar_write.py"
SPEC = importlib.util.spec_from_file_location("google_calendar_write", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import google_calendar_write script: {SCRIPT}")
gcw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gcw
SPEC.loader.exec_module(gcw)

LINEAR_URL = "https://linear.app/sisyphusx/issue/SIS-84/razdelit-project-manager-linear-i-personal-assistant-google-calendar"
PLAN_RUN_ID = "11111111-1111-4111-8111-111111111111"
APPROVAL_RUN_ID = "22222222-2222-4222-8222-222222222222"
PLAN_INPUTS = {
    "operation": "create",
    "blockKey": "primary",
    "summary": "Связанная задача",
    "start": "2026-09-07T10:00",
    "end": "2026-09-07T10:30",
    "linearUrl": LINEAR_URL,
    "details": "",
}


class PlanTests(unittest.TestCase):
    def test_plan_places_canonical_linear_link_in_event_description(self):
        plan = gcw.build_plan(
            summary="Подготовить календарную интеграцию",
            start="2026-09-07T10:00",
            end="2026-09-07T10:30",
            linear_url=LINEAR_URL,
            details="Проверить критерии готовности.",
        )

        event = plan["event"]
        self.assertEqual(
            event["description"],
            f"Проверить критерии готовности.\n\nLinear: {LINEAR_URL}",
        )
        self.assertEqual(event["start"]["timeZone"], "Europe/Kyiv")
        self.assertEqual(event["end"]["timeZone"], "Europe/Kyiv")
        self.assertTrue(gcw.verify_plan_checksum(plan))

    def test_standalone_plan_uses_block_identity_without_linear_link(self):
        first = gcw.build_plan(
            operation="create",
            block_key="lavina-rusanovka-2026-09-06",
            summary="Поехать посмотреть вещи: Lavina + Русановка",
            start="2026-09-06T10:00",
            end="2026-09-06T12:00",
            linear_url="",
            details="Посмотреть вещи в двух местах.",
        )
        updated = gcw.build_plan(
            operation="update",
            block_key="lavina-rusanovka-2026-09-06",
            summary="Поехать посмотреть вещи",
            start="2026-09-06T10:30",
            end="2026-09-06T12:30",
            linear_url="",
            details="",
        )

        self.assertIsNone(first["linearIssue"])
        self.assertEqual(first["event"]["description"], "Посмотреть вещи в двух местах.")
        self.assertRegex(first["eventId"], r"^evt[a-f0-9]{64}$")
        self.assertEqual(first["eventId"], updated["eventId"])
        self.assertTrue(gcw.verify_plan_checksum(first))

    def test_stable_event_id_depends_only_on_linear_identifier_and_block_key(self):
        first = gcw.build_plan(
            operation="create",
            block_key="primary",
            summary="Первый заголовок",
            start="2026-09-07T10:00",
            end="2026-09-07T10:30",
            linear_url=LINEAR_URL,
        )
        changed_fields = gcw.build_plan(
            operation="update",
            block_key="primary",
            summary="Другой заголовок",
            start="2026-09-08T11:00",
            end="2026-09-08T12:00",
            linear_url=LINEAR_URL,
            details="Другие детали",
        )
        second_block = gcw.build_plan(
            operation="create",
            block_key="deep-work",
            summary="Второй блок",
            start="2026-09-07T12:00",
            end="2026-09-07T12:30",
            linear_url=LINEAR_URL,
        )

        self.assertEqual(
            first["eventId"],
            "sis1289b0c4fa190ec42568624c9b5732b3d184958383454ea634967d5e411cd6a3",
        )
        self.assertEqual(first["eventId"], changed_fields["eventId"])
        self.assertNotEqual(first["eventId"], second_block["eventId"])
        self.assertNotEqual(first["checksum"], changed_fields["checksum"])
        self.assertEqual(first["operation"], "create")
        self.assertEqual(first["blockKey"], "primary")

    def test_operation_enum_and_safe_block_key_are_enforced(self):
        for kwargs in (
            {"operation": "move", "block_key": "primary"},
            {"operation": "create", "block_key": "Unsafe Key"},
            {"operation": "create", "block_key": "../other"},
            {"operation": "create", "block_key": "x" * 65},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(gcw.CalendarWriteError):
                gcw.build_plan(
                    summary="Задача",
                    start="2026-09-07T10:00",
                    end="2026-09-07T10:30",
                    linear_url=LINEAR_URL,
                    **kwargs,
                )

    def test_delete_requires_only_link_and_block_key_and_rejects_event_fields(self):
        plan = gcw.build_plan(
            operation="delete",
            block_key="primary",
            summary="",
            start="",
            end="",
            linear_url=LINEAR_URL,
            details="",
        )
        self.assertEqual(plan["event"], {})
        self.assertTrue(gcw.verify_plan_checksum(plan))

        for field, value in (
            ("summary", "must be empty"),
            ("start", "2026-09-07T10:00"),
            ("end", "2026-09-07T10:30"),
            ("details", "must be empty"),
        ):
            kwargs = {
                "operation": "delete",
                "block_key": "primary",
                "summary": "",
                "start": "",
                "end": "",
                "linear_url": LINEAR_URL,
                "details": "",
            }
            kwargs[field] = value
            with self.subTest(field=field), self.assertRaises(gcw.CalendarWriteError):
                gcw.build_plan(**kwargs)

    def test_plan_without_details_still_contains_linear_link(self):
        plan = gcw.build_plan(
            summary="Связанная задача",
            start="2026-09-07T10:00",
            end="2026-09-07T10:30",
            linear_url=LINEAR_URL,
        )
        self.assertEqual(plan["event"]["description"], f"Linear: {LINEAR_URL}")

    def test_noncanonical_or_shell_unsafe_linear_links_are_rejected(self):
        invalid = (
            "http://linear.app/sisyphusx/issue/SIS-84/title",
            "https://evil.example/issue/SIS-84/title",
            "https://linear.app/sisyphusx/issue/ABC-84/title",
            "https://linear.app/sisyphusx/issue/SIS-84/title?x=1",
            "https://linear.app/sisyphusx/issue/SIS-84/title#fragment",
            "https://linear.app/sisyphusx/issue/SIS-84",
            "https://linear.app/sisyphusx/issue/SIS-84/",
            "https://linear.app/sisyphus'x/issue/SIS-84/title;touch-pwned",
            "https://linear.app/sisyphus%27x/issue/SIS-84/title",
            "https://linear.app/sisyphusx/issue/SIS-84/title%2Fextra",
            "https://linear.app/sisyphusx/issue/SIS-84/%74itle",
        )
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(gcw.CalendarWriteError):
                gcw.build_plan(
                    summary="Задача",
                    start="2026-09-07T10:00",
                    end="2026-09-07T10:30",
                    linear_url=url,
                )

    def test_end_must_be_after_start(self):
        with self.assertRaises(gcw.CalendarWriteError):
            gcw.build_plan(
                summary="Задача",
                start="2026-09-07T10:30",
                end="2026-09-07T10:00",
                linear_url=LINEAR_URL,
            )

    def test_plan_resolves_kyiv_dst_from_local_wall_time(self):
        winter = gcw.build_plan(
            summary="Зима",
            start="2026-01-15T10:00",
            end="2026-01-15T10:30",
            linear_url=LINEAR_URL,
        )
        summer = gcw.build_plan(
            summary="Лето",
            start="2026-07-15T10:00",
            end="2026-07-15T10:30",
            linear_url=LINEAR_URL,
        )
        self.assertTrue(winter["event"]["start"]["dateTime"].endswith("+02:00"))
        self.assertTrue(summer["event"]["start"]["dateTime"].endswith("+03:00"))


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}

    def execute(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class HttpFailure(Exception):
    def __init__(self, status, message="sensitive provider detail"):
        super().__init__(message)
        self.resp = type("Response", (), {"status": status})()


class FakeEvents:
    def __init__(self):
        self.insert_calls = []
        self.update_calls = []
        self.delete_calls = []
        self.get_calls = []
        self.created = None
        self.update_request = None
        self.delete_request = None

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        self.created = dict(kwargs["body"])
        return FakeRequest({"id": self.created["id"]})

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        self.created = dict(kwargs["body"])
        self.update_request = FakeRequest({"id": kwargs["eventId"]})
        return self.update_request

    def delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        self.created = None
        self.delete_request = FakeRequest({})
        return self.delete_request

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return FakeRequest(dict(self.created) if self.created else HttpFailure(404))


class ScriptedEvents(FakeEvents):
    def __init__(self, *, get_script, insert_error=None, persist_on_error=False):
        super().__init__()
        self.get_script = list(get_script)
        self.insert_error = insert_error
        self.persist_on_error = persist_on_error

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        value = self.get_script.pop(0) if self.get_script else dict(self.created or {})
        if value == "created":
            value = dict(self.created or {})
        return FakeRequest(value)

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        if self.insert_error is not None:
            if self.persist_on_error:
                self.created = dict(kwargs["body"])
            return FakeRequest(self.insert_error)
        self.created = dict(kwargs["body"])
        return FakeRequest({"id": self.created["id"]})


class MutationResponseEvents(FakeEvents):
    def __init__(self, *, update_response=None, delete_response=None):
        super().__init__()
        self.update_response = update_response
        self.delete_response = delete_response

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        self.created = dict(kwargs["body"])
        return FakeRequest(
            {"id": kwargs["eventId"]} if self.update_response is None else self.update_response
        )

    def delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        self.created = None
        return FakeRequest({} if self.delete_response is None else self.delete_response)


class MalformedDeleteReadBackEvents(MutationResponseEvents):
    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return FakeRequest(dict(self.created) if self.created else {})


class FakeService:
    def __init__(self, events_api=None):
        self.events_api = events_api or FakeEvents()

    def events(self):
        return self.events_api


class SnapshotTests(unittest.TestCase):
    def test_target_snapshot_is_hash_only_and_changes_with_live_state(self):
        events = FakeEvents()
        service = FakeService(events)
        absent = gcw.snapshot_target(
            linear_url=LINEAR_URL, block_key="primary", service=service
        )
        self.assertEqual(set(absent), {
            "operation", "status", "linearIssue", "blockKey", "beforeStateHash"
        })
        self.assertEqual(absent["operation"], "snapshot")
        self.assertNotIn("summary", json.dumps(absent))

        events.created = {
            "id": gcw.stable_event_id("SIS-84", "primary"),
            "status": "confirmed",
            "summary": "Private title",
            "description": f"Linear: {LINEAR_URL}",
            "start": {"dateTime": "2026-09-07T10:00:00+03:00", "timeZone": "Europe/Kyiv"},
            "end": {"dateTime": "2026-09-07T10:30:00+03:00", "timeZone": "Europe/Kyiv"},
        }
        present = gcw.snapshot_target(
            linear_url=LINEAR_URL, block_key="primary", service=service
        )
        self.assertNotEqual(absent["beforeStateHash"], present["beforeStateHash"])
        self.assertNotIn("Private title", json.dumps(present))

        events.created["summary"] = "Intervening edit"
        changed = gcw.snapshot_target(
            linear_url=LINEAR_URL, block_key="primary", service=service
        )
        self.assertNotEqual(present["beforeStateHash"], changed["beforeStateHash"])

    def test_target_snapshot_detects_out_of_allowlist_event_field_changes(self):
        events = FakeEvents()
        events.created = {
            "id": gcw.stable_event_id("SIS-84", "primary"),
            "status": "confirmed",
            "summary": "Private title",
            "description": f"Linear: {LINEAR_URL}",
            "start": {"dateTime": "2026-09-07T10:00:00+03:00", "timeZone": "Europe/Kyiv"},
            "end": {"dateTime": "2026-09-07T10:30:00+03:00", "timeZone": "Europe/Kyiv"},
        }
        service = FakeService(events)
        before = gcw.snapshot_target(
            linear_url=LINEAR_URL, block_key="primary", service=service
        )
        events.created["attendees"] = [{"email": "private@example.com"}]
        after = gcw.snapshot_target(
            linear_url=LINEAR_URL, block_key="primary", service=service
        )
        self.assertNotEqual(before["beforeStateHash"], after["beforeStateHash"])
        self.assertNotIn("private@example.com", json.dumps(after))


def approval_fixture(
    plan,
    *,
    history_status="succeeded",
    approval_step_status="succeeded",
    plan_history_overrides=None,
    plan_owner_workflow="google-calendar-write-plan",
    plan_inputs=None,
):
    attestation = {
        "schemaVersion": 1,
        "mode": "attestation",
        "decision": "owner_approved",
        "workflow": "google-calendar-write-approval",
        "model": "google-calendar-write-approval",
        "plan": {
            "workflow": "google-calendar-write-plan",
            "model": "google-calendar-write",
            "runId": PLAN_RUN_ID,
            "artifactVersion": 1,
            "checksum": plan["checksum"],
        },
    }
    attestation["checksum"] = gcw._plan_checksum(attestation)

    def runner(argv, **kwargs):
        if argv[:4] == ["swamp", "workflow", "history", "get"]:
            run_id = argv[4]
            if run_id == PLAN_RUN_ID:
                payload = {
                    "id": PLAN_RUN_ID,
                    "workflowName": "google-calendar-write-plan",
                    "status": "succeeded",
                    "inputs": dict(plan_inputs or PLAN_INPUTS),
                }
                payload.update(plan_history_overrides or {})
            else:
                payload = {
                    "id": APPROVAL_RUN_ID,
                    "workflowName": "google-calendar-write-approval",
                    "status": history_status,
                    "inputs": {
                        "planRunId": PLAN_RUN_ID,
                        "planArtifactVersion": 1,
                        "planChecksum": plan["checksum"],
                    },
                    "jobs": [{"name": "attest", "steps": [{
                        "name": "approve-calendar-write",
                        "status": approval_step_status,
                    }]}],
                }
        elif "google-calendar-write-approval" in argv:
            payload = {
                "modelName": "google-calendar-write-approval",
                "name": "result",
                "version": 1,
                "ownerDefinition": {
                    "workflowRunId": APPROVAL_RUN_ID,
                    "workflowName": "google-calendar-write-approval",
                },
                "content": {"exitCode": 0, "stdout": json.dumps(attestation)},
            }
        else:
            payload = {
                "modelName": "google-calendar-write",
                "name": "result",
                "version": 1,
                "ownerDefinition": {
                    "workflowRunId": PLAN_RUN_ID,
                    "workflowName": plan_owner_workflow,
                },
                "content": {"exitCode": 0, "stdout": json.dumps(plan)},
            }
        return {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}

    return runner, attestation["checksum"]


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.plan = gcw.build_plan(
            summary="Связанная задача",
            start="2026-09-07T10:00",
            end="2026-09-07T10:30",
            linear_url=LINEAR_URL,
        )

    def test_verified_approval_loads_exact_plan_and_checks_workflow_provenance(self):
        runner, approval_checksum = approval_fixture(self.plan)
        plan, authorization = gcw.verify_calendar_approval(
            plan_run_id=PLAN_RUN_ID,
            plan_artifact_version=1,
            plan_checksum=self.plan["checksum"],
            approval_run_id=APPROVAL_RUN_ID,
            approval_artifact_version=1,
            approval_checksum=approval_checksum,
            runner=runner,
        )
        self.assertEqual(plan, self.plan)
        self.assertIsInstance(authorization, gcw.VerifiedApproval)

    def test_attestation_is_bound_to_exact_loaded_plan_artifact(self):
        runner, _ = approval_fixture(self.plan)
        attestation = gcw.build_approval_attestation(
            plan_run_id=PLAN_RUN_ID,
            plan_artifact_version=1,
            plan_checksum=self.plan["checksum"],
            runner=runner,
        )
        self.assertEqual(attestation["decision"], "owner_approved")
        self.assertEqual(attestation["plan"]["checksum"], self.plan["checksum"])
        self.assertTrue(gcw.verify_plan_checksum(attestation))

    def test_plan_artifact_requires_succeeded_plan_workflow_with_exact_inputs(self):
        cases = (
            {"workflowName": "some-other-workflow"},
            {"status": "failed"},
            {"inputs": {**PLAN_INPUTS, "summary": "Другой текст"}},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                runner, _ = approval_fixture(
                    self.plan, plan_history_overrides=overrides
                )
                with self.assertRaisesRegex(gcw.CalendarWriteError, "plan workflow"):
                    gcw.build_approval_attestation(
                        plan_run_id=PLAN_RUN_ID,
                        plan_artifact_version=1,
                        plan_checksum=self.plan["checksum"],
                        runner=runner,
                    )

    def test_plan_artifact_owner_workflow_must_match_when_present(self):
        runner, _ = approval_fixture(
            self.plan, plan_owner_workflow="some-other-workflow"
        )
        with self.assertRaisesRegex(gcw.CalendarWriteError, "provenance"):
            gcw.build_approval_attestation(
                plan_run_id=PLAN_RUN_ID,
                plan_artifact_version=1,
                plan_checksum=self.plan["checksum"],
                runner=runner,
            )

    def test_unsucceeded_approval_workflow_is_rejected(self):
        runner, approval_checksum = approval_fixture(self.plan, history_status="failed")
        with self.assertRaisesRegex(gcw.CalendarWriteError, "explicitly approved"):
            gcw.verify_calendar_approval(
                plan_run_id=PLAN_RUN_ID,
                plan_artifact_version=1,
                plan_checksum=self.plan["checksum"],
                approval_run_id=APPROVAL_RUN_ID,
                approval_artifact_version=1,
                approval_checksum=approval_checksum,
                runner=runner,
            )

    def test_unsucceeded_manual_approval_step_is_rejected(self):
        runner, approval_checksum = approval_fixture(
            self.plan, approval_step_status="failed"
        )
        with self.assertRaisesRegex(gcw.CalendarWriteError, "explicitly approved"):
            gcw.verify_calendar_approval(
                plan_run_id=PLAN_RUN_ID,
                plan_artifact_version=1,
                plan_checksum=self.plan["checksum"],
                approval_run_id=APPROVAL_RUN_ID,
                approval_artifact_version=1,
                approval_checksum=approval_checksum,
                runner=runner,
            )

    def test_mismatched_attestation_checksum_is_rejected(self):
        runner, _ = approval_fixture(self.plan)
        with self.assertRaisesRegex(gcw.CalendarWriteError, "attestation binding"):
            gcw.verify_calendar_approval(
                plan_run_id=PLAN_RUN_ID,
                plan_artifact_version=1,
                plan_checksum=self.plan["checksum"],
                approval_run_id=APPROVAL_RUN_ID,
                approval_artifact_version=1,
                approval_checksum="0" * 64,
                runner=runner,
            )


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.plan = gcw.build_plan(
            summary="Связанная задача",
            start="2026-09-07T10:00",
            end="2026-09-07T10:30",
            linear_url=LINEAR_URL,
        )
        runner, approval_checksum = approval_fixture(self.plan)
        _, self.authorization = gcw.verify_calendar_approval(
            plan_run_id=PLAN_RUN_ID,
            plan_artifact_version=1,
            plan_checksum=self.plan["checksum"],
            approval_run_id=APPROVAL_RUN_ID,
            approval_artifact_version=1,
            approval_checksum=approval_checksum,
            runner=runner,
        )

    def authorization_for(self, plan, *, plan_inputs=None):
        runner, approval_checksum = approval_fixture(plan, plan_inputs=plan_inputs)
        _, authorization = gcw.verify_calendar_approval(
            plan_run_id=PLAN_RUN_ID,
            plan_artifact_version=1,
            plan_checksum=plan["checksum"],
            approval_run_id=APPROVAL_RUN_ID,
            approval_artifact_version=1,
            approval_checksum=approval_checksum,
            runner=runner,
        )
        return authorization

    def test_apply_creates_primary_event_then_verifies_exact_description(self):
        service = FakeService()
        result = gcw.apply_plan(
            self.plan,
            approved_checksum=self.plan["checksum"],
            service=service,
            authorization=self.authorization,
        )

        self.assertEqual(
            result,
            {
                "operation": "create",
                "status": "verified",
                "reused": False,
                "linearIssue": "SIS-84",
                "blockKey": "primary",
            },
        )
        self.assertEqual(len(service.events_api.insert_calls), 1)
        call = service.events_api.insert_calls[0]
        self.assertEqual(call["calendarId"], "primary")
        self.assertEqual(call["sendUpdates"], "none")
        self.assertEqual(call["body"]["description"], f"Linear: {LINEAR_URL}")
        self.assertEqual(service.events_api.get_calls[0]["eventId"], call["body"]["id"])

    def test_apply_creates_standalone_event_without_linear_link(self):
        plan = gcw.build_plan(
            operation="create",
            block_key="lavina-rusanovka-2026-09-06",
            summary="Поехать посмотреть вещи: Lavina + Русановка",
            start="2026-09-06T10:00",
            end="2026-09-06T12:00",
            linear_url="",
            details="Посмотреть вещи в двух местах.",
        )
        authorization = self.authorization_for(
            plan,
            plan_inputs={
                "operation": "create",
                "blockKey": "lavina-rusanovka-2026-09-06",
                "summary": "Поехать посмотреть вещи: Lavina + Русановка",
                "start": "2026-09-06T10:00",
                "end": "2026-09-06T12:00",
                "linearUrl": "",
                "details": "Посмотреть вещи в двух местах.",
            },
        )
        service = FakeService()

        result = gcw.apply_plan(
            plan,
            approved_checksum=plan["checksum"],
            service=service,
            authorization=authorization,
        )

        self.assertEqual(result, {
            "operation": "create",
            "status": "verified",
            "reused": False,
            "linearIssue": None,
            "blockKey": "lavina-rusanovka-2026-09-06",
        })
        self.assertEqual(
            service.events_api.insert_calls[0]["body"]["description"],
            "Посмотреть вещи в двух местах.",
        )

    def test_standalone_create_accepts_provider_omission_of_empty_description(self):
        plan = gcw.build_plan(
            operation="create",
            block_key="doctor-2026-09-07-0900",
            summary="Врач",
            start="2026-09-07T09:00",
            end="2026-09-07T09:30",
            linear_url="",
            details="",
        )
        inputs = {
            "operation": "create",
            "blockKey": "doctor-2026-09-07-0900",
            "summary": "Врач",
            "start": "2026-09-07T09:00",
            "end": "2026-09-07T09:30",
            "linearUrl": "",
            "details": "",
        }
        authorization = self.authorization_for(plan, plan_inputs=inputs)
        provider_event = {
            key: value for key, value in {**plan["event"], "id": plan["eventId"]}.items()
            if key != "description"
        }
        events = ScriptedEvents(get_script=[HttpFailure(404), provider_event])

        result = gcw.apply_plan(
            plan,
            approved_checksum=plan["checksum"],
            service=FakeService(events),
            authorization=authorization,
        )

        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["reused"])

    def test_standalone_block_supports_update_then_delete_with_replay(self):
        block_key = "doctor-2026-09-07-0900"
        service = FakeService()
        create_plan = gcw.build_plan(
            operation="create", block_key=block_key, summary="Врач",
            start="2026-09-07T09:00", end="2026-09-07T09:30",
            linear_url="", details="",
        )
        create_authorization = self.authorization_for(
            create_plan,
            plan_inputs={
                "operation": "create", "blockKey": block_key, "summary": "Врач",
                "start": "2026-09-07T09:00", "end": "2026-09-07T09:30",
                "linearUrl": "", "details": "",
            },
        )
        gcw.apply_plan(
            create_plan, approved_checksum=create_plan["checksum"], service=service,
            authorization=create_authorization,
        )

        update_plan = gcw.build_plan(
            operation="update", block_key=block_key, summary="Врач — перенос",
            start="2026-09-07T09:30", end="2026-09-07T10:00",
            linear_url="", details="Новый кабинет",
        )
        update_authorization = self.authorization_for(
            update_plan,
            plan_inputs={
                "operation": "update", "blockKey": block_key,
                "summary": "Врач — перенос", "start": "2026-09-07T09:30",
                "end": "2026-09-07T10:00", "linearUrl": "",
                "details": "Новый кабинет",
            },
        )
        updated = gcw.apply_plan(
            update_plan, approved_checksum=update_plan["checksum"], service=service,
            authorization=update_authorization,
        )
        update_replay = gcw.apply_plan(
            update_plan, approved_checksum=update_plan["checksum"], service=service,
            authorization=update_authorization,
        )

        self.assertEqual(create_plan["eventId"], update_plan["eventId"])
        self.assertIsNone(updated["linearIssue"])
        self.assertFalse(updated["reused"])
        self.assertTrue(update_replay["reused"])
        self.assertEqual(len(service.events_api.update_calls), 1)

        delete_plan = gcw.build_plan(
            operation="delete", block_key=block_key, summary="", start="", end="",
            linear_url="", details="",
        )
        delete_authorization = self.authorization_for(
            delete_plan,
            plan_inputs={
                "operation": "delete", "blockKey": block_key, "summary": "",
                "start": "", "end": "", "linearUrl": "", "details": "",
            },
        )
        deleted = gcw.apply_plan(
            delete_plan, approved_checksum=delete_plan["checksum"], service=service,
            authorization=delete_authorization,
        )
        delete_replay = gcw.apply_plan(
            delete_plan, approved_checksum=delete_plan["checksum"], service=service,
            authorization=delete_authorization,
        )

        self.assertEqual(create_plan["eventId"], delete_plan["eventId"])
        self.assertIsNone(deleted["linearIssue"])
        self.assertFalse(deleted["reused"])
        self.assertTrue(delete_replay["reused"])
        self.assertEqual(len(service.events_api.delete_calls), 1)

    def test_update_requires_link_updates_exact_body_then_replays_without_write(self):
        service = FakeService()
        gcw.apply_plan(
            self.plan,
            approved_checksum=self.plan["checksum"],
            service=service,
            authorization=self.authorization,
        )
        update_plan = gcw.build_plan(
            operation="update",
            block_key="primary",
            summary="Перенесенная задача",
            start="2026-09-08T11:00",
            end="2026-09-08T11:45",
            linear_url=LINEAR_URL,
            details="Новый срок",
        )
        authorization = self.authorization_for(
            update_plan,
            plan_inputs={
                "operation": "update",
                "blockKey": "primary",
                "summary": "Перенесенная задача",
                "start": "2026-09-08T11:00",
                "end": "2026-09-08T11:45",
                "linearUrl": LINEAR_URL,
                "details": "Новый срок",
            },
        )

        first = gcw.apply_plan(
            update_plan,
            approved_checksum=update_plan["checksum"],
            service=service,
            authorization=authorization,
        )
        second = gcw.apply_plan(
            update_plan,
            approved_checksum=update_plan["checksum"],
            service=service,
            authorization=authorization,
        )

        self.assertEqual(len(service.events_api.update_calls), 1)
        call = service.events_api.update_calls[0]
        self.assertEqual(call["calendarId"], "primary")
        self.assertEqual(call["eventId"], update_plan["eventId"])
        self.assertEqual(call["sendUpdates"], "none")
        self.assertEqual(call["body"], {**update_plan["event"], "id": update_plan["eventId"]})
        self.assertEqual(
            first,
            {
                "operation": "update",
                "status": "verified",
                "reused": False,
                "linearIssue": "SIS-84",
                "blockKey": "primary",
            },
        )
        self.assertTrue(second["reused"])

    def test_ambiguous_and_malformed_update_responses_reconcile_by_deterministic_get(self):
        for response in (TimeoutError("ambiguous update"), {}):
            with self.subTest(response=type(response).__name__):
                plan = gcw.build_plan(
                    operation="update",
                    block_key="primary",
                    summary="После обновления",
                    start="2026-09-08T13:00",
                    end="2026-09-08T13:30",
                    linear_url=LINEAR_URL,
                )
                authorization = self.authorization_for(
                    plan,
                    plan_inputs={
                        "operation": "update",
                        "blockKey": "primary",
                        "summary": "После обновления",
                        "start": "2026-09-08T13:00",
                        "end": "2026-09-08T13:30",
                        "linearUrl": LINEAR_URL,
                        "details": "",
                    },
                )
                events = MutationResponseEvents(update_response=response)
                events.created = {**self.plan["event"], "id": plan["eventId"]}

                result = gcw.apply_plan(
                    plan,
                    approved_checksum=plan["checksum"],
                    service=FakeService(events),
                    authorization=authorization,
                )

                self.assertEqual(result["status"], "verified")
                self.assertFalse(result["reused"])
                self.assertEqual(len(events.update_calls), 1)
                self.assertGreaterEqual(len(events.get_calls), 2)

    def test_cancelled_google_tombstone_remains_visible_to_operation_logic(self):
        tombstone = {**self.plan["event"], "id": self.plan["eventId"], "status": "cancelled"}
        events = ScriptedEvents(get_script=[tombstone])
        self.assertEqual(
            gcw._get_event(FakeService(events), self.plan["eventId"]),
            tombstone,
        )

    def test_create_replay_restores_linked_cancelled_tombstone(self):
        events = FakeEvents()
        events.created = {
            **self.plan["event"],
            "id": self.plan["eventId"],
            "status": "cancelled",
        }
        result = gcw.apply_plan(
            self.plan,
            approved_checksum=self.plan["checksum"],
            service=FakeService(events),
            authorization=self.authorization,
        )
        self.assertEqual(len(events.insert_calls), 0)
        self.assertEqual(len(events.update_calls), 1)
        self.assertEqual(events.update_calls[0]["body"]["status"], "confirmed")
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["reused"])

    def test_delete_verifies_absence_then_replays_as_no_op(self):
        service = FakeService()
        gcw.apply_plan(
            self.plan,
            approved_checksum=self.plan["checksum"],
            service=service,
            authorization=self.authorization,
        )
        delete_plan = gcw.build_plan(
            operation="delete",
            block_key="primary",
            summary="",
            start="",
            end="",
            linear_url=LINEAR_URL,
            details="",
        )
        authorization = self.authorization_for(
            delete_plan,
            plan_inputs={
                "operation": "delete",
                "blockKey": "primary",
                "summary": "",
                "start": "",
                "end": "",
                "linearUrl": LINEAR_URL,
                "details": "",
            },
        )

        first = gcw.apply_plan(
            delete_plan,
            approved_checksum=delete_plan["checksum"],
            service=service,
            authorization=authorization,
        )
        second = gcw.apply_plan(
            delete_plan,
            approved_checksum=delete_plan["checksum"],
            service=service,
            authorization=authorization,
        )

        self.assertEqual(len(service.events_api.delete_calls), 1)
        self.assertEqual(
            service.events_api.delete_calls[0],
            {
                "calendarId": "primary",
                "eventId": delete_plan["eventId"],
                "sendUpdates": "none",
            },
        )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["operation"], "delete")

    def test_delete_requires_a_404_read_back_not_a_malformed_empty_payload(self):
        plan = gcw.build_plan(
            operation="delete",
            block_key="primary",
            summary="",
            start="",
            end="",
            linear_url=LINEAR_URL,
        )
        authorization = self.authorization_for(
            plan,
            plan_inputs={
                "operation": "delete",
                "blockKey": "primary",
                "summary": "",
                "start": "",
                "end": "",
                "linearUrl": LINEAR_URL,
                "details": "",
            },
        )
        events = MalformedDeleteReadBackEvents()
        events.created = {**self.plan["event"], "id": plan["eventId"]}

        with self.assertRaisesRegex(gcw.CalendarWriteError, "malformed"):
            gcw.apply_plan(
                plan,
                approved_checksum=plan["checksum"],
                service=FakeService(events),
                authorization=authorization,
            )

    def test_ambiguous_delete_reconciles_by_verified_absence(self):
        plan = gcw.build_plan(
            operation="delete",
            block_key="primary",
            summary="",
            start="",
            end="",
            linear_url=LINEAR_URL,
        )
        authorization = self.authorization_for(
            plan,
            plan_inputs={
                "operation": "delete",
                "blockKey": "primary",
                "summary": "",
                "start": "",
                "end": "",
                "linearUrl": LINEAR_URL,
                "details": "",
            },
        )
        events = MutationResponseEvents(delete_response=TimeoutError("ambiguous delete"))
        events.created = {**self.plan["event"], "id": plan["eventId"]}

        result = gcw.apply_plan(
            plan,
            approved_checksum=plan["checksum"],
            service=FakeService(events),
            authorization=authorization,
        )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(events.delete_calls), 1)
        self.assertGreaterEqual(len(events.get_calls), 2)

    def test_update_and_delete_never_mutate_wrong_linear_link(self):
        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                if operation == "update":
                    plan = gcw.build_plan(
                        operation="update",
                        block_key="primary",
                        summary="Изменение",
                        start="2026-09-08T11:00",
                        end="2026-09-08T11:30",
                        linear_url=LINEAR_URL,
                    )
                    fields = {
                        "summary": "Изменение",
                        "start": "2026-09-08T11:00",
                        "end": "2026-09-08T11:30",
                        "details": "",
                    }
                else:
                    plan = gcw.build_plan(
                        operation="delete",
                        block_key="primary",
                        summary="",
                        start="",
                        end="",
                        linear_url=LINEAR_URL,
                    )
                    fields = {"summary": "", "start": "", "end": "", "details": ""}
                authorization = self.authorization_for(
                    plan,
                    plan_inputs={
                        "operation": operation,
                        "blockKey": "primary",
                        "linearUrl": LINEAR_URL,
                        **fields,
                    },
                )
                events = FakeEvents()
                events.created = {
                    **self.plan["event"],
                    "id": plan["eventId"],
                    "description": "Linear: https://linear.app/sisyphusx/issue/SIS-85/other",
                }

                with self.assertRaisesRegex(gcw.CalendarWriteError, "different Linear"):
                    gcw.apply_plan(
                        plan,
                        approved_checksum=plan["checksum"],
                        service=FakeService(events),
                        authorization=authorization,
                    )
                self.assertEqual(events.update_calls, [])
                self.assertEqual(events.delete_calls, [])

    def test_sequential_replay_performs_zero_additional_inserts_and_reports_reused(self):
        service = FakeService()
        first = gcw.apply_plan(
            self.plan,
            approved_checksum=self.plan["checksum"],
            service=service,
            authorization=self.authorization,
        )
        insert_count = len(service.events_api.insert_calls)
        second = gcw.apply_plan(
            self.plan,
            approved_checksum=self.plan["checksum"],
            service=service,
            authorization=self.authorization,
        )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(service.events_api.insert_calls) - insert_count, 0)

    def test_409_race_and_timeout_after_create_reconcile_by_exact_get(self):
        for failure in (HttpFailure(409), TimeoutError("ambiguous timeout")):
            with self.subTest(failure=type(failure).__name__):
                events = ScriptedEvents(
                    get_script=[HttpFailure(404), "created"],
                    insert_error=failure,
                    persist_on_error=True,
                )
                result = gcw.apply_plan(
                    self.plan,
                    approved_checksum=self.plan["checksum"],
                    service=FakeService(events),
                    authorization=self.authorization,
                )
                self.assertEqual(result["status"], "verified")
                self.assertTrue(result["reused"])
                self.assertEqual(len(events.insert_calls), 1)

    def test_malformed_success_response_reconciles_persisted_event_by_exact_id(self):
        events = ScriptedEvents(get_script=[HttpFailure(404), "created"])
        original_insert = events.insert

        def insert_and_return_malformed(**kwargs):
            original_insert(**kwargs)
            return FakeRequest({})

        events.insert = insert_and_return_malformed
        result = gcw.apply_plan(
            self.plan,
            approved_checksum=self.plan["checksum"],
            service=FakeService(events),
            authorization=self.authorization,
        )
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["reused"])
        self.assertEqual(len(events.insert_calls), 1)

    def test_provider_kyiv_alias_and_offset_normalization_preserves_exact_instant(self):
        expected = {
            "summary": "SIS-84 Calendar E2E",
            "description": f"Linear: {LINEAR_URL}",
            "start": {"dateTime": "2026-09-07T10:00:00+03:00", "timeZone": "Europe/Kyiv"},
            "end": {"dateTime": "2026-09-07T10:30:00+03:00", "timeZone": "Europe/Kyiv"},
        }
        observed = {
            "summary": "SIS-84 Calendar E2E",
            "description": f"Linear: {LINEAR_URL}",
            "start": {"dateTime": "2026-09-07T09:00:00+02:00", "timeZone": "Europe/Kiev"},
            "end": {"dateTime": "2026-09-07T09:30:00+02:00", "timeZone": "Europe/Kiev"},
        }
        gcw._verify_event(observed, expected)

    def test_each_read_back_field_mismatch_fails_closed(self):
        for field in ("summary", "description", "start", "end"):
            with self.subTest(field=field):
                wrong = dict(self.plan["event"])
                wrong[field] = "wrong" if field in {"summary", "description"} else {}
                events = ScriptedEvents(get_script=[wrong])
                with self.assertRaisesRegex(gcw.CalendarWriteError, field):
                    gcw.apply_plan(
                        self.plan,
                        approved_checksum=self.plan["checksum"],
                        service=FakeService(events),
                        authorization=self.authorization,
                    )
                self.assertEqual(events.insert_calls, [])

    def test_realistic_404_is_absent_but_non_404_get_error_is_sanitized(self):
        absent = ScriptedEvents(get_script=[HttpFailure(404), "created"])
        result = gcw.apply_plan(
            self.plan,
            approved_checksum=self.plan["checksum"],
            service=FakeService(absent),
            authorization=self.authorization,
        )
        self.assertEqual(result["status"], "verified")
        denied = ScriptedEvents(get_script=[HttpFailure(403, "secret response")])
        with self.assertRaisesRegex(gcw.CalendarWriteError, "Calendar event lookup failed") as caught:
            gcw.apply_plan(
                self.plan,
                approved_checksum=self.plan["checksum"],
                service=FakeService(denied),
                authorization=self.authorization,
            )
        self.assertNotIn("secret response", str(caught.exception))
        self.assertEqual(denied.insert_calls, [])

    def test_ambiguous_create_fails_closed_when_exact_event_is_absent_or_mismatched(self):
        for read_back in (HttpFailure(404), {"summary": "different"}):
            with self.subTest(read_back=type(read_back).__name__):
                events = ScriptedEvents(
                    get_script=[HttpFailure(404), read_back],
                    insert_error=TimeoutError("ambiguous timeout"),
                )
                with self.assertRaises(gcw.CalendarWriteError):
                    gcw.apply_plan(
                        self.plan,
                        approved_checksum=self.plan["checksum"],
                        service=FakeService(events),
                        authorization=self.authorization,
                    )

    def test_direct_apply_with_checksum_but_without_verified_approval_is_blocked(self):
        service = FakeService()
        with self.assertRaisesRegex(gcw.CalendarWriteError, "verified manual approval"):
            gcw.apply_plan(
                self.plan,
                approved_checksum=self.plan["checksum"],
                service=service,
            )
        self.assertEqual(service.events_api.insert_calls, [])

    def test_wrong_checksum_blocks_before_calendar_write(self):
        service = FakeService()
        with self.assertRaises(gcw.CalendarWriteError):
            gcw.apply_plan(
                self.plan,
                approved_checksum="0" * 64,
                service=service,
                authorization=self.authorization,
            )
        self.assertEqual(service.events_api.insert_calls, [])

    def test_tampered_plan_blocks_before_calendar_write(self):
        service = FakeService()
        self.plan["event"]["description"] = "Linear: https://evil.example/SIS-84"
        with self.assertRaises(gcw.CalendarWriteError):
            gcw.apply_plan(
                self.plan,
                approved_checksum=self.plan["checksum"],
                service=service,
                authorization=self.authorization,
            )
        self.assertEqual(service.events_api.insert_calls, [])

    def test_extra_event_field_blocks_before_calendar_api_call(self):
        plan = gcw.build_plan(
            summary=PLAN_INPUTS["summary"],
            start=PLAN_INPUTS["start"],
            end=PLAN_INPUTS["end"],
            linear_url=PLAN_INPUTS["linearUrl"],
            details=PLAN_INPUTS["details"],
        )
        plan["event"]["attendees"] = [{"email": "other@example.com"}]
        plan["checksum"] = gcw._plan_checksum(plan)
        authorization = gcw.VerifiedApproval(
            plan["checksum"], _marker=gcw._VERIFIED_APPROVAL_MARKER
        )
        service = FakeService()
        with self.assertRaisesRegex(gcw.CalendarWriteError, "schema"):
            gcw.apply_plan(
                plan,
                approved_checksum=plan["checksum"],
                service=service,
                authorization=authorization,
            )
        self.assertEqual(service.events_api.get_calls, [])
        self.assertEqual(service.events_api.insert_calls, [])

    def test_all_day_date_blocks_before_calendar_api_call(self):
        plan = gcw.build_plan(
            summary=PLAN_INPUTS["summary"],
            start=PLAN_INPUTS["start"],
            end=PLAN_INPUTS["end"],
            linear_url=PLAN_INPUTS["linearUrl"],
            details=PLAN_INPUTS["details"],
        )
        plan["event"]["start"] = {
            "date": "2026-09-07",
            "timeZone": "Europe/Kyiv",
        }
        plan["checksum"] = gcw._plan_checksum(plan)
        authorization = gcw.VerifiedApproval(
            plan["checksum"], _marker=gcw._VERIFIED_APPROVAL_MARKER
        )
        service = FakeService()
        with self.assertRaisesRegex(gcw.CalendarWriteError, "schema"):
            gcw.apply_plan(
                plan,
                approved_checksum=plan["checksum"],
                service=service,
                authorization=authorization,
            )
        self.assertEqual(service.events_api.get_calls, [])
        self.assertEqual(service.events_api.insert_calls, [])


    def test_apply_rechecks_approved_before_state_inside_mutation_process(self):
        plan = gcw.build_plan(
            operation="update", block_key="primary", summary="Approved title",
            start="2026-09-07T10:00", end="2026-09-07T10:30",
            linear_url=LINEAR_URL, details="",
        )
        service = FakeService()
        service.events_api.created = {
            "id": plan["eventId"], "status": "confirmed", "summary": "Before title",
            "description": f"Linear: {LINEAR_URL}",
            "start": {"dateTime": "2026-09-07T09:00:00+03:00", "timeZone": "Europe/Kyiv"},
            "end": {"dateTime": "2026-09-07T09:30:00+03:00", "timeZone": "Europe/Kyiv"},
        }
        before_hash = gcw.snapshot_target(
            linear_url=LINEAR_URL, block_key="primary", service=service
        )["beforeStateHash"]
        service.events_api.created["attendees"] = [{"email": "intervening@example.com"}]
        authorization = gcw.VerifiedApproval(
            plan["checksum"], _marker=gcw._VERIFIED_APPROVAL_MARKER
        )
        with self.assertRaisesRegex(gcw.CalendarWriteError, "changed after owner preview"):
            gcw.apply_plan(
                plan, approved_checksum=plan["checksum"], service=service,
                authorization=authorization,
                expected_before_state_hash=before_hash,
            )
        self.assertEqual(service.events_api.update_calls, [])

    def test_apply_allows_verified_converged_replay_after_before_state_changed(self):
        plan = gcw.build_plan(
            operation="update", block_key="primary", summary="Approved title",
            start="2026-09-07T10:00", end="2026-09-07T10:30",
            linear_url=LINEAR_URL, details="",
        )
        service = FakeService()
        service.events_api.created = dict(plan["event"])
        authorization = gcw.VerifiedApproval(
            plan["checksum"], _marker=gcw._VERIFIED_APPROVAL_MARKER
        )
        result = gcw.apply_plan(
            plan, approved_checksum=plan["checksum"], service=service,
            authorization=authorization,
            expected_before_state_hash="f" * 64,
        )
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["reused"])
        self.assertEqual(service.events_api.update_calls, [])


    def test_before_state_bound_update_uses_provider_if_match_precondition(self):
        plan = gcw.build_plan(
            operation="update", block_key="primary", summary="Approved title",
            start="2026-09-07T10:00", end="2026-09-07T10:30",
            linear_url=LINEAR_URL, details="",
        )
        service = FakeService()
        service.events_api.created = {
            "id": plan["eventId"], "etag": '"provider-v1"', "status": "confirmed",
            "summary": "Before title", "description": f"Linear: {LINEAR_URL}",
            "start": {"dateTime": "2026-09-07T09:00:00+03:00", "timeZone": "Europe/Kyiv"},
            "end": {"dateTime": "2026-09-07T09:30:00+03:00", "timeZone": "Europe/Kyiv"},
        }
        before_hash = gcw.snapshot_target(
            linear_url=LINEAR_URL, block_key="primary", service=service
        )["beforeStateHash"]
        authorization = gcw.VerifiedApproval(
            plan["checksum"], _marker=gcw._VERIFIED_APPROVAL_MARKER
        )
        result = gcw.apply_plan(
            plan, approved_checksum=plan["checksum"], service=service,
            authorization=authorization,
            expected_before_state_hash=before_hash,
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(
            service.events_api.update_request.headers.get("If-Match"), '"provider-v1"'
        )


    def test_before_state_bound_update_requires_provider_revision(self):
        plan = gcw.build_plan(
            operation="update", block_key="primary", summary="Approved title",
            start="2026-09-07T10:00", end="2026-09-07T10:30",
            linear_url=LINEAR_URL, details="",
        )
        service = FakeService()
        service.events_api.created = {
            "id": plan["eventId"], "status": "confirmed", "summary": "Before title",
            "description": f"Linear: {LINEAR_URL}",
            "start": {"dateTime": "2026-09-07T09:00:00+03:00", "timeZone": "Europe/Kyiv"},
            "end": {"dateTime": "2026-09-07T09:30:00+03:00", "timeZone": "Europe/Kyiv"},
        }
        before_hash = gcw.snapshot_target(
            linear_url=LINEAR_URL, block_key="primary", service=service
        )["beforeStateHash"]
        authorization = gcw.VerifiedApproval(
            plan["checksum"], _marker=gcw._VERIFIED_APPROVAL_MARKER
        )
        with self.assertRaisesRegex(gcw.CalendarWriteError, "provider revision"):
            gcw.apply_plan(
                plan, approved_checksum=plan["checksum"], service=service,
                authorization=authorization,
                expected_before_state_hash=before_hash,
            )


class SwampContractTests(unittest.TestCase):
    def test_write_models_and_four_workflows_are_valid_uuid_documents(self):
        root = SCRIPT.parents[1]
        paths = (
            root / "models" / "command" / "shell" / "google-calendar-write.yaml",
            root / "models" / "command" / "shell" / "google-calendar-write-approval.yaml",
            root / "workflows" / "workflow-google-calendar-write-plan.yaml",
            root / "workflows" / "workflow-google-calendar-write-snapshot.yaml",
            root / "workflows" / "workflow-google-calendar-write-approval.yaml",
            root / "workflows" / "workflow-google-calendar-write-apply.yaml",
        )
        for path in paths:
            with self.subTest(path=path):
                document = yaml.safe_load(path.read_text())
                parsed = uuid.UUID(document["id"])
                self.assertEqual(parsed.variant, uuid.RFC_4122)

    def test_snapshot_workflow_is_read_only_and_accepts_only_exact_target(self):
        path = SCRIPT.parents[1] / "workflows" / "workflow-google-calendar-write-snapshot.yaml"
        workflow = yaml.safe_load(path.read_text())
        self.assertEqual(
            set(workflow["inputs"]["properties"]), {"blockKey", "linearUrl"}
        )
        linear_url = workflow["inputs"]["properties"]["linearUrl"]
        self.assertEqual(linear_url["default"], "")
        self.assertIsNotNone(re.fullmatch(linear_url["pattern"], ""))
        command = workflow["jobs"][0]["steps"][0]["task"]["inputs"]["run"]
        self.assertIn("--mode snapshot", command)
        self.assertIn("--profile personal-assistant", command)
        self.assertNotIn("summary", command)

    def test_plan_workflow_exposes_operation_block_key_and_raw_event_fields(self):
        path = SCRIPT.parents[1] / "workflows" / "workflow-google-calendar-write-plan.yaml"
        workflow = yaml.safe_load(path.read_text())
        properties = workflow["inputs"]["properties"]
        self.assertEqual(
            set(properties),
            {"operation", "blockKey", "summary", "start", "end", "linearUrl", "details"},
        )
        self.assertEqual(properties["operation"]["enum"], ["create", "update", "delete"])
        self.assertEqual(properties["operation"]["default"], "create")
        self.assertEqual(properties["blockKey"]["default"], "primary")
        self.assertEqual(
            set(workflow["inputs"]["required"]),
            {"operation", "blockKey", "summary", "start", "end", "linearUrl", "details"},
        )
        for field in ("summary", "start", "end", "details"):
            self.assertEqual(properties[field]["default"], "")
        command = workflow["jobs"][0]["steps"][0]["task"]["inputs"]["run"]
        self.assertIn("--operation '${{ inputs.operation }}'", command)
        self.assertIn("--block-key '${{ inputs.blockKey }}'", command)
        self.assertEqual(
            properties["linearUrl"]["pattern"],
            r"^$|^https://linear\.app/[A-Za-z0-9_-]+/issue/SIS-[1-9][0-9]*/[A-Za-z0-9][A-Za-z0-9_-]*$",
        )
        self.assertEqual(properties["linearUrl"]["default"], "")

    def test_approval_workflow_owns_manual_gate_and_emits_attestation(self):
        path = SCRIPT.parents[1] / "workflows" / "workflow-google-calendar-write-approval.yaml"
        workflow = yaml.safe_load(path.read_text())
        self.assertEqual(
            set(workflow["inputs"]["properties"]),
            {"planRunId", "planArtifactVersion", "planChecksum"},
        )
        steps = workflow["jobs"][0]["steps"]
        self.assertEqual(steps[0]["name"], "approve-calendar-write")
        self.assertEqual(steps[0]["task"]["type"], "manual_approval")
        self.assertEqual(steps[1]["task"]["modelIdOrName"], "google-calendar-write-approval")

    def test_apply_workflow_accepts_only_fixed_plan_and_approval_references(self):
        path = SCRIPT.parents[1] / "workflows" / "workflow-google-calendar-write-apply.yaml"
        workflow = yaml.safe_load(path.read_text())
        self.assertEqual(
            set(workflow["inputs"]["properties"]),
            {
                "planRunId", "planArtifactVersion", "planChecksum",
                "approvalRunId", "approvalArtifactVersion", "approvalChecksum",
                "beforeStateHash",
            },
        )
        steps = workflow["jobs"][0]["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["task"]["type"], "model_method")
        command = steps[0]["task"]["inputs"]["run"]
        self.assertNotIn("summary", command)
        self.assertNotIn("linear-url", command)
        self.assertNotIn("details", command)
        self.assertIn("--approval-run-id '${{ inputs.approvalRunId }}'", command)
        self.assertIn("--before-state-hash '${{ inputs.beforeStateHash }}'", command)

    def test_plan_cli_accepts_delete_with_default_empty_event_fields(self):
        args = gcw.parse_args([
            "--mode", "plan",
            "--operation", "delete",
            "--block-key", "deep-work",
            "--linear-url", LINEAR_URL,
        ])
        self.assertEqual(args.operation, "delete")
        self.assertEqual(args.block_key, "deep-work")
        self.assertEqual((args.summary, args.start, args.end, args.details), ("", "", "", ""))

    def test_apply_cli_contract_accepts_only_fixed_plan_and_approval_references(self):
        args = gcw.parse_args([
            "--mode", "apply",
            "--plan-run-id", PLAN_RUN_ID,
            "--plan-artifact-version", "1",
            "--plan-checksum", "a" * 64,
            "--approval-run-id", APPROVAL_RUN_ID,
            "--approval-artifact-version", "2",
            "--approval-checksum", "b" * 64,
            "--before-state-hash", "c" * 64,
        ])
        self.assertEqual(args.mode, "apply")
        self.assertFalse(hasattr(args, "summary"))
        self.assertFalse(hasattr(args, "linear_url"))
        self.assertEqual(args.before_state_hash, "c" * 64)

    def test_workflows_use_hermes_venv_with_google_sdk(self):
        root = SCRIPT.parents[1] / "workflows"
        expected = "/Users/hermes/.hermes/hermes-agent/venv/bin/python"
        for name, step_index in (
            ("workflow-google-calendar-write-plan.yaml", 0),
            ("workflow-google-calendar-write-snapshot.yaml", 0),
            ("workflow-google-calendar-write-approval.yaml", 1),
            ("workflow-google-calendar-write-apply.yaml", 0),
        ):
            with self.subTest(name=name):
                workflow = yaml.safe_load((root / name).read_text())
                command = workflow["jobs"][0]["steps"][step_index]["task"]["inputs"]["run"]
                self.assertTrue(command.startswith(expected), command)


if __name__ == "__main__":
    unittest.main()
