"""Optional Cloudflare DNS-01 certificates; no secret values in rendered files/logs."""
import base64
import hashlib
import json
from pathlib import Path
import re
import socket
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
VERSION = "v1.19.1"
MANIFEST_SHA256 = "876a41a57e36b85619f4124b24b3deb80912b5ffed515f90e2f160b6e6338e81"
PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
OWNER = {"infra.sasano8.dev/owner": "sas47"}
DEFAULTS = {
    "enabled": False,
    "email": "",
    "cloudflare_zone": "",
    "cloudflare_token_file": ".local/secrets/cloudflare-api-token",
    "acme_server": PRODUCTION,
    "host_https_port": 443,
}


def normalize(config):
    supplied = config.get("tls", {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
        raise ValueError("tls に未対応の設定があります")
    options = dict(DEFAULTS, **supplied)
    if type(options["enabled"]) is not bool:
        raise ValueError("tls.enabled は true / false")
    config["tls"] = options
    if not options["enabled"]:
        return
    if not isinstance(options["email"], str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", options["email"]):
        raise ValueError("tls.email に ACME アカウントのメールアドレスを指定してください")
    zone = options["cloudflare_zone"]
    if not isinstance(zone, str) or not zone or len(zone) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in zone.split(".")
    ):
        raise ValueError("tls.cloudflare_zone に Cloudflare の DNS ゾーン名を指定してください")
    for name in (config["knative_domain"], config["standard_domain"]):
        if name != zone and not name.endswith("." + zone):
            raise ValueError(f"証明書の対象 {name} が Cloudflare ゾーン {zone} の範囲外です")
    if options["acme_server"] not in (PRODUCTION, STAGING):
        raise ValueError("tls.acme_server は Let's Encrypt production / staging の URL を指定してください")
    port = options["host_https_port"]
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("tls.host_https_port は null または 1〜65535")
    if not isinstance(options["cloudflare_token_file"], str) or not options["cloudflare_token_file"]:
        raise ValueError("tls.cloudflare_token_file にトークンファイルのパスを指定してください")


def render(config, output):
    """Render only public configuration. Never write a Secret to the output directory."""
    options = config["tls"]
    target = output / "tls.json"
    if not options["enabled"]:
        target.unlink(missing_ok=True)
        return
    issuer = {
        "apiVersion": "cert-manager.io/v1", "kind": "Issuer",
        "metadata": {"name": "sas47-cloudflare", "namespace": "test", "labels": OWNER},
        "spec": {"acme": {
            "server": options["acme_server"], "email": options["email"],
            "privateKeySecretRef": {"name": "sas47-acme-account"},
            "solvers": [{"selector": {"dnsZones": [options["cloudflare_zone"]]},
                         "dns01": {"cloudflare": {"apiTokenSecretRef": {
                             "name": "sas47-cloudflare-api-token", "key": "api-token"}}}}],
        }},
    }
    resources = [issuer]
    for name, domains in (
        ("standard-tls", [config["standard_domain"], "*." + config["standard_domain"]]),
        ("knative-wildcard-tls", [config["knative_domain"], "*." + config["knative_domain"]]),
    ):
        resources.append({
            "apiVersion": "cert-manager.io/v1", "kind": "Certificate",
            "metadata": {"name": name, "namespace": "test", "labels": OWNER},
            "spec": {"secretName": name, "dnsNames": domains,
                     "issuerRef": {"name": "sas47-cloudflare", "kind": "Issuer"}},
        })
    target.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}, indent=2) + "\n")


def install_manager(cluster, fetch):
    found = cluster.call("get", "namespace", "cert-manager", "--ignore-not-found", "-o", "json",
                         capture_output=True, text=True)
    owned = False
    if found.stdout.strip():
        namespace = json.loads(found.stdout)
        owned = namespace.get("metadata", {}).get("labels", {}).get("infra.sasano8.dev/owner") == "sas47"
        if not owned:
            print("既存の cert-manager を使用します。本体は変更しません。", flush=True)
    else:
        cluster.call("create", "-f", "-", text=True, input=json.dumps({
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "cert-manager", "labels": OWNER},
        }))
        owned = True
    if owned:
        cache = ROOT / ".local/downloads"
        cache.mkdir(parents=True, exist_ok=True)
        manifest = cache / f"cert-manager-{VERSION}.yaml"
        if not manifest.exists():
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                downloaded = Path(directory) / "cert-manager.yaml"
                fetch(f"https://github.com/cert-manager/cert-manager/releases/download/{VERSION}/cert-manager.yaml", downloaded)
                if hashlib.sha256(downloaded.read_bytes()).hexdigest() != MANIFEST_SHA256:
                    raise ValueError("cert-manager マニフェストの SHA-256 が不一致です")
                manifest.write_bytes(downloaded.read_bytes())
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != MANIFEST_SHA256:
            raise ValueError("cert-manager キャッシュの SHA-256 が不一致です")
        cluster.call("apply", "--server-side", "--field-manager=sas47-cert-manager", "-f", manifest)
    for name in ("certificates.cert-manager.io", "issuers.cert-manager.io",
                 "orders.acme.cert-manager.io", "challenges.acme.cert-manager.io"):
        cluster.call("wait", "crd", name, "--for=condition=Established", "--timeout=120s")
    for name in ("cert-manager", "cert-manager-webhook", "cert-manager-cainjector"):
        cluster.call("rollout", "status", f"deployment/{name}", "-n", "cert-manager", "--timeout=300s")


