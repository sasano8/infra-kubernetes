#!/usr/bin/env python3
"""Plan and explicitly delete obsolete project resources, using Git and deployment inventories."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import infra
import inventory as inv

# Only these project resource types may be automatically deleted. No storage or CRDs.
API = {
    ("", "Namespace"): ("v1", "namespaces", False),
    ("", "Service"): ("v1", "services", True),
    ("", "Secret"): ("v1", "secrets", True),
    ("", "ConfigMap"): ("v1", "configmaps", True),
    ("", "ServiceAccount"): ("v1", "serviceaccounts", True),
    ("apps", "Deployment"): ("v1", "deployments", True),
    ("networking.k8s.io", "Ingress"): ("v1", "ingresses", True),
    ("networking.k8s.io", "IngressClass"): ("v1", "ingressclasses", False),
    ("rbac.authorization.k8s.io", "Role"): ("v1", "roles", True),
    ("rbac.authorization.k8s.io", "RoleBinding"): ("v1", "rolebindings", True),
    ("rbac.authorization.k8s.io", "ClusterRole"): ("v1", "clusterroles", False),
    ("rbac.authorization.k8s.io", "ClusterRoleBinding"): ("v1", "clusterrolebindings", False),
    ("serving.knative.dev", "Service"): ("v1", "services", True),
    ("cert-manager.io", "Issuer"): ("v1", "issuers", True),
    ("cert-manager.io", "Certificate"): ("v1", "certificates", True),
}
SHARED_NAMESPACES = {"default", "kube-system", "kube-public", "kube-node-lease",
                     "cert-manager", "knative-serving", "kourier-system"}


def qualified(group, plural):
    return plural + ("." + group if group else "")


class Kube:
    def __init__(self, command):
        self.command = shlex.split(command)
        if not self.command:
            raise ValueError("kubectl コマンドが空です")
        self.available = set(self.call("api-resources", "--verbs=list", "-o", "name").stdout.split())

    def call(self, *args, **kwargs):
        return subprocess.run(self.command + ["--request-timeout=15s", *map(str, args)],
                              check=True, capture_output=True, text=True, timeout=25, **kwargs)

    def meta(self, resource):
        group, kind = resource["group"], resource["kind"]
        _, plural, namespaced = API[(group, kind)]
        resource_type = qualified(group, plural)
        if resource_type not in self.available:
            return None
        args = ["get", resource_type, resource["name"], "--ignore-not-found"]
        if namespaced:
            args += ["-n", resource["namespace"]]
        # Never retrieve Secret data or annotations (last-applied may contain Secret data).
        template = '{{.metadata.uid}}|{{with .metadata.labels}}{{index . "infra.sasano8.dev/owner"}}{{end}}|{{range .metadata.ownerReferences}}{{.uid}},{{end}}'
        text = self.call(*args, "-o", "go-template=" + template).stdout.strip()
        if not text:
            return None
        uid, owner, parents = text.split("|", 2)
        if not uid:
            raise ValueError("UID がありません: " + inv.label(resource))
        return {"uid": uid, "owner": owner, "parents": parents}

    def cluster_uid(self):
        return self.meta(inv.identity("v1", "Namespace", "kube-system"))["uid"]

    def objects(self, resource_type):
        if resource_type not in self.available:
            return []
        return json.loads(self.call("get", resource_type, "--all-namespaces", "-o", "json").stdout).get("items", [])

    def ready(self, current, config):
        for resource in current.values():
            kind, group = resource["kind"], resource["group"]
            if (group, kind) != ("apps", "Deployment") and not (
                group == "serving.knative.dev" and kind == "Service"
            ) and not (config["tls"]["enabled"] and group == "cert-manager.io" and kind in ("Issuer", "Certificate")):
                continue
            _, plural, _ = API[(group, kind)]
            obj = json.loads(self.call("get", qualified(group, plural), resource["name"],
                                       "-n", resource["namespace"], "-o", "json").stdout)
            status = obj.get("status", {})
            ready = status.get("availableReplicas", 0) >= 1 if kind == "Deployment" else any(
                c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
            observed = status.get("observedGeneration", obj["metadata"].get("generation", 0))
            if not ready or observed < obj["metadata"].get("generation", 0):
                raise ValueError("新構成が Ready ではないため削除できません: " + inv.label(resource))

    def namespace_empty(self, name):
        types = self.call("api-resources", "--namespaced=true", "--verbs=list", "-o", "name").stdout.split()
        leftovers = []
        for resource_type in types:
            if resource_type in ("events", "events.events.k8s.io") or resource_type.endswith(
                (".metrics.k8s.io", ".custom.metrics.k8s.io", ".external.metrics.k8s.io")
            ):
                continue
            # List names only, including custom kinds. An access/list error blocks deletion.
            text = self.call("get", resource_type, "-n", name, "-o",
                             'go-template={{range .items}}{{.metadata.name}}{{"\\n"}}{{end}}').stdout
            for item in text.split():
                if (resource_type, item) not in (("serviceaccounts", "default"), ("configmaps", "kube-root-ca.crt")):
                    leftovers.append(resource_type + "/" + item)
        return leftovers


def from_object(obj):
    metadata = obj["metadata"]
    return inv.identity(obj["apiVersion"], obj["kind"], metadata["name"], metadata.get("namespace", ""))


def references(kube):
    """Declared Kubernetes references only; no Secret/ConfigMap contents are read."""
    edges = []
    types = ("deployments.apps", "statefulsets.apps", "daemonsets.apps", "replicasets.apps", "replicationcontrollers",
             "jobs.batch", "cronjobs.batch", "serviceaccounts",
             "pods", "services.serving.knative.dev", "ingresses.networking.k8s.io",
             "certificates.cert-manager.io", "issuers.cert-manager.io", "clusterissuers.cert-manager.io",
             "rolebindings.rbac.authorization.k8s.io", "clusterrolebindings.rbac.authorization.k8s.io",
             "horizontalpodautoscalers.autoscaling")
    for resource_type in types:
        for obj in kube.objects(resource_type):
            source = from_object(obj)
            # Controller-owned Pods are removed with their parents; check the parent's spec.
            if obj["kind"] in ("Pod", "ReplicaSet") and obj["metadata"].get("ownerReferences"):
                continue
            namespace = source["namespace"]
            def add(api, kind, name, ns=None):
                if name:
                    edges.append((inv.key(source), inv.key(inv.identity(api, kind, name, namespace if ns is None else ns))))
            def walk(value):
                if isinstance(value, list):
                    for entry in value:
                        walk(entry)
                elif isinstance(value, dict):
                    for field, child in value.items():
                        if field == "secretName" and isinstance(child, str):
                            add("v1", "Secret", child)
                        elif field in ("secretKeyRef", "secretRef", "apiTokenSecretRef", "privateKeySecretRef") and isinstance(child, dict):
                            add("v1", "Secret", child.get("name"), "cert-manager" if obj["kind"] == "ClusterIssuer" else namespace)
                        elif field in ("configMapKeyRef", "configMapRef", "configMap") and isinstance(child, dict):
                            add("v1", "ConfigMap", child.get("name"))
                        elif field == "imagePullSecrets" and isinstance(child, list):
                            for ref in child:
                                add("v1", "Secret", ref.get("name"))
                        elif field == "serviceAccountName" and isinstance(child, str):
                            add("v1", "ServiceAccount", child)
                        walk(child)
            spec = obj.get("spec", {})
            walk(spec)
            if obj["kind"] == "ServiceAccount":
                walk({"imagePullSecrets": obj.get("imagePullSecrets", [])})
                for secret in obj.get("secrets", []):
                    add("v1", "Secret", secret.get("name"))
            if obj["kind"] == "Ingress":
                add("networking.k8s.io/v1", "IngressClass", spec.get("ingressClassName"), "")
                backends = [spec.get("defaultBackend", {})]
                for rule in spec.get("rules", []):
                    backends += [p.get("backend", {}) for p in rule.get("http", {}).get("paths", [])]
                for backend in backends:
                    add("v1", "Service", backend.get("service", {}).get("name"))
            elif obj["kind"] == "Certificate":
                ref = spec.get("issuerRef", {})
                add(ref.get("group", "cert-manager.io") + "/v1", ref.get("kind", "Issuer"), ref.get("name"),
                    "" if ref.get("kind") == "ClusterIssuer" else namespace)
            elif obj["kind"] in ("RoleBinding", "ClusterRoleBinding"):
                ref = obj.get("roleRef", {})
                add("rbac.authorization.k8s.io/v1", ref.get("kind", "Role"), ref.get("name"),
                    "" if ref.get("kind") == "ClusterRole" else namespace)
                for subject in obj.get("subjects", []):
                    if subject.get("kind") == "ServiceAccount":
                        ns = subject.get("namespace", namespace)
                        add("v1", "ServiceAccount", subject.get("name"), ns)
                        add("v1", "Namespace", ns, "")
            elif obj["kind"] == "HorizontalPodAutoscaler":
                ref = spec.get("scaleTargetRef", {})
                add(ref.get("apiVersion", "apps/v1"), ref.get("kind", "Deployment"), ref.get("name"))
    return edges


def candidates(kube, historical, current):
    result = []
    namespace_cache = {}
    for resource_key in sorted(set(historical) - set(current)):
        resource = dict(historical[resource_key])
        descriptor = API.get((resource["group"], resource["kind"]))
        entry = dict(resource, action="delete", reason="")
        if descriptor is None:
            entry.update(action="blocked", reason="自動削除対象外の種類（共有基盤・保存データを含む）")
            result.append(entry)
            continue
        meta = kube.meta(resource)
        if meta is None:
            continue
        entry["uid"] = meta["uid"]
        if descriptor[2]:
            namespace = resource["namespace"]
            if namespace not in namespace_cache:
                namespace_cache[namespace] = kube.meta(inv.identity("v1", "Namespace", namespace))
            owner = (namespace_cache[namespace] or {}).get("owner")
        else:
            owner = meta["owner"]
        if owner not in inv.OWNERS:
            entry.update(action="blocked", reason="プロジェクトの所有情報を確認できない")
        elif meta["parents"]:
            entry.update(action="blocked", reason="コントローラ管理下のため親リソースから削除する")
        elif resource["kind"] == "Namespace":
            if resource["name"] in SHARED_NAMESPACES:
                entry.update(action="blocked", reason="共有 Namespace")
            else:
                entry["action"] = "delete-if-empty"
        result.append(entry)
    # A blocked old Issuer can retain its Secret too: propagate retained references to a fixed point.
    edges = references(kube)
    while True:
        eligible = {inv.key(e) for e in result if e["action"] in ("delete", "delete-if-empty")}
        newly_blocked = False
        for entry in result:
            if inv.key(entry) not in eligible:
                continue
            referrers = [source for source, target in edges if target == inv.key(entry) and source not in eligible]
            if referrers:
                entry.update(action="blocked", reason="残るリソースから参照中: " + ", ".join(sorted(set(referrers))))
                newly_blocked = True
        if not newly_blocked:
            break
    return result


@contextmanager
def api_proxy(kube):
    process = subprocess.Popen(kube.command + ["proxy", "--address=127.0.0.1", "--port=0"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    startup = queue.Queue(maxsize=100)
    def drain():
        for line in process.stdout:
            try:
                startup.put_nowait(line)
            except queue.Full:
                pass
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 15
        while True:
            if time.monotonic() >= deadline or process.poll() is not None:
                raise ValueError("UID 付き削除用のローカル API proxy を起動できません")
            try:
                line = startup.get(timeout=0.2)
            except queue.Empty:
                continue
            match = re.search(r"Starting to serve on 127\.0\.0\.1:(\d+)", line)
            if match:
                yield int(match.group(1))
                break
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=5)
        if not reader.is_alive():
            process.stdout.close()


def delete_uid(port, entry):
    group = entry["group"]
    version, plural, namespaced = API[(group, entry["kind"])]
    path = (f"/apis/{group}/{version}" if group else f"/api/{version}")
    if namespaced:
        path += "/namespaces/" + quote(entry["namespace"], safe="")
    path += "/" + plural + "/" + quote(entry["name"], safe="")
    body = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": entry["uid"]},
            "propagationPolicy": "Foreground"}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("DELETE", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()  # A Secret DELETE may return data; never log or save its response.
        if response.status not in (200, 202, 204, 404):
            raise ValueError(f"削除失敗 HTTP {response.status}: {inv.label(entry)}（UID 競合の場合は再計画してください）")
    finally:
        connection.close()


def source_inventory(from_ref, kube):
    head, historical, current = inv.history(from_ref)
    cluster_uid = kube.cluster_uid()
    # Successful deployment records cover definitions no longer reachable through Git ancestors.
    for path in (inv.STATE / "deployments").glob("*.json"):
        saved = json.loads(path.read_text())
        if saved.get("cluster_uid") != cluster_uid:
            continue
        for resource in saved.get("resources", []):
            historical.setdefault(inv.key(resource), dict(resource, first_seen=saved["commit"], last_seen=saved["commit"]))
    return head, historical, current


def apply_plan(kube, plan, historical, current, config):
    if plan.get("version") != 1 or plan.get("commit") != inv.resolve("HEAD") or plan.get("cluster_uid") != kube.cluster_uid():
        raise ValueError("plan の版・Git HEAD・クラスタ UID が一致しません。plan を作り直してください")
    kube.ready(current, config)
    verified = {inv.key(e): e for e in candidates(kube, historical, current)}
    selected = []
    for entry in plan["candidates"]:
        if entry["action"] not in ("delete", "delete-if-empty"):
            continue
        live = verified.get(inv.key(entry))
        if live is None:
            if kube.meta(entry) is None:
                continue
            raise ValueError("履歴差分の対象外になりました: " + inv.label(entry))
        if live["action"] != entry["action"] or live["uid"] != entry["uid"]:
            raise ValueError("所有・参照・UID が変わりました。再計画してください: " + inv.label(entry))
        selected.append(live)
    priority = {"Ingress": 0, "RoleBinding": 0, "ClusterRoleBinding": 0, "Deployment": 1,
                "Certificate": 2, "Issuer": 3, "Secret": 4, "ConfigMap": 4, "ServiceAccount": 5, "Role": 5,
                "ClusterRole": 5, "Namespace": 9}
    selected.sort(key=lambda e: (priority.get(e["kind"], 1), inv.key(e)))
    results = []
    remaining_keys = {inv.key(entry) for entry in selected}
    with api_proxy(kube) as port:
        for entry in selected:
            if inv.resolve("HEAD") != plan["commit"]:
                raise ValueError("削除中に Git HEAD が変わりました。再計画してください")
            meta = kube.meta(entry)
            if meta is None:
                remaining_keys.discard(inv.key(entry))
                continue
            if meta["uid"] != entry["uid"]:
                raise ValueError("UID が変わりました: " + inv.label(entry))
            owner = meta["owner"]
            if entry["namespace"]:
                owner = (kube.meta(inv.identity("v1", "Namespace", entry["namespace"])) or {}).get("owner")
            if owner not in inv.OWNERS or meta["parents"]:
                raise ValueError("所有情報が変わりました: " + inv.label(entry))
            if any(target == inv.key(entry) and source not in remaining_keys for source, target in references(kube)):
                raise ValueError("新しい参照を検出しました。再計画してください: " + inv.label(entry))
            if entry["kind"] == "Namespace":
                print("Namespace の残存リソースを確認します: " + entry["name"], flush=True)
                leftovers = kube.namespace_empty(entry["name"])
                if leftovers:
                    print("保留: " + inv.label(entry) + " / " + ", ".join(leftovers), flush=True)
                    results.append({"resource": entry, "status": "retained", "remaining": leftovers})
                    continue
            print("削除: " + inv.label(entry), flush=True)
            delete_uid(port, entry)
            deadline = time.monotonic() + 180
            while True:
                remaining = kube.meta(entry)
                if remaining is None:
                    break
                if remaining["uid"] != entry["uid"]:
                    raise ValueError("削除中に同名リソースが再作成されました: " + inv.label(entry))
                if time.monotonic() >= deadline:
                    raise ValueError("削除完了待ちがタイムアウトしました: " + inv.label(entry))
                time.sleep(2)
            results.append({"resource": entry, "status": "deleted"})
            remaining_keys.discard(inv.key(entry))
            inv.write_json(inv.STATE / "prune-result.json", {"commit": plan["commit"], "results": results})
    print(f"削除処理完了: {sum(r['status'] == 'deleted' for r in results)} 件削除。保留対象は再確認してください。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--from-ref", help="このコミットから HEAD までを確認（既定: 全祖先履歴）")
    parser.add_argument("--config", type=Path, default=inv.ROOT / "configs/environment/local.json")
    parser.add_argument("--plan", type=Path, default=inv.STATE / "prune-plan.json")
    parser.add_argument("--kubectl", default="kubectl")
    args = parser.parse_args()
    changed = inv.git("diff", "--name-only", "HEAD").splitlines()
    if any(inv.selected(path) for path in changed):
        raise ValueError("管理対象の定義に未コミット変更があります。確定してから計画してください")
    kube = Kube(args.kubectl)
    from_ref = args.from_ref
    plan = None
    if args.action == "apply":
        plan = json.loads(args.plan.read_text())
        from_ref = plan.get("from_ref")
    head, historical, current = source_inventory(from_ref, kube)
    if args.action == "plan":
        entries = candidates(kube, historical, current)
        data = {"version": 1, "commit": head, "from_ref": inv.resolve(from_ref) if from_ref else None,
                "cluster_uid": kube.cluster_uid(), "created_at": datetime.now(timezone.utc).isoformat(),
                "candidates": entries}
        inv.write_json(args.plan, data)
        for entry in entries:
            print(f"{entry['action']}: {inv.label(entry)}" + (" / " + entry["reason"] if entry["reason"] else ""))
        print(f"計画を保存しました: {args.plan}（削除は実行していません）")
    else:
        apply_plan(kube, plan, historical, current, infra.load(args.config))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, http.client.HTTPException) as exc:
        # Do not echo raw kubectl payloads; only metadata is written to plan/result files.
        print(f"エラー: {exc}", file=sys.stderr)
        sys.exit(1)
