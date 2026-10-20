#!/usr/bin/env python3
"""Host installation and isolated sample application resources; Python standard library only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import string
import subprocess
import sys
import tarfile
import tempfile
import time
import tls

ROOT = Path(__file__).resolve().parents[1]
K0S = "v1.34.3+k0s.0"
KNATIVE = "knative-v1.20.0"
KONG_CHART = "2.47.0"
# Published digest from Kong/charts gh-pages/index.yaml.
KONG_CHART_SHA256 = "41679730cf7016c2d43e28176c2b5850a166d90f59e0003de7096318985a4696"
TEMPLATE = "{{.Name}}-{{.Namespace}}-ksvc.{{.Domain}}"
OWNER = {"infra.sasano8.dev/owner": "infra-kubernetes"}
NAMESPACES = ("test", "kong-system")
MANIFESTS = {
    "apps": ROOT / "apps/hello/manifests/apps.yaml.tmpl",
    "kong": ROOT / "plugins/kong/manifests/kong.yaml.tmpl",
    "routes": ROOT / "plugins/kong/manifests/routes.yaml.tmpl",
}


def run(args, **kwargs):
    print("+ " + shlex.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def load(path):
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"設定項目が重複しています: {key}")
            result[key] = value
        return result

    c = json.loads(path.read_text(), object_pairs_hook=unique_fields)
    if not isinstance(c, dict):
        raise ValueError("設定ファイルのルートは JSON オブジェクトにしてください")
    if "domain" in c:
        if "knative_domain" in c:
            raise ValueError("domain と knative_domain は同時に指定できません")
        c["knative_domain"] = c.pop("domain")
    if "standard_host" in c:
        if "standard_domain" in c:
            raise ValueError("standard_host と standard_domain は同時に指定できません")
        c["standard_domain"] = c.pop("standard_host")
    c.setdefault("base_domain", None)
    required = {"mode", "knative_domain", "standard_domain", "kourier_service", "node_port"}
    missing = required - set(c)
    unknown = set(c) - required - {"base_domain", "tls"}
    if missing or unknown:
        errors = []
        if missing:
            errors.append("必須項目がありません: " + ", ".join(sorted(missing)))
        if unknown:
            errors.append("未対応の項目: " + ", ".join(sorted(unknown)))
        raise ValueError(" / ".join(errors))
    if c["mode"] not in ("new-host", "existing"):
        raise ValueError("mode は new-host / existing")
    for key in ("base_domain", "knative_domain", "standard_domain", "kourier_service"):
        value = c[key]
        if key == "base_domain" and value is None:
            continue
        if not isinstance(value, str) or len(value) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
            for x in value.split(".")
        ):
            raise ValueError(f"{key}: 小文字の DNS 名を指定してください")
    host = "hello-test-ksvc." + c["knative_domain"]
    standard_host = "hello-test-ssvc." + c["standard_domain"]
    if max(len(host), len(standard_host)) > 253 or host == standard_host:
        raise ValueError("Knative と通常 Service のホスト名を区別してください（最大253文字）")
    port = c["node_port"]
    if port is not None and (type(port) is not int or not 30000 <= port <= 32767):
        raise ValueError("node_port は null または 30000〜32767")
    if c["mode"] == "new-host" and c["kourier_service"] != "kourier.kourier-system.svc.cluster.local":
        raise ValueError("new-host の Kourier Service は kourier.kourier-system.svc.cluster.local 固定です")
    tls.normalize(c)
    return c


def configure(path):
    if path.exists():
        raise ValueError(f"既存設定を上書きしません: {path}")
    c = json.loads((ROOT / "configs/environment/example.json").read_text())
    prompts = {
        "mode": "新規 VM=new-host / 既存クラスタ=existing",
        "base_domain": "ベースドメイン（参照情報。各経路は個別指定）",
        "knative_domain": "Knative ドメイン（既存の場合は既存設定に合わせる）",
        "standard_domain": "通常経路のベースドメイン（hello-test-ssvc を付ける）",
        "kourier_service": "Kourier のクラスタ内 Service DNS 名",
        "node_port": "Kong NodePort（空欄なら非公開、port-forward 使用）",
    }
    for key, prompt in prompts.items():
        answer = input(f"{prompt} [{c[key] if c[key] is not None else 'なし'}]: ").strip()
        if answer:
            c[key] = int(answer) if key == "node_port" else answer
    # Validate before creating the destination.
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "config.json"
        candidate.write_text(json.dumps(c))
        load(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as f:
        f.write(json.dumps(c, indent=2) + "\n")
    print(f"設定を保存しました: {path}")


def render(c, output):
    output.mkdir(parents=True, exist_ok=True)
    values = dict(c, knative_host="hello-test-ksvc." + c["knative_domain"],
                  standard_host="hello-test-ssvc." + c["standard_domain"])
    values.update(
        service_type="NodePort" if c["node_port"] else "ClusterIP",
        node_port_line=f"      nodePort: {c['node_port']}" if c["node_port"] else "",
        host_https_port_line=(f"              hostPort: {c['tls']['host_https_port']}"
                              if c["tls"]["enabled"] and c["tls"]["host_https_port"] else ""),
        rollout_strategy=("  strategy:\n    type: Recreate"
                          if c["tls"]["enabled"] and c["tls"]["host_https_port"] else ""),
        standard_tls=(f"  tls:\n    - hosts: [{values['standard_host']}]\n      secretName: standard-tls"
                      if c["tls"]["enabled"] else ""),
        knative_tls=(f"  tls:\n    - hosts: [{values['knative_host']}]\n      secretName: knative-wildcard-tls"
                     if c["tls"]["enabled"] else ""),
    )
    for name in MANIFESTS:
        text = MANIFESTS[name].read_text()
        (output / f"{name}.yaml").write_text(string.Template(text).substitute(values))
    tls.render(c, output)
    knative = output / "knative-config"
    knative.mkdir(exist_ok=True)
    for name in ("network.yaml", "domain.yaml.tmpl", "kourier-svc.yaml"):
        text = (ROOT / "plugins/knative/config" / name).read_text()
        (knative / name.removesuffix(".tmpl")).write_text(string.Template(text).substitute(values))
    print(f"マニフェストを生成しました: {output}")


class Cluster:
    def __init__(self, command):
        self.command = shlex.split(command)
        if not self.command:
            raise ValueError("kubectl コマンドが空です")

    def call(self, *args, **kwargs):
        return run(self.command + list(args), **kwargs)

    def get(self, *args):
        return json.loads(self.call("get", *args, "-o", "json", capture_output=True, text=True).stdout)


def fetch(url, destination):
    run(["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "20",
         "--max-time", "600", "--proto", "=https", "--tlsv1.2", "--output", destination, url])


def install_host():
    if os.geteuid() != 0 or platform.system() != "Linux":
        raise ValueError("新規 Linux VM 上で sudo を付けて実行してください")
    if not Path("/run/systemd/system").is_dir():
        raise ValueError("systemd が動作するホストが必要です")
    for tool in ("curl", "systemctl"):
        if not shutil.which(tool):
            raise ValueError(f"前提ツールがありません: {tool}")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if not arch:
        raise ValueError("対応 CPU: amd64 / arm64")
    state = Path("/var/lib/infra-kubernetes")
    marker = state / "k0s-version"
    owned = marker.exists() and marker.read_text().strip() == K0S
    if not owned and any(p.exists() for p in (
        Path("/var/lib/k0s"), Path("/etc/k0s"), Path("/usr/local/bin/k0s"),
        Path("/etc/systemd/system/k0scontroller.service"),
        Path("/etc/systemd/system/k0sworker.service"),
    )):
        raise ValueError("既存 k0s を検出しました。new-host は使用せず existing を選択してください")
    if not owned and shutil.which("k0s"):
        raise ValueError("既存 k0s が PATH にあります。existing を使用してください")
    if not owned and Path("/etc/cni/net.d").is_dir():
        existing_cni = [p.name for p in Path("/etc/cni/net.d").iterdir()
                        if p.suffix in (".conf", ".conflist", ".json")]
        if existing_cni:
            raise ValueError("既存 CNI 設定と競合する可能性があります。自動削除しません: "
                             + ", ".join(sorted(existing_cni)))
    binary = Path("/usr/local/bin/k0s")
    if not binary.exists():
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            asset = f"k0s-{K0S}-{arch}"
            base = f"https://github.com/k0sproject/k0s/releases/download/{K0S}"
            fetch(f"{base}/{asset}", tmp / asset)
            fetch(f"{base}/sha256sums.txt", tmp / "sha256sums.txt")
            hashes = [line.split()[0] for line in (tmp / "sha256sums.txt").read_text().splitlines()
                      if len(line.split()) == 2 and line.split()[1].lstrip("*") == asset]
            digest = hashlib.sha256((tmp / asset).read_bytes()).hexdigest()
            if hashes != [digest]:
                raise ValueError("k0s SHA-256 検証に失敗しました")
            state.mkdir(parents=True, exist_ok=True)
            marker.write_text(K0S + "\n")
            run(["install", "-m", "0755", tmp / asset, binary])
    version = run([binary, "version"], capture_output=True, text=True).stdout.strip()
    if version != K0S:
        raise ValueError(f"k0s のバージョンが異なります: {version}; 自動更新しません")
    config_source = ROOT / "configs/kube/k0s.yaml"
    config_target = Path("/etc/k0s/k0s.yaml")
    service = Path("/etc/systemd/system/k0scontroller.service")
    if service.exists() and not config_target.exists():
        raise ValueError("既存サービスに対応する /etc/k0s/k0s.yaml がありません。自動再設定しません")
    if config_target.exists() and config_target.read_bytes() != config_source.read_bytes():
        raise ValueError("/etc/k0s/k0s.yaml がリポジトリ設定と異なります。自動上書きしません")
    if not config_target.exists():
        config_target.parent.mkdir(parents=True, exist_ok=True)
        run(["install", "-m", "0600", config_source, config_target])
    if not service.exists():
        run([binary, "install", "controller", "--single", "--config", config_target])
    status = subprocess.run(
        ["systemctl", "is-active", "--quiet", "k0scontroller.service"],
        check=False,
    )
    if status.returncode != 0:
        run([binary, "start"])
    else:
        print("k0s は起動済みです。API / Node の確認に進みます。", flush=True)
    cluster = Cluster(f"{binary} kubectl")
    deadline = time.monotonic() + 300
    while True:
        try:
            if cluster.get("nodes", "--request-timeout=10s").get("items"):
                break
        except subprocess.CalledProcessError:
            pass
        if time.monotonic() >= deadline:
            raise ValueError("k0s API / Node 登録がタイムアウトしました。journalctl -u k0scontroller を確認してください")
        time.sleep(5)
    cluster.call("wait", "node", "--all", "--for=condition=Ready", "--timeout=300s")
    return cluster


def apply_admission_config(cluster, path, description, manager, force=False):
    # A Ready Pod/endpoint alone does not demonstrate admission calls from the API server.
    # Probe the actual intended configuration without persisting it, then apply it.
    deadline = time.monotonic() + 180
    transient = (
        "context deadline exceeded", "i/o timeout", "connection refused",
        'no endpoints available for service',
        "net/http: request canceled", "TLS handshake timeout",
        "x509: certificate signed by unknown authority",
    )
    arguments = ("apply", "--server-side", f"--field-manager={manager}", "--request-timeout=20s",
                 "-f", path) + (("--force-conflicts",) if force else ())
    attempt = 0
    while True:
        attempt += 1
        print(f"{description} webhook の実呼び出しを確認します（試行 {attempt}、待機上限180秒）。", flush=True)
        try:
            cluster.call(*arguments, "--dry-run=server", capture_output=True, text=True,
                         timeout=min(25, max(1, deadline - time.monotonic())))
            cluster.call(*arguments, capture_output=True, text=True,
                         timeout=min(25, max(1, deadline - time.monotonic())))
            print(f"{description} webhook の呼び出しと追加設定の適用が成功しました。", flush=True)
            return
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "") + (exc.stdout or "")
            if not ("failed calling webhook" in detail and any(x in detail for x in transient)):
                raise
            print(detail.strip(), file=sys.stderr, flush=True)
        except subprocess.TimeoutExpired:
            # The bounded readiness request itself timed out; never count this as ready.
            print("webhook 確認リクエストがタイムアウトしました。", file=sys.stderr, flush=True)
        if time.monotonic() >= deadline:
            raise ValueError(f"{description} webhook の呼び出しが180秒以内に安定しませんでした。"
                             "対象 webhook のログを確認してください")
        print("webhook の通信準備を待っています。5秒後に実呼び出しを再確認します。", flush=True)
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def apply_serving_core(cluster, source):
    # Use kubectl to decode the official YAML; no additional Python YAML dependency.
    converted = cluster.call("create", "--dry-run=client", "--validate=false", "-f", source,
                             "-o", "json", capture_output=True, text=True)
    # Depending on kubectl's version/input, output may be a List or consecutive
    # pretty-printed JSON objects. Decode the complete stream, including all objects.
    decoder = json.JSONDecoder()
    text = converted.stdout
    offset = 0
    resources = []
    while offset < len(text):
        if text[offset].isspace():
            offset += 1
            continue
        manifest, offset = decoder.raw_decode(text, offset)
        resources.extend(manifest["items"] if manifest.get("kind") == "List" else [manifest])
    if not resources:
        raise ValueError("Serving マニフェストの JSON 変換結果が空です")
    retained = []
    for resource in resources:
        kind = resource["kind"]
        metadata = resource["metadata"]
        dynamic = kind in ("MutatingWebhookConfiguration", "ValidatingWebhookConfiguration")
        dynamic = dynamic or (kind == "Secret" and metadata.get("namespace") == "knative-serving"
                              and metadata["name"] == "webhook-certs")
        if dynamic:
            arguments = ["get", kind, metadata["name"], "--ignore-not-found", "-o", "name"]
            if metadata.get("namespace"):
                arguments += ["-n", metadata["namespace"]]
            found = cluster.call(*arguments, capture_output=True, text=True)
            if found.stdout.strip():
                print(f"Knative 管理の既存リソースを保持します: {kind}/{metadata['name']}", flush=True)
                continue
        retained.append(resource)
    cluster.call("apply", "--server-side", "--field-manager=knative-upstream", "-f", "-",
                 input=json.dumps({"apiVersion": "v1", "kind": "List", "items": retained}), text=True)


def bootstrap(cluster, output):
    # Only called for a new, installer-owned host. Never in existing mode.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for asset in ("serving-crds.yaml", "serving-core.yaml"):
            fetch(f"https://github.com/knative/serving/releases/download/{KNATIVE}/{asset}", tmp / asset)
        fetch(f"https://github.com/knative-extensions/net-kourier/releases/download/{KNATIVE}/kourier.yaml",
              tmp / "kourier.yaml")
        # Match the new-host override before applying upstream, including on reruns.
        # Avoid transient LoadBalancer exposure and competing field ownership.
        kourier = (tmp / "kourier.yaml").read_text()
        if kourier.count("  type: LoadBalancer\n") != 1:
            raise ValueError("上流 Kourier Service の形式が変わりました。公開方法を確認してください")
        (tmp / "kourier.yaml").write_text(
            kourier.replace("  type: LoadBalancer\n", "  type: ClusterIP\n")
        )
        upstream = ("apply", "--server-side", "--field-manager=knative-upstream")
        cluster.call(*upstream, "-f", tmp / "serving-crds.yaml")
        cluster.call("wait", "crd", "--all", "--for=condition=Established", "--timeout=120s")
        apply_serving_core(cluster, tmp / "serving-core.yaml")
        cluster.call("rollout", "status", "deployment/webhook", "-n", "knative-serving", "--timeout=300s")
        cluster.call(*upstream, "-f", tmp / "kourier.yaml")
    apply_admission_config(cluster, output / "knative-config", "Knative", "knative-config", force=True)
    for namespace in ("knative-serving", "kourier-system"):
        cluster.call("rollout", "status", "deployment", "-n", namespace, "--timeout=300s")


def find_legacy_gateway(cluster):
    # Old resources are identified by both their dedicated namespace and original selector.
    result = cluster.call("get", "namespace/sas47-kong", "--ignore-not-found", "-o", "json",
                          capture_output=True, text=True)
    if not result.stdout.strip():
        return {}
    if not tls.is_owned(json.loads(result.stdout)):
        raise ValueError("所有ラベルのない旧 Kong Namespace は移行しません")
    result = cluster.call("get", "deployment/kong", "-n", "sas47-kong", "--ignore-not-found", "-o", "json",
                          capture_output=True, text=True)
    if not result.stdout.strip():
        return {}
    deployment = json.loads(result.stdout)
    if deployment.get("spec", {}).get("selector", {}).get("matchLabels") != {"app": "sas47-kong"}:
        raise ValueError("旧 Kong の selector が変更されています。自動移行しません")
    return deployment


def deploy(cluster, c, output):
    network = cluster.get("configmap/config-network", "-n", "knative-serving")
    if network.get("data", {}).get("domain-template") != TEMPLATE:
        raise ValueError("既存 domain-template がこの構成の前提と異なります。自動変更しません")
    # Reserve namespaces before writing anything inside them. Do not adopt unrelated namespaces.
    missing = []
    for name in NAMESPACES:
        result = cluster.call("get", "namespace", name, "--ignore-not-found", "-o", "json",
                              capture_output=True, text=True)
        if result.stdout.strip():
            found = json.loads(result.stdout)
            if not tls.is_owned(found):
                raise ValueError(f"所有ラベルのない既存 namespace を変更しません: {name}")
        else:
            missing.append(name)
    for name in missing:
        manifest = {"apiVersion": "v1", "kind": "Namespace",
                    "metadata": {"name": name, "labels": OWNER}}
        cluster.call("create", "-f", "-", input=json.dumps(manifest), text=True)
    render(c, output)
    install_kic_prerequisites(cluster, output)
    legacy_gateway = find_legacy_gateway(cluster)
    legacy_ports = [p["hostPort"] for container in legacy_gateway.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                    for p in container.get("ports", []) if p.get("hostPort")] if legacy_gateway.get("spec", {}).get("replicas", 1) != 0 else []
    tls.prepare(cluster, c, output, fetch, apply_admission_config, legacy_ports)
    cluster.call("apply", "-f", output / "apps.yaml")
    cluster.call("rollout", "status", "deployment/hello-ssvc", "-n", "test", "--timeout=300s")
    cluster.call("wait", "ksvc/hello", "-n", "test", "--for=condition=Ready", "--timeout=300s")
    expected = "http://hello-test-ksvc." + c["knative_domain"]
    deadline = time.monotonic() + 180
    while True:
        service = cluster.get("ksvc/hello", "-n", "test", "--request-timeout=10s")
        status = service.get("status", {})
        ready = any(x.get("type") == "Ready" and x.get("status") == "True"
                    for x in status.get("conditions", []))
        if status.get("url") == expected and ready:
            break
        if time.monotonic() >= deadline:
            raise ValueError(f"Knative URL / Ready が {expected} に更新されませんでした。"
                             "ドメイン・TLS・namespace selector を確認してください")
        print(f"Knative の URL / Ready 更新を待っています: {expected}", flush=True)
        time.sleep(5)
    if legacy_gateway:
        print("旧 Kong を停止し、kong-system へ切り替えます。", flush=True)
        if legacy_gateway.get("spec", {}).get("replicas", 1) != 0:
            cluster.call("scale", "deployment/kong", "-n", "sas47-kong", "--replicas=0")
        pods = cluster.call("get", "pods", "-n", "sas47-kong", "-l", "app=sas47-kong", "-o", "name",
                            capture_output=True, text=True).stdout.split()
        if pods:
            cluster.call("wait", *pods, "-n", "sas47-kong", "--for=delete", "--timeout=180s")
    cluster.call("apply", "-f", output / "routes.yaml")
    cluster.call("apply", "-f", output / "kong.yaml")
    cluster.call("rollout", "status", "deployment/kong", "-n", "kong-system", "--timeout=300s")
    cluster.call("delete", "configmap/kong-declarative", "-n", "kong-system", "--ignore-not-found")
    print("配備完了。HTTP とスケールゼロ復帰は docs/verification.md に沿って確認してください。")


KIC_CLUSTER_RESOURCES = (
    ("ingressclass", "kong-ingress"),
    ("clusterrolebinding", "kong-ingress"),
    ("clusterrole", "kong-ingress"),
)


def check_kic_ownership(cluster):
    found_resources = []
    for kind, name in KIC_CLUSTER_RESOURCES:
        result = cluster.call("get", kind, name, "--ignore-not-found", "-o", "json",
                              capture_output=True, text=True)
        if not result.stdout.strip():
            continue
        resource = json.loads(result.stdout)
        if not tls.is_owned(resource):
            raise ValueError(f"所有ラベルのない既存リソースは変更しません: {kind}/{name}")
        found_resources.append((kind, name))
    return found_resources


def install_kic_prerequisites(cluster, output):
    check_kic_ownership(cluster)
    cache = ROOT / ".local/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"kong-{KONG_CHART}.tgz"
    if not archive.exists():
        # Move into the cache only after the full download and digest verification.
        with tempfile.TemporaryDirectory() as tmp:
            downloaded = Path(tmp) / archive.name
            fetch(f"https://github.com/Kong/charts/releases/download/kong-{KONG_CHART}/{archive.name}", downloaded)
            if hashlib.sha256(downloaded.read_bytes()).hexdigest() != KONG_CHART_SHA256:
                raise ValueError("Kong chart の SHA-256 検証に失敗しました")
            shutil.copyfile(downloaded, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != KONG_CHART_SHA256:
        raise ValueError(f"Kong chart キャッシュの SHA-256 が不一致です: {archive}")
    with tarfile.open(archive, "r:gz") as chart:
        # Read exactly this member; do not extract archive paths to the filesystem.
        member = chart.extractfile("kong/crds/custom-resource-definitions.yaml")
        if member is None:
            raise ValueError("Kong chart に CRD がありません")
        crds = member.read().decode("utf-8")
    crd_names = re.findall(r"^  name: ([a-z0-9.-]+\.configuration\.konghq\.com)$", crds, re.MULTILINE)
    if not crd_names:
        raise ValueError("Kong CRD の名前を確認できませんでした")
    target = output / "kong-crds.yaml"
    target.write_text(crds)
    cluster.call("apply", "--server-side", "--field-manager=kong-crds", "-f", target)
    cluster.call("wait", "crd", *crd_names, "--for=condition=Established", "--timeout=120s")
    for name in ("rbac.yaml", "controller.yaml"):
        cluster.call("apply", "-f", ROOT / "plugins/kong/manifests" / name)


def remove(cluster):
    # Check every namespace before deleting either one.
    names = []
    for name in NAMESPACES:
        result = cluster.call("get", "namespace", name, "--ignore-not-found", "-o", "json",
                              capture_output=True, text=True)
        if not result.stdout.strip():
            continue
        found = json.loads(result.stdout)
        if not tls.is_owned(found):
            raise ValueError(f"所有ラベルのない namespace は削除しません: {name}")
        names.append(name)
    cluster_resources = check_kic_ownership(cluster)
    if names:
        cluster.call("delete", "namespace", *names, "--wait=true", "--timeout=180s")
    for kind, name in cluster_resources:
        cluster.call("delete", kind, name, "--ignore-not-found")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "validate", "render", "up", "deploy", "remove"))
    parser.add_argument("--config", type=Path, default=ROOT / "configs/environment/local.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/rendered")
    parser.add_argument("--kubectl", default="kubectl", help="例: 'sudo k0s kubectl'（shell 展開しません）")
    parser.add_argument("--require-https", action="store_true", help="TLS が無効・未設定なら停止する")
    args = parser.parse_args()
    if args.action == "configure":
        configure(args.config)
        return
    if args.action == "remove":
        remove(Cluster(args.kubectl))
        return
    c = load(args.config)
    if args.require_https and not c["tls"]["enabled"]:
        raise ValueError("HTTPS を要求しましたが tls.enabled が true ではありません。docs/https.md を参照してください")
    if args.action == "render":
        render(c, args.output)
        return
    cluster = Cluster(args.kubectl)
    if c["tls"]["enabled"]:
        credential_cluster = cluster
        if c["mode"] == "new-host" and args.kubectl == "kubectl":
            # A fresh host has no API yet. An installed host may already hold a usable Secret.
            credential_cluster = (Cluster("/usr/local/bin/k0s kubectl")
                                  if Path("/usr/local/bin/k0s").is_file() else None)
        tls.preflight(c, credential_cluster)
    print("設定検証: OK", flush=True)
    if c["tls"]["enabled"]:
        print(f"TLS: 有効 / Cloudflare zone: {c['tls']['cloudflare_zone']} / "
              f"HTTPS hostPort: {c['tls']['host_https_port']}", flush=True)
    else:
        print("TLS: 無効（443番への HTTPS 公開は設定しません）。HTTPS が必要なら --require-https を指定してください。", flush=True)
    if args.action == "validate":
        print("検証のみ完了しました。ダウンロード・インストール・クラスタ変更は行っていません。", flush=True)
        return
    if args.action == "up" and c["mode"] == "new-host":
        render(c, args.output)
        cluster = install_host()
        bootstrap(cluster, args.output)
    deploy(cluster, c, args.output)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            print(detail, file=sys.stderr)
        sys.exit(1)
