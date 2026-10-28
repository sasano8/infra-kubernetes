"""Exercise the actual error HTTP server, not a mocked status-code helper."""
import http.client
import importlib.util
import json
import re
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra
import inventory

spec = importlib.util.spec_from_file_location("route_error_server", ROOT / "apps/route-errors/server.py")
errors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(errors)


class ErrorServerTests(unittest.TestCase):
    def request(self, layer, host, path="/", method="GET", headers=None):
        server = errors.make_server(("127.0.0.1", 0), layer, {"known_hosts": ["app.example.test"]})
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request(method, path, headers={"Host": host, **(headers or {})})
            response = conn.getresponse()
            result = response.status, response.read(), dict(response.getheaders())
            conn.close()
            return result
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_unknown_host_is_421_for_every_path_and_method(self):
        for method in ("GET", "HEAD", "POST", "OPTIONS", "CUSTOM"):
            with self.subTest(method=method):
                status, body, headers = self.request("kong", "unknown.example.test:443", "/api/missing", method)
                self.assertEqual(status, 421)
                self.assertEqual(body, b"" if method == "HEAD" else b'{"error":"unknown_host"}\n')
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn("Location", headers)
                self.assertNotIn("Set-Cookie", headers)

    def test_known_host_missing_kong_path_is_404(self):
        status, body, _ = self.request("kong", "APP.EXAMPLE.TEST:443", "/missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "path_not_found")

    def test_kourier_fallback_is_421_even_for_previously_registered_host(self):
        self.assertEqual(self.request("kourier", "app.example.test")[0], 421)

    def test_network_probes_echo_the_fallback_hash_to_prevent_false_readiness(self):
        headers = {"K-Network-Probe": "probe", "K-Network-Hash": "a" * 64}
        status, _, response = self.request("kourier", "unknown.example.test", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(response["K-Network-Hash"], "a" * 64)
        self.assertEqual(self.request("kong", "unknown.example.test", headers=headers)[0], 421)
        headers["K-Network-Hash"] = "override"
        self.assertEqual(self.request("kourier", "unknown.example.test", headers=headers)[0], 421)

    def test_request_forwarding_hints_do_not_change_the_error(self):
        status, _, _ = self.request("kong", "unknown.example.test", headers={
            "X-Forwarded-Host": "app.example.test", "Authorization": "Bearer fake",
            "Cookie": "__Secure-infra_sso=fake"})
        self.assertEqual(status, 421)


class ErrorManifestTests(unittest.TestCase):
    def test_fallbacks_are_scoped_and_in_inventory_with_sso_enabled_or_disabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as td:
                path = Path(td)
                raw = json.loads((ROOT / "configs/environment/example.json").read_text())
                raw["sso"] = {"enabled": enabled, "email_domain": "example.test"}
                raw["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test")
                (path / "config.json").write_text(json.dumps(raw))
                infra.render(infra.load(path / "config.json"), path)
                routes = (path / "routes.yaml").read_text()
                fallback = next(doc for doc in routes.split("---") if "  name: route-errors\n" in doc)
                self.assertNotRegex(fallback, r"(?m)^\s+(?:- )?host:")
                self.assertIn('konghq.com/plugins: ""', fallback)
                self.assertIn("number: 81", fallback)
                apps = (path / "apps.yaml").read_text()
                fallback_hosts = json.loads(re.search(r"hosts: (\[\"\*\..*\])", apps).group(1))
                self.assertEqual(set(fallback_hosts), {"*.knative-public.example.test", "*.knative.example.test",
                                                       "*.knative-nobuffer.example.test"})
                self.assertIn("servicePort: 80", apps)
                identities = inventory.yaml_identities(
                    (ROOT / "apps/route-errors/manifests/apps.yaml.tmpl").read_text(), "errors")
                self.assertEqual({i["kind"] for i in identities}, {"ConfigMap", "Deployment", "Service", "Ingress"})
                self.assertIn("networking.internal.knative.dev", {i["group"] for i in identities})
