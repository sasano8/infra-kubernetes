"""Platform bootstrap boundaries, scoped RBAC, Helm ownership, and read-only waits."""
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra
import platform_plugins as plugins


class Cluster:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ("config", "view"):
            return SimpleNamespace(stdout='{"apiVersion":"v1","clusters":[]}')
        return SimpleNamespace(stdout=self.responses.get(args, ""))

    def get(self, *args):
        self.calls.append((args, {}))
        return self.workloads


class PlatformPluginTests(unittest.TestCase):
    def config(self, supplied=None):
        c = {"plugins": supplied or {}}
        plugins.normalize(c)
        return c

    def test_existing_config_keeps_all_standard_plugins_opt_in(self):
        c = self.config()
        self.assertFalse(any(c["plugins"][name]["enabled"] for name in plugins.STANDARD))
        with patch.object(plugins, "install") as install:
            plugins.up(Cluster(), c, lambda *args: None)
        install.assert_not_called()

    def test_eso_cannot_be_enabled_in_up(self):
        with self.assertRaisesRegex(ValueError, "ESO"):
            self.config({"external-secrets": {"enabled": True}})
        for field in ("argocd", "infisical", "pinner"):
            c = self.config({field: {"enabled": True}})
            self.assertEqual(c["plugins"]["external-secrets"], {})

    def test_up_installs_and_waits_in_agreed_order_without_eso(self):
        c = self.config({name: {"enabled": True} for name in plugins.STANDARD})
        calls = []
        with patch.object(plugins, "install", side_effect=lambda name, *a: calls.append(("install", name))), \
             patch.object(plugins, "wait", side_effect=lambda name, *a: calls.append(("wait", name))):
            plugins.up(Cluster(), c, lambda *args: None)
        self.assertEqual(calls, [(action, name) for name in ("argocd", "infisical", "pinner") for action in ("install", "wait")])

    def test_validation_rejects_dangerous_or_invalid_pinner_options(self):
        bad = (("enabled", "true"), ("dry_run", 1), ("namespaces", []), ("namespaces", ["test", "test"]),
               ("namespaces", ["../test"]), ("interval", "0m"), ("interval", "5m --debug"),
               ("image", "image"), ("image", "repo:latest --flag"), ("registry_secret", "../secret"))
        for name, value in bad:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                self.config({"pinner": {name: value}})

    def test_pinner_only_grants_node_reads_at_cluster_scope(self):
        c = self.config({"pinner": {"namespaces": ["test", "apps"]}})
        items = plugins.pinner_resources(c)
        cluster_roles = [x for x in items if x["kind"] == "ClusterRole"]
        self.assertEqual(len(cluster_roles), 1)
        self.assertEqual(cluster_roles[0]["rules"], [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}])
        targets = [x for x in items if x["kind"] == "RoleBinding" and x["metadata"]["name"] == "infra-pinner-target"]
        self.assertEqual({x["metadata"]["namespace"] for x in targets}, {"test", "apps"})
        self.assertTrue(all(x["subjects"][0]["namespace"] == "pinner-system" for x in targets))
        pod = items[-1]["spec"]["template"]["spec"]
        self.assertEqual(len(pod["containers"]), 4)
        for container in pod["containers"]:
            self.assertIn("--dry-run", container["args"])
            self.assertTrue(any(arg in ("--namespace=test", "--namespace=apps") for arg in container["args"]))
        self.assertNotIn("annotations", items[-1]["metadata"])  # no implicit self-update
        self.assertEqual(items[-1]["spec"]["strategy"], {"type": "Recreate"})

    def test_registry_credentials_are_references_not_values(self):
        c = self.config({"pinner": {"registry_secret": "registry", "dry_run": False}})
        pod = plugins.pinner_resources(c)[-1]["spec"]["template"]["spec"]
        pull, trigger = pod["containers"]
        self.assertIn("--credential=/credential/config.json", pull["args"])
        self.assertNotIn("--dry-run", trigger["args"])
        self.assertNotIn("volumeMounts", trigger)
        self.assertEqual(pod["volumes"][0]["secret"]["secretName"], "registry")

    def test_foreign_pinner_object_is_not_overwritten(self):
        c = Cluster()
        item = plugins.resource("v1", "ConfigMap", "example", "test")
        c.responses[("get", "ConfigMap", "example", "--ignore-not-found", "-o", "json", "-n", "test")] = '{"metadata":{"labels":{}}}'
        with self.assertRaisesRegex(ValueError, "所有確認"):
            plugins.owned_apply(c, [item])
        self.assertFalse(any(args[0] == "apply" for args, _ in c.calls))

    def test_helm_context_uses_selected_cluster_and_private_temporary_file(self):
        c = Cluster()
        with plugins.helm_context(c) as env:
            path = Path(env["KUBECONFIG"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text())["apiVersion"], "v1")
        self.assertFalse(path.exists())
        self.assertIn("--flatten", c.calls[0][0])

    def test_helm_install_is_namespaced_pinned_and_does_not_wait_or_force(self):
        for name in plugins.CHARTS:
            c = Cluster()
            with patch.object(plugins, "require_helm"), \
                 patch.object(plugins, "helm_call", return_value=SimpleNamespace(stdout="[]")) as run:
                plugins.install(name, c, self.config(), lambda *args: None)
            args = run.call_args.args[1:]
            self.assertIn("--version", args)
            self.assertIn(plugins.CHARTS[name]["version"], args)
            self.assertIn("--namespace", args)
            self.assertIn(plugins.CHARTS[name]["namespace"], args)
            self.assertIn("--wait=false", args)
            self.assertNotIn("--force", args)
            self.assertNotIn("--reuse-values", args)

    def test_helm_foreign_release_is_rejected_before_namespace_changes(self):
        c = Cluster()
        with patch.object(plugins, "require_helm"), \
             patch.object(plugins, "helm_call", side_effect=[SimpleNamespace(stdout='[{"name":"argo-cd"}]'),
                                                            SimpleNamespace(stdout='[]')]) as run:
            with self.assertRaisesRegex(ValueError, "外部管理"):
                plugins.install("argocd", c, self.config(), lambda *args: self.fail("namespace mutation"))
        self.assertTrue(any("--selector" in call.args for call in run.call_args_list))

    def test_owned_release_can_be_reapplied_using_helm_storage_labels(self):
        with patch.object(plugins, "require_helm"), \
             patch.object(plugins, "helm_call", side_effect=[SimpleNamespace(stdout='[{"name":"argo-cd"}]'),
                 SimpleNamespace(stdout='[{"name":"argo-cd"}]'), SimpleNamespace(stdout='')]) as run:
            plugins.install("argocd", Cluster(), self.config(), lambda *args: None)
        self.assertEqual(run.call_args.args[1], "upgrade")
        self.assertFalse(any("status" in call.args for call in run.call_args_list))

    def test_helm_errors_do_not_disclose_rendered_secrets(self):
        with patch.object(subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["helm"], stderr="secret-value")):
            with self.assertRaises(ValueError) as caught:
                plugins.helm_call({}, "upgrade")
        self.assertNotIn("secret-value", str(caught.exception))

    def test_wait_includes_statefulsets_and_never_writes(self):
        c = Cluster()
        c.workloads = {"items": [{"kind": "StatefulSet", "metadata": {"name": "controller"}},
                                  {"kind": "Deployment", "metadata": {"name": "server"}}]}
        plugins.wait("argocd", c)
        self.assertEqual([args[2] for args, _ in c.calls[1:]], ["statefulset/controller", "deployment/server"])
        self.assertTrue(all(args[0] in ("deployments,statefulsets", "rollout") for args, _ in c.calls))

    def test_pinner_namespace_reduction_revokes_old_owned_rbac(self):
        c = Cluster()
        c.responses[("get", "namespace", "test", "--ignore-not-found", "-o", "name")] = "namespace/test"
        old = plugins.resource("v1", "ConfigMap", "infra-pinner-targets", "pinner-system", data={"namespaces": '["test","retired"]'})
        c.responses[("get", "configmap", "infra-pinner-targets", "-n", "pinner-system", "--ignore-not-found", "-o", "json")] = json.dumps(old)
        for kind in ("RoleBinding", "Role"):
            c.responses[("get", kind, "infra-pinner-target", "-n", "retired", "--ignore-not-found", "-o", "json")] = json.dumps(plugins.resource("rbac.authorization.k8s.io/v1", kind, "infra-pinner-target", "retired"))
        plugins.install("pinner", c, self.config(), lambda *args: None)
        deletes = [args for args, _ in c.calls if args[0] == "delete"]
        self.assertEqual({args[1] for args in deletes}, {"RoleBinding", "Role"})
        self.assertTrue(all(args[4] == "retired" for args in deletes))
        final = json.loads([kwargs["input"] for args, kwargs in c.calls if args[0] == "apply"][-1])
        self.assertEqual(final["items"][0]["data"]["namespaces"], '["test"]')

    def test_cli_wait_only_dispatches_read_only_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "environment.json"
            config.write_text((ROOT / "configs/environment/example.json").read_text())
            with patch.object(sys, "argv", ["infra", "plugin", "wait", "external-secrets", "--config", str(config)]), \
                 patch.object(plugins, "wait") as wait, patch.object(plugins, "install") as install, \
                 patch.object(infra, "render") as render:
                infra.main()
            wait.assert_called_once()
            install.assert_not_called()
            render.assert_not_called()


if __name__ == "__main__":
    unittest.main()
