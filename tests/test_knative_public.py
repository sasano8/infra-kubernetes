"""Public route, ordinary Knative login app, and shared OAuth session contracts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra
import sso


class KnativePublicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.raw = json.loads((ROOT / "configs/environment/example.json").read_text())
        self.raw["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test")
        self.raw["sso"] = {"enabled": True, "email_domain": "example.test"}

    def load(self):
        path = self.directory / "config.json"
        path.write_text(json.dumps(self.raw))
        return infra.load(path)

    def test_old_config_defaults_public_domain_and_preserves_callback(self):
        del self.raw["knative_public"]
        config = self.load()
        self.assertEqual(config["knative_public"], "knative-public.example.test")
        self.assertEqual(config["sso"]["auth_host"], "auth.knative.example.test")
        self.assertEqual(config["sso"]["cookie_domain"], "example.test")

    def test_public_wildcard_route_and_label_selector(self):
        config = self.load()
        infra.render(config, self.directory)
        routes = (self.directory / "routes.yaml").read_text()
        self.assertIn('host: "*.knative-public.example.test"', routes)
        self.assertIn('konghq.com/plugins: "sso-knative-public"', routes)
        domain = (self.directory / "knative-config/domain.yaml").read_text()
        self.assertIn('infra.sasano8.dev/route: knative_public', domain)
        items = json.loads((self.directory / "sso.json").read_text())["items"]
        plugin = next(i for i in items if i["metadata"]["name"] == "sso-knative-public")
        self.assertEqual(plugin["config"]["mode"], "public")
        self.assertEqual(plugin["config"]["hosts"], ["*.knative-public.example.test"])
        self.assertFalse(plugin["config"]["forward_id_token"])

    def test_portal_is_knative_app_with_return_url_allowed_by_oauth(self):
        config = self.load()
        infra.render(config, self.directory)
        documents = (self.directory / "apps.yaml").read_text().split("\n---\n")
        page, service = map(json.loads, documents[-2:])
        self.assertEqual(service["apiVersion"], "serving.knative.dev/v1")
        self.assertEqual(service["metadata"]["labels"]["infra.sasano8.dev/route"], "knative_public")
        self.assertIn(sso.origin(config) + "/oauth2/start", page["data"]["index.html"])
        self.assertIn(sso.origin(config) + "/oauth2/sign_out", page["data"]["index.html"])
        self.assertIn(sso.portal_origin(config) + "/", page["data"]["index.html"])
        items = json.loads((self.directory / "sso.json").read_text())["items"]
        proxy = next(i for i in items if i["kind"] == "Deployment")
        args = proxy["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("--whitelist-domain=" + sso.portal_host(config), args)
        self.assertIn("--cookie-domain=example.test", args)
        handler = (ROOT / "plugins/sso/kong/plugins/session-auth/handler.lua").read_text()
        self.assertNotIn("<!doctype html>", handler)

    def test_public_policy_and_token_forwarding_cannot_be_overridden(self):
        self.raw["sso"]["routes"] = {"knative_public": "sso"}
        with self.assertRaisesRegex(ValueError, "public専用"):
            self.load()
        self.raw["sso"]["routes"] = {}
        self.raw["sso"]["id_token_routes"] = ["knative_public"]
        with self.assertRaisesRegex(ValueError, "public"):
            self.load()

    def test_public_default_route_has_unauthenticated_200_in_test_plan(self):
        self.raw["default_routes"]["knative"] = "knative_public"
        config = self.load()
        plan = sso.test_plan(config, "markdown")
        self.assertIn("https://hello-default-test-ksvc.knative-public.example.test/get — 未認証期待HTTP 200", plan)

    def test_public_domain_uses_its_own_certificate_and_validates_zone(self):
        infra.render(self.load(), self.directory)
        items = json.loads((self.directory / "tls.json").read_text())["items"]
        certificate = next(i for i in items if i["metadata"]["name"] == "knative-public-tls")
        self.assertEqual(certificate["spec"]["dnsNames"],
                         ["knative-public.example.test", "*.knative-public.example.test"])
        self.raw["knative_public"] = "public.other.test"
        with self.assertRaisesRegex(ValueError, "範囲外"):
            self.load()
