"""Install platform controllers; application and Secret sync definitions are external."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"infra.sasano8.dev/owner": "infra-kubernetes"}
NAMES = ("argocd", "infisical", "external-secrets", "pinner")
STANDARD = ("argocd", "infisical", "pinner")  # ESO is deliberately never part of up.
CHARTS = {
    "argocd": {"namespace": "argocd", "release": "argo-cd", "chart": "argo-cd", "version": "10.9.2",
               "repo": "https://argoproj.github.io/argo-helm"},
    "infisical": {"namespace": "infisical-system", "release": "infisical-operator", "chart": "secrets-operator",
                  "version": "0.11.10", "repo": "https://dl.cloudsmith.io/public/infisical/helm-charts/helm/charts/"},
    "external-secrets": {"namespace": "external-secrets", "release": "external-secrets", "chart": "external-secrets",
                         "version": "2.11.0", "repo": "https://charts.external-secrets.io"},
}
DEFAULTS = {"argocd": {"enabled": False}, "infisical": {"enabled": False}, "external-secrets": {},
            "pinner": {"enabled": False, "image": "ghcr.io/sasano8/pinner:latest",
                       "dry_run": False, "interval": "5m", "registry_secret": ""}}


def dns_label(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value)


def normalize(config):
    supplied = config.get("plugins", {})
    if not isinstance(supplied, dict) or set(supplied) - set(NAMES):
        raise ValueError("plugins は argocd / infisical / external-secrets / pinner を指定してください")
    result = copy.deepcopy(DEFAULTS)
    for name, options in supplied.items():
        if not isinstance(options, dict) or set(options) - set(DEFAULTS[name]):
            raise ValueError("plugins." + name + " に未対応の設定があります（ESOはupの対象外です）")
        result[name].update(options)
    for name in STANDARD:
        if type(result[name]["enabled"]) is not bool:
            raise ValueError("plugins." + name + ".enabled はtrue / falseです")
    p = result["pinner"]
    if type(p["dry_run"]) is not bool:
        raise ValueError("plugins.pinner.dry_run はtrue / falseです")
    if not isinstance(p["interval"], str) or not re.fullmatch(r"[1-9][0-9]*(?:s|m|h)", p["interval"]):
        raise ValueError("plugins.pinner.interval は5m等の正の期間です")
    if (not isinstance(p["image"], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]+", p["image"])
            or (":" not in p["image"].rsplit("/", 1)[-1] and "@sha256:" not in p["image"])):
        raise ValueError("plugins.pinner.image はタグまたはdigest付きイメージ参照です")
    if p["registry_secret"] != "" and not dns_label(p["registry_secret"]):
        raise ValueError("plugins.pinner.registry_secret はpinner-systemの既存Secret名です")
    config["plugins"] = result


def resource(api, kind, name, namespace="", **fields):
    meta = {"name": name, "labels": dict(OWNER)}
    if namespace:
        meta["namespace"] = namespace
    return {"apiVersion": api, "kind": kind, "metadata": meta, **fields}


def pinner_resources(config):
    p = config["plugins"]["pinner"]
    ns = "pinner-system"
    subject = [{"kind": "ServiceAccount", "name": "pinner", "namespace": ns}]
    rbac = "rbac.authorization.k8s.io/v1"
    items = [resource("v1", "ServiceAccount", "pinner", ns),
             resource(rbac, "ClusterRole", "infra-pinner-reader", rules=[
                 {"apiGroups": [""], "resources": ["nodes", "pods"], "verbs": ["get", "list"]},
                 {"apiGroups": ["apps"], "resources": ["deployments", "statefulsets", "daemonsets"], "verbs": ["get", "list"]},
                 {"apiGroups": ["serving.knative.dev"], "resources": ["services", "revisions"], "verbs": ["get", "list"]}]),
             resource(rbac, "ClusterRoleBinding", "infra-pinner-reader", subjects=subject,
                      roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "infra-pinner-reader"}),
             resource(rbac, "ClusterRole", "infra-pinner-actor", rules=[
                 {"apiGroups": ["apps"], "resources": ["deployments", "statefulsets", "daemonsets"], "verbs": ["patch"]},
                 {"apiGroups": ["serving.knative.dev"], "resources": ["services"], "verbs": ["patch"]},
                 {"apiGroups": [""], "resources": ["pods"], "verbs": ["delete"]}]),
             resource(rbac, "ClusterRoleBinding", "infra-pinner-actor-global", subjects=subject,
                      roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "infra-pinner-actor"}),
             resource(rbac, "Role", "infra-pinner-pull", ns, rules=[
                 {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "get", "delete"]}]),
             resource(rbac, "RoleBinding", "infra-pinner-pull", ns, subjects=subject,
                      roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "infra-pinner-pull"})]
    # Match infra-k0s: one pull/trigger pair, all namespaces, annotation opt-in.
    containers = []
    for command in ("pull", "trigger"):
        args = [command, "--daemon", "--interval=" + p["interval"]]
        if command == "pull":
            args.append("--pull-namespace=" + ns)
        if p["dry_run"]:
            args.append("--dry-run")
        container = {"name": command, "image": p["image"], "imagePullPolicy": "Always",
                     "args": args, "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                     "limits": {"cpu": "250m", "memory": "128Mi"}},
                     "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                         "capabilities": {"drop": ["ALL"]}}}
        if command == "pull" and p["registry_secret"]:
            container["volumeMounts"] = [{"name": "registry", "mountPath": "/credential", "readOnly": True}]
            args.append("--credential=/credential/config.json")
        containers.append(container)
    pod = {"serviceAccountName": "pinner", "securityContext": {"runAsNonRoot": True,
           "seccompProfile": {"type": "RuntimeDefault"}}, "containers": containers}
    if p["registry_secret"]:
        pod["volumes"] = [{"name": "registry", "secret": {"secretName": p["registry_secret"],
                           "items": [{"key": ".dockerconfigjson", "path": "config.json"}]}}]
    items.append(resource("apps/v1", "Deployment", "pinner", ns, spec={"replicas": 1,
        "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": "pinner"}},
        "template": {"metadata": {"labels": {"app": "pinner", **OWNER}}, "spec": pod}}))
    items[-1]["metadata"]["annotations"] = {
        "floatingtag-pinner/enabled": "true",
        "floatingtag-pinner/on-mismatch": "delete-pods",
    }
    return items


def render(config, output):
    items = pinner_resources(config) if config["plugins"]["pinner"]["enabled"] else []
    (output / "pinner.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2) + "\n")


def require_helm():
    if not shutil.which("helm"):
        raise ValueError("Helm 3.13以降の3系を別途インストールしてください。make install-helmで導入・確認できます")
    result = subprocess.run(["helm", "version", "--short"], check=True, capture_output=True, text=True)
    match = re.match(r"v3\.([0-9]+)\.", result.stdout)
    if not match or int(match[1]) < 13:
        raise ValueError("Helm 3.13以降の3系を使用してください。make install-helmを使用するか既存Helmを更新してください")


@contextmanager
def helm_context(cluster):
    # Helm must use exactly the context selected by --kubectl (including k0s).
    # Never log the flattened kubeconfig or expose it as a command argument.
    result = cluster.call("config", "view", "--raw", "--minify", "--flatten", "-o", "json",
                          capture_output=True, text=True)
    with tempfile.TemporaryDirectory(prefix="infra-helm-") as directory:
        path = Path(directory) / "kubeconfig"
        with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
            file.write(result.stdout)
        yield dict(os.environ, KUBECONFIG=str(path))


def helm_call(env, *args):
    try:
        return subprocess.run(["helm", *map(str, args)], env=env, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        # Helm errors can contain rendered Secret material. Do not relay them.
        raise ValueError("Helm操作に失敗しました。対象releaseとcontrollerの状態を確認してください（機密保護のため出力非表示）") from None


def preflight(config):
    if any(config["plugins"][name]["enabled"] for name in ("argocd", "infisical")):
        require_helm()


def owned_apply(cluster, items):
    # Inspect every object first; do not partially overwrite foreign resources.
    for item in items:
        meta = item["metadata"]
        args = ["get", item["kind"], meta["name"], "--ignore-not-found", "-o", "json"]
        if meta.get("namespace"):
            args += ["-n", meta["namespace"]]
        found = cluster.call(*args, capture_output=True, text=True).stdout
        if found.strip() and json.loads(found).get("metadata", {}).get("labels", {}).get("infra.sasano8.dev/owner") != "infra-kubernetes":
            raise ValueError("所有確認できないpluginリソースは変更しません: " + item["kind"] + "/" + meta["name"])
    cluster.call("apply", "--server-side", "--field-manager=infra-platform", "-f", "-",
                 input=json.dumps({"apiVersion": "v1", "kind": "List", "items": items}), text=True)


def install(name, cluster, config, ensure_namespaces):
    if name == "pinner":
        ensure_namespaces(cluster, ("pinner-system",))
        if config["plugins"]["pinner"]["registry_secret"]:
            cluster.call("get", "secret", config["plugins"]["pinner"]["registry_secret"], "-n", "pinner-system", "-o", "name")
        saved = cluster.call("get", "configmap", "infra-pinner-targets", "-n", "pinner-system",
                             "--ignore-not-found", "-o", "json", capture_output=True, text=True).stdout
        previous = json.loads(saved) if saved.strip() else {}
        if previous and previous.get("metadata", {}).get("labels", {}).get("infra.sasano8.dev/owner") != "infra-kubernetes":
            raise ValueError("所有確認できないpinner配備記録は変更しません")
        old = json.loads(previous.get("data", {}).get("namespaces", "[]"))
        if not isinstance(old, list) or any(not dns_label(target) for target in old):
            raise ValueError("pinner配備記録のNamespace一覧が不正です")
        retired = []
        for target in sorted(set(old)):
            for kind in ("RoleBinding", "Role"):
                found = cluster.call("get", kind, "infra-pinner-target", "-n", target,
                                     "--ignore-not-found", "-o", "json", capture_output=True, text=True).stdout
                if found.strip():
                    if json.loads(found).get("metadata", {}).get("labels", {}).get("infra.sasano8.dev/owner") != "infra-kubernetes":
                        raise ValueError("旧pinner権限の所有確認ができません: " + target)
                    retired.append((kind, target))
        owned_apply(cluster, pinner_resources(config))
        for kind, target in retired:
            cluster.call("delete", kind, "infra-pinner-target", "-n", target, "--ignore-not-found")
        # The old namespace-scoped installation is obsolete after global RBAC.
        for kind, name in (("ClusterRoleBinding", "infra-pinner-nodes"),
                           ("ClusterRole", "infra-pinner-nodes"),
                           ("ConfigMap", "infra-pinner-targets")):
            args = ["get", kind, name, "--ignore-not-found", "-o", "json"]
            if kind == "ConfigMap":
                args += ["-n", "pinner-system"]
            found = cluster.call(*args, capture_output=True, text=True).stdout
            if found.strip() and json.loads(found).get("metadata", {}).get("labels", {}).get("infra.sasano8.dev/owner") == "infra-kubernetes":
                delete = ["delete", kind, name, "--ignore-not-found"]
                if kind == "ConfigMap":
                    delete += ["-n", "pinner-system"]
                cluster.call(*delete)
        return
    chart = CHARTS[name]
    require_helm()
    with helm_context(cluster) as env:
        existing = json.loads(helm_call(env, "list", "--all", "-n", chart["namespace"],
                                        "--filter", "^" + chart["release"] + "$", "-o", "json").stdout)
        if existing:
            # Helm Release.Labels has json:"-"; `helm status -o json` cannot
            # verify ownership. Query storage labels through list --selector.
            owned = json.loads(helm_call(env, "list", "--all", "-n", chart["namespace"],
                                        "--filter", "^" + chart["release"] + "$", "--selector",
                                        "infra.sasano8.dev/owner=infra-kubernetes", "-o", "json").stdout)
            if not owned:
                raise ValueError("外部管理のHelm releaseを引き継ぎません: " + chart["release"])
        ensure_namespaces(cluster, (chart["namespace"],))
        print("plugin install: " + name + " / chart " + chart["version"], flush=True)
        helm_call(env, "upgrade", "--install", chart["release"], chart["chart"], "--repo", chart["repo"],
                  "--version", chart["version"], "--namespace", chart["namespace"],
                  "--values", ROOT / "plugins" / name / "values.json", "--reset-values",
                  "--labels", "infra.sasano8.dev/owner=infra-kubernetes", "--wait=false", "--timeout", "300s")


def wait(name, cluster):
    if name == "pinner":
        cluster.call("rollout", "status", "deployment/pinner", "-n", "pinner-system", "--timeout=300s")
        return
    chart = CHARTS[name]
    # Include Argo CD's StatefulSet and operator webhook/cert-controller Deployments.
    found = cluster.get("deployments,statefulsets", "-n", chart["namespace"])
    if not found.get("items"):
        raise ValueError("pluginのworkloadが見つかりません。先にplugin installを実行してください: " + name)
    for item in found["items"]:
        cluster.call("rollout", "status", item["kind"].lower() + "/" + item["metadata"]["name"],
                     "-n", chart["namespace"], "--timeout=300s")


def up(cluster, config, ensure_namespaces):
    for name in STANDARD:
        if config["plugins"][name]["enabled"]:
            install(name, cluster, config, ensure_namespaces)
            wait(name, cluster)
