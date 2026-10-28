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
import inventory
import sso
import platform_plugins

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


PUBLIC_ROUTES = {
    "standard_domain": {"kind": "standard", "secret": "standard-tls"},
    "standard_nobuffer_domain": {"kind": "standard", "secret": "standard-nobuffer-tls"},
    "knative_domain": {"kind": "knative", "secret": "knative-wildcard-tls"},
    "knative_nobuffer_domain": {"kind": "knative", "secret": "knative-nobuffer-tls"},
    "knative_public": {"kind": "knative", "secret": "knative-public-tls"},
}
DEFAULT_ROUTES = {"standard": "standard_domain", "knative": "knative_domain"}


def resolve_route(config, kind, explicit=None):
    route = config["default_routes"][kind] if explicit is None else explicit
    if not isinstance(route, str) or route not in PUBLIC_ROUTES or config["routes"][route]["kind"] != kind:
        raise ValueError(f"{kind}: 有効な同種別の公開ルートIDを指定してください（無効化は未対応）")
    return route


def knative_domain_data(config):
    default = resolve_route(config, "knative")
    return {config["routes"][route]["domain"]: ("" if route == default else
            "selector:\n  infra.sasano8.dev/route: " + route + "\n")
            for route in config["routes"] if config["routes"][route]["kind"] == "knative"}


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
    c.setdefault("base_domain", None)
    required = {"mode", "routes", "kourier_service", "node_port"}
    missing = required - set(c)
    unknown = set(c) - required - {"base_domain", "tls", "default_routes", "sso", "plugins"}
    if missing or unknown:
        errors = []
        if missing:
            errors.append("必須項目がありません: " + ", ".join(sorted(missing)))
        if unknown:
            errors.append("未対応の項目: " + ", ".join(sorted(unknown)))
        raise ValueError(" / ".join(errors))
    if c["mode"] not in ("new-host", "existing"):
        raise ValueError("mode は new-host / existing")
    def dns_name(value, label):
        if not isinstance(value, str) or len(value) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
            for x in value.split(".")
        ):
            raise ValueError(f"{label}: 小文字の DNS 名を指定してください")
    if c["base_domain"] is not None:
        dns_name(c["base_domain"], "base_domain")
    dns_name(c["kourier_service"], "kourier_service")
    if not isinstance(c["routes"], dict) or set(c["routes"]) != set(PUBLIC_ROUTES):
        raise ValueError("routesには5つの公開経路IDを指定してください: " + ", ".join(PUBLIC_ROUTES))
    for route_id, route in c["routes"].items():
        label = "routes." + route_id
        if not isinstance(route, dict) or set(route) != {"kind", "domain", "auth", "buffering"}:
            raise ValueError(label + " は kind / domain / auth / buffering を指定してください")
        if route["kind"] != PUBLIC_ROUTES[route_id]["kind"]:
            raise ValueError(label + ".kind: " + PUBLIC_ROUTES[route_id]["kind"] + "を指定してください")
        dns_name(route["domain"], label + ".domain")
        if len("hello-nobuffer-test-ksvc." + route["domain"]) > 253:
            raise ValueError(label + ".domain: 公開ホスト名が253文字を超えます")
        if route["auth"] not in ("sso", "api", "public"):
            raise ValueError(label + ".auth は sso / api / public を指定してください")
        if type(route["buffering"]) is not bool:
            raise ValueError(label + ".buffering は true / false を指定してください")
    if c["routes"]["knative_public"]["auth"] != "public":
        raise ValueError("knative_public はpublic専用経路です")
    domains = [route["domain"] for route in c["routes"].values()]
    if len(set(domains)) != len(domains):
        raise ValueError("公開経路のドメインをそれぞれ区別してください")
    port = c["node_port"]
    if port is not None and (type(port) is not int or not 30000 <= port <= 32767):
        raise ValueError("node_port は null または 30000〜32767")
    if c["mode"] == "new-host" and c["kourier_service"] != "kourier.kourier-system.svc.cluster.local":
        raise ValueError("new-host の Kourier Service は kourier.kourier-system.svc.cluster.local 固定です")
    supplied = c.get("default_routes", {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_ROUTES):
        raise ValueError("default_routes は standard / knative のルートIDを指定するオブジェクトです")
    c["default_routes"] = dict(DEFAULT_ROUTES, **supplied)
    for kind in DEFAULT_ROUTES:
        resolve_route(c, kind)
    tls.normalize(c)
    sso.normalize(c)
    platform_plugins.normalize(c)
    return c


def configure(path):
    if path.exists():
        raise ValueError(f"既存設定を上書きしません: {path}")
    c = json.loads((ROOT / "configs/environment/example.json").read_text())
    prompts = {
        "mode": "新規 VM=new-host / 既存クラスタ=existing",
        "base_domain": "ベースドメイン（参照情報。各経路は個別指定）",
        "kourier_service": "Kourier のクラスタ内 Service DNS 名",
        "node_port": "Kong NodePort（空欄なら非公開、port-forward 使用）",
    }
    for key, prompt in prompts.items():
        answer = input(f"{prompt} [{c[key] if c[key] is not None else 'なし'}]: ").strip()
        if answer:
            c[key] = int(answer) if key == "node_port" else answer
    for route_id, route in c["routes"].items():
        domain = input(f"{route_id} のドメイン [{route['domain']}]: ").strip()
        if domain:
            route["domain"] = domain
        auth = input(f"{route_id} の認証 sso / api / public [{route['auth']}]: ").strip()
        if auth:
            route["auth"] = auth
        buffering = input(f"{route_id} のbuffering true / false [{str(route['buffering']).lower()}]: ").strip()
        if buffering not in ("", "true", "false"):
            raise ValueError("buffering は true / false")
        if buffering:
            route["buffering"] = buffering == "true"
    for kind in DEFAULT_ROUTES:
        choices = [route for route, definition in c["routes"].items() if definition["kind"] == kind]
        answer = input(f"{kind} の既定公開ルート ({', '.join(choices)}) [{c['default_routes'][kind]}]: ").strip()
        if answer:
            c["default_routes"][kind] = answer
    enabled = input("SSO を有効にしますか true / false [false]: ").strip()
    if enabled not in ("", "true", "false"):
        raise ValueError("SSO は true / false を指定してください")
    c["sso"] = {"enabled": enabled == "true"}
    if c["sso"]["enabled"]:
        c["tls"]["enabled"] = True
        for field, prompt in (("email", "ACMEメール"), ("cloudflare_zone", "Cloudflareゾーン")):
            c["tls"][field] = input(prompt + ": ").strip()
        c["sso"]["email_domain"] = input("SSO_EMAIL_DOMAIN（許可メールドメイン）: ").strip()
        c["sso"]["secret_file"] = input("SSO Secret JSONファイル（既存Secretなら空欄）: ").strip()
        c["sso"]["auth_host"] = input("認証専用ホスト [auth." + c["routes"]["knative_domain"]["domain"] + "]: ").strip()
        c["sso"]["cookie_domain"] = input("Cookie共有親ドメイン [" + (c["base_domain"] or "4経路の共通親ドメイン") + "]: ").strip()
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
    platform_plugins.render(c, output)
    values = dict(c)
    values.update({key: route["domain"] for key, route in c["routes"].items()})
    values.update(
        knative_host="hello-test-ksvc." + c["routes"]["knative_domain"]["domain"],
        standard_host="hello-test-ssvc." + c["routes"]["standard_domain"]["domain"],
        standard_nobuffer_host="hello-test-ssvc." + c["routes"]["standard_nobuffer_domain"]["domain"],
        knative_nobuffer_host="hello-nobuffer-test-ksvc." + c["routes"]["knative_nobuffer_domain"]["domain"],
    )
    values["knative_public_tls"] = (
        f'  tls:\n    - hosts: ["*.{c["routes"]["knative_public"]["domain"]}"]\n      secretName: knative-public-tls'
        if c["tls"]["enabled"] else "")
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
    for route in ("standard_nobuffer", "knative_nobuffer"):
        secret = route.replace("_", "-") + "-tls"
        values[route + "_tls"] = (f"  tls:\n    - hosts: [{values[route + '_host']}]\n      secretName: {secret}"
                                 if c["tls"]["enabled"] else "")
    for route, definition in c["routes"].items():
        prefix = route.removesuffix("_domain")
        values[prefix + "_buffering"] = str(definition["buffering"]).lower()
    domains = knative_domain_data(c)
    values["knative_domain_entries"] = "\n".join(
        "  " + json.dumps(domain) + ": " + json.dumps(selector) for domain, selector in domains.items())
    for kind, prefix in (("standard", "hello-default-test-ssvc."),
                         ("knative", "hello-default-test-ksvc.")):
        route = resolve_route(c, kind)
        definition = dict(c["routes"][route], secret=PUBLIC_ROUTES[route]["secret"])
        host = prefix + c["routes"][route]["domain"]
        values[kind + "_default_host"] = host
        values[kind + "_default_buffering"] = str(definition["buffering"]).lower()
        values[kind + "_default_tls"] = (
            f"  tls:\n    - hosts: [{host}]\n      secretName: {definition['secret']}"
            if c["tls"]["enabled"] else "")
    auth = sso.render(c, output, c["routes"])
    for route in PUBLIC_ROUTES:
        values[route.removesuffix("_domain") + "_auth"] = auth.get(route, '    konghq.com/plugins: ""')
    for kind in ("standard", "knative"):
        values[kind + "_default_auth"] = auth.get(resolve_route(c, kind), '    konghq.com/plugins: ""')
    enabled = c["sso"]["enabled"]
    values["sso_env"] = ('            - {name: KONG_PLUGINS, value: "bundled,session-auth"}\n'
                         '            - {name: KONG_LUA_PACKAGE_PATH, value: "/opt/?.lua;;"}' if enabled else "")
    values["proxy_access_log"] = "/dev/stdout sso_safe" if enabled else "/dev/stdout"
    if enabled:
        values["sso_env"] += '\n            - {name: KONG_NGINX_HTTP_LOG_FORMAT, value: "sso_safe \'$remote_addr $request_method $uri $status\'"}'
    values["sso_mount"] = ('          volumeMounts:\n            - {name: session-auth-code, mountPath: /opt/kong/plugins/session-auth, readOnly: true}' if enabled else "")
    values["sso_volume"] = ('        - name: session-auth-code\n          configMap: {name: session-auth-code}' if enabled else "")
    # Updating plugin files must roll Kong so Lua module caches cannot retain old code.
    plugin_hash = hashlib.sha256("".join(p.read_text() for p in sorted((ROOT / "plugins/sso/kong/plugins/session-auth").glob("*.lua"))).encode()).hexdigest()
    values["sso_pod_annotation"] = ('      annotations: {infra.sasano8.dev/sso-code: "' + plugin_hash + '"}' if enabled else "")
    for name in MANIFESTS:
        text = MANIFESTS[name].read_text()
        (output / f"{name}.yaml").write_text(string.Template(text).substitute(values))
    sso.render_portal(c, output)
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


def existing_k0s_paths(root=Path("/")):
    """Empty reset remnants and our unchanged config are not a live installation."""
    config = root / "etc/k0s/k0s.yaml"
    matching_config = (config.is_file() and not config.is_symlink()
                       and config.read_bytes() == (ROOT / "configs/kube/k0s.yaml").read_bytes())

    def occupied(path):
        if path.is_symlink():
            return True
        if not path.exists():
            return False
        if path == config and matching_config:
            return False
        if path.is_dir():
            return any(occupied(child) for child in path.iterdir())
        return True

    directories = (root / "var/lib/k0s", root / "etc/k0s")
    files = (root / "usr/local/bin/k0s", root / "etc/systemd/system/k0scontroller.service",
             root / "etc/systemd/system/k0sworker.service")
    return ([path for path in directories if occupied(path)]
            + [path for path in files if path.exists() or path.is_symlink()])


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
    if not owned:
        remaining = existing_k0s_paths()
        if remaining:
            raise ValueError("既存k0sのファイル・データが残っています（自動上書きしません）: "
                             + ", ".join(map(str, remaining)))
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
        print("k0s は起動済みです。ホスト設定を維持します。", flush=True)
    print("k0sホストの導入・起動が完了しました。ユーザー用kubeconfigを設定してmake upを実行してください。")


def host_action(action):
    if os.geteuid() != 0 or platform.system() != "Linux":
        raise ValueError("k0sホスト操作はLinux上でsudoを付けて実行してください")
    if action == "install":
        install_host()
        return
    binary = Path("/usr/local/bin/k0s")
    if not binary.is_file():
        raise ValueError("k0sがありません。先にmake install-k0sを実行してください")
    if action == "reset":
        print("k0sホスト全体を停止・リセットします。クラスタ状態は削除されます。", flush=True)
        run([binary, "stop"])
        run([binary, "reset"])
        config = Path("/etc/k0s/k0s.yaml")
        if (config.is_file() and not config.is_symlink()
                and config.read_bytes() == (ROOT / "configs/kube/k0s.yaml").read_bytes()):
            config.unlink()
            # Preserve unrelated files; only remove the directory if empty.
            try:
                config.parent.rmdir()
            except OSError:
                pass
        # Only remove the binary and our marker after k0s reset succeeds.
        binary.unlink(missing_ok=True)
        Path("/var/lib/infra-kubernetes/k0s-version").unlink(missing_ok=True)
        print("k0s本体と導入記録を削除しました。sudo rebootでホストを再起動してください。", flush=True)
        print("再構築は再起動後にmake install-k0sから行い、kubeconfigを再取得してください。", flush=True)
        return
    if action == "kubeconfig":
        # stdout must contain YAML only, for explicit export to a user-owned file.
        subprocess.run([str(binary), "kubeconfig", "admin"], check=True)
    else:
        run([binary, action])


def wait_cluster(cluster):
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


def knative_field_manager(cluster):
    # A field-manager is persistent ownership metadata, not a resource name.
    # Retain the original ID in old installations to avoid taking over its fields.
    for namespace in ("knative-serving", "kourier-system"):
        result = cluster.call("get", "deployments", "-n", namespace, "--ignore-not-found",
                              "-o", "json", "--show-managed-fields=true", "--request-timeout=10s",
                              capture_output=True, text=True)
        resources = json.loads(result.stdout) if result.stdout.strip() else {}
        if any(field.get("manager") == "sas47-upstream"
               for resource in resources.get("items", [])
               for field in resource.get("metadata", {}).get("managedFields", [])):
            print("既存 Knative の管理 ID を引き継ぎます（リソース名は変更しません）。", flush=True)
            return "sas47-upstream"
    return "knative-upstream"


def apply_serving_core(cluster, source, manager="knative-upstream"):
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
    cluster.call("apply", "--server-side", f"--field-manager={manager}", "-f", "-",
                 input=json.dumps({"apiVersion": "v1", "kind": "List", "items": retained}), text=True)


def install_knative(cluster):
    # Only called for a new, installer-owned host. Never in existing mode.
    manager = knative_field_manager(cluster)
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
        upstream = ("apply", "--server-side", f"--field-manager={manager}")
        cluster.call(*upstream, "-f", tmp / "serving-crds.yaml")
        cluster.call("wait", "crd", "--all", "--for=condition=Established", "--timeout=120s")
        apply_serving_core(cluster, tmp / "serving-core.yaml", manager)
        cluster.call("rollout", "status", "deployment/webhook", "-n", "knative-serving", "--timeout=300s")
        cluster.call(*upstream, "-f", tmp / "kourier.yaml")


def apply_config_knative(cluster, c, output):
    if c["mode"] != "new-host":
        raise ValueError("existingのKnative共通設定は自動変更しません。管理者が生成物を確認してください")
    apply_admission_config(cluster, output / "knative-config", "Knative", "knative-config", force=True)


def wait_knative(cluster):
    for namespace in ("knative-serving", "kourier-system"):
        cluster.call("rollout", "status", "deployment", "-n", namespace, "--timeout=300s")


def bootstrap(cluster, output, c):
    install_knative(cluster)
    apply_config_knative(cluster, c, output)
    wait_knative(cluster)


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


def ensure_namespaces(cluster, names=NAMESPACES):
    # Reserve namespaces before writing anything inside them. Do not adopt unrelated namespaces.
    missing = []
    for name in names:
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


def apply_apps(cluster, output):
    cluster.call("apply", "-f", output / "apps.yaml")


def wait_apps(cluster, c):
    cluster.call("rollout", "status", "deployment/hello-ssvc", "-n", "test", "--timeout=300s")
    services = [("hello", c["routes"]["knative_domain"]["domain"]),
                ("hello-nobuffer", c["routes"]["knative_nobuffer_domain"]["domain"]),
                ("hello-default", c["routes"][resolve_route(c, "knative")]["domain"])]
    if c["sso"]["enabled"]:
        services.append(("sso", c["routes"]["knative_public"]["domain"]))
    for name, domain in services:
        cluster.call("wait", "ksvc/" + name, "-n", "test", "--for=condition=Ready", "--timeout=300s")
        expected = "http://" + name + "-test-ksvc." + domain
        deadline = time.monotonic() + 180
        while True:
            service = cluster.get("ksvc/" + name, "-n", "test", "--request-timeout=10s")
            status = service.get("status", {})
            ready = any(x.get("type") == "Ready" and x.get("status") == "True"
                        for x in status.get("conditions", []))
            if status.get("url") == expected and ready:
                break
            if time.monotonic() >= deadline:
                raise ValueError(f"Knative URL / Ready が {expected} に更新されませんでした。ドメインselectorを確認してください")
            print(f"Knative の URL / Ready 更新を待っています: {expected}", flush=True)
            time.sleep(5)


def apply_kong(cluster, output):
    cluster.call("apply", "-f", output / "kong.yaml")


def wait_kong(cluster):
    cluster.call("rollout", "status", "deployment/kong", "-n", "kong-system", "--timeout=300s")


def apply_routes(cluster, output):
    cluster.call("apply", "-f", output / "routes.yaml")


def deploy(cluster, c, output):
    network = cluster.get("configmap/config-network", "-n", "knative-serving")
    if network.get("data", {}).get("domain-template") != TEMPLATE:
        raise ValueError("既存 domain-template がこの構成の前提と異なります。自動変更しません")
    sso.preflight(c, cluster)
    render(c, output)
    domain_file = output / "knative-config/domain.yaml"
    if c["mode"] == "new-host":
        cluster.call("apply", "--server-side", "--field-manager=knative-config", "--force-conflicts", "-f", domain_file)
    else:
        current = cluster.get("configmap/config-domain", "-n", "knative-serving").get("data", {})
        if any(current.get(domain) != selector for domain, selector in knative_domain_data(c).items()):
            raise ValueError("既存クラスタの config-domain を生成された domain.yaml と照合し、管理者が設定してください。自動上書きしません")
    ensure_namespaces(cluster)
    install_kic_prerequisites(cluster, output)
    legacy_gateway = find_legacy_gateway(cluster)
    legacy_ports = [p["hostPort"] for container in legacy_gateway.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                    for p in container.get("ports", []) if p.get("hostPort")] if legacy_gateway.get("spec", {}).get("replicas", 1) != 0 else []
    tls.prepare(cluster, c, output, fetch, apply_admission_config, legacy_ports)
    sso.install(cluster, c, output)
    apply_apps(cluster, output)
    wait_apps(cluster, c)
    if legacy_gateway:
        print("旧 Kong を停止し、kong-system へ切り替えます。", flush=True)
        if legacy_gateway.get("spec", {}).get("replicas", 1) != 0:
            cluster.call("scale", "deployment/kong", "-n", "sas47-kong", "--replicas=0")
        pods = cluster.call("get", "pods", "-n", "sas47-kong", "-l", "app=sas47-kong", "-o", "name",
                            capture_output=True, text=True).stdout.split()
        if pods:
            cluster.call("wait", *pods, "-n", "sas47-kong", "--for=delete", "--timeout=180s")
    if not c["sso"]["enabled"]:
        # Remove Plugin references before unloading Lua; otherwise DB-less readiness can deadlock.
        apply_routes(cluster, output)
        sso.cleanup_disabled(cluster, c)
    apply_kong(cluster, output)
    wait_kong(cluster)
    if c["sso"]["enabled"]:
        apply_routes(cluster, output)
    cluster.call("delete", "configmap/kong-declarative", "-n", "kong-system", "--ignore-not-found")
    platform_plugins.up(cluster, c, ensure_namespaces)
    inventory.record(cluster, c)
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


PHASE_ACTIONS = (
    "apply-namespaces", "record-inventory", "apply-config-knative", "wait-knative", "wait-cert-manager",
    "apply-certificates", "wait-certificates", "apply-sso", "wait-sso",
    "apply-apps", "wait-apps", "apply-kong", "wait-kong", "apply-routes",
)


def run_phase(action, cluster, c, output, plugin=None):
    # Waiting never renders, retrieves credentials, installs, or applies resources.
    waits = {
        "wait-knative": lambda: wait_knative(cluster),
        "wait-cert-manager": lambda: tls.wait_manager(cluster),
        "wait-certificates": lambda: tls.wait_certificates(cluster),
        "wait-sso": lambda: sso.wait_ready(cluster),
        "wait-apps": lambda: wait_apps(cluster, c),
        "wait-kong": lambda: wait_kong(cluster),
    }
    if action in waits:
        if action == "wait-certificates" and not c["tls"]["enabled"]:
            raise ValueError("tls.enabled=true が必要です")
        if action == "wait-sso" and not c["sso"]["enabled"]:
            raise ValueError("sso.enabled=true が必要です")
        waits[action]()
        return
    if action == "apply-namespaces":
        ensure_namespaces(cluster)
        return
    if action == "record-inventory":
        inventory.record(cluster, c)
        return
    if action == "plugin":
        if plugin in platform_plugins.NAMES:
            platform_plugins.install(plugin, cluster, c, ensure_namespaces)
        elif plugin == "knative":
            if c["mode"] != "new-host":
                raise ValueError("existingのKnative本体は変更しません")
            install_knative(cluster)
        elif plugin == "cert-manager":
            tls.install_manager(cluster, fetch)
        elif plugin == "kong":
            output.mkdir(parents=True, exist_ok=True)
            ensure_namespaces(cluster, ("kong-system",))
            install_kic_prerequisites(cluster, output)
        return
    render(c, output)
    if action == "apply-config-knative":
        apply_config_knative(cluster, c, output)
    elif action == "apply-certificates":
        if not c["tls"]["enabled"]:
            raise ValueError("tls.enabled=true が必要です")
        ensure_namespaces(cluster, ("test",))
        tls.apply_certificates(cluster, c, output, apply_admission_config)
    elif action == "apply-sso":
        if not c["sso"]["enabled"]:
            raise ValueError("無効化は参照解除の順序を維持するためdeploy/upを使用してください")
        sso.preflight(c, cluster)
        ensure_namespaces(cluster)
        sso.install(cluster, c, output, wait=False)
    elif action == "apply-apps":
        ensure_namespaces(cluster, ("test",))
        apply_apps(cluster, output)
    elif action in ("apply-kong", "apply-routes"):
        # An isolated phase must not bypass coordinated legacy/SSO transitions.
        ensure_namespaces(cluster, ("kong-system",) if action == "apply-kong" else NAMESPACES)
        if find_legacy_gateway(cluster):
            raise ValueError("旧Kongからの切替はdeploy/upを使用してください")
        found = cluster.call("get", "deployment/kong", "-n", "kong-system", "--ignore-not-found", "-o", "json", capture_output=True, text=True)
        current = json.loads(found.stdout) if found.stdout.strip() else {}
        template = current.get("spec", {}).get("template", {})
        loaded = "infra.sasano8.dev/sso-code" in template.get("metadata", {}).get("annotations", {})
        if current and loaded != c["sso"]["enabled"]:
            raise ValueError("SSO有効/無効の切替はdeploy/upを使用してください")
        if c["sso"]["enabled"]:
            files = sorted((ROOT / "plugins/sso/kong/plugins/session-auth").glob("*.lua"))
            desired_code = {p.name: p.read_text() for p in files}
            mounted_code = cluster.get("configmap/session-auth-code", "-n", "kong-system")
            if mounted_code.get("data") != desired_code:
                raise ValueError("先にapply-ssoでSSOコードを適用してください")
        if action == "apply-kong":
            apply_kong(cluster, output)
        else:
            if not current:
                raise ValueError("先にapply-kongとwait-kongを実行してください")
            # Refuse plugin references until the exact mounted Lua version is ready.
            if loaded:
                expected = hashlib.sha256("".join(desired_code.values()).encode()).hexdigest()
                if template["metadata"]["annotations"].get("infra.sasano8.dev/sso-code") != expected:
                    raise ValueError("先にapply-kongとwait-kongを実行してください")
                status = current.get("status", {})
                generation = current.get("metadata", {}).get("generation", 0)
                if (status.get("observedGeneration", -1) < generation
                        or status.get("updatedReplicas", 0) != current["spec"].get("replicas", 1)
                        or status.get("replicas", 0) != current["spec"].get("replicas", 1)
                        or status.get("availableReplicas", 0) != current["spec"].get("replicas", 1)):
                    raise ValueError("wait-kongでrollout完了を確認してください")
            apply_routes(cluster, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "validate", "render", "up", "deploy", "remove", "test-plan", "host", "configure-secret-cloudflare-token", "apply-secret-cloudflare-token", "configure-secret-google-sso", "apply-secret-google-sso", *PHASE_ACTIONS, "plugin"))
    parser.add_argument("plugin_action", nargs="?", choices=("install", "wait", "start", "stop", "status", "reset", "kubeconfig"))
    parser.add_argument("plugin_name", nargs="?", choices=("knative", "cert-manager", "kong", *platform_plugins.NAMES))
    parser.add_argument("--config", type=Path, default=ROOT / "configs/environment/local.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/rendered")
    parser.add_argument("--kubectl", default="kubectl", help="設定済みkubectlを使用（shell展開しません）")
    parser.add_argument("--require-https", action="store_true", help="TLS が無効・未設定なら停止する")
    parser.add_argument("--rotate-cookie", action="store_true", help="SSO資格情報の変更時にCookie鍵も再生成する")
    parser.add_argument("--format", choices=("shell", "markdown"), default="markdown")
    args = parser.parse_args()
    if args.action == "host":
        if args.plugin_action not in ("install", "start", "stop", "status", "reset", "kubeconfig") or args.plugin_name:
            parser.error("host <install|start|stop|status|reset|kubeconfig> を指定してください")
        host_action(args.plugin_action)
        return
    if args.action == "plugin":
        if args.plugin_action not in ("install", "wait") or not args.plugin_name:
            parser.error("plugin install/wait <plugin名> を指定してください")
        if args.plugin_action == "wait" and args.plugin_name not in platform_plugins.NAMES:
            parser.error("既存pluginの待機にはwait-knative / wait-cert-manager / wait-kongを使用してください")
    elif args.plugin_action or args.plugin_name:
        parser.error("plugin以外に追加の位置引数は指定できません")
    if args.rotate_cookie and args.action != "configure-secret-google-sso":
        parser.error("--rotate-cookieはconfigure-secret-google-sso専用です")
    if args.action == "configure":
        configure(args.config)
        return
    if args.action == "remove":
        remove(Cluster(args.kubectl))
        return
    c = load(args.config)
    if args.action == "configure-secret-cloudflare-token":
        tls.configure_token(c)
        return
    if args.action == "configure-secret-google-sso":
        sso.configure_credentials(c, rotate_cookie=args.rotate_cookie)
        return
    if args.require_https and not c["tls"]["enabled"]:
        raise ValueError("HTTPS を要求しましたが tls.enabled が true ではありません。docs/https.md を参照してください")
    if args.action == "render":
        render(c, args.output)
        return
    if args.action == "test-plan":
        print(sso.test_plan(c, args.format))
        return
    cluster = Cluster(args.kubectl)
    if args.action == "plugin" and args.plugin_action == "wait":
        platform_plugins.wait(args.plugin_name, cluster)
        return
    if args.action in PHASE_ACTIONS or args.action == "plugin":
        run_phase(args.action, cluster, c, args.output, args.plugin_name)
        return
    if args.action == "apply-secret-google-sso":
        sso.apply_credentials(cluster, c, require_file=True)
        return
    if args.action == "apply-secret-cloudflare-token":
        tls.ensure_token(cluster, c, require_file=True)
        return
    if c["tls"]["enabled"]:
        tls.preflight(c, cluster)
    sso.preflight(c, cluster)
    print("設定検証: OK", flush=True)
    if c["tls"]["enabled"]:
        print(f"TLS: 有効 / Cloudflare zone: {c['tls']['cloudflare_zone']} / "
              f"HTTPS hostPort: {c['tls']['host_https_port']}", flush=True)
    else:
        print("TLS: 無効（443番への HTTPS 公開は設定しません）。HTTPS が必要なら --require-https を指定してください。", flush=True)
    if args.action == "validate":
        platform_plugins.preflight(c)
        print("検証のみ完了しました。ダウンロード・インストール・クラスタ変更は行っていません。", flush=True)
        return
    platform_plugins.preflight(c)
    if args.action == "up" and c["mode"] == "new-host":
        render(c, args.output)
        wait_cluster(cluster)
        bootstrap(cluster, args.output, c)
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
