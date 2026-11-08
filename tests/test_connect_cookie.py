"""Check secret handling and the scope of connectivity Cookie preparation/cleanup."""
from contextlib import redirect_stdout
import getpass
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra

spec = importlib.util.spec_from_file_location("connect_cookie", ROOT / "scripts/test-connect-cookie.py")
cookie_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cookie_tool)


class ConnectCookieTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cookie = self.root / "secrets/test-cookie"
        raw = json.loads((ROOT / "configs/environment/example.json").read_text())
        raw["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test", host_https_port=9443)
        raw["sso"] = {"enabled": True, "email_domain": "example.test"}
        for route in raw["routes"].values():
            route["domain"] = route["domain"].replace("example.test", "custom.example.test")
        config_path = self.root / "environment.json"
        config_path.write_text(json.dumps(raw))
        self.config = infra.load(config_path)

    def prepare(self, value):
        output = io.StringIO()
        with patch.object(cookie_tool.getpass, "getpass", return_value=value), redirect_stdout(output):
            cookie_tool.prepare(self.config, self.cookie)
        return output.getvalue()

    def test_prepare_guides_using_config_and_saves_split_cookie_without_logging(self):
        value = "__Secure-infra_sso_0=dummy-part0; __Secure-infra_sso_1=dummy-part1"
        output = self.prepare("Cookie: " + value)
        self.assertEqual(self.cookie.read_text(), value)
        self.assertEqual(stat.S_IMODE(self.cookie.stat().st_mode), 0o600)
        self.assertIn("https://auth.knative.custom.example.test:9443/oauth2/start?rd=", output)
        self.assertIn("https://hello-test-ssvc.standard.custom.example.test:9443/get", output)
        self.assertNotIn("dummy-part", output)
        self.assertEqual(list(self.cookie.parent.iterdir()), [self.cookie])

    def test_cancel_invalid_input_and_input_failure_preserve_existing_cookie(self):
        self.cookie.parent.mkdir()
        self.cookie.write_text("existing-fixture")
        self.prepare("")
        for value in ("__Secure-infra_sso_csrf=csrf-only", "__Secure-infra_sso=", "session=unrelated", "__Secure-infra_sso=dummy\nInjected: value"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.prepare(value)
        with patch.object(cookie_tool.getpass, "getpass", side_effect=getpass.GetPassWarning), self.assertRaises(ValueError), redirect_stdout(io.StringIO()):
            cookie_tool.prepare(self.config, self.cookie)
        self.assertEqual(self.cookie.read_text(), "existing-fixture")

    def test_replace_does_not_overwrite_symlink_target(self):
        target = self.root / "other-secret"
        target.write_text("other-fixture")
        self.cookie.parent.mkdir()
        self.cookie.symlink_to(target)
        self.prepare("__Secure-infra_sso=dummy-session")
        self.assertEqual(target.read_text(), "other-fixture")
        self.assertFalse(self.cookie.is_symlink())

    def test_cleanup_only_deletes_test_cookie_and_is_repeatable(self):
        self.prepare("__Secure-infra_sso=dummy-session")
        other = self.cookie.parent / "google-sso.json"
        other.write_text("other-fixture")
        with redirect_stdout(io.StringIO()):
            cookie_tool.cleanup(self.cookie)
            cookie_tool.cleanup(self.cookie)
        self.assertFalse(self.cookie.exists())
        self.assertTrue(other.exists())

    def test_write_failure_keeps_previous_cookie_and_removes_temporary_file(self):
        self.prepare("__Secure-infra_sso=dummy-old")
        with patch.object(cookie_tool.os, "replace", side_effect=OSError), self.assertRaises(OSError):
            self.prepare("__Secure-infra_sso=dummy-new")
        self.assertEqual(self.cookie.read_text(), "__Secure-infra_sso=dummy-old")
        self.assertEqual(list(self.cookie.parent.iterdir()), [self.cookie])
