import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.personal_assistant_calendar import (  # noqa: E402
    PA_CALENDAR_EXECUTE_SCHEMA,
    SwampCalendarWorkflows,
    _approval_token,
    _completed_result_path,
    _load_completed_result,
    _write_completed_result,
    _verify_runtime_workspace,
    _validated_approval_plan,
    handle_pa_calendar_execute as real_handle_pa_calendar_execute,
)
from plugins.linear_source_route.calendar_route import build_calendar_task_body, parse_calendar_request  # noqa: E402


UUID = "11111111-1111-4111-8111-111111111111"
PLAN_RUN = "22222222-2222-4222-8222-222222222222"
APPROVAL_RUN = "33333333-3333-4333-8333-333333333333"


def handle_pa_calendar_execute(args, **kwargs):
    kwargs.setdefault("result_loader", lambda *_args: None)
    kwargs.setdefault("result_writer", lambda *_args: None)
    kwargs.setdefault("execution_guard", lambda *_args: nullcontext())
    return real_handle_pa_calendar_execute(args, **kwargs)


def command(request=None):
    return parse_calendar_request(
        request or {"operation": "events", "window": "today"},
        source_profile="default",
        uuid_factory=lambda: UUID,
    ).command


def task_record(raw_command=None, **overrides):
    values = {
        "id": "t_deadbeef",
        "assignee": "personal-assistant",
        "status": "running",
        "current_run_id": 7,
        "session_id": "20260904_120000_abcdef12",
        "body": build_calendar_task_body(raw_command or command()),
    }
    values.update(overrides)
    return values


class Lifecycle:
    def __init__(self):
        self.completed = []
        self.blocked = []

    def complete(self, *, summary, result):
        self.completed.append({"summary": summary, "result": result})

    def block(self, *, reason, kind):
        self.blocked.append({"reason": reason, "kind": kind})


class Workflows:
    def __init__(self):
        self.calls = []

    def read(self, operation, window):
        self.calls.append(("read", operation, window))
        return {
            "operation": operation, "status": "ok", "timezone": "Europe/Kyiv",
            "window": window,
            "bounds": {
                "start": "2026-09-04T00:00:00+03:00",
                "end": "2026-09-05T00:00:00+03:00",
            },
            "event_count": 0,
        }

    def plan(self, request):
        self.calls.append(("plan", request))
        description = f"Linear: {request['linear_url']}"
        if request["details"].strip():
            description = f"{request['details'].strip()}\n\n{description}"
        return {
            "run_id": PLAN_RUN,
            "artifact_version": 7,
            "preview": {
                "schemaVersion": 1,
                "mode": "plan",
                "readOnly": True,
                "ready": True,
                "calendarId": "primary",
                "operation": request["operation"],
                "blockKey": request["block_key"],
                "linearIssue": {"identifier": "SIS-123", "url": request["linear_url"]},
                "event": {
                    "summary": request["summary"].strip(),
                    "description": description,
                    "start": {"dateTime": request["start"], "timeZone": "Europe/Kyiv"},
                    "end": {"dateTime": request["end"], "timeZone": "Europe/Kyiv"},
                },
                "blockers": [],
                "checksum": "a" * 64,
            },
        }

    def snapshot(self, request):
        self.calls.append(("snapshot", request))
        return {
            "operation": "snapshot", "status": "ok", "linearIssue": "SIS-123",
            "blockKey": request["block_key"], "beforeStateHash": "d" * 64,
        }

    def start_approval(self, plan_reference):
        self.calls.append(("start_approval", plan_reference))
        return {"run_id": APPROVAL_RUN, "status": "suspended"}

    def approve(self, run_id):
        self.calls.append(("approve", run_id))

    def resume_approval(self, run_id):
        self.calls.append(("resume_approval", run_id))
        return {"run_id": run_id, "status": "succeeded", "artifact_version": 8, "checksum": "b" * 64}

    def apply(self, plan_reference, approval_reference):
        self.calls.append(("apply", plan_reference, approval_reference))
        return {"operation": "create", "status": "verified", "reused": False, "linearIssue": "SIS-123", "blockKey": "primary"}


def environ():
    return {
        "HERMES_PROFILE": "personal-assistant",
        "HERMES_KANBAN_TASK": "t_deadbeef",
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_DB": "/tmp/kanban.db",
        "HERMES_KANBAN_CLAIM_LOCK": "claim",
    }


