"""Regression coverage for explicit SSO disable and unauthenticated GET plans."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra
import sso

OMITTED = object()
ROUTE_IDS = ("standard_domain", "standard_nobuffer_domain", "knative_domain", "knative_nobuffer_domain")


class InstalledCluster:
    """Only simulate the names queried by SSO preflight, never credential values."""
    def __init__(self, installed):
        self.installed = installed
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("get", "ingress/oauth2-proxy"):
            return SimpleNamespace(stdout="ingress.networking.k8s.io/oauth2-proxy\n" if self.installed else "")
        if args[:2] == ("get", "secret/google-sso"):
            return SimpleNamespace(stdout="\n".join(sso.KEYS) + "\n")
        raise AssertionError("Unexpected query: " + repr(args))


class SSORegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.base = json.loads((ROOT / "configs/environment/example.json").read_text())
        self.base["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test")

    def config(self, payload=OMITTED):
        raw = copy.deepcopy(self.base)
        if payload is OMITTED:
            raw.pop("sso", None)
        else:
            raw["sso"] = payload
        path = self.path / "environment.json"
        path.write_text(json.dumps(raw))
        return infra.load(path)

    def test_missing_enabled_never_disables_installed_sso(self):
        for payload in (OMITTED, {}, {"email_domain": "example.test"}, {"cookie_expire_hours": 24}):
            with self.subTest(payload="omitted" if payload is OMITTED else payload):
                cluster = InstalledCluster(installed=True)
                config = self.config(payload)
                with self.assertRaisesRegex(ValueError, "sso.enabled=false"):
                    sso.preflight(config, cluster)
                self.assertIn(("get", "ingress/oauth2-proxy"), [args[:2] for args in cluster.calls])

    def test_missing_enabled_is_compatible_without_installed_sso(self):
        for payload in (OMITTED, {}, {"email_domain": "example.test"}):
            with self.subTest(payload="omitted" if payload is OMITTED else payload):
                config = self.config(payload)
                self.assertFalse(config["sso"]["enabled"])
                sso.preflight(config, InstalledCluster(installed=False))
                sso.preflight(config, None)

    def test_explicit_boolean_false_allows_disabling_or_fresh_disabled_install(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                config = self.config({"enabled": False})
                self.assertIs(config["sso"]["enabled"], False)
                sso.preflight(config, InstalledCluster(installed))

    def test_explicit_boolean_true_checks_secret_and_stays_enabled(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                config = self.config({"enabled": True, "email_domain": "example.test"})
                cluster = InstalledCluster(installed)
                sso.preflight(config, cluster)
                self.assertIs(config["sso"]["enabled"], True)
                self.assertIn(("get", "secret/google-sso"), [args[:2] for args in cluster.calls])

    def test_enabled_rejects_json_non_booleans(self):
        for enabled in (None, 0, 1, "", "false", "true", [], {}):
            with self.subTest(enabled=enabled):
                with self.assertRaisesRegex(ValueError, "sso.enabled"):
                    self.config({"enabled": enabled, "email_domain": "example.test"})

    def plan_config(self, enabled, mode, api_paths):
        for route in ROUTE_IDS:
            self.base["routes"][route]["auth"] = mode
        return self.config({"enabled": enabled, "email_domain": "example.test", "api_paths": api_paths})

    def test_sso_get_plan_respects_api_path_and_exact_prefix_boundaries(self):
        for api_paths, expected in ((["/get"], "401"), (["/ge"], "302"), (["/getter"], "302"),
                                    (["/get/item"], "302"), (["/get-suffix"], "302"),
                                    (["/GET"], "302"), ([], "302")):
            config = self.plan_config(True, "sso", api_paths)
            for output in ("shell", "markdown"):
                with self.subTest(api_paths=api_paths, output=output):
                    plan = sso.test_plan(config, output)
                    if output == "shell":
                        self.assertEqual(plan.count('test "$status" = ' + expected), 6)
                    else:
                        self.assertEqual(plan.count("未認証期待HTTP " + expected), 6)
                    self.assertEqual("/oauth2/start?rd=" in plan, expected == "302")

    def test_plan_policy_and_global_enable_matrix(self):
        for enabled in (False, True):
            for mode in ("sso", "api", "public"):
                for api_paths in ([], ["/get"], ["/ge"]):
                    expected = ("200" if not enabled or mode == "public" else
                                "401" if mode == "api" or "/get" in api_paths else "302")
                    config = self.plan_config(enabled, mode, api_paths)
                    for output in ("shell", "markdown"):
                        with self.subTest(enabled=enabled, mode=mode, paths=api_paths, output=output):
                            plan = sso.test_plan(config, output)
                            marker = ('test "$status" = ' if output == "shell" else "未認証期待HTTP ") + expected
                            count = 6 + int(output == "markdown" and enabled and expected == "200")
                            self.assertEqual(plan.count(marker), count)

    def test_generated_sso_api_shell_accepts_401_and_rejects_302(self):
        fake_bin = self.path / "bin"
        fake_bin.mkdir()
        curl = fake_bin / "curl"
        curl.write_text('''#!/usr/bin/env bash
set -eu
headers=""
status="$FAKE_STATUS"
while [ "$#" -gt 0 ]; do
  case "$1" in https://sso-test-ksvc.knative-public.example.test/) status=200;; esac
  if [ "$1" = "--dump-header" ]; then shift; headers="$1"; fi
  shift
done
if [ -n "$headers" ]; then
  printf 'HTTP/1.1 %s\\r\\nLocation: /oauth2/start?rd=fixture\\r\\n\\r\\n' "$status" > "$headers"
fi
printf '%s' "$status"
''')
        curl.chmod(0o755)
        config = self.plan_config(True, "sso", ["/get"])
        plan = sso.test_plan(config, "shell")
        subprocess.run(["bash", "-n"], input=plan, text=True, check=True)
        for status, success in (("401", True), ("302", False)):
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"], FAKE_STATUS=status)
            with self.subTest(status=status):
                result = subprocess.run(["bash"], input=plan, text=True, env=env, capture_output=True)
                self.assertEqual(result.returncode == 0, success)


if __name__ == "__main__":
    unittest.main()
