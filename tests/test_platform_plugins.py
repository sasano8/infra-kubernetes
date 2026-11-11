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
        if args[0] == "deployment/argo-cd-argocd-server":
            return {"spec": {"template": {"metadata": {}, "spec": {
                "serviceAccountName": "argocd-server", "volumes": [], "containers": [{
                    "name": "server", "image": "quay.io/argoproj/argocd:v3.5.3",
                    "ports": [{"name": "server", "containerPort": 8080}], "env": [], "volumeMounts": [],
                }],
            }}}}
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

    def test_legacy_argocd_publish_only_uses_standard_nobuffer(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            config_path = output / "config.json"
            raw = json.loads((ROOT / "configs/environment/example.json").read_text())
            raw["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test")
            raw["sso"].update(enabled=True, email_domain="example.test")
            raw["plugins"]["argocd"] = {"enabled": False, "publish": True}
            config_path.write_text(json.dumps(raw))
            config = infra.load(config_path)
            infra.render(config, output)
            target = output / "argocd-routes.json"
            service, ingress = json.loads(target.read_text())["items"]
            host = "argocd.standard-nobuffer.example.test"
            self.assertEqual(ingress["spec"]["rules"][0]["host"], host)
            self.assertEqual(ingress["spec"]["tls"], [{"hosts": [host], "secretName": "standard-nobuffer-tls"}])
            annotations = ingress["metadata"]["annotations"]
            self.assertEqual(annotations["konghq.com/request-buffering"], "false")
            self.assertEqual(annotations["konghq.com/response-buffering"], "false")
            self.assertEqual(annotations["konghq.com/plugins"], "strip-error-reason,sso-standard-nobuffer-domain")
            self.assertEqual(service["spec"]["externalName"], "argo-cd-argocd-server.argocd.svc.cluster.local")
            self.assertEqual(service["metadata"]["annotations"]["konghq.com/protocol"], "https")
            cluster = Cluster()
            with patch.object(plugins, "install") as install, patch.object(plugins, "wait") as wait:
                plugins.up(cluster, config, infra.ensure_namespaces)
            install.assert_not_called()
            wait.assert_not_called()
            infra.apply_routes(cluster, output)
            self.assertEqual(json.loads(cluster.calls[-1][1]["input"])["items"], [service, ingress])

            config["plugins"]["argocd"]["publish"] = False
            infra.render(config, output)
            self.assertFalse(target.exists())

    def test_argocd_knative_route_and_install(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            path = output / "config.json"
            raw = json.loads((ROOT / "configs/environment/example.json").read_text())
            raw["tls"].update(enabled=True, email="admin@example.test", cloudflare_zone="example.test")
            raw["sso"].update(enabled=True, email_domain="example.test")
            raw["plugins"]["argocd"] = {"enabled": True}
            path.write_text(json.dumps(raw))
            config = infra.load(path)
            infra.render(config, output)
            ingress, = json.loads((output / "argocd-routes.json").read_text())["items"]
            host = "argocd-server-argocd-ksvc.knative-nobuffer.example.test"
            self.assertEqual(ingress["spec"]["tls"], [{"hosts": [host], "secretName": "knative-nobuffer-tls"}])
            rule, = ingress["spec"]["rules"]
            self.assertEqual(rule["host"], host)
            self.assertEqual(rule["http"]["paths"][0]["backend"]["service"]["name"], "kourier-proxy")
            self.assertEqual(ingress["metadata"]["annotations"]["konghq.com/plugins"],
                             "strip-error-reason,sso-knative-nobuffer-domain")

            cluster = Cluster()
            with patch.object(plugins, "require_helm"), \
                 patch.object(plugins, "helm_call", return_value=SimpleNamespace(stdout="[]")) as helm:
                plugins.install("argocd", cluster, config, lambda *_: None)
            self.assertIn("server.replicas=0,server.service.type=ClusterIP", helm.call_args.args)
            self.assertIn("configs.cm.url=https://" + host, helm.call_args.args)
            service, = json.loads(cluster.calls[-1][1]["input"])["items"]
            self.assertEqual(service["apiVersion"], "serving.knative.dev/v1")
            self.assertEqual(service["metadata"]["labels"]["infra.sasano8.dev/route"], "knative_nobuffer_domain")
            template = service["spec"]["template"]
            self.assertEqual(template["metadata"]["annotations"]["autoscaling.knative.dev/min-scale"], "0")
            self.assertEqual(template["spec"]["serviceAccountName"], "argocd-server")
            self.assertEqual(template["spec"]["containers"][0]["ports"], [{"name": "h2c", "containerPort": 8080}])
            cluster.workloads = {"items": [{"kind": "Deployment", "metadata": {"name": "argo-cd-argocd-server"}}]}
            plugins.wait("argocd", cluster, config)
            self.assertIn("--for=jsonpath={.status.url}=http://" + host, cluster.calls[-1][0])

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
        bad = (("enabled", "true"), ("dry_run", 1), ("interval", "0m"), ("interval", "5m --debug"),
               ("image", "image"), ("image", "repo:latest --flag"), ("registry_secret", "../secret"))
        for name, value in bad:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                self.config({"pinner": {name: value}})

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
            values_file = "values.yaml" if name == "argocd" else "values.json"
            self.assertEqual(args[args.index("--values") + 1], ROOT / "plugins" / name / values_file)
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
