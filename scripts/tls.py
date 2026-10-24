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
OWNER = {"infra.sasano8.dev/owner": "infra-kubernetes"}
DEFAULTS = {
    "enabled": False,
    "email": "",
    "cloudflare_zone": "",
    "cloudflare_token_file": ".local/secrets/cloudflare-api-token",
    "acme_server": PRODUCTION,
    "host_https_port": 443,
}


def is_owned(resource):
    return resource.get("metadata", {}).get("labels", {}).get("infra.sasano8.dev/owner") in (
        "infra-kubernetes", "sas47",  # The latter is accepted only for existing installations.
    )


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
    for name in (config["knative_domain"], config["standard_domain"], config["standard_nobuffer_domain"], config["knative_nobuffer_domain"]):
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
        "metadata": {"name": "cloudflare-letsencrypt", "namespace": "test", "labels": OWNER},
        "spec": {"acme": {
            "server": options["acme_server"], "email": options["email"],
            "privateKeySecretRef": {"name": "letsencrypt-account"},
            "solvers": [{"selector": {"dnsZones": [options["cloudflare_zone"]]},
                         "dns01": {"cloudflare": {"apiTokenSecretRef": {
                             "name": "cloudflare-api-token", "key": "api-token"}}}}],
        }},
    }
    resources = [issuer]
    for name, domains in (
        ("standard-tls", [config["standard_domain"], "*." + config["standard_domain"]]),
        ("standard-nobuffer-tls", [config["standard_nobuffer_domain"], "*." + config["standard_nobuffer_domain"]]),
        ("knative-nobuffer-tls", [config["knative_nobuffer_domain"], "*." + config["knative_nobuffer_domain"]]),
        ("knative-wildcard-tls", [config["knative_domain"], "*." + config["knative_domain"]]),
    ):
        resources.append({
            "apiVersion": "cert-manager.io/v1", "kind": "Certificate",
            "metadata": {"name": name, "namespace": "test", "labels": OWNER},
            "spec": {"secretName": name, "dnsNames": domains,
                     "issuerRef": {"name": "cloudflare-letsencrypt", "kind": "Issuer"}},
        })
    target.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}, indent=2) + "\n")


def install_manager(cluster, fetch):
    found = cluster.call("get", "namespace", "cert-manager", "--ignore-not-found", "-o", "json",
                         capture_output=True, text=True)
    owned = False
    if found.stdout.strip():
        namespace = json.loads(found.stdout)
        owned = is_owned(namespace)
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
        cluster.call("apply", "--server-side", "--field-manager=cert-manager-install", "-f", manifest)
    wait_manager(cluster)


def wait_manager(cluster):
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


def find_token(cluster, allow_legacy=True):
    # Query names only; never include credential values in logs or preflight output.
    template = 'present:{{with .metadata.labels}}{{index . "infra.sasano8.dev/owner"}}{{end}}:{{range $k,$v := .data}}{{$k}},{{end}}'
    names = ("cloudflare-api-token", "sas47-cloudflare-api-token") if allow_legacy else ("cloudflare-api-token",)
    for name in names:
        result = cluster.call("get", f"secret/{name}", "-n", "test", "--ignore-not-found",
                              "--request-timeout=10s", "-o", "go-template=" + template,
                              capture_output=True, text=True, timeout=15)
        if not result.stdout.strip():
            continue
        fields = result.stdout.strip().split(":", 2)
        if len(fields) != 3 or fields[1] not in ("infra-kubernetes", "sas47"):
            raise ValueError("所有ラベルのない既存 Cloudflare Secret は使用・上書きしません")
        if "api-token" not in fields[2].split(","):
            raise ValueError("既存 Cloudflare Secret に api-token キーがありません")
        return name
    return None


def existing_token(cluster):
    return find_token(cluster) is not None


def apply_secret(cluster, name, data):
    resource = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": "test", "labels": OWNER}, "data": data}
    try:
        cluster.call("apply", "--server-side", "--field-manager=certificate-secrets", "-f", "-",
                     input=json.dumps(resource), text=True, capture_output=True)
    except subprocess.CalledProcessError:
        raise ValueError("証明書用 Secret の登録に失敗しました（機密保護のため応答は非表示）") from None


