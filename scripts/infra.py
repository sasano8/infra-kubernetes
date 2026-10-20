#!/usr/bin/env python3
"""Host installation and isolated SAS-47 resources; Python standard library only."""
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
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
K0S = "v1.34.3+k0s.0"
KNATIVE = "knative-v1.20.0"
TEMPLATE = "{{.Namespace}}--{{.Name}}.{{.Domain}}"
OWNER = {"infra.sasano8.dev/owner": "sas47"}
NAMESPACES = ("sas47-apps", "sas47-kong")
MANIFESTS = {
    "apps": ROOT / "apps/sas47/manifests/apps.yaml.tmpl",
    "kong": ROOT / "plugins/kong/manifests/kong.yaml.tmpl",
}


def run(args, **kwargs):
    print("+ " + shlex.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def load(path):
    c = json.loads(path.read_text())
    expected = set(json.loads((ROOT / "configs/environment/example.json").read_text()))
    if set(c) != expected:
        raise ValueError(f"設定キーは {sorted(expected)} を指定してください")
    if c["mode"] not in ("new-host", "existing"):
        raise ValueError("mode は new-host / existing")
    for key in ("domain", "standard_host", "kourier_service"):
        value = c[key]
        if not isinstance(value, str) or len(value) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
            for x in value.split(".")
        ):
            raise ValueError(f"{key}: 小文字の DNS 名を指定してください")
    host = "sas47-apps--serverless." + c["domain"]
    if len(host) > 253 or host == c["standard_host"]:
        raise ValueError("Knative と通常 Service のホスト名を区別してください（最大253文字）")
    port = c["node_port"]
    if port is not None and (type(port) is not int or not 30000 <= port <= 32767):
        raise ValueError("node_port は null または 30000〜32767")
    if c["mode"] == "new-host" and c["kourier_service"] != "kourier.kourier-system.svc.cluster.local":
        raise ValueError("new-host の Kourier Service は kourier.kourier-system.svc.cluster.local 固定です")
    return c


