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
    "argocd": {"namespace": "argocd", "release": "argo-cd", "chart": "argo-cd", "version": "10.9.2", "values": "values.yaml",
               "repo": "https://argoproj.github.io/argo-helm"},
    "infisical": {"namespace": "infisical-system", "release": "infisical-operator", "chart": "secrets-operator",
                  "version": "0.11.10", "repo": "https://dl.cloudsmith.io/public/infisical/helm-charts/helm/charts/"},
    "external-secrets": {"namespace": "external-secrets", "release": "external-secrets", "chart": "external-secrets",
                         "version": "2.11.0", "repo": "https://charts.external-secrets.io"},
}
DEFAULTS = {"argocd": {"enabled": False}, "infisical": {"enabled": False}, "external-secrets": {},
            "pinner": {"enabled": False, "image": "ghcr.io/sasano8/pinner:latest",
                       "dry_run": False, "interval": "5m", "registry_secret": ""}}
RESOURCE_IDENTITIES = (
    ("v1", "Service", "argocd-nobuffer-proxy", "test"),
    ("configuration.konghq.com/v1", "KongPlugin", "argocd-gateway-error", "test"),
    ("networking.k8s.io/v1", "Ingress", "argocd-nobuffer", "test"),
    ("serving.knative.dev/v1", "Service", "argocd-server", "argocd"),
    ("networking.k8s.io/v1", "NetworkPolicy", "argocd-knative-to-redis", "argocd"),
    ("networking.k8s.io/v1", "NetworkPolicy", "argocd-knative-to-repo-server", "argocd"),
    ("networking.k8s.io/v1", "NetworkPolicy", "argocd-knative-to-dex-server", "argocd"),
)
ARGOCD_KSERVICE = "argocd-server"


def dns_label(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value)


def normalize(config):
    supplied = config.get("plugins", {})
    if not isinstance(supplied, dict) or set(supplied) - set(NAMES):
        raise ValueError("plugins は argocd / infisical / external-secrets / pinner を指定してください")
    result = copy.deepcopy(DEFAULTS)
    for name, options in supplied.items():
        allowed = set(DEFAULTS[name]) | ({"publish"} if name == "argocd" else set())
        if not isinstance(options, dict) or set(options) - allowed:
            raise ValueError("plugins." + name + " に未対応の設定があります（ESOはupの対象外です）")
        result[name].update(options)
    for name in STANDARD:
        if type(result[name]["enabled"]) is not bool:
            raise ValueError("plugins." + name + ".enabled はtrue / falseです")
    argocd = result["argocd"]
    tls_enabled = config.get("tls", {}).get("enabled", False)
    # Preserve an explicitly private or externally installed Argo CD in older settings.
    argocd.setdefault("publish", argocd["enabled"] and tls_enabled)
    if type(argocd["publish"]) is not bool:
        raise ValueError("plugins.argocd.publish はtrue / falseです")
    config["plugins"] = result
    if argocd["publish"]:
        if not tls_enabled:
            raise ValueError("Argo CD の公開には tls.enabled=true が必要です")
        if argocd_host(config) == config["sso"]["auth_host"] and config["sso"]["enabled"]:
            raise ValueError("Argo CD の公開ホストと sso.auth_host は別の名前にしてください")
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


def resource(api, kind, name, namespace="", **fields):
    meta = {"name": name, "labels": dict(OWNER)}
    if namespace:
        meta["namespace"] = namespace
    return {"apiVersion": api, "kind": kind, "metadata": meta, **fields}


def argocd_route_id(config):
    argocd = config["plugins"]["argocd"]
    # The old publish-only configuration targets an externally installed Deployment.
    return "standard_nobuffer_domain" if argocd["publish"] and not argocd["enabled"] else "knative_nobuffer_domain"


def argocd_host(config):
    route = argocd_route_id(config)
    prefix = ARGOCD_KSERVICE + "-argocd-ksvc." if route == "knative_nobuffer_domain" else "argocd."
    return prefix + config["routes"][route]["domain"]


