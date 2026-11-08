"""Verify network routing semantics and authentication expectations without a live cluster."""
import importlib.util
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra

spec = importlib.util.spec_from_file_location("https_checks", ROOT / "scripts/check-https-routes.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class HTTPSRouteTests(unittest.TestCase):
    def test_run_groups_all_anonymous_requests_before_session_requests(self):
        config = self.config()
        for anonymous_only in (False, True):
            with self.subTest(anonymous_only=anonymous_only), tempfile.TemporaryDirectory() as td:
                cookie_file = Path(td) / "cookie"
                cookie_file.write_text("__Secure-infra_sso=dummy-session")
                argv = ["check-https-routes.py"] + (["--anonymous-only"] if anonymous_only else
                                                       ["--cookie-file", str(cookie_file)])
                output = io.StringIO()
                with patch.object(sys, "argv", argv), patch.object(checks.infra, "load", return_value=config), \
                        patch.object(checks, "request", return_value=(200, {}, b"")) as request, \
                        patch.object(checks, "verify", return_value=None), redirect_stdout(output):
                    self.assertEqual(checks.main(), 0)
                cookies = [call.args[2] for call in request.call_args_list]
                expected = [""] * 20 + ([] if anonymous_only else [cookie_file.read_text()] * 20)
                self.assertEqual(cookies, expected)
                self.assertIn("未認証結果: 20/20 成功", output.getvalue())
                self.assertEqual("認証後結果: 20/20 成功" in output.getvalue(), not anonymous_only)
                if not anonymous_only:
                    self.assertLess(output.getvalue().index("未認証結果:"), output.getvalue().index("=== 認証後"))
                    self.assertIn("結果: 40/40 成功", output.getvalue())
                self.assertNotIn("dummy-session", output.getvalue())

    def config(self):
        raw = json.loads((ROOT / "configs/environment/example.json").read_text())
        raw["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test")
        raw["sso"] = {"enabled": True, "email_domain": "example.test"}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps(raw))
            return infra.load(path)

    def test_loopback_preserves_sni_and_verification(self):
        conn = checks.HTTPSConnection("app.example.test", 443, "127.0.0.1", 10, 8443)
        self.assertTrue(conn._context.check_hostname)
        self.assertEqual(conn._context.verify_mode, checks.ssl.CERT_REQUIRED)
        context = Mock()
        conn._context = context
        with patch.object(checks.socket, "create_connection") as create:
            conn.connect()
            create.assert_called_once_with(("127.0.0.1", 8443), 10)
            context.wrap_socket.assert_called_once_with(create.return_value, server_hostname="app.example.test")

    def test_local_port_does_not_change_host_or_follow_redirects(self):
        with patch.object(checks, "HTTPSConnection") as cls:
            response = cls.return_value.getresponse.return_value
            response.status = 302
            response.getheaders.return_value = [("Location", "https://login.example.test/")]
            response.read.return_value = b""
            result = checks.request("https://app.example.test/get", "127.0.0.1", "session=secret", 10, 8443)
            self.assertEqual(result[0], 302)
            cls.return_value.request.assert_called_once_with("GET", "/get", headers={
                "Host": "app.example.test", "Cookie": "session=secret"})
            cls.return_value.close.assert_called_once()

    def test_all_domains_and_authentication_matrix(self):
        config = self.config()
        plan = list(checks.cases(config, {}, "unique", "current"))
        self.assertEqual(len(plan), 20)
        for route_id, kind, authenticated, url, expected in plan:
            self.assertTrue(checks.urlsplit(url).hostname.endswith("." + config["routes"][route_id]["domain"]))
            if kind == "missing":
                self.assertEqual(expected, 421)
            elif authenticated or route_id == "knative_public":
                self.assertEqual(expected, 200)
            else:
                self.assertEqual(expected, 302)
        strict = list(checks.cases(config, {}, "unique", "required"))
        for route_id, kind, authenticated, url, expected in strict:
            if kind == "missing" and not authenticated and route_id != "knative_public":
                self.assertEqual(expected, 302)

    def test_api_policy_and_custom_apps(self):
        config = self.config()
        config["routes"]["knative_domain"]["auth"] = "api"
        url = "https://custom." + config["routes"]["knative_domain"]["domain"] + "/get"
        plan = list(checks.cases(config, {"knative_domain": url}, "unique", "required"))
        selected = [case for case in plan if case[0] == "knative_domain"]
        self.assertEqual([case[-1] for case in selected], [401, 200, 401, 421])
        with self.assertRaises(ValueError):
            list(checks.cases(config, {"knative_domain": "https://untrusted.invalid/"}, "unique", "current"))

    def test_expired_session_and_wrong_errors_fail(self):
        config = self.config()
        self.assertIsNotNone(checks.verify(config, (302, {}, b""), 200))
        self.assertIsNotNone(checks.verify(config, (421, {}, b"{}"), 421))
        self.assertIsNotNone(checks.verify(config, (302, {"location": "https://untrusted.invalid/"}, b""), 302))
        body = json.dumps({"code": "unknown_host", "status": 421}).encode()
        self.assertIsNone(checks.verify(config, (421, {"content-type": "application/problem+json"}, body), 421))