def configure(path):
    if path.exists():
        raise ValueError(f"既存設定を上書きしません: {path}")
    c = json.loads((ROOT / "configs/environment/example.json").read_text())
    prompts = {
        "mode": "新規 VM=new-host / 既存クラスタ=existing",
        "domain": "Knative ドメイン（既存の場合は既存設定に合わせる）",
        "standard_host": "通常 Service の Host",
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
    values = dict(c, knative_host="sas47-apps--serverless." + c["domain"])
    values.update(
        config_hash=hashlib.sha256(
            json.dumps(c, sort_keys=True).encode()
            + (ROOT / "plugins/kong/manifests/kong.yaml.tmpl").read_bytes()
        ).hexdigest(),
        service_type="NodePort" if c["node_port"] else "ClusterIP",
        node_port_line=f"      nodePort: {c['node_port']}" if c["node_port"] else "",
    )
    for name in ("apps", "kong"):
        text = MANIFESTS[name].read_text()
        (output / f"{name}.yaml").write_text(string.Template(text).substitute(values))
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


def apply_knative_config(cluster, output):
    # A Ready Pod/endpoint alone does not demonstrate admission calls from the API server.
    # Probe the actual intended configuration without persisting it, then apply it.
    deadline = time.monotonic() + 180
    transient = (
        "context deadline exceeded", "i/o timeout", "connection refused",
        'no endpoints available for service "webhook"',
        "net/http: request canceled", "TLS handshake timeout",
    )
    arguments = ("apply", "--server-side", "--force-conflicts",
                 "--field-manager=sas47-knative-config", "--request-timeout=20s",
                 "-f", output / "knative-config")
    attempt = 0
    while True:
        attempt += 1
        print(f"Knative webhook の実呼び出しを確認します（試行 {attempt}、待機上限180秒）。", flush=True)
        try:
            cluster.call(*arguments, "--dry-run=server", capture_output=True, text=True,
                         timeout=min(25, max(1, deadline - time.monotonic())))
            cluster.call(*arguments, capture_output=True, text=True,
                         timeout=min(25, max(1, deadline - time.monotonic())))
            print("Knative webhook の呼び出しと追加設定の適用が成功しました。", flush=True)
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
            raise ValueError("Knative webhook の呼び出しが180秒以内に安定しませんでした。"
                             "scripts/check-knative-webhook.sh で状態を確認してください")
        print("webhook の通信準備を待っています。5秒後に実呼び出しを再確認します。", flush=True)
        time.sleep(min(5, max(0, deadline - time.monotonic())))


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
        upstream = ("apply", "--server-side", "--field-manager=sas47-upstream")
        cluster.call(*upstream, "-f", tmp / "serving-crds.yaml")
        cluster.call("wait", "crd", "--all", "--for=condition=Established", "--timeout=120s")
        cluster.call(*upstream, "-f", tmp / "serving-core.yaml")
        cluster.call("rollout", "status", "deployment/webhook", "-n", "knative-serving", "--timeout=300s")
        cluster.call(*upstream, "-f", tmp / "kourier.yaml")
    apply_knative_config(cluster, output)
    for namespace in ("knative-serving", "kourier-system"):
        cluster.call("rollout", "status", "deployment", "-n", namespace, "--timeout=300s")


def deploy(cluster, c, output):
    network = cluster.get("configmap/config-network", "-n", "knative-serving")
    if network.get("data", {}).get("domain-template") != TEMPLATE:
        raise ValueError("既存 domain-template が SAS-47 の前提と異なります。自動変更しません")
    # Reserve namespaces before writing anything inside them. Do not adopt unrelated namespaces.
    missing = []
    for name in NAMESPACES:
        result = cluster.call("get", "namespace", name, "--ignore-not-found", "-o", "json",
                              capture_output=True, text=True)
        if result.stdout.strip():
            found = json.loads(result.stdout)
            if any(found.get("metadata", {}).get("labels", {}).get(k) != v for k, v in OWNER.items()):
                raise ValueError(f"所有ラベルのない既存 namespace を変更しません: {name}")
        else:
            missing.append(name)
    for name in missing:
        manifest = {"apiVersion": "v1", "kind": "Namespace",
                    "metadata": {"name": name, "labels": OWNER}}
        cluster.call("create", "-f", "-", input=json.dumps(manifest), text=True)
    render(c, output)
    cluster.call("apply", "-f", output / "apps.yaml")
    cluster.call("rollout", "status", "deployment/standard", "-n", "sas47-apps", "--timeout=300s")
    cluster.call("wait", "ksvc/serverless", "-n", "sas47-apps", "--for=condition=Ready", "--timeout=300s")
    service = cluster.get("ksvc/serverless", "-n", "sas47-apps")
    expected = "http://sas47-apps--serverless." + c["domain"]
    if service.get("status", {}).get("url") != expected:
        raise ValueError(f"Knative URL が {expected} と異なります。既存ドメイン・TLS・namespace selector を確認してください")
    cluster.call("apply", "-f", output / "kong.yaml")
    cluster.call("rollout", "status", "deployment/kong", "-n", "sas47-kong", "--timeout=300s")
    print("配備完了。HTTP とスケールゼロ復帰は docs/verification.md に沿って確認してください。")


def remove(cluster):
    # Check every namespace before deleting either one.
    names = []
    for name in NAMESPACES:
        result = cluster.call("get", "namespace", name, "--ignore-not-found", "-o", "json",
                              capture_output=True, text=True)
        if not result.stdout.strip():
            continue
        found = json.loads(result.stdout)
        if any(found.get("metadata", {}).get("labels", {}).get(k) != v for k, v in OWNER.items()):
            raise ValueError(f"所有ラベルのない namespace は削除しません: {name}")
        names.append(name)
    if names:
        cluster.call("delete", "namespace", *names, "--wait=true", "--timeout=180s")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "render", "up", "deploy", "remove"))
    parser.add_argument("--config", type=Path, default=ROOT / "configs/environment/local.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/rendered")
    parser.add_argument("--kubectl", default="kubectl", help="例: 'sudo k0s kubectl'（shell 展開しません）")
    args = parser.parse_args()
    if args.action == "configure":
        configure(args.config)
        return
    if args.action == "remove":
        remove(Cluster(args.kubectl))
        return
    c = load(args.config)
    if args.action == "render":
        render(c, args.output)
        return
    cluster = Cluster(args.kubectl)
    if args.action == "up" and c["mode"] == "new-host":
        render(c, args.output)
        cluster = install_host()
        bootstrap(cluster, args.output)
    deploy(cluster, c, args.output)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            print(detail, file=sys.stderr)
        sys.exit(1)
