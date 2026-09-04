import importlib.util
import sys
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "calendar_creds.py"
SPEC = importlib.util.spec_from_file_location("calendar_creds", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import calendar_creds script: {SCRIPT}")
creds = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = creds
SPEC.loader.exec_module(creds)


class ScopeValidationTests(unittest.TestCase):
    def test_allowed_scopes_contain_only_calendar(self):
        expected = {
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.freebusy",
        }
        self.assertEqual(set(creds.ALLOWED_SCOPES), expected)

    def test_no_non_calendar_api_scopes_are_allowed(self):
        non_calendar = [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/contacts.readonly",
        ]
        for s in non_calendar:
            with self.subTest(scope=s):
                self.assertIn(s, creds.FORBIDDEN_SCOPES)

    def test_validate_scopes_rejects_extra_scope(self):
        with self.assertRaisesRegex(ValueError, "extra scopes"):
            creds.validate_scopes(list(creds.ALLOWED_SCOPES) + ["https://www.googleapis.com/auth/userinfo.email"])

    def test_validate_scopes_rejects_forbidden_scope(self):
        with self.assertRaisesRegex(ValueError, "forbidden"):
            creds.validate_scopes(["https://www.googleapis.com/auth/gmail.modify"])

    def test_validate_scopes_accepts_exact_allowed(self):
        # Should not raise
        creds.validate_scopes(creds.ALLOWED_SCOPES)


class ProfilePathTests(unittest.TestCase):
    def test_paths_are_profile_local(self):
        root = creds.profile_root("personal-assistant")
        self.assertEqual(root.name, "personal-assistant")
        self.assertTrue(str(root).startswith(str(Path.home() / ".hermes")))
        self.assertEqual(creds.token_path("personal-assistant"), root / "google_token.json")
        self.assertEqual(creds.client_secret_path("personal-assistant"), root / "google_client_secret.json")

    def test_paths_respect_hermes_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            tp = creds.token_path("personal-assistant", hermes_home=home)
            self.assertEqual(tp, home / "profiles" / "personal-assistant" / "google_token.json")
            profile_home = home / "profiles" / "personal-assistant"
            self.assertEqual(
                creds.token_path("personal-assistant", hermes_home=profile_home),
                profile_home / "google_token.json",
            )

    def test_profile_paths_reject_unknown_or_path_shaped_names(self):
        for profile in ("other", "../personal-assistant", "/tmp/personal-assistant", "personal-assistant/../other"):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                creds.profile_root(profile)


class FileModeTests(unittest.TestCase):
    def test_token_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles" / "personal-assistant"
            root.mkdir(parents=True)
            token = root / "google_token.json"
            token.write_text("{}")
            os.chmod(token, 0o600)
            self.assertEqual(creds.secure_file_mode(token), 0o600)

    def test_load_credentials_rejects_group_readable_token_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / "profiles" / "personal-assistant"
            root.mkdir(parents=True)
            token = root / "google_token.json"
            token.write_text("{}")
            os.chmod(token, 0o640)
            with mock.patch.object(creds.google.auth, "load_credentials_from_file") as loader:
                with self.assertRaisesRegex(PermissionError, "owner-only"):
                    creds.load_credentials("personal-assistant", hermes_home=home)
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