def render_argocd_routes(config, output, auth_plugin):
    target = output / "argocd-routes.json"
    if not config["plugins"]["argocd"]["publish"]:
        target.unlink(missing_ok=True)
        return
    host = argocd_host(config)
    route_id = argocd_route_id(config)
    route = config["routes"][route_id]
    items = []
    if route_id == "knative_nobuffer_domain":
        backend, port, secret = "kourier-proxy", 80, "knative-nobuffer-tls"
    else:
        backend, port, secret = "argocd-nobuffer-proxy", 443, "standard-nobuffer-tls"
        service = resource("v1", "Service", backend, "test", spec={
            "type": "ExternalName", "externalName": "argo-cd-argocd-server.argocd.svc.cluster.local",
            "ports": [{"name": "https", "port": port}],
        })
        service["metadata"]["annotations"] = {"konghq.com/protocol": "https"}
        items.append(service)
    ingress = resource("networking.k8s.io/v1", "Ingress", "argocd-nobuffer", "test", spec={
        "ingressClassName": "kong-ingress",
        "tls": [{"hosts": [host], "secretName": secret}],
        "rules": [{"host": host, "http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": {
            "service": {"name": backend, "port": {"number": port}},
        }}]}}],
    })
    plugins = "strip-error-reason"
    if auth_plugin:
        plugins += "," + auth_plugin
    if route_id == "knative_nobuffer_domain":
        # Knative's reverse proxy exposes its Go transport error in a plain-text 502.
        items.append(resource("configuration.konghq.com/v1", "KongPlugin", "argocd-gateway-error", "test",
            plugin="post-function", config={
                "header_filter": ["""if kong.response.get_source() == "service" and kong.response.get_status() == 502
    and kong.response.get_header("Content-Type") == "text/plain; charset=utf-8"
    and kong.response.get_header("X-Content-Type-Options") == "nosniff" then
    kong.ctx.plugin.replace_gateway_error = true
    kong.response.clear_header("Content-Length")
    kong.response.clear_header("Content-Encoding")
    kong.response.set_header("Cache-Control", "no-store")
end"""],
                "body_filter": [r'''if kong.ctx.plugin.replace_gateway_error then
    kong.ctx.plugin.replace_gateway_error = false
    kong.response.set_raw_body("Bad Gateway\n")
end'''],
            }))
        plugins += ",argocd-gateway-error"
    ingress["metadata"]["annotations"] = {
        "konghq.com/protocols": "https", "konghq.com/preserve-host": "true", "konghq.com/strip-path": "false",
        "konghq.com/plugins": plugins,
        "konghq.com/request-buffering": str(route.get("request_buffering", False)).lower(),
        "konghq.com/response-buffering": str(route["buffering"]).lower(),
    }
    items.append(ingress)
    target.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2) + "\n")


def apply_argocd_routes(cluster, output):
    target = output / "argocd-routes.json"
    items = []
    if target.exists():
        items = json.loads(target.read_text())["items"]
        owned_apply(cluster, items)
    active = {item["metadata"]["name"] for item in items}
    for _, kind, name, namespace in reversed(RESOURCE_IDENTITIES):
        if namespace == "test" and name not in active:
            remove_owned(cluster, kind.lower() + "/" + name, namespace)


def remove_owned(cluster, identifier, namespace):
    found = cluster.call("get", identifier, "-n", namespace, "--ignore-not-found", "-o",
                         'go-template={{index .metadata.labels "infra.sasano8.dev/owner"}}',
                         capture_output=True, text=True)
    if found.stdout.strip() == "infra-kubernetes":
        cluster.call("delete", identifier, "-n", namespace, "--ignore-not-found")


def argocd_knative_service(config, deployment):
    pod = deployment["spec"]["template"]
    pod_spec = pod["spec"]
    container = copy.deepcopy(pod_spec["containers"][0])
    port = next(port["containerPort"] for port in container["ports"] if port["name"] == "server")
    # Argo CD's plaintext HTTP/2 listener serves native gRPC, not browser HTTP.
    container["ports"] = [{"name": "http1", "containerPort": port}]
    container["env"] = [var for var in container["env"] if var["name"] != "ARGOCD_SERVER_INSECURE"]
    container["env"].append({"name": "ARGOCD_SERVER_INSECURE", "value": "true"})
    writable = {volume["name"] for volume in pod_spec["volumes"] if "emptyDir" in volume}
    for mount in container["volumeMounts"]:
        if mount["name"] not in writable:
            mount["readOnly"] = True
    for name in ("readinessProbe", "livenessProbe", "startupProbe"):
        if "httpGet" in container.get(name, {}):
            container[name]["httpGet"]["port"] = port
    # Keep the Helm server's RBAC and mounts. Do not select the Revision Pods from the old Service.
    annotations = dict(pod["metadata"].get("annotations", {}), **{"autoscaling.knative.dev/min-scale": "0"})
    service = resource("serving.knative.dev/v1", "Service", ARGOCD_KSERVICE, "argocd", spec={
        "template": {"metadata": {"annotations": annotations}, "spec": {
            "serviceAccountName": pod_spec["serviceAccountName"], "containers": [container],
            "volumes": pod_spec["volumes"], "timeoutSeconds": 300,
        }},
    })
    labels = service["metadata"]["labels"]
    labels["infra.sasano8.dev/route"] = "knative_nobuffer_domain"
    if not config["plugins"]["argocd"]["publish"]:
        labels["networking.knative.dev/visibility"] = "cluster-local"
    return service


