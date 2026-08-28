import importlib.util
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


SCRIPT = Path(__file__).parents[1] / "scripts" / "chunked_stt_ops.py"
SPEC = importlib.util.spec_from_file_location("chunked_stt_ops", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import chunked STT ops script: {SCRIPT}")
ops = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ops
SPEC.loader.exec_module(ops)


class InputContractTests(unittest.TestCase):
    def test_sample_slug_accepts_bounded_name(self):
        self.assertEqual(ops.validate_sample_slug("books-7m"), "books-7m")

    def test_sample_slug_rejects_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "sample"):
            ops.validate_sample_slug("../books-7m")

    def test_numeric_aiff_sample_is_supported(self):
        old_root = ops.SAMPLE_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            samples = Path(tmp) / "samples"
            samples.mkdir()
            expected = samples / "sis69-numbers.aiff"
            expected.write_bytes(b"fixture")
            setattr(ops, "SAMPLE_ROOT", samples)
            try:
                self.assertEqual(
                    ops.resolve_sample("sis69-numbers"), expected.resolve()
                )
            finally:
                setattr(ops, "SAMPLE_ROOT", old_root)

    def test_sample_symlink_outside_runtime_root_is_rejected(self):
        old_root = ops.SAMPLE_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            samples = root / "samples"
            samples.mkdir()
            outside = root / "outside.ogg"
            outside.write_bytes(b"private")
            (samples / "books-7m.ogg").symlink_to(outside)
            setattr(ops, "SAMPLE_ROOT", samples)
            try:
                with self.assertRaisesRegex(RuntimeError, "symlink|outside"):
                    ops.resolve_sample("books-7m")
            finally:
                setattr(ops, "SAMPLE_ROOT", old_root)

    def test_symlinked_runtime_parent_is_rejected_for_input_and_output(self):
        old_sample_root = ops.SAMPLE_ROOT
        old_output_root = ops.SMOKE_OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            link = root / "runtime-link"
            link.symlink_to(outside, target_is_directory=True)
            (outside / "stt-samples").mkdir()
            (outside / "stt-samples" / "books-7m.ogg").write_bytes(b"private")
            setattr(ops, "SAMPLE_ROOT", link / "stt-samples")
            setattr(ops, "SMOKE_OUTPUT_ROOT", link / "stt-smoke")
            try:
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    ops.resolve_sample("books-7m")
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    ops.validated_runtime_directory(
                        ops.SMOKE_OUTPUT_ROOT,
                        label="smoke output root",
                        create=True,
                    )
            finally:
                setattr(ops, "SAMPLE_ROOT", old_sample_root)
                setattr(ops, "SMOKE_OUTPUT_ROOT", old_output_root)


