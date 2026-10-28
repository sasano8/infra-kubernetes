"""Unified route definitions are the sole source of domain/auth/buffering."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra
import sso


class RouteConfiguration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.raw = json.loads((ROOT / "configs/environment/example.json").read_text())
        self.raw["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test")
        self.raw["sso"] = {"enabled": True, "email_domain": "example.test"}

    def load(self, raw=None):
        path = self.directory / "config.json"
        path.write_text(json.dumps(self.raw if raw is None else raw))
        return infra.load(path)

    def test_legacy_fields_and_aliases_are_rejected(self):
        for key in (*infra.PUBLIC_ROUTES, "domain", "standard_host"):
            raw = copy.deepcopy(self.raw)
            raw[key] = "old.example.test"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "未対応"):
                self.load(raw)
        self.raw["sso"]["routes"] = {"standard_domain": "sso"}
        with self.assertRaisesRegex(ValueError, "未対応"):
            self.load()

    def test_all_route_fields_are_required_and_strictly_typed(self):
        for field in ("domain", "kind", "auth", "buffering"):
            raw = copy.deepcopy(self.raw)
            del raw["routes"]["standard_domain"][field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                self.load(raw)
        invalid = {"kind": ["other", "knative", None],
                   "domain": ["Bad_domain", None, False],
                   "auth": ["browser", None, True, "unknown"],
                   "buffering": ["false", 0, 1, None]}
        for field, values in invalid.items():
            for value in values:
                raw = copy.deepcopy(self.raw)
                raw["routes"]["standard_domain"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.load(raw)

    def test_missing_extra_and_duplicate_domains_are_rejected(self):
        raw = copy.deepcopy(self.raw)
        del raw["routes"]["knative_public"]
        with self.assertRaises(ValueError):
            self.load(raw)
        raw = copy.deepcopy(self.raw)
        raw["routes"]["custom"] = raw["routes"]["standard_domain"]
        with self.assertRaises(ValueError):
            self.load(raw)
        self.raw["routes"]["knative_public"]["domain"] = self.raw["routes"]["standard_domain"]["domain"]
        with self.assertRaisesRegex(ValueError, "区別"):
            self.load()

    def test_buffering_and_auth_are_generated_from_the_route_not_its_name(self):
        self.raw["routes"]["standard_domain"].update(buffering=False, auth="api")
        self.raw["routes"]["knative_nobuffer_domain"].update(buffering=True, auth="public")
        config = self.load()
        infra.render(config, self.directory)
        documents = (self.directory / "routes.yaml").read_text().split("---")
        standard = next(d for d in documents if "  name: standard\n" in d)
        knative = next(d for d in documents if "  name: hello-nobuffer\n" in d)
        self.assertIn('konghq.com/request-buffering: "false"', standard)
        self.assertIn('konghq.com/response-buffering: "false"', standard)
        self.assertIn('konghq.com/request-buffering: "true"', knative)
        self.assertIn('konghq.com/response-buffering: "true"', knative)
        items = json.loads((self.directory / "sso.json").read_text())["items"]
        plugins = {i["metadata"]["name"]: i["config"] for i in items if i["kind"] == "KongPlugin"}
        self.assertEqual(plugins["sso-standard-domain"]["mode"], "api")
        self.assertEqual(plugins["sso-knative-nobuffer-domain"]["mode"], "public")
        plan = sso.test_plan(config, "markdown")
        self.assertIn("standard.example.test/get — 未認証期待HTTP 401", plan)
        self.assertIn("knative-nobuffer.example.test/get — 未認証期待HTTP 200", plan)
