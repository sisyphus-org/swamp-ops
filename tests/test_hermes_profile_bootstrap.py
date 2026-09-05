import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


SCRIPT = Path(__file__).parents[1] / "scripts" / "hermes_profile_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("hermes_profile_bootstrap", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import bootstrap script: {SCRIPT}")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapContractTests(unittest.TestCase):
    def test_calendar_source_skill_requires_literal_user_identifiers(self):
        skill = " ".join(
            (
                SCRIPT.parents[1]
                / "skills"
                / "calendar-source-request-routing"
                / "SKILL.md"
            ).read_text().split()
        )
        self.assertIn("copy it byte-for-byte", skill)
        self.assertIn("Never shorten, expand, repair, or regenerate a Linear URL slug", skill)
        self.assertIn("Never derive `block_key` from the SIS identifier", skill)
        self.assertIn("report that validation error or ask the owner for a replacement; do not substitute", skill)
        self.assertIn("compare both outgoing values with the owner's message", skill)
        self.assertIn("The preview must preserve them exactly; otherwise do not ask for approval", skill)

    def test_default_model_is_sol_900k(self):
        self.assertEqual(
            bootstrap.DEFAULT_MODEL,
            "openai-codex/gpt-5.6-sol-900k",
        )

    def test_rendered_baseline_is_valid_and_complete(self):
        rendered = bootstrap.render_config(
            "openai-codex",
            "gpt-5.6-sol-900k",
            Path("/Users/hermes/workspaces"),
        )
        ok, error = bootstrap.validate_yaml_text(rendered)
        self.assertTrue(ok, error)
        self.assertIn('default: "gpt-5.6-sol-900k"', rendered)
        self.assertIn("provider: qwen3", rendered)
        self.assertIn("language: ru", rendered)
        self.assertIn(bootstrap.QWEN3_STT_COMMAND, rendered)
        self.assertIn(
            "/Users/hermes/workspaces/runtime/hermes-stt/chunked_qwen_stt.py",
            bootstrap.QWEN3_STT_COMMAND,
        )
        self.assertIn('cwd: "/Users/hermes/workspaces"', rendered)
        self.assertIn("laguna-s-2.1-free", rendered)
        self.assertIn("nemotron-3.5-lightning-free", rendered)
        self.assertNotIn("https://mcp.linear.app/mcp", rendered)
        self.assertIn("linear-source-route", rendered)
        self.assertIn("dispatch_in_gateway: false", rendered)
        self.assertEqual(yaml.safe_load(rendered)["_config_version"], 38)

    def test_broker_role_is_headless_and_has_no_linear_mcp(self):
        rendered = bootstrap.render_config(
            "openai-codex",
            "gpt-5.6-sol-900k",
            Path("/Users/hermes/workspaces"),
            role="broker",
        )
        parsed = yaml.safe_load(rendered)
        self.assertFalse(parsed["gateway"]["platforms"]["telegram"]["enabled"])
        self.assertFalse(parsed["kanban"]["dispatch_in_gateway"])
        self.assertNotIn("mcp_servers", parsed)
        self.assertNotIn("secrets", parsed)

    def test_personal_assistant_role_is_calendar_only_and_has_no_linear_access(self):
        rendered = bootstrap.render_config(
            "openai-codex",
            "gpt-5.6-sol-900k",
            Path("/Users/hermes/workspaces"),
            role="personal-assistant",
        )
        parsed = yaml.safe_load(rendered)
        self.assertFalse(parsed["gateway"]["platforms"]["telegram"]["enabled"])
        self.assertFalse(parsed["kanban"]["dispatch_in_gateway"])
        self.assertNotIn("mcp_servers", parsed)
        self.assertNotIn("secrets", parsed)
        self.assertEqual(parsed["plugins"]["enabled"], ["personal-assistant-calendar"])
        self.assertNotIn("LINEAR_TOKEN", rendered)

        profile = "personal-assistant"
        old_argv = sys.argv
        old_root = bootstrap.HERMES_ROOT
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bootstrap.HERMES_ROOT = Path(tmp)  # type: ignore[attr-defined]
                sys.argv = [
                    str(SCRIPT),
                    "--profile",
                    profile,
                    "--role",
                    "personal-assistant",
                    "--mode",
                    "plan",
                ]
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(bootstrap.main(), 0)
                payload = json.loads(output.getvalue())
        finally:
            bootstrap.HERMES_ROOT = old_root  # type: ignore[attr-defined]
            sys.argv = old_argv
        self.assertTrue(payload["telegram"]["explicitlyDisabled"])
        self.assertTrue(payload["calendarRouting"]["enabled"])
        self.assertEqual(payload["calendarRouting"]["workerProfile"], "personal-assistant")
        self.assertFalse(payload["calendarRouting"]["directLinearAccessAvailable"])

    def test_personal_assistant_role_rejects_noncanonical_profile_name(self):
        old_argv = sys.argv
        old_root = bootstrap.HERMES_ROOT
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bootstrap.HERMES_ROOT = Path(tmp) / "profiles"
                sys.argv = [
                    str(SCRIPT), "--profile", "pa-fixture",
                    "--role", "personal-assistant", "--mode", "plan",
                ]
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(bootstrap.main(), 1)
                payload = json.loads(output.getvalue())
        finally:
            bootstrap.HERMES_ROOT = old_root
            sys.argv = old_argv
        self.assertIn("canonical profile name", payload["issues"][0])

    def test_general_source_plan_exposes_calendar_route_without_google_credentials(self):
        rendered = bootstrap.render_config(
            "openai-codex", "gpt-5.6-sol-900k", Path("/Users/hermes/workspaces")
        )
        parsed = yaml.safe_load(rendered)
        self.assertIn("linear-source-route", parsed["plugins"]["enabled"])
        for forbidden in ("GOOGLE_CLIENT_SECRET", "GOOGLE_APPLICATION_CREDENTIALS", "token.json", "googleapiclient"):
            self.assertNotIn(forbidden, rendered)

    def test_personal_assistant_scripts_do_not_implement_direct_linear_access(self):
        scripts = SCRIPT.parents[1] / "scripts"
        forbidden = ("api.linear.app", "LINEAR_TOKEN", "_linear_graphql")
        violations = []
        for pattern in ("google_calendar*.py", "calendar_creds.py", "task_and_calendar*.py"):
            for path in sorted(scripts.glob(pattern)):
                source = path.read_text()
                for marker in forbidden:
                    if marker in source:
                        violations.append(f"{path.name}:{marker}")
        self.assertEqual(violations, [])

    def test_source_profile_has_no_linear_secret_or_mcp_and_pm_keeps_them(self):
        source = bootstrap.render_config(
            "openai-codex",
            "gpt-5.6-sol-900k",
            Path("/Users/hermes/workspaces"),
        )
        parsed_source = yaml.safe_load(source)
        self.assertFalse(
            parsed_source["gateway"]["platforms"]["telegram"]["enabled"]
        )
        self.assertNotIn("mcp_servers", parsed_source)
        source_command = parsed_source["secrets"]["command"]["command"]
        self.assertNotIn("LINEAR_TOKEN", source_command)
        self.assertIn("TELEGRAM_ALLOWED_USERS", source_command)
        self.assertIn("linear-source-route", parsed_source["plugins"]["enabled"])

        pm = bootstrap.render_config(
            "openai-codex",
            "gpt-5.6-sol-900k",
            Path("/Users/hermes/workspaces"),
            role="project-manager",
        )
        parsed_pm = yaml.safe_load(pm)
        self.assertIn("linear", parsed_pm["mcp_servers"])
        self.assertNotIn("secrets", parsed_pm)
        self.assertIn('Authorization: "Bearer ${LINEAR_TOKEN}"', pm)

    def test_project_manager_config_requires_profile_local_linear_token(self):
        rendered = bootstrap.render_config(
            "openai-codex",
            "gpt-5.6-sol-900k",
            Path("/Users/hermes/workspaces"),
            role="project-manager",
        )
        parsed = yaml.safe_load(rendered)
        self.assertNotIn("secrets", parsed)
        self.assertIn("${LINEAR_TOKEN}", parsed["mcp_servers"]["linear"]["headers"]["Authorization"])
        self.assertNotIn(str(bootstrap.SHARED_ENV), rendered)

    def test_plan_lists_owner_variables_without_writing(self):
        profile = "bootstrap-contract-test"
        profile_dir = bootstrap.HERMES_ROOT / profile
        self.assertFalse(profile_dir.exists())
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--profile", profile, "--mode", "plan"],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"], "would_create")
        self.assertEqual(
            payload["primaryModel"],
            "gpt-5.6-sol-900k (via openai-codex)",
        )
        self.assertFalse(payload["linear"]["enabled"])
        self.assertFalse(payload["linear"]["profileOverrideSupported"])
        self.assertTrue(payload["sourceRouting"]["enabled"])
        self.assertEqual(payload["sourceRouting"]["plugin"], "linear-source-route")
        self.assertIn("restart broker", " ".join(payload["verificationGates"]).lower())
        self.assertFalse(payload["telegramAllowlist"]["profileValueRequired"])
        self.assertTrue(payload["telegramAllowlist"]["profileOverrideSupported"])
        self.assertEqual(
            {item["name"] for item in payload["requiredProfileEnv"]},
            {"TELEGRAM_BOT_TOKEN"},
        )
        self.assertFalse(profile_dir.exists())

    def test_project_manager_plan_requires_unique_token_before_activation(self):
        profile = "project-manager-contract-test"
        profile_dir = bootstrap.HERMES_ROOT / profile
        self.assertFalse(profile_dir.exists())
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--profile",
                profile,
                "--role",
                "project-manager",
                "--mode",
                "plan",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["role"], "project-manager")
        self.assertFalse(payload["telegram"]["enabledAtBootstrap"])
        self.assertFalse(payload["telegram"]["preparedForOwnerToken"])
        self.assertTrue(payload["linear"]["enabled"])
        self.assertTrue(payload["linear"]["profileTokenRequired"])
        self.assertIsNone(payload["linear"]["sharedTokenSource"])
        self.assertEqual(
            {item["name"] for item in payload["requiredProfileEnv"]},
            {"LINEAR_TOKEN"},
        )
        self.assertEqual(
            payload["verificationGates"],
            [
                "run config check and a real model response",
                "run a real Russian STT transcription",
                "verify profile-local LINEAR_TOKEN presence without exposing its value",
                "verify a real Project Manager Linear read with exact read-back",
            ],
        )
        self.assertFalse(profile_dir.exists())

    def test_broker_plan_requires_no_shared_or_profile_secrets(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--profile",
                "broker-contract-test",
                "--role",
                "broker",
                "--mode",
                "plan",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["role"], "broker")
        self.assertEqual(payload["requiredSharedEnv"], [])
        self.assertEqual(payload["requiredProfileEnv"], [])
        self.assertFalse(payload["linear"]["enabled"])
        self.assertFalse(payload["telegram"]["preparedForOwnerToken"])
        self.assertFalse(payload["telegramAllowlist"]["profileOverrideSupported"])
        self.assertIsNone(payload["telegramAllowlist"]["sharedSource"])

    def test_rejects_invalid_profile_name(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--profile", "BAD", "--mode", "plan"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["result"], "error")

    def test_rejects_workspace_outside_workspaces_root(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--profile",
                "valid-name",
                "--mode",
                "plan",
                "--workspace",
                "/tmp",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must be inside", json.loads(proc.stdout)["issues"][0])

    def test_rejects_workspace_traversal(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--profile",
                "valid-name",
                "--mode",
                "plan",
                "--workspace",
                "/Users/hermes/workspaces/../.hermes",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must be inside", json.loads(proc.stdout)["issues"][0])

    def test_rejects_model_yaml_injection(self):
        with self.assertRaises(ValueError):
            bootstrap.parse_model("openai-codex/safe\nsecurity:\n  injected: true")

    def test_env_has_key_matches_runtime_grep_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("  TELEGRAM_ALLOWED_USERS=indented\n")
            self.assertFalse(
                bootstrap.env_has_key(env_file, "TELEGRAM_ALLOWED_USERS")
            )
            env_file.write_text("TELEGRAM_ALLOWED_USERS=exact\n")
            self.assertTrue(
                bootstrap.env_has_key(env_file, "TELEGRAM_ALLOWED_USERS")
            )

    def test_model_and_provider_remain_strings_after_yaml_parse(self):
        for value in ("openai/true", "openai/null", "openai/123", "openai/2026-08-27"):
            provider, model = bootstrap.parse_model(value)
            rendered = bootstrap.render_config(
                provider,
                model,
                Path("/Users/hermes/workspaces"),
            )
            proc = subprocess.run(
                [
                    str(bootstrap.HERMES_PYTHON),
                    "-c",
                    "import json,sys,yaml; print(json.dumps(yaml.safe_load(sys.stdin.read())['model']))",
                ],
                input=rendered,
                capture_output=True,
                text=True,
                check=True,
            )
            parsed = json.loads(proc.stdout)
            self.assertIsInstance(parsed["provider"], str)
            self.assertIsInstance(parsed["default"], str)
            self.assertEqual(parsed, {"provider": provider, "default": model})

    def test_rejects_workspace_control_characters(self):
        with self.assertRaises(ValueError):
            bootstrap.validate_workspace("/Users/hermes/workspaces/safe\nunsafe")

    def test_workflow_does_not_interpolate_free_form_shell_inputs(self):
        workflow = (
            SCRIPT.parents[1] / "workflows" / "workflow-hermes-profile-bootstrap.yaml"
        ).read_text()
        self.assertNotIn("inputs.model", workflow)
        self.assertNotIn("inputs.workspace", workflow)
        self.assertIn("--profile '${{ inputs.profile }}'", workflow)

    def test_workflow_routes_a_bounded_role_input(self):
        workflow_path = (
            SCRIPT.parents[1] / "workflows" / "workflow-hermes-profile-bootstrap.yaml"
        )
        parsed = yaml.safe_load(workflow_path.read_text())
        self.assertEqual(
            parsed["inputs"]["role"]["enum"],
            ["general", "broker", "personal-assistant", "project-manager"],
        )
        command = parsed["jobs"][0]["steps"][0]["task"]["inputs"]["run"]
        self.assertIn("--role '${{ inputs.role }}'", command)

    def test_apply_writes_only_fixture_root_and_refuses_overwrite(self):
        old_root = getattr(bootstrap, "HERMES_ROOT")
        old_shared_env = getattr(bootstrap, "SHARED_ENV")
        old_argv = sys.argv
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            root = fixture / "profiles"
            shared_env = fixture / ".env"
            shared_env.write_text(
                f"{'LINEAR_TOKEN'}=fixture-value\n"
                f"{'TELEGRAM_ALLOWED_USERS'}=fixture-user\n"
            )
            setattr(bootstrap, "HERMES_ROOT", root)
            setattr(bootstrap, "SHARED_ENV", shared_env)
            try:
                sys.argv = [
                    str(SCRIPT),
                    "--profile",
                    "fixture-profile",
                    "--mode",
                    "apply",
                ]
                first_out = io.StringIO()
                with contextlib.redirect_stdout(first_out):
                    first_code = bootstrap.main()
                self.assertEqual(first_code, 0)
                profile = root / "fixture-profile"
                self.assertTrue((profile / "config.yaml").is_file())
                self.assertTrue((profile / "plugins" / "linear_source_route" / "plugin.yaml").is_file())
                self.assertTrue((profile / "skills" / "linear-source-request-routing" / "SKILL.md").is_file())
                self.assertFalse((profile / ".env").exists())
                self.assertEqual((profile / "config.yaml").stat().st_mode & 0o777, 0o600)

                second_out = io.StringIO()
                with contextlib.redirect_stdout(second_out):
                    second_code = bootstrap.main()
                self.assertNotEqual(second_code, 0)
                self.assertEqual(json.loads(second_out.getvalue())["result"], "error")
            finally:
                setattr(bootstrap, "HERMES_ROOT", old_root)
                setattr(bootstrap, "SHARED_ENV", old_shared_env)
                sys.argv = old_argv
    def test_apply_installs_personal_assistant_worker_plugin_and_skill(self):
        old_root = bootstrap.HERMES_ROOT
        old_argv = sys.argv
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bootstrap.HERMES_ROOT = Path(tmp) / "profiles"
                sys.argv = [
                    str(SCRIPT), "--profile", "personal-assistant", "--role", "personal-assistant", "--mode", "apply"
                ]
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(bootstrap.main(), 0)
                profile = bootstrap.HERMES_ROOT / "personal-assistant"
                self.assertTrue((profile / "plugins" / "personal_assistant_calendar" / "plugin.yaml").is_file())
                self.assertTrue((profile / "skills" / "personal-assistant-calendar-worker" / "SKILL.md").is_file())
                self.assertFalse((profile / ".env").exists())
                payload = json.loads(output.getvalue())
                self.assertTrue(payload["calendarRouting"]["enabled"])
        finally:
            bootstrap.HERMES_ROOT = old_root
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