class AuditTests(unittest.TestCase):
    def test_numeric_prompt_is_versioned_and_covers_required_formats(self):
        prompt = ops.read_desired_prompt()
        for example in (
            "25",
            "-7",
            "3,5",
            "12%",
            "28 августа 2026",
            "14:30",
            "3.2",
            "1200 рублей",
        ):
            with self.subTest(example=example):
                self.assertIn(example, prompt)

    def test_launchdaemon_prompt_reader_returns_exact_environment_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist_path = Path(tmp) / "local.qwen-stt.plist"
            plist_path.write_bytes(
                plistlib.dumps(
                    {"EnvironmentVariables": {"HERMES_STT_VOCAB": "numeric prompt"}}
                )
            )
            self.assertEqual(ops.read_launchdaemon_prompt(plist_path), "numeric prompt")

    def test_plan_requires_owner_rollout_when_qwen_prompt_differs(self):
        desired = ops.desired_command(Path("/runtime/chunked_qwen_stt.py"))
        payload = ops.build_plan(
            profile_commands={"default": desired, "books": desired},
            deployed_script=Path("/runtime/chunked_qwen_stt.py"),
            source_script=Path("/source/chunked_qwen_stt.py"),
            deployed_hash="same",
            source_hash="same",
            desired_prompt="write numbers as digits",
            live_prompt=None,
        )
        self.assertEqual(payload["result"], "changes_required")
        self.assertFalse(payload["qwenPromptCompliant"])
        self.assertIn(
            {
                "type": "set-qwen-prompt",
                "launchDaemon": str(ops.QWEN_LAUNCHDAEMON),
                "environmentVariable": "HERMES_STT_VOCAB",
                "ownerRequired": True,
            },
            payload["plannedActions"],
        )

    def test_plan_reports_exact_noncompliant_profiles_without_writing(self):
        desired = ops.desired_command(Path("/runtime/chunked_qwen_stt.py"))
        commands = {
            "default": "/old/python /old/qwen3_stt.py {input_path} {output_path}",
            "books": desired,
        }
        payload = ops.build_plan(
            profile_commands=commands,
            deployed_script=Path("/runtime/chunked_qwen_stt.py"),
            source_script=Path("/source/chunked_qwen_stt.py"),
            deployed_hash=None,
            source_hash="abc123",
        )
        self.assertEqual(payload["mode"], "plan")
        self.assertEqual(payload["result"], "changes_required")
        self.assertEqual(payload["noncompliantProfiles"], ["default"])
        self.assertEqual(payload["desiredCommand"], desired)
        self.assertFalse(Path("/runtime/chunked_qwen_stt.py").exists())

    def test_all_matching_profiles_are_compliant(self):
        desired = ops.desired_command(Path("/runtime/chunked_qwen_stt.py"))
        payload = ops.build_plan(
            profile_commands={"default": desired, "books": desired},
            deployed_script=Path("/runtime/chunked_qwen_stt.py"),
            source_script=Path("/source/chunked_qwen_stt.py"),
            deployed_hash="same",
            source_hash="same",
        )
        self.assertEqual(payload["result"], "compliant")
        self.assertEqual(payload["noncompliantProfiles"], [])

    def test_unreadable_profile_is_reported_without_write_plan(self):
        desired = ops.desired_command(Path("/runtime/chunked_qwen_stt.py"))
        payload = ops.build_plan(
            profile_commands={"default": None, "books": desired},
            deployed_script=Path("/runtime/chunked_qwen_stt.py"),
            source_script=Path("/source/chunked_qwen_stt.py"),
            deployed_hash="same",
            source_hash="same",
        )
        self.assertEqual(payload["result"], "error")
        self.assertEqual(payload["unreadableProfiles"], ["default"])
        self.assertEqual(payload["noncompliantProfiles"], [])
        self.assertFalse(
            any(
                action.get("profile") == "default"
                for action in payload["plannedActions"]
            )
        )

    def test_metrics_parser_ignores_trailing_diagnostics(self):
        metrics = ops.parse_metrics(
            "warning before\n"
            '{"durationSeconds":426.22,"chunkCount":3,"ok":true}\n'
            "warning after\n"
        )
        self.assertEqual(metrics["durationSeconds"], 426.22)
        self.assertEqual(metrics["chunkCount"], 3)


class NumericTranscriptTests(unittest.TestCase):
    def test_numeric_smoke_accepts_digit_formatted_transcript(self):
        transcript = (
            "В заказе 25 деталей. Температура -7 градусов. Расход 3,5 литра. "
            "Скидка 12%. "
            "Встреча 28 августа 2026 года в 14:30. Версия 3.2. "
            "Сумма 1200 рублей."
        )
        self.assertEqual(ops.validate_numeric_transcript(transcript), [])

    def test_numeric_smoke_reports_missing_formats_and_number_words(self):
        issues = ops.validate_numeric_transcript(
            "В заказе двадцать пять деталей. Расход три целых пять десятых "
            "литра. Скидка двенадцать процентов."
        )
        self.assertIn("missing:quantity-25", issues)
        self.assertIn("missing:decimal-3-5", issues)
        self.assertIn("missing:percent-12", issues)
        self.assertIn("number-words-remain", issues)


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_has_bounded_mode_and_fixed_sample_slug(self):
        workflow_path = (
            SCRIPT.parents[1] / "workflows" / "workflow-chunked-qwen-stt.yaml"
        )
        parsed = yaml.safe_load(workflow_path.read_text())
        self.assertEqual(
            parsed["inputs"]["mode"]["enum"],
            ["plan", "smoke", "numeric-smoke"],
        )
        self.assertNotIn("sample", parsed["inputs"])
        command = parsed["jobs"][0]["steps"][0]["task"]["inputs"]["run"]
        self.assertIn("--mode '${{ inputs.mode }}'", command)
        self.assertIn("--sample books-7m", command)
        self.assertIn("--numeric-sample sis69-numbers", command)
        self.assertNotIn("inputs.sample", command)
        self.assertEqual(
            parsed["jobs"][0]["steps"][0]["task"]["inputs"]["workingDir"],
            ".",
        )
        self.assertNotIn("input_path", workflow_path.read_text())


if __name__ == "__main__":
    unittest.main()
