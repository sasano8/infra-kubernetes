"""Offline configuration, TLS, and resource-lifecycle checks for the four routes."""
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import infra
import inventory

ROOT = Path(__file__).resolve().parents[1]


class FourRouteDomains(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "configs/environment/example.json").read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def load(self):
        path = self.directory / "config.json"
        path.write_text(json.dumps(self.config))
        return infra.load(path)

    def test_old_config_requires_explicit_nobuffer_domains(self):
        del self.config["standard_nobuffer_domain"]
        with self.assertRaisesRegex(ValueError, "standard_nobuffer_domain"):
            self.load()

    def test_domains_must_be_distinct_and_valid(self):
        for value in (self.config["standard_domain"], "Bad_domain", None, "x." * 120 + "example.test"):
            self.config["knative_nobuffer_domain"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load()

    def test_four_rendered_routes_and_knative_selector(self):
        infra.render(self.load(), self.directory)
        routes = (self.directory / "routes.yaml").read_text()
        self.assertEqual(routes.count('konghq.com/request-buffering: "true"'), 4)
        self.assertEqual(routes.count('konghq.com/request-buffering: "false"'), 2)
        self.assertEqual(routes.count('konghq.com/response-buffering: "true"'), 4)
        self.assertEqual(routes.count('konghq.com/response-buffering: "false"'), 2)
        for key in ("standard_domain", "standard_nobuffer_domain", "knative_domain", "knative_nobuffer_domain"):
            self.assertIn(self.config[key], routes)
        domain = (self.directory / "knative-config/domain.yaml").read_text()
        self.assertIn('infra.sasano8.dev/route: knative_nobuffer_domain', domain)
        self.assertIn('  name: config-domain\n  namespace: knative-serving\ndata:\n', domain)
        apps = (self.directory / "apps.yaml").read_text()
        self.assertIn('name: hello-nobuffer\n  labels:\n    infra.sasano8.dev/route: knative_nobuffer_domain', apps)

    def test_application_selects_route_without_buffering_policy(self):
        infra.render(self.load(), self.directory)
        apps = (self.directory / "apps.yaml").read_text()
        self.assertIn('infra.sasano8.dev/route: knative_domain', apps)
        self.assertIn('infra.sasano8.dev/route: knative_nobuffer_domain', apps)
        self.assertNotIn('infra.sasano8.dev/buffering', apps)
        self.assertNotIn('konghq.com/request-buffering', apps)
        self.assertNotIn('konghq.com/response-buffering', apps)

    def test_knative_services_leave_reserved_environment_to_knative(self):
        infra.render(self.load(), self.directory)
        documents = (self.directory / 'apps.yaml').read_text().split('\n---\n')
        services = [doc for doc in documents if 'apiVersion: serving.knative.dev/v1\n' in doc]
        self.assertEqual(len(services), 3)
        for doc in services:
            name = re.search(r'^  name: (.+)$', doc, re.M).group(1)
            with self.subTest(service=name):
                reserved = re.findall(r'\bname:\s*[\"\']?(PORT|K_SERVICE|K_CONFIGURATION|K_REVISION)[\"\']?\s*[,}\n]', doc)
                self.assertEqual(reserved, [], 'Knative injects reserved environment variables itself')
                self.assertIn('containerPort: 8080', doc)
                self.assertIn('name: MAX_BODY_SIZE, value: "67108864"', doc)
        deployment = documents[0]
        self.assertIn('name: PORT, value: "8080"', deployment)

    def enable_tls(self):
        self.config['tls'].update(enabled=True, email='admin@example.test', cloudflare_zone='example.test')

    def test_all_four_certificates_and_secrets_are_in_inventory(self):
        self.enable_tls()
        infra.render(self.load(), self.directory)
        tls = json.loads((self.directory / "tls.json").read_text())
        certs = [r for r in tls['items'] if r['kind'] == 'Certificate']
        self.assertEqual(len(certs), 4)
        expected = {'standard-tls', 'knative-wildcard-tls', 'standard-nobuffer-tls', 'knative-nobuffer-tls'}
        self.assertEqual({r['metadata']['name'] for r in certs}, expected)
        identities = inventory.python_identities((ROOT / 'scripts/tls.py').read_text(), 'scripts/tls.py')
        for kind in ('Certificate', 'Secret'):
            self.assertTrue(expected <= {r['name'] for r in identities if r['kind'] == kind})
        domains = {d for r in certs for d in r['spec']['dnsNames']}
        for key in ('standard_domain', 'knative_domain', 'standard_nobuffer_domain', 'knative_nobuffer_domain'):
            self.assertIn('*.' + self.config[key], domains)

    def test_nobuffer_certificate_zone_is_validated(self):
        self.enable_tls()
        self.config['knative_nobuffer_domain'] = 'outside.invalid'
        with self.assertRaisesRegex(ValueError, '範囲外'):
            self.load()


    def test_legacy_defaults_are_preserved(self):
        del self.config["default_routes"]
        c = self.load()
        self.assertEqual(c["default_routes"], infra.DEFAULT_ROUTES)

    def test_default_disable_unknown_and_wrong_kind_are_rejected(self):
        for supplied in (None, [], {"standard": None}, {"knative": ""},
                         {"standard": "knative_domain"}, {"knative": "unknown"},
                         {"standard": False}, {"other": "standard_domain"}):
            self.config["default_routes"] = supplied
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                self.load()

    def test_selectable_defaults_preserve_explicit_selection(self):
        for standard in ("standard_domain", "standard_nobuffer_domain"):
            for knative in ("knative_domain", "knative_nobuffer_domain"):
                self.config["default_routes"] = {"standard": standard, "knative": knative}
                c = self.load()
                for kind in ("standard", "knative"):
                    self.assertEqual(infra.resolve_route(c, kind), c["default_routes"][kind])
                    for route in infra.PUBLIC_ROUTES:
                        if infra.PUBLIC_ROUTES[route]["kind"] == kind:
                            self.assertEqual(infra.resolve_route(c, kind, route), route)
                domains = infra.knative_domain_data(c)
                self.assertEqual(domains[c[knative]], "")
                other = "knative_domain" if knative == "knative_nobuffer_domain" else "knative_nobuffer_domain"
                self.assertIn("infra.sasano8.dev/route: " + other, domains[c[other]])
                infra.render(c, self.directory)
                routes = (self.directory / "routes.yaml").read_text()
                for kind, route in (("standard", standard), ("knative", knative)):
                    name = "standard-default" if kind == "standard" else "hello-default"
                    document = next(d for d in routes.split("---") if "  name: " + name + "\n" in d)
                    self.assertIn(c[route], document)
                    self.assertIn('buffering: "' + str(infra.PUBLIC_ROUTES[route]["buffering"]).lower() + '"', document)


    def test_installer_saves_selected_defaults(self):
        from unittest.mock import patch
        target = self.directory / "installed.json"
        def answer(prompt):
            if prompt.startswith("standard の既定"):
                return "standard_nobuffer_domain"
            if prompt.startswith("knative の既定"):
                return "knative_nobuffer_domain"
            return ""
        with patch("builtins.input", side_effect=answer):
            infra.configure(target)
        self.assertEqual(infra.load(target)["default_routes"], {
            "standard": "standard_nobuffer_domain", "knative": "knative_nobuffer_domain"})
        with self.assertRaises(ValueError):
            infra.configure(target)
