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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra
import inventory

spec = importlib.util.spec_from_file_location("route_error_server", ROOT / "apps/platform-portal/server.py")
errors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(errors)


class ErrorServerTests(unittest.TestCase):
    def request(self, reason, host, path="/", method="GET", headers=None):
        server = errors.make_server(("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request(method, path, headers={"Host": host, **({"X-Infra-Error": reason} if reason else {}), **(headers or {})})
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
                status, body, headers = self.request("unknown_host", "unknown.example.test:443", "/api/missing", method)
                self.assertEqual(status, 421)
                if method == "HEAD":
                    self.assertEqual(body, b"")
                    self.assertGreater(int(headers["Content-Length"]), 0)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Content-Type"], "application/problem+json")
                if method != "HEAD":
                    data = json.loads(body)
                    self.assertEqual(data["code"], "unknown_host")
                    self.assertEqual(data["status"], 421)
                    self.assertEqual(data["type"], "about:blank")
                    self.assertEqual(data["title"], "Misdirected Request")
                self.assertNotIn("Location", headers)
                self.assertNotIn("Set-Cookie", headers)

    def test_proxy_cannot_request_path_not_found_classification(self):
        status, body, _ = self.request("path_not_found", "APP.EXAMPLE.TEST:443", "/missing")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "invalid_error_reason")

    def test_unknown_host_reason_does_not_depend_on_host_value(self):
        self.assertEqual(self.request("unknown_host", "app.example.test")[0], 421)

    def test_network_probes_echo_the_fallback_hash_to_prevent_false_readiness(self):
        headers = {"K-Network-Probe": "probe", "K-Network-Hash": "a" * 64}
        status, _, response = self.request("unknown_host", "unknown.example.test", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(response["K-Network-Hash"], "a" * 64)
        headers["K-Network-Probe"] = "invalid"
        self.assertEqual(self.request("unknown_host", "unknown.example.test", headers=headers)[0], 421)
        headers["K-Network-Hash"] = "override"
        self.assertEqual(self.request("unknown_host", "unknown.example.test", headers=headers)[0], 421)

    def test_request_forwarding_hints_do_not_change_the_error(self):
        status, _, _ = self.request("unknown_host", "unknown.example.test", headers={
            "X-Forwarded-Host": "app.example.test", "Authorization": "Bearer fake",
            "Cookie": "__Secure-infra_sso=fake"})
        self.assertEqual(status, 421)

    def test_missing_reason_serves_portal_and_unavailable_path_returns_problem(self):
        status, _, headers = self.request(None, "portal.example.test")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(self.request(None, "portal.example.test", "/?next=private")[0], 200)
        status, body, _ = self.request(None, "portal.example.test", "/server.py?secret=hidden")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["code"], "path_not_found")
        self.assertNotIn(b"hidden", body)

    def test_invalid_reason_is_not_reflected_and_returns_bad_request(self):
        status, body, _ = self.request("untrusted-value", "unknown.example.test")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "invalid_error_reason")
        self.assertNotIn(b"untrusted-value", body)

    def test_renderer_responds_before_receiving_an_upload_body(self):
        status, _, _ = self.request("unknown_host", "unknown.example.test", method="POST",
                                    headers={"Content-Length": "1000000000"})
        self.assertEqual(status, 421)


