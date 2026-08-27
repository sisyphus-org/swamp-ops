import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "hermes_profile_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("hermes_profile_bootstrap", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import bootstrap script: {SCRIPT}")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapContractTests(unittest.TestCase):
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
        self.assertIn('cwd: "/Users/hermes/workspaces"', rendered)
        self.assertIn("laguna-s-2.1-free", rendered)
        self.assertIn("nemotron-3.5-lightning-free", rendered)
        self.assertIn("https://mcp.linear.app/mcp", rendered)

    def test_shared_secrets_and_profile_allowlist_override_contract(self):
        rendered = bootstrap.render_config(
            "openai-codex",
            "gpt-5.6-sol-900k",
            Path("/Users/hermes/workspaces"),
        )
        self.assertIn(
            "grep '^LINEAR_TOKEN=' /Users/hermes/.hermes/.env",
            rendered,
        )
        self.assertIn(
            "if ! grep -q '^TELEGRAM_ALLOWED_USERS=' \"${HERMES_HOME}/.env\"",
            rendered,
        )
        self.assertIn(
            "grep '^TELEGRAM_ALLOWED_USERS=' /Users/hermes/.hermes/.env",
            rendered,
        )
        self.assertIn('Authorization: "Bearer ${LINEAR_TOKEN}"', rendered)
        self.assertEqual(
            [item["name"] for item in bootstrap.SHARED_ENV_VARS],
            ["LINEAR_TOKEN", "TELEGRAM_ALLOWED_USERS"],
        )
        self.assertNotIn(
            "LINEAR_TOKEN",
            [item["name"] for item in bootstrap.PROFILE_ENV_VARS],
        )
        self.assertNotIn(
            "TELEGRAM_ALLOWED_USERS",
            [item["name"] for item in bootstrap.PROFILE_ENV_VARS],
        )
        self.assertIn(
            "TELEGRAM_ALLOWED_USERS",
            [item["name"] for item in bootstrap.OPTIONAL_PROFILE_ENV_VARS],
        )

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
        self.assertFalse(payload["linear"]["profileTokenRequired"])
        self.assertFalse(payload["telegramAllowlist"]["profileValueRequired"])
        self.assertTrue(payload["telegramAllowlist"]["profileOverrideSupported"])
        self.assertEqual(
            {item["name"] for item in payload["requiredProfileEnv"]},
            {"TELEGRAM_BOT_TOKEN"},
        )
        self.assertFalse(profile_dir.exists())

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


if __name__ == "__main__":
    unittest.main()
