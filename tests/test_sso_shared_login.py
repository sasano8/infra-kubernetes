"""Central callback, shared session scope, and unchanged application routing."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra
import sso


class SharedLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.raw = json.loads((ROOT / "configs/environment/example.json").read_text())
        self.raw["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test")
        self.raw["sso"] = {"enabled": True, "email_domain": "example.test"}

    def load(self):
        path = self.path / "environment.json"
        path.write_text(json.dumps(self.raw))
        return infra.load(path)

    def resources(self, config):
        infra.render(config, self.path)
        return json.loads((self.path / "sso.json").read_text())["items"]

    def test_existing_config_defaults_to_one_callback_and_shared_cookie(self):
        config = self.load()
        self.assertEqual(config["sso"]["auth_host"], "auth.knative.example.test")
        self.assertEqual(config["sso"]["cookie_domain"], "example.test")
        items = self.resources(config)
        proxy = next(item for item in items if item["kind"] == "Deployment")
        args = proxy["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("--redirect-url=https://auth.knative.example.test/oauth2/callback", args)
        self.assertIn("--cookie-expire=72h", args)
        for argument in ("--cookie-name=__Secure-infra_sso", "--cookie-domain=example.test",
                         "--cookie-path=/", "--cookie-secure=true", "--cookie-httponly=true",
                         "--cookie-samesite=lax"):
            self.assertIn(argument, args)
        self.assertEqual({arg for arg in args if arg.startswith("--whitelist-domain=")},
                         {"--whitelist-domain=" + host for host in sso.hosts(config)})
        self.assertFalse(any(arg.startswith("--whitelist-domain=.") for arg in args))

    def test_generated_cookie_options_match_pinned_upstream_cli(self):
        # Contract from upstream, independent of our manifest generation.
        contract = json.loads((ROOT / "tests/fixtures/oauth2-proxy-cookie-cli.json").read_text())
        items = self.resources(self.load())
        proxy = next(item for item in items if item["kind"] == "Deployment")
        container = proxy["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], contract["image"], "Update the upstream CLI contract when changing versions")
        generated = {arg[2:].split("=", 1)[0] for arg in container["args"] if arg.startswith("--cookie-")}
        self.assertFalse(generated - set(contract["flags"]), "Unsupported Cookie CLI flags: " + str(generated - set(contract["flags"])))

    def test_cookie_lifetime_is_configurable_in_hours(self):
        self.raw["sso"]["cookie_expire_hours"] = 24
        items = self.resources(self.load())
        container = next(item for item in items if item["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("--cookie-expire=24h", container["args"])
        self.assertIn("--cookie-refresh=1h", container["args"])

    def test_cookie_lifetime_rejects_invalid_values(self):
        for value in (None, True, False, "72", 1.5, 0, -1, 1, 8761):
            with self.subTest(value=value):
                self.raw["sso"]["cookie_expire_hours"] = value
                with self.assertRaisesRegex(ValueError, "cookie_expire_hours"):
                    self.load()

    def test_auth_route_has_its_own_tls_binding_and_only_auth_host(self):
        config = self.load()
        items = self.resources(config)
        ingress = next(item for item in items if item["kind"] == "Ingress")
        self.assertEqual([rule["host"] for rule in ingress["spec"]["rules"]], [config["sso"]["auth_host"]])
        paths = ingress["spec"]["rules"][0]["http"]["paths"]
        self.assertEqual([(p["path"], p["pathType"]) for p in paths],
                         [("/oauth2", "Prefix"), ("/", "Exact")])
        self.assertEqual(ingress["spec"]["tls"], [{"hosts": [config["sso"]["auth_host"]],
                                                  "secretName": "knative-wildcard-tls"}])
        plugins = [item for item in items if item["kind"] == "KongPlugin"]
        oauth = next(item for item in plugins if item["config"]["mode"] == "oauth")
        self.assertEqual(oauth["config"]["hosts"], [config["sso"]["auth_host"]])
        for plugin in plugins:
            self.assertEqual(plugin["config"]["auth_host"], config["sso"]["auth_host"])

    def test_custom_auth_host_uses_matching_existing_wildcard_and_public_port(self):
        self.raw["sso"].update(auth_host="login.standard-nobuffer.example.test", cookie_domain="example.test")
        self.raw["tls"]["host_https_port"] = 9443
        config = self.load()
        items = self.resources(config)
        ingress = next(item for item in items if item["kind"] == "Ingress")
        self.assertEqual(ingress["spec"]["tls"][0]["secretName"], "standard-nobuffer-tls")
        args = next(item for item in items if item["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("--redirect-url=https://login.standard-nobuffer.example.test:9443/oauth2/callback", args)
        self.assertIn("https://login.standard-nobuffer.example.test:9443/oauth2/start?rd=", sso.test_plan(config, "markdown"))

    def test_plan_includes_auth_page_only_when_enabled(self):
        self.raw["sso"]["auth_host"] = "login.knative.example.test"
        self.raw["tls"]["host_https_port"] = 9443
        config = self.load()
        for output in ("markdown", "shell"):
            plan = sso.test_plan(config, output)
            self.assertIn("https://login.knative.example.test:9443/", plan)
            if output == "shell":
                self.assertIn('test "$auth_status" = 200', plan)
            else:
                self.assertIn("ログイン・ログアウトボタン", plan)
        config["sso"]["enabled"] = False
        self.assertNotIn("https://login.knative.example.test:9443/", sso.test_plan(config, "markdown"))

    def test_invalid_auth_host_and_cookie_scope_are_rejected(self):
        bad = (("auth_host", "evil.test"), ("auth_host", "deep.auth.knative.example.test"),
               ("auth_host", "hello-test-ksvc.knative.example.test"), ("auth_host", "auth.knative.example.test:443"),
               ("auth_host", None), ("cookie_domain", "knative.example.test"),
               ("cookie_domain", "ample.test"), ("cookie_domain", ".example.test"),
               ("cookie_domain", "test"), ("cookie_domain", None))
        for key, value in bad:
            with self.subTest(key=key, value=value):
                self.raw["sso"] = {"enabled": True, "email_domain": "example.test", key: value}
                with self.assertRaises(ValueError):
                    self.load()

    def test_cookie_domain_can_be_derived_without_base_domain(self):
        self.raw.pop("base_domain")
        self.assertEqual(self.load()["sso"]["cookie_domain"], "example.test")

    def test_unrelated_route_domains_cannot_silently_lose_shared_sessions(self):
        config = self.load()
        config["standard_domain"] = "standard.other.test"
        with self.assertRaisesRegex(ValueError, "cookie_domain"):
            sso.normalize(config, infra.PUBLIC_ROUTES)

    def test_callback_change_does_not_change_buffering_or_application_hosts(self):
        config = self.load()
        self.resources(config)
        before = (self.path / "routes.yaml").read_text()
        self.raw["sso"].update(auth_host="login.knative.example.test", cookie_domain="example.test")
        self.resources(self.load())
        self.assertEqual(before, (self.path / "routes.yaml").read_text())
        self.assertEqual(before.count('konghq.com/request-buffering: "false"'), 2)
        self.assertEqual(before.count('konghq.com/response-buffering: "false"'), 2)


if __name__ == "__main__":
    unittest.main()