def approval_plan(approval_ref, *, session_id="20260904_120000_abcdef12"):
    return {
        "source_profile": "default",
        "session_id": session_id,
        "approval_reference": approval_ref,
        "plan_reference": {"run_id": PLAN_RUN, "artifact_version": 7, "checksum": "a" * 64, "before_state_hash": "d" * 64},
        "request": {
            "operation": "create", "block_key": "primary", "summary": "Review SIS-123",
            "start": "2026-09-07T10:00", "end": "2026-09-07T10:30",
            "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing", "details": "",
        },
    }


class PersonalAssistantCalendarWorkerTests(unittest.TestCase):
    def test_schema_accepts_no_model_supplied_command(self):
        self.assertEqual(PA_CALENDAR_EXECUTE_SCHEMA["parameters"], {
            "type": "object", "properties": {}, "required": [], "additionalProperties": False
        })

    def test_completed_result_journal_is_scoped_to_the_persisted_command(self):
        first = command()
        second = dict(first)
        second["command_id"] = "99999999-9999-4999-8999-999999999999"
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        env = {"HERMES_HOME": "/Users/hermes/workspaces/runtime/test-pa-home"}
        self.assertNotEqual(
            _completed_result_path(first, env), _completed_result_path(second, env)
        )

    def test_read_runs_bounded_workflow_and_completes_calendar_result_v1(self):
        lifecycle = Lifecycle()
        workflows = Workflows()
        output = json.loads(handle_pa_calendar_execute(
            {},
            environ=environ(),
            task_loader=lambda *_args: task_record(),
            run_reserver=lambda *_args: True,
            lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=lambda: workflows,
        ))
        self.assertEqual(output["status"], "completed")
        self.assertEqual(workflows.calls, [("read", "events", "today")])
        persisted = json.loads(lifecycle.completed[0]["result"])
        self.assertEqual(persisted["schema_version"], "calendar-result.v1")
        self.assertEqual(persisted["outcome"], "read")
        self.assertTrue(persisted["verified"])

    def test_write_plan_returns_exact_preview_and_opaque_approval_reference(self):
        request = {
            "operation": "create", "block_key": "primary", "summary": "Review SIS-123",
            "start": "2026-09-07T10:00", "end": "2026-09-07T10:30",
            "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing", "details": "",
        }
        raw = command(request)
        lifecycle = Lifecycle()
        workflows = Workflows()
        handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(raw),
            run_reserver=lambda *_args: True, lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=lambda: workflows,
        )
        persisted = json.loads(lifecycle.completed[0]["result"])
        self.assertEqual(persisted["phase"], "awaiting_approval")
        self.assertEqual(persisted["preview"], {
            "operation": "create",
            "block_key": "primary",
            "summary": "Review SIS-123",
            "details": "",
            "start": "2026-09-07T10:00",
            "end": "2026-09-07T10:30",
            "timezone": "Europe/Kyiv",
            "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing",
        })
        self.assertNotIn("checksum", persisted["preview"])
        self.assertNotIn("eventId", persisted["preview"])
        self.assertRegex(persisted["approval_reference"], r"^calendar-approval:v1:[a-f0-9]{64}$")
        self.assertEqual(persisted["plan_reference"], {
            "run_id": PLAN_RUN, "artifact_version": 7, "checksum": "a" * 64,
            "before_state_hash": "d" * 64,
        })

    def test_same_session_approval_suspends_approves_resumes_then_applies(self):
        approval_ref = "calendar-approval:v1:" + "c" * 64
        raw = command({"operation": "approve", "approval_reference": approval_ref})
        plan = approval_plan(approval_ref)
        lifecycle = Lifecycle()
        workflows = Workflows()
        output = json.loads(handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(raw),
            approval_loader=lambda *_args: plan,
            run_reserver=lambda *_args: True, lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=lambda: workflows,
        ))
        self.assertEqual(output["status"], "completed")
        self.assertEqual([call[0] for call in workflows.calls], [
            "start_approval", "approve", "resume_approval", "snapshot", "apply"
        ])
        persisted = json.loads(lifecycle.completed[0]["result"])
        self.assertEqual(persisted["outcome"], "applied")
        self.assertNotIn("summary", persisted["data"])

    def test_approval_renews_claim_before_each_network_step_and_apply(self):
        approval_ref = "calendar-approval:v1:" + "c" * 64
        raw = command({"operation": "approve", "approval_reference": approval_ref})
        plan = approval_plan(approval_ref)
        reservations = []
        lifecycle = Lifecycle()
        output = json.loads(handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(raw),
            approval_loader=lambda *_args: plan,
            run_reserver=lambda *args: reservations.append(args) or True,
            lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=Workflows,
        ))
        self.assertEqual(output["status"], "completed")
        self.assertGreaterEqual(len(reservations), 5)

    def test_mid_execution_supersession_rejects_without_blocking_task(self):
        lifecycle = Lifecycle()
        reservations = iter((True, False))
        workflows = mock.Mock()
        output = json.loads(handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(),
            run_reserver=lambda *_args: next(reservations),
            lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=lambda: workflows,
        ))
        self.assertEqual(output["status"], "rejected")
        self.assertEqual(lifecycle.blocked, [])
        workflows.read.assert_not_called()

    def test_apply_result_rejects_unexpected_private_fields_before_persistence(self):
        approval_ref = "calendar-approval:v1:" + "c" * 64
        raw = command({"operation": "approve", "approval_reference": approval_ref})
        plan = approval_plan(approval_ref)
        class UnsafeApply(Workflows):
            def apply(self, plan_reference, approval_reference):
                return {
                    "operation": "create", "status": "verified", "reused": False,
                    "linearIssue": "SIS-123", "blockKey": "primary",
                    "eventId": "private-id", "summary": "PRIVATE_EVENT_TITLE",
                }
        lifecycle = Lifecycle()
        output = json.loads(handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(raw),
            approval_loader=lambda *_args: plan,
            run_reserver=lambda *_args: True,
            lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=UnsafeApply,
        ))
        self.assertEqual(output["status"], "blocked")
        self.assertEqual(lifecycle.completed, [])
        self.assertNotIn("PRIVATE_EVENT_TITLE", json.dumps(lifecycle.blocked) + json.dumps(output))

    def test_intervening_calendar_edit_blocks_before_apply(self):
        approval_ref = "calendar-approval:v1:" + "c" * 64
        raw = command({"operation": "approve", "approval_reference": approval_ref})

        class DriftedTarget(Workflows):
            def snapshot(self, request):
                result = super().snapshot(request)
                result["beforeStateHash"] = "e" * 64
                return result

            def apply(self, plan_reference, approval_reference):
                raise AssertionError("apply must not run after target-state drift")

        lifecycle = Lifecycle()
        workflows = DriftedTarget()
        output = json.loads(handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(raw),
            approval_loader=lambda *_args: approval_plan(approval_ref),
            run_reserver=lambda *_args: True,
            lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=lambda: workflows,
        ))
        self.assertEqual(output["status"], "blocked")
        self.assertEqual([call[0] for call in workflows.calls], [
            "start_approval", "approve", "resume_approval", "snapshot",
        ])
        self.assertEqual(lifecycle.completed, [])

    def test_successful_apply_survives_lifecycle_failure_and_retry_does_not_reapply(self):
        approval_ref = "calendar-approval:v1:" + "c" * 64
        raw = command({"operation": "approve", "approval_reference": approval_ref})
        plan = approval_plan(approval_ref)
        stored = []
        workflows = Workflows()

        class FailingLifecycle(Lifecycle):
            def complete(self, *, summary, result):
                raise RuntimeError("lifecycle unavailable")

        failed_lifecycle = FailingLifecycle()
        first = json.loads(handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(raw),
            approval_loader=lambda *_args: plan,
            run_reserver=lambda *_args: True,
            lifecycle_factory=lambda _task_id: failed_lifecycle,
            workflow_runner_factory=lambda: workflows,
            result_loader=lambda *_args: None,
            result_writer=lambda _command, result, _environ: stored.append(result),
        ))
        self.assertEqual(first["status"], "rejected")
        self.assertEqual(failed_lifecycle.blocked, [])
        self.assertEqual([call[0] for call in workflows.calls], [
            "start_approval", "approve", "resume_approval", "snapshot", "apply",
        ])
        self.assertEqual(len(stored), 1)

        retry_lifecycle = Lifecycle()
        factory = mock.Mock()
        second = json.loads(handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(raw),
            approval_loader=lambda *_args: plan,
            run_reserver=lambda *_args: True,
            lifecycle_factory=lambda _task_id: retry_lifecycle,
            workflow_runner_factory=factory,
            result_loader=lambda *_args: stored[0],
            result_writer=lambda *_args: self.fail("completed result must not be rewritten"),
        ))
        self.assertEqual(second["status"], "completed")
        factory.assert_not_called()
        self.assertEqual(len(retry_lifecycle.completed), 1)

    def test_concurrent_replay_serializes_load_execute_and_journal(self):
        approval_ref = "calendar-approval:v1:" + "c" * 64
        raw = command({"operation": "approve", "approval_reference": approval_ref})
        plan = approval_plan(approval_ref)
        counter_lock = threading.Lock()
        apply_calls = 0

        class SlowWorkflows(Workflows):
            def start_approval(self, plan_reference):
                time.sleep(0.05)
                return super().start_approval(plan_reference)

            def apply(self, plan_reference, approval_reference):
                nonlocal apply_calls
                with counter_lock:
                    apply_calls += 1
                return super().apply(plan_reference, approval_reference)

        with tempfile.TemporaryDirectory() as tmp:
            env = {**environ(), "HERMES_HOME": tmp}
            barrier = threading.Barrier(2)
            outputs = []

            def invoke():
                barrier.wait()
                outputs.append(json.loads(real_handle_pa_calendar_execute(
                    {}, environ=env, task_loader=lambda *_args: task_record(raw),
                    approval_loader=lambda *_args: plan,
                    run_reserver=lambda *_args: True,
                    lifecycle_factory=lambda _task_id: Lifecycle(),
                    workflow_runner_factory=SlowWorkflows,
                    result_loader=_load_completed_result,
                    result_writer=_write_completed_result,
                )))

            threads = [threading.Thread(target=invoke) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(apply_calls, 1)
        self.assertEqual([item["status"] for item in outputs], ["completed", "completed"])

    def test_cross_session_approval_fails_before_workflow_access(self):
        approval_ref = "calendar-approval:v1:" + "c" * 64
        raw = command({"operation": "approve", "approval_reference": approval_ref})
        workflows = mock.Mock()
        lifecycle = Lifecycle()
        output = json.loads(handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(raw),
            approval_loader=lambda *_args: approval_plan(
                approval_ref, session_id="20260904_130000_deadbeef"
            ),
            run_reserver=lambda *_args: True, lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=lambda: workflows,
        ))
        self.assertEqual(output["status"], "blocked")
        workflows.start_approval.assert_not_called()

    def test_approval_plan_result_is_cryptographically_bound_to_persisted_command(self):
        request = {
            "operation": "create", "block_key": "primary", "summary": "Review SIS-123",
            "start": "2026-09-07T10:00", "end": "2026-09-07T10:30",
            "linear_url": "https://linear.app/sisyphusx/issue/SIS-123/calendar-routing", "details": "",
        }
        raw = command(request)
        plan_reference = {
            "run_id": PLAN_RUN, "artifact_version": 7, "checksum": "a" * 64,
            "before_state_hash": "d" * 64,
        }
        session_id = "20260904_120000_abcdef12"
        approval_reference = _approval_token(raw, plan_reference, session_id)
        result = {
            "schema_version": "calendar-result.v1",
            "command_id": raw["command_id"],
            "idempotency_key": raw["idempotency_key"],
            "source_profile": raw["source_profile"],
            "operation": "plan_write",
            "phase": "awaiting_approval",
            "outcome": "planned",
            "preview": {
                "operation": "create", "block_key": "primary", "summary": "Review SIS-123",
                "details": "", "start": "2026-09-07T10:00:00+03:00",
                "end": "2026-09-07T10:30:00+03:00", "timezone": "Europe/Kyiv",
                "linear_url": request["linear_url"],
            },
            "approval_reference": approval_reference,
            "plan_reference": plan_reference,
            "verified": True,
        }
        task = task_record(raw, status="done", result=json.dumps(result))
        validated = _validated_approval_plan(task, approval_reference)
        self.assertEqual(validated["plan_reference"], plan_reference)

        tampered = dict(result)
        tampered["plan_reference"] = {**plan_reference, "checksum": "b" * 64}
        with self.assertRaisesRegex(Exception, "binding"):
            _validated_approval_plan(
                task_record(raw, status="done", result=json.dumps(tampered)),
                approval_reference,
            )

    def test_malformed_persisted_request_is_blocked_before_workflow_access(self):
        raw = command()
        raw["request"] = {"window": "today", "calendar_id": "other"}
        workflows = mock.Mock()
        lifecycle = Lifecycle()
        output = json.loads(handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(raw),
            run_reserver=lambda *_args: True, lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=lambda: workflows,
        ))
        self.assertEqual(output["status"], "blocked")
        workflows.read.assert_not_called()

    def test_unexpected_or_secret_shaped_read_fields_are_not_persisted(self):
        class Unsafe(Workflows):
            def read(self, operation, window):
                return {
                    "operation": operation, "status": "ok", "window": window,
                    "access_token": "ya29.supersecretvalue",
                }
        lifecycle = Lifecycle()
        output = handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(),
            run_reserver=lambda *_args: True, lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=Unsafe,
        )
        self.assertEqual(json.loads(output)["status"], "blocked")
        self.assertEqual(lifecycle.completed, [])
        self.assertNotIn("supersecretvalue", output + json.dumps(lifecycle.blocked))

    def test_nested_read_bounds_are_a_closed_pii_free_schema(self):
        class UnsafeBounds(Workflows):
            def read(self, operation, window):
                return {
                    "operation": operation, "status": "ok", "timezone": "Europe/Kyiv",
                    "window": window,
                    "bounds": {
                        "start": "2026-09-04T00:00:00+03:00",
                        "end": "2026-09-05T00:00:00+03:00",
                        "summary": "PRIVATE_EVENT_TITLE",
                    },
                }
        lifecycle = Lifecycle()
        output = handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(),
            run_reserver=lambda *_args: True, lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=UnsafeBounds,
        )
        self.assertEqual(json.loads(output)["status"], "blocked")
        self.assertNotIn("PRIVATE_EVENT_TITLE", output + json.dumps(lifecycle.blocked))

    def test_wrong_profile_args_or_superseded_claim_prevents_workflow_access(self):
        for args, env, reserve in (
            ({"command": {}}, environ(), True),
            ({}, {**environ(), "HERMES_PROFILE": "default"}, True),
            ({}, environ(), False),
        ):
            factory = mock.Mock()
            result = json.loads(handle_pa_calendar_execute(
                args, environ=env, task_loader=lambda *_args: task_record(),
                run_reserver=lambda *_args, value=reserve: value,
                workflow_runner_factory=factory,
            ))
            self.assertEqual(result["status"], "rejected")
            factory.assert_not_called()

    def test_errors_are_redacted_before_persisting_blocker(self):
        class Broken(Workflows):
            def read(self, operation, window):
                raise RuntimeError('Authorization: Bearer secret-value "refresh_token":"1//supersecret"')
        lifecycle = Lifecycle()
        output = handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(),
            run_reserver=lambda *_args: True, lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=Broken,
        )
        combined = output + json.dumps(lifecycle.blocked)
        self.assertNotIn("secret-value", combined)
        self.assertNotIn("supersecret", combined)
        self.assertIn("safe capability error", combined)

    def test_arbitrary_provider_error_text_never_reaches_result_or_blocker(self):
        class Broken(Workflows):
            def read(self, operation, window):
                raise RuntimeError(
                    "/Users/hermes/private/token.json PRIVATE_EVENT_TITLE "
                    "AIzaSyExampleUnmatched"
                )
        lifecycle = Lifecycle()
        output = handle_pa_calendar_execute(
            {}, environ=environ(), task_loader=lambda *_args: task_record(),
            run_reserver=lambda *_args: True, lifecycle_factory=lambda _task_id: lifecycle,
            workflow_runner_factory=Broken,
        )
        combined = output + json.dumps(lifecycle.blocked)
        for forbidden in ("token.json", "PRIVATE_EVENT_TITLE", "AIzaSyExampleUnmatched"):
            self.assertNotIn(forbidden, combined)
        self.assertIn("safe capability error", combined)

    def test_pa_plugin_contains_no_linear_client_or_credentials(self):
        source = "\n".join(path.read_text() for path in (ROOT / "plugins" / "personal_assistant_calendar").glob("*.py"))
        for forbidden in ("LINEAR_TOKEN", "api.linear.app", "LinearClient", "mcp.linear"):
            self.assertNotIn(forbidden, source)

    def test_worker_manifest_and_read_workflow_are_bounded(self):
        manifest = (ROOT / "plugins" / "personal_assistant_calendar" / "plugin.yaml").read_text()
        self.assertIn("pa_calendar_execute", manifest)
        import yaml
        workflow = yaml.safe_load((ROOT / "workflows" / "workflow-google-calendar-read.yaml").read_text())
        self.assertEqual(workflow["inputs"]["operation"]["enum"], ["inventory", "events", "freebusy"])
        command_text = workflow["jobs"][0]["steps"][0]["task"]["inputs"]["run"]
        self.assertIn("--operation '${{ inputs.operation }}'", command_text)
        self.assertIn("--live", command_text)
        self.assertNotIn("calendarId", json.dumps(workflow))

    def test_swamp_adapter_uses_immutable_runtime_checkout(self):
        self.assertEqual(
            SwampCalendarWorkflows().workspace,
            Path("/Users/hermes/workspaces/swamp-ops-runtime"),
        )

    def test_runtime_verifier_requires_attested_head_clean_tree_and_no_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "runtime"
            workspace.mkdir()
            revision_file = Path(tmp) / "runtime-revision"
            revision = "a" * 40
            revision_file.write_text(revision + "\n")
            calls = []

            class Completed:
                def __init__(self, returncode=0, stdout=""):
                    self.returncode = returncode
                    self.stdout = stdout
                    self.stderr = ""

            def runner(argv, **kwargs):
                calls.append((argv, kwargs))
                if argv[-2:] == ["rev-parse", "HEAD"]:
                    return Completed(stdout=revision + "\n")
                return Completed(stdout="")

            _verify_runtime_workspace(
                workspace, revision_file, runner=runner,
                expected_workspace=workspace,
            )
            self.assertEqual(len(calls), 2)

            (workspace / ".swamp-sources.yaml").write_text("sources: []\n")
            with self.assertRaisesRegex(Exception, "override"):
                _verify_runtime_workspace(
                    workspace, revision_file, runner=runner,
                    expected_workspace=workspace,
                )

    def test_swamp_adapter_extracts_unique_result_artifact_from_real_run_shape(self):
        run = {
            "id": PLAN_RUN,
            "status": "succeeded",
            "workflowName": "google-calendar-write-plan",
            "jobs": [{
                "name": "plan",
                "steps": [{
                    "name": "build-plan",
                    "dataArtifacts": [{"name": "result", "version": 17}],
                }],
            }],
        }
        self.assertEqual(SwampCalendarWorkflows._artifact_version(run), 17)

    def test_result_artifact_rejects_wrong_workflow_before_artifact_access(self):
        adapter = SwampCalendarWorkflows()
        adapter._json = mock.Mock()
        with self.assertRaisesRegex(Exception, "workflow"):
            adapter._result_artifact(
                "google-calendar-write",
                "google-calendar-write-plan",
                {
                    "id": PLAN_RUN,
                    "status": "succeeded",
                    "workflowName": "untrusted-workflow",
                    "jobs": [{"steps": [{"dataArtifacts": [{"name": "result", "version": 1}]}]}],
                },
            )
        adapter._json.assert_not_called()

    def test_swamp_adapter_requires_approve_response_to_echo_exact_run(self):
        adapter = SwampCalendarWorkflows()
        adapter._json = mock.Mock(return_value={"runId": "44444444-4444-4444-8444-444444444444"})
        with self.assertRaisesRegex(Exception, "wrong run"):
            adapter.approve(APPROVAL_RUN)

    def test_swamp_apply_passes_the_approved_before_state_hash(self):
        apply_run = "55555555-5555-4555-8555-555555555555"
        adapter = SwampCalendarWorkflows()
        adapter._json = mock.Mock(side_effect=[
            {
                "id": apply_run, "status": "succeeded",
                "workflowName": "google-calendar-write-apply",
                "jobs": [{"steps": [{"dataArtifacts": [{"name": "result", "version": 9}]}]}],
            },
            {
                "modelName": "google-calendar-write", "name": "result", "version": 9,
                "ownerDefinition": {
                    "workflowRunId": apply_run,
                    "workflowName": "google-calendar-write-apply",
                },
                "content": {"exitCode": 0, "stdout": json.dumps({
                    "operation": "create", "status": "verified", "reused": False,
                    "linearIssue": "SIS-123", "blockKey": "primary",
                })},
            },
        ])
        adapter.apply(
            approval_plan("calendar-approval:v1:" + "c" * 64)["plan_reference"],
            {"run_id": APPROVAL_RUN, "artifact_version": 8, "checksum": "b" * 64},
        )
        argv = adapter._json.call_args_list[0].args[0]
        self.assertIn("beforeStateHash=" + "d" * 64, argv)


if __name__ == "__main__":
    unittest.main()
