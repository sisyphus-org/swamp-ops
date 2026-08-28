import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "kanban_dispatcher_audit.py"
SPEC = importlib.util.spec_from_file_location("kanban_dispatcher_audit", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import audit script: {SCRIPT}")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class DispatcherConfigTests(unittest.TestCase):
    def test_parse_dispatch_flag_requires_explicit_boolean(self):
        self.assertIs(audit.parse_dispatch_flag("kanban:\n  dispatch_in_gateway: true\n"), True)
        self.assertIs(audit.parse_dispatch_flag("kanban:\n  dispatch_in_gateway: false\n"), False)
        self.assertIsNone(audit.parse_dispatch_flag("kanban:\n  interval: 60\n"))
        self.assertIsNone(
            audit.parse_dispatch_flag(
                "kanban:\n  worker:\n    dispatch_in_gateway: false\n"
            )
        )
        self.assertIsNone(audit.parse_dispatch_flag("kanban: [unterminated\n"))

    def test_audit_configs_requires_only_broker_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profiles" / "broker").mkdir(parents=True)
            (root / "profiles" / "swe").mkdir(parents=True)
            (root / "config.yaml").write_text(
                "kanban:\n  dispatch_in_gateway: false\n"
            )
            (root / "profiles" / "broker" / "config.yaml").write_text(
                "kanban:\n  dispatch_in_gateway: true\n"
            )
            (root / "profiles" / "swe" / "config.yaml").write_text(
                "kanban:\n  dispatch_in_gateway: false\n"
            )
            result = audit.audit_configs(root, ["default", "broker", "swe"])
            self.assertTrue(result["valid"])
            self.assertEqual(result["enabled"], ["broker"])
            self.assertEqual(result["missing_explicit"], [])

    def test_profile_from_gateway_command(self):
        self.assertEqual(
            audit.profile_from_gateway_command(
                "python -m hermes_cli.main --profile broker gateway run"
            ),
            "broker",
        )
        self.assertEqual(
            audit.profile_from_gateway_command(
                "python -m hermes_cli.main gateway run --replace"
            ),
            "default",
        )
        self.assertIsNone(audit.profile_from_gateway_command("python worker.py"))

    def test_audit_lock_requires_one_expected_gateway_owner(self):
        def fake_run(argv, **kwargs):
            if argv[0] == "lsof":
                return SimpleNamespace(returncode=0, stdout="123\n", stderr="")
            self.assertEqual(argv[:3], ["ps", "-p", "123"])
            return SimpleNamespace(
                returncode=0,
                stdout="python -m hermes_cli.main --profile broker gateway run\n",
                stderr="",
            )

        result = audit.audit_lock(Path("/fixed/dispatcher.lock"), "broker", fake_run)
        self.assertTrue(result["valid"])
        self.assertEqual(result["owner_profile"], "broker")
        self.assertEqual(result["pids"], [123])

    def test_build_report_requires_config_and_lock_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profiles" / "broker").mkdir(parents=True)
            (root / "config.yaml").write_text(
                "kanban:\n  dispatch_in_gateway: false\n"
            )
            (root / "profiles" / "broker" / "config.yaml").write_text(
                "kanban:\n  dispatch_in_gateway: true\n"
            )
            report = audit.build_report(
                root,
                ["default", "broker"],
                "broker",
                lambda *_: {"valid": True, "owner_profile": "broker", "pids": [123]},
            )
            self.assertEqual(report["result"], "pass")
            self.assertTrue(report["readOnly"])

    def test_main_exit_status_tracks_report_result(self):
        for result, expected in (("pass", 0), ("drift", 1)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = audit.main(lambda *_args, value=result: {"result": value})
            self.assertEqual(code, expected)
            self.assertIn(f'"result": "{result}"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
