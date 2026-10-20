"""Offline safety checks. No Kubernetes cluster, network access, or credentials required."""
from contextlib import contextmanager, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import inventory as inv

spec = importlib.util.spec_from_file_location("prune_resources", ROOT / "scripts/prune-resources.py")
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


def resource(api, kind, name, namespace=""):
    return inv.identity(api, kind, name, namespace)


def obj(api, kind, name, namespace="", **kwargs):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": namespace}, **kwargs}


class FakeKube:
    def __init__(self, resources, objects=None, leftovers=None):
        self.metadata = {inv.key(r): {"uid": "uid-" + str(i), "owner": "infra-kubernetes", "parents": ""}
                         for i, r in enumerate(resources)}
        for r in resources:
            if r["namespace"]:
                ns = resource("v1", "Namespace", r["namespace"])
                self.metadata.setdefault(inv.key(ns), {"uid": "ns-" + r["namespace"], "owner": "infra-kubernetes", "parents": ""})
        self.listed_objects = objects or {}
        self.leftovers = leftovers or []
        self.ready_calls = 0

    def meta(self, r):
        return self.metadata.get(inv.key(r))

    def objects(self, kind):
        return self.listed_objects.get(kind, [])

    def cluster_uid(self):
        return "cluster-id"

    def ready(self, current, config):
        self.ready_calls += 1

    def namespace_empty(self, name):
        return self.leftovers