def copy_secret(cluster, source, destination, require_owner=False):
    try:
        result = cluster.call("get", f"secret/{source}", "-n", "test", "-o", "json",
                              capture_output=True, text=True, timeout=15)
        secret = json.loads(result.stdout)
        if require_owner and not is_owned(secret):
            raise ValueError("所有ラベルのない旧 Secret はコピーしません")
        apply_secret(cluster, destination, secret["data"])
    except (OSError, subprocess.SubprocessError, KeyError, json.JSONDecodeError):
        raise ValueError("旧 Secret の移行に失敗しました（値は非表示）") from None
    print(f"Secret を移行しました: {source} → {destination}（値は非表示）", flush=True)


def migrate_acme_account(cluster):
    result = cluster.call("get", "issuer/sas47-cloudflare", "-n", "test", "--ignore-not-found",
                          "-o", "json", capture_output=True, text=True)
    if not result.stdout.strip():
        return
    issuer = json.loads(result.stdout)
    if not is_owned(issuer):
        raise ValueError("所有ラベルのない旧 Issuer は移行しません")
    if issuer.get("spec", {}).get("acme", {}).get("privateKeySecretRef", {}).get("name") != "sas47-acme-account":
        raise ValueError("旧 Issuer の ACME アカウント参照が変更されています。自動移行しません")
    for name in ("letsencrypt-account", "sas47-acme-account"):
        result = cluster.call("get", f"secret/{name}", "-n", "test", "--ignore-not-found", "-o", "name",
                              capture_output=True, text=True)
        if name == "letsencrypt-account" and result.stdout.strip():
            return
        if name == "sas47-acme-account" and result.stdout.strip():
            copy_secret(cluster, name, "letsencrypt-account")


def tls_manager_migration(cluster):
    legacy = False
    for name in ("issuer/cloudflare-letsencrypt", "certificate/standard-tls", "certificate/knative-wildcard-tls"):
        result = cluster.call("get", name, "-n", "test", "--ignore-not-found", "-o", "json",
                              "--show-managed-fields=true", capture_output=True, text=True)
        if not result.stdout.strip():
            continue
        resource = json.loads(result.stdout)
        if not is_owned(resource):
            raise ValueError(f"所有ラベルのない既存リソースは変更しません: {name}")
        legacy = legacy or any(x.get("manager") == "sas47-tls" for x in resource["metadata"].get("managedFields", []))
    return legacy


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


def ensure_token(cluster, config, *, require_file=False):
    token = read_token_file(config)
    if token is None and require_file:
        raise ValueError(f"更新するCloudflareトークンファイルがありません: {token_file(config)}")
    present = find_token(cluster, allow_legacy=token is None)
    if token is None:
        if present:
            if present != "cloudflare-api-token":
                copy_secret(cluster, present, "cloudflare-api-token", require_owner=True)
            print("登録済みの Cloudflare Secret を使用します。", flush=True)
            return
        raise ValueError(f"Cloudflare トークンファイルを権限600で配置してください: {token_file(config)}")
    apply_secret(cluster, "cloudflare-api-token", {"api-token": base64.b64encode(token.encode()).decode()})
    print("Cloudflare Secret を登録しました（値は非表示）。", flush=True)


def check_port(cluster, config, legacy_ports=()):
    port = config["tls"]["host_https_port"]
    if port is None:
        return
    if port in legacy_ports:
        print("HTTPS ポートは所有確認済みの旧 Kong から切り替えます。", flush=True)
        return
    found = cluster.call("get", "deployment/kong", "-n", "kong-system", "--ignore-not-found",
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


def prepare(cluster, config, output, fetch, apply_config, legacy_ports=()):
    if not config["tls"]["enabled"]:
        return
    check_port(cluster, config, legacy_ports)
    ensure_token(cluster, config)
    install_manager(cluster, fetch)
    apply_certificates(cluster, config, output, apply_config)
    wait_certificates(cluster)


def apply_certificates(cluster, config, output, apply_config):
    migrate_acme_account(cluster)
    # Only explicitly owned resources can transfer from the old field manager.
    migrate_manager = tls_manager_migration(cluster)
    apply_config(cluster, output / "tls.json", "cert-manager", "tls-config", force=migrate_manager)


def wait_certificates(cluster):
    cluster.call("wait", "issuer/cloudflare-letsencrypt", "-n", "test", "--for=condition=Ready", "--timeout=180s")
    print("DNS-01 による証明書発行を待っています（各証明書は最大600秒）。", flush=True)
    for name in ("standard-tls", "knative-wildcard-tls", "standard-nobuffer-tls", "knative-nobuffer-tls"):
        cluster.call("wait", f"certificate/{name}", "-n", "test", "--for=condition=Ready", "--timeout=600s")
    print("証明書の準備ができました。KIC が Secret の更新を Kong に反映します。", flush=True)
