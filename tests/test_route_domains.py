"""Offline configuration, TLS, and resource-lifecycle checks for the four routes."""
import json
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
        self.assertEqual(routes.count('konghq.com/request-buffering: "true"'), 2)
        self.assertEqual(routes.count('konghq.com/request-buffering: "false"'), 2)
        self.assertEqual(routes.count('konghq.com/response-buffering: "true"'), 2)
        self.assertEqual(routes.count('konghq.com/response-buffering: "false"'), 2)
        for key in ("standard_domain", "standard_nobuffer_domain", "knative_domain", "knative_nobuffer_domain"):
            self.assertIn(self.config[key], routes)
        domain = (self.directory / "knative-config/domain.yaml").read_text()
        self.assertIn('infra.sasano8.dev/route: knative_nobuffer_domain', domain)
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
