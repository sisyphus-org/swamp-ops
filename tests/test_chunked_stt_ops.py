import importlib.util
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


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_has_bounded_mode_and_sample_slug(self):
        workflow_path = (
            SCRIPT.parents[1] / "workflows" / "workflow-chunked-qwen-stt.yaml"
        )
        parsed = yaml.safe_load(workflow_path.read_text())
        self.assertEqual(parsed["inputs"]["mode"]["enum"], ["plan", "smoke"])
        self.assertEqual(parsed["inputs"]["sample"]["pattern"], "^[a-z0-9][a-z0-9-]{0,63}$")
        command = parsed["jobs"][0]["steps"][0]["task"]["inputs"]["run"]
        self.assertIn("--mode '${{ inputs.mode }}'", command)
        self.assertIn("--sample '${{ inputs.sample }}'", command)
        self.assertEqual(
            parsed["jobs"][0]["steps"][0]["task"]["inputs"]["workingDir"],
            ".",
        )
        self.assertNotIn("input_path", workflow_path.read_text())


if __name__ == "__main__":
    unittest.main()