def argocd_knative_network_policies():
    peer = {"podSelector": {"matchLabels": {"serving.knative.dev/service": ARGOCD_KSERVICE}}}
    return [resource("networking.k8s.io/v1", "NetworkPolicy", "argocd-knative-to-" + component, "argocd", spec={
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "argocd-" + component,
                                         "app.kubernetes.io/instance": CHARTS["argocd"]["release"]}},
        "policyTypes": ["Ingress"],
        "ingress": [{"from": [peer], "ports": [{"port": port, "protocol": "TCP"} for port in ports]}],
    }) for component, ports in (("redis", ("redis",)), ("repo-server", ("repo-server",)),
                                ("dex-server", ("http", "grpc")))]


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


def chart_values(name):
    return ROOT / "plugins" / name / CHARTS[name].get("values", "values.json")


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


def owned_apply(cluster, items, *, dry_run=False):
    # Inspect every object first; do not partially overwrite foreign resources.
    for item in items:
        meta = item["metadata"]
        args = ["get", item["kind"], meta["name"], "--ignore-not-found", "-o", "json"]
        if meta.get("namespace"):
            args += ["-n", meta["namespace"]]
        found = cluster.call(*args, capture_output=True, text=True).stdout
        if found.strip() and json.loads(found).get("metadata", {}).get("labels", {}).get("infra.sasano8.dev/owner") != "infra-kubernetes":
            raise ValueError("所有確認できないpluginリソースは変更しません: " + item["kind"] + "/" + meta["name"])
    options = ("--dry-run=server",) if dry_run else ()
    cluster.call("apply", "--server-side", "--field-manager=infra-platform", *options, "-f", "-",
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
    knative = name == "argocd" and argocd_route_id(config) == "knative_nobuffer_domain"
    values = []
    if knative:
        cluster.call("get", "crd/services.serving.knative.dev", "-o", "name")
        values += ["--set", "server.replicas=0,server.service.type=ClusterIP"]
        if config["plugins"]["argocd"]["publish"]:
            port = config["tls"]["host_https_port"] or 443
            origin = "https://" + argocd_host(config) + ("" if port == 443 else f":{port}")
            values += ["--set-string", "configs.cm.url=" + origin]
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
        if knative and existing:
            # Check the Knative admission policy before scaling down an existing server.
            deployment = cluster.get("deployment/argo-cd-argocd-server", "-n", "argocd")
            owned_apply(cluster, [argocd_knative_service(config, deployment)], dry_run=True)
        print("plugin install: " + name + " / chart " + chart["version"], flush=True)
        helm_call(env, "upgrade", "--install", chart["release"], chart["chart"], "--repo", chart["repo"],
                  "--version", chart["version"], "--namespace", chart["namespace"],
                  "--values", chart_values(name), "--reset-values", *values,
                  "--labels", "infra.sasano8.dev/owner=infra-kubernetes", "--wait=false", "--timeout", "300s")
    if knative:
        deployment = cluster.get("deployment/argo-cd-argocd-server", "-n", "argocd")
        owned_apply(cluster, argocd_knative_network_policies() + [argocd_knative_service(config, deployment)])
    elif name == "argocd" and existing:
        found = cluster.call("get", "crd/services.serving.knative.dev", "--ignore-not-found", "-o", "name",
                             capture_output=True, text=True)
        if found.stdout.strip():
            remove_owned(cluster, "ksvc/" + ARGOCD_KSERVICE, "argocd")
        for policy in argocd_knative_network_policies():
            remove_owned(cluster, "networkpolicy/" + policy["metadata"]["name"], "argocd")


def wait(name, cluster, config=None):
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
    if name == "argocd" and config and argocd_route_id(config) == "knative_nobuffer_domain":
        service = "ksvc/" + ARGOCD_KSERVICE
        cluster.call("wait", service, "-n", "argocd", "--for=condition=Ready", "--timeout=300s")
        if config["plugins"]["argocd"]["publish"]:
            cluster.call("wait", service, "-n", "argocd", "--for=jsonpath={.status.url}=http://" + argocd_host(config), "--timeout=180s")


def up(cluster, config, ensure_namespaces):
    for name in STANDARD:
        if config["plugins"][name]["enabled"]:
            install(name, cluster, config, ensure_namespaces)
            wait(name, cluster, config)
