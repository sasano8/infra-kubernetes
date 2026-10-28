"""Google ID token forwarding is opt-in and cannot overwrite routing or API auth."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra


class IDTokenForwardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.raw = json.loads((ROOT / "configs/environment/example.json").read_text())
        self.raw["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test")
        self.raw["sso"] = {"enabled": True, "email_domain": "example.test"}

    def config(self):
        path = self.path / "environment.json"
        path.write_text(json.dumps(self.raw))
        return infra.load(path)

    def render(self):
        config = self.config()
        infra.render(config, self.path)
        resources = json.loads((self.path / "sso.json").read_text())["items"]
        plugins = {r["metadata"]["name"]: r["config"] for r in resources if r["kind"] == "KongPlugin"}
        proxy = next(r for r in resources if r["kind"] == "Deployment")
        return plugins, proxy["spec"]["template"]["spec"]["containers"][0]["args"]

    def test_existing_configuration_keeps_id_token_forwarding_disabled(self):
        config = self.config()
        self.assertEqual(config["sso"]["id_token_routes"], [])
        self.assertEqual(config["sso"]["id_token_header"], "X-Forwarded-Id-Token")
        plugins, args = self.render()
        self.assertTrue(all(p["forward_id_token"] is False for p in plugins.values()))
        self.assertIn("--set-authorization-header=false", args)
        self.assertIn("--pass-authorization-header=false", args)
        self.assertIn("--pass-access-token=false", args)

    def test_only_selected_protected_routes_forward_to_the_configured_header(self):
        self.raw["sso"].update(
            id_token_routes=["standard_domain", "knative_nobuffer_domain"],
            id_token_header="X-SSO-Identity",
        )
        self.raw["routes"]["knative_nobuffer_domain"]["auth"] = "api"
        self.raw["routes"]["knative_domain"]["auth"] = "public"
        self.raw["default_routes"] = {
            "standard": "standard_nobuffer_domain", "knative": "knative_nobuffer_domain"
        }
        plugins, args = self.render()
        for name, config in plugins.items():
            expected = name in ("sso-standard-domain", "sso-knative-nobuffer-domain")
            with self.subTest(plugin=name):
                self.assertIs(config["forward_id_token"], expected)
                self.assertEqual(config["id_token_header"], "X-SSO-Identity")
        self.assertIn("--set-authorization-header=true", args)
        self.assertIn("--pass-authorization-header=false", args)
        self.assertIn("--pass-access-token=false", args)
        routes = (self.path / "routes.yaml").read_text()
        self.assertEqual(routes.count('konghq.com/plugins: "sso-knative-nobuffer-domain"'), 2)
        self.assertEqual(routes.count('konghq.com/plugins: "sso-standard-nobuffer-domain"'), 2)
        self.assertEqual(routes.count('konghq.com/request-buffering: "false"'), 4)

    def test_all_four_route_ids_are_supported_without_enabling_oauth_endpoints(self):
        self.raw["sso"]["id_token_routes"] = [route for route in infra.PUBLIC_ROUTES if route != "knative_public"]
        plugins, _ = self.render()
        self.assertEqual(sum(p["forward_id_token"] for p in plugins.values()), 4)
        self.assertFalse(plugins["sso-oauth-endpoints"]["forward_id_token"])

    def test_unknown_duplicate_or_non_list_routes_are_rejected(self):
        base = copy.deepcopy(self.raw)
        for routes in (None, True, "standard_domain", {}, ["missing"], [1], [{}],
                       ["standard_domain", "standard_domain"]):
            with self.subTest(routes=routes):
                self.raw = copy.deepcopy(base)
                self.raw["sso"]["id_token_routes"] = routes
                with self.assertRaisesRegex(ValueError, "id_token_routes"):
                    self.config()

    def test_public_route_cannot_request_an_id_token(self):
        self.raw["sso"].update(id_token_routes=["standard_domain"])
        self.raw["routes"]["standard_domain"]["auth"] = "public"
        with self.assertRaisesRegex(ValueError, "public"):
            self.config()

    def test_invalid_and_reserved_header_names_are_rejected(self):
        for header in (None, False, "", "Authorization", "Cookie", "Host", "X Token", "X_Token",
                       "X-Token\r\nHost", "X-トークン", "X-Forwarded-Host", "x-forwarded-proto",
                       "X-Auth-Request-Email", "X-Kong-Request-Debug", "X-Original-URI", "X-Real-IP"):
            with self.subTest(header=header):
                self.raw["sso"]["id_token_header"] = header
                with self.assertRaisesRegex(ValueError, "id_token_header"):
                    self.config()

    def test_dedicated_header_names_are_case_insensitive_and_configurable(self):
        for header in ("X-Forwarded-Id-Token", "x-forwarded-id-token", "X-SSO-ID-Token", "x-company-identity"):
            with self.subTest(header=header):
                self.raw["sso"]["id_token_header"] = header
                self.assertEqual(self.config()["sso"]["id_token_header"], header)

    def test_sso_can_be_disabled_without_losing_the_forwarding_policy(self):
        self.raw["sso"].update(enabled=False, id_token_routes=["standard_domain"])
        config = self.config()
        infra.render(config, self.path)
        self.assertEqual(json.loads((self.path / "sso.json").read_text())["items"], [])
        self.assertNotIn("session-auth", (self.path / "kong.yaml").read_text())
        self.assertEqual(config["sso"]["id_token_routes"], ["standard_domain"])