class ErrorManifestTests(unittest.TestCase):
    def test_legacy_cleanup_preserves_unowned_services_and_the_routing_ingress(self):
        class Cluster:
            def __init__(self):
                self.calls = []

            def call(self, *args, **kwargs):
                self.calls.append(args)
                owner = "infra-kubernetes" if args[1] in ("deployment/route-errors", "configmap/route-errors") else ""
                return SimpleNamespace(stdout=owner)

        cluster = Cluster()
        infra.cleanup_legacy_error_service(cluster)
        deleted = [call[1] for call in cluster.calls if call[0] == "delete"]
        self.assertEqual(deleted, ["deployment/route-errors", "configmap/route-errors"])
        self.assertFalse(any("ingress" in call[1] for call in cluster.calls))

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
                self.assertNotIn("pre-function", routes)
                self.assertNotIn("path_not_found", routes)
                self.assertNotIn("local known", routes)
                self.assertIn('headers: ["X-Infra-Error:unknown_host"]', routes)
                self.assertIn("headers: [X-Infra-Error, K-Network-Probe, K-Network-Hash]", routes)
                fallback = next(doc for doc in routes.split("---") if "  name: route-errors\n" in doc)
                self.assertNotRegex(fallback, r"(?m)^\s+(?:- )?host:")
                self.assertIn('konghq.com/plugins: "routing-error-reason"', fallback)
                self.assertIn("number: 8080", fallback)
                apps = (path / "apps.yaml").read_text()
                fallback_hosts = json.loads(re.search(r"hosts: (\[\"\*\..*\])", apps).group(1))
                self.assertEqual(set(fallback_hosts), {"*.knative-public.example.test", "*.knative.example.test",
                                                       "*.knative-nobuffer.example.test"})
                self.assertIn("servicePort: 8080", apps)
                identities = inventory.yaml_identities(
                    (ROOT / "apps/route-errors/manifests/apps.yaml.tmpl").read_text(), "errors")
                self.assertEqual({i["kind"] for i in identities}, {"Service", "Ingress"})
                self.assertIn("networking.internal.knative.dev", {i["group"] for i in identities})

    def test_portal_is_resident_and_the_direct_service_bypasses_kourier(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            raw = json.loads((ROOT / "configs/environment/example.json").read_text())
            (path / "config.json").write_text(json.dumps(raw))
            infra.render(infra.load(path / "config.json"), path)
            docs = [json.loads(doc) for doc in (path / "apps.yaml").read_text().split("\n---\n")
                    if doc.lstrip().startswith("{")]
            portal = next(i for i in docs if i["apiVersion"] == "serving.knative.dev/v1")
            self.assertEqual(portal["spec"]["template"]["metadata"]["annotations"]["autoscaling.knative.dev/min-scale"], "1")
            direct = next(i for i in docs if i["apiVersion"] == "v1" and i["kind"] == "Service")
            self.assertEqual(direct["spec"]["ports"], [{"name": "http", "port": 8080, "targetPort": 8080}])
            self.assertEqual(direct["spec"]["selector"]["serving.knative.dev/service"], "sso")
            self.assertEqual(direct["spec"]["selector"]["infra.sasano8.dev/component"],
                             portal["spec"]["template"]["metadata"]["labels"]["infra.sasano8.dev/component"])
            self.assertNotIn("externalName", direct["spec"])
            self.assertNotIn("name: route-errors\n  namespace: test\n  labels", (path / "apps.yaml").read_text().split("apiVersion: networking.internal.knative.dev")[0])

    def test_external_error_service_uses_one_port_without_writing_its_namespace(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            raw = json.loads((ROOT / "configs/environment/example.json").read_text())
            raw["error_service"] = {"name": "errors", "namespace": "platform", "port": 9000}
            (path / "config.json").write_text(json.dumps(raw))
            infra.render(infra.load(path / "config.json"), path)
            apps = (path / "apps.yaml").read_text()
            self.assertIn('externalName: "errors.platform.svc.cluster.local"', apps)
            self.assertIn("servicePort: 9000", apps)
            self.assertNotIn("namespace: platform", apps)
            self.assertNotIn('"name": "sso-portal"', apps)
            self.assertIn("number: 9000", (path / "routes.yaml").read_text())

    def test_error_endpoint_requires_a_single_valid_port(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            for endpoint in ({"name": "errors", "namespace": "test", "port": True},
                             {"name": "errors", "namespace": "test", "port": 0},
                             {"name": "errors", "namespace": "test", "port": 65536},
                             {"name": "errors", "namespace": "test", "kong_port": 80, "kourier_port": 81},
                             {"name": "error-service", "namespace": "test", "port": 8080},
                             {"name": "kourier", "namespace": "kourier-system", "port": 80},
                             {"name": "kong-proxy", "namespace": "kong-system", "port": 80}):
                raw = json.loads((ROOT / "configs/environment/example.json").read_text())
                raw["error_service"] = endpoint
                path.write_text(json.dumps(raw))
                with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, "error_service"):
                    infra.load(path)