def token_file(config):
    path = Path(config["tls"]["cloudflare_token_file"])
    return path if path.is_absolute() else ROOT / path


def read_token_file(config):
    path = token_file(config)
    if not path.exists():
        return None
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(f"Cloudflare トークンファイルは通常ファイル・権限600で配置してください: {path}")
    token = path.read_text().strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("Cloudflare トークンファイルが空、または空白を含んでいます")
    return token


def existing_token(cluster):
    # Query metadata/key names only. Existing credential values are never dumped.
    template = 'present:{{with .metadata.labels}}{{index . "infra.sasano8.dev/owner"}}{{end}}:{{range $k,$v := .data}}{{$k}},{{end}}'
    found = cluster.call("get", "secret/sas47-cloudflare-api-token", "-n", "test",
                         "--ignore-not-found", "--request-timeout=10s", "-o", "go-template=" + template,
                         capture_output=True, text=True, timeout=15)
    present = found.stdout.strip()
    if present and not present.startswith("present:sas47:"):
        raise ValueError("所有ラベルのない既存 Cloudflare Secret は上書きしません")
    return bool(present and "api-token" in present.split(":", 2)[2].split(","))


def preflight(config, cluster=None):
    if read_token_file(config) is not None:
        print("Cloudflare トークンファイルの事前検証: OK（値は非表示）", flush=True)
        return
    if cluster is not None:
        try:
            if existing_token(cluster):
                print("登録済み Cloudflare Secret の事前確認: OK（値は取得しません）", flush=True)
                return
        except (OSError, subprocess.SubprocessError):
            raise ValueError("トークンファイルがなく、既存 Secret も確認できませんでした。"
                             f"クラスタ接続を確認するか、{token_file(config)} に権限600で配置してください。"
                             "手順: docs/https.md") from None
    raise ValueError(f"Cloudflare トークンがありません。{token_file(config)} に権限600で配置してください。"
                     "インストールは開始していません。手順: docs/https.md")


def ensure_token(cluster, config):
    present = existing_token(cluster)
    token = read_token_file(config)
    if token is None:
        if present:
            print("登録済みの Cloudflare Secret を使用します。", flush=True)
            return
        raise ValueError(f"Cloudflare トークンファイルを権限600で配置してください: {token_file(config)}")
    resource = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": "sas47-cloudflare-api-token", "namespace": "test", "labels": OWNER},
                "data": {"api-token": base64.b64encode(token.encode()).decode()}}
    try:
        cluster.call("apply", "--server-side", "--field-manager=sas47-cloudflare-secret", "-f", "-",
                     input=json.dumps(resource), text=True, capture_output=True)
    except subprocess.CalledProcessError:
        # Do not include API error payloads that might echo submitted Secret data.
        raise ValueError("Cloudflare Secret の登録に失敗しました（機密保護のため応答は非表示）") from None
    print("Cloudflare Secret を登録しました（値は非表示）。", flush=True)


def check_port(cluster, config):
    port = config["tls"]["host_https_port"]
    if port is None:
        return
    found = cluster.call("get", "deployment/kong", "-n", "sas47-kong", "--ignore-not-found",
                         "-o", "json", capture_output=True, text=True)
    if found.stdout.strip():
        containers = json.loads(found.stdout)["spec"]["template"]["spec"]["containers"]
        if any(p.get("hostPort") == port for container in containers for p in container.get("ports", [])):
            return
    # Prevent claiming an HTTPS port already used by a host process such as Caddy.
    with socket.socket() as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError as exc:
            raise ValueError(f"ホストの TCP {port} 番が使用中です。既存プロセスを停止・変更しません") from exc


def prepare(cluster, config, output, fetch, apply_config):
    if not config["tls"]["enabled"]:
        return
    check_port(cluster, config)
    ensure_token(cluster, config)
    install_manager(cluster, fetch)
    apply_config(cluster, output / "tls.json", "cert-manager", "sas47-tls", force=False)
    cluster.call("wait", "issuer/sas47-cloudflare", "-n", "test", "--for=condition=Ready", "--timeout=180s")
    print("DNS-01 による証明書発行を待っています（各証明書は最大600秒）。", flush=True)
    for name in ("standard-tls", "knative-wildcard-tls"):
        cluster.call("wait", f"certificate/{name}", "-n", "test", "--for=condition=Ready", "--timeout=600s")
    print("証明書の準備ができました。KIC が Secret の更新を Kong に反映します。", flush=True)