class SafetyChecks(unittest.TestCase):
    def setUp(self):
        self.old = resource("apps/v1", "Deployment", "old", "test")
        self.new = resource("apps/v1", "Deployment", "new", "test")

    def plan(self, kube, historical, current):
        return {"version": 1, "commit": "commit-id", "cluster_uid": "cluster-id",
                "candidates": prune.candidates(kube, historical, current)}

    def run_apply(self, kube, plan, historical, current):
        @contextmanager
        def proxy(_):
            yield 12345
        def delete(_, entry):
            kube.metadata.pop(inv.key(entry), None)
        with tempfile.TemporaryDirectory() as directory, patch.object(inv, "STATE", Path(directory)), \
                patch.object(inv, "resolve", return_value="commit-id"), patch.object(prune, "api_proxy", proxy), \
                patch.object(prune, "delete_uid", side_effect=delete) as deletion, redirect_stdout(io.StringIO()):
            prune.apply_plan(kube, plan, historical, current, {})
            return deletion.call_count

    def test_current_resources_never_become_candidates(self):
        kube = FakeKube([self.old, self.new])
        entries = prune.candidates(kube, {inv.key(self.old): self.old, inv.key(self.new): self.new}, {inv.key(self.new): self.new})
        self.assertEqual([inv.key(e) for e in entries], [inv.key(self.old)])

    def test_foreign_namespace_is_blocked(self):
        kube = FakeKube([self.old])
        kube.metadata[inv.key(resource("v1", "Namespace", "test"))]["owner"] = "someone-else"
        self.assertEqual(prune.candidates(kube, {inv.key(self.old): self.old}, {})[0]["action"], "blocked")

    def test_references_protect_an_old_issuer_and_its_secret(self):
        issuer = resource("cert-manager.io/v1", "Issuer", "old-issuer", "test")
        secret = resource("v1", "Secret", "old-account", "test")
        objects = {
            "certificates.cert-manager.io": [obj("cert-manager.io/v1", "Certificate", "active", "test",
                                                  spec={"issuerRef": {"name": "old-issuer"}})],
            "issuers.cert-manager.io": [obj("cert-manager.io/v1", "Issuer", "old-issuer", "test",
                                             spec={"acme": {"privateKeySecretRef": {"name": "old-account"}}})],
        }
        entries = prune.candidates(FakeKube([issuer, secret], objects), {inv.key(r): r for r in (issuer, secret)}, {})
        self.assertTrue(all(e["action"] == "blocked" for e in entries))

    def test_retained_service_account_protects_its_pull_secret(self):
        secret = resource("v1", "Secret", "old-pull-secret", "test")
        objects = {"serviceaccounts": [obj("v1", "ServiceAccount", "custom", "test",
                                          imagePullSecrets=[{"name": "old-pull-secret"}])]}
        entries = prune.candidates(FakeKube([secret], objects), {inv.key(secret): secret}, {})
        self.assertEqual(entries[0]["action"], "blocked")

    def test_not_ready_replacement_blocks_apply(self):
        kube = FakeKube([self.old])
        kube.ready = lambda *_: (_ for _ in ()).throw(ValueError("新構成が Ready ではない"))
        historical = {inv.key(self.old): self.old}
        with self.assertRaisesRegex(ValueError, "Ready"):
            self.run_apply(kube, self.plan(kube, historical, {}), historical, {})
        self.assertIsNotNone(kube.meta(self.old))

    def test_namespace_list_failure_blocks_deletion(self):
        namespace = resource("v1", "Namespace", "old-namespace")
        kube = FakeKube([namespace])
        kube.namespace_empty = lambda *_: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["kubectl", "get"]))
        historical = {inv.key(namespace): namespace}
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_apply(kube, self.plan(kube, historical, {}), historical, {})
        self.assertIsNotNone(kube.meta(namespace))

    def test_uid_change_aborts_before_any_delete(self):
        historical = {inv.key(self.old): self.old}
        kube = FakeKube([self.old])
        plan = self.plan(kube, historical, {})
        kube.metadata[inv.key(self.old)]["uid"] = "replacement-uid"
        with self.assertRaisesRegex(ValueError, "UID"):
            self.run_apply(kube, plan, historical, {})

    def test_cluster_change_aborts_before_readiness_or_delete(self):
        kube = FakeKube([self.old])
        plan = self.plan(kube, {inv.key(self.old): self.old}, {})
        plan["cluster_uid"] = "different-cluster"
        with self.assertRaisesRegex(ValueError, "クラスタ UID"):
            self.run_apply(kube, plan, {inv.key(self.old): self.old}, {})
        self.assertEqual(kube.ready_calls, 0)

    def test_current_resource_cannot_be_injected_into_a_plan(self):
        kube = FakeKube([self.old, self.new])
        historical = {inv.key(self.old): self.old, inv.key(self.new): self.new}
        plan = self.plan(kube, historical, {inv.key(self.new): self.new})
        plan["candidates"].append(dict(self.new, action="delete", uid=kube.meta(self.new)["uid"]))
        with self.assertRaisesRegex(ValueError, "対象外"):
            self.run_apply(kube, plan, historical, {inv.key(self.new): self.new})

    def test_unknown_resources_keep_a_namespace(self):
        namespace = resource("v1", "Namespace", "old-namespace")
        kube = FakeKube([namespace], leftovers=["persistentvolumeclaims/important-data"])
        historical = {inv.key(namespace): namespace}
        self.assertEqual(self.run_apply(kube, self.plan(kube, historical, {}), historical, {}), 0)
        self.assertIsNotNone(kube.meta(namespace))

    def test_an_empty_owned_namespace_can_be_deleted(self):
        namespace = resource("v1", "Namespace", "old-namespace")
        historical = {inv.key(namespace): namespace}
        kube = FakeKube([namespace])
        self.assertEqual(self.run_apply(kube, self.plan(kube, historical, {}), historical, {}), 1)

    def test_shared_namespace_is_never_deleted(self):
        namespace = resource("v1", "Namespace", "cert-manager")
        entry = prune.candidates(FakeKube([namespace]), {inv.key(namespace): namespace}, {})[0]
        self.assertEqual(entry["action"], "blocked")

    def test_delete_uses_api_uid_preconditions(self):
        entry = dict(resource("v1", "Secret", "retired-token", "test"), uid="expected-uid")
        response = SimpleNamespace(status=200, read=lambda: b'{"data":{"token":"never-log-this"}}')
        with patch.object(prune.http.client, "HTTPConnection") as connection, redirect_stdout(io.StringIO()) as output:
            connection.return_value.getresponse.return_value = response
            prune.delete_uid(12345, entry)
            args, kwargs = connection.return_value.request.call_args
        self.assertEqual(args[:2], ("DELETE", "/api/v1/namespaces/test/secrets/retired-token"))
        self.assertEqual(json.loads(kwargs["body"])["preconditions"], {"uid": "expected-uid"})
        self.assertNotIn("never-log-this", output.getvalue())

    def test_history_restores_renamed_and_deleted_definitions_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
            git("init", "--initial-branch=main")
            git("config", "user.name", "Inventory Check")
            git("config", "user.email", "inventory@example.test")
            app = root / "apps/demo/app.yaml"
            app.parent.mkdir(parents=True)
            app.write_text("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: old\n  namespace: test\n")
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "tls.py").write_text('raise RuntimeError("history must not execute")\ndef render():\n    return {"apiVersion":"v1", "kind":"Secret", "metadata":{"name":"token", "namespace":"test"}}\n')
            git("add", ".")
            git("commit", "-m", "old")
            app.write_text(app.read_text().replace("name: old", "name: new"))
            git("add", ".")
            git("commit", "-m", "rename")
            with patch.object(inv, "ROOT", root):
                _, historical, current = inv.history()
            self.assertIn(inv.key(self.old), historical)
            self.assertNotIn(inv.key(self.old), current)
            self.assertIn(inv.key(self.new), current)
            self.assertIn(inv.key(resource("v1", "Secret", "token", "test")), current)


if __name__ == "__main__":
    unittest.main()
