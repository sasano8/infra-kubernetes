"""Optional Google SSO: render public configuration; keep credentials out of artifacts."""
import base64
import json
from pathlib import Path
import re
import stat
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"infra.sasano8.dev/owner": "infra-kubernetes"}
SECRET = "google-sso"
KEYS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH2_PROXY_COOKIE_SECRET")
ID_TOKEN_HEADER = "X-Forwarded-Id-Token"
DEFAULTS = {"enabled": False, "email_domain": "", "secret_file": "", "routes": {}, "api_paths": ["/api", "/upload"],
            "id_token_routes": [], "id_token_header": ID_TOKEN_HEADER}


def valid_id_token_header(header):
    if not isinstance(header, str) or not re.fullmatch(r"[Xx]-[A-Za-z0-9][A-Za-z0-9-]*", header):
        return False
    name = header.lower()
    return name == ID_TOKEN_HEADER.lower() or (
        name != "x-real-ip" and not name.startswith(("x-forwarded-", "x-auth-request-", "x-kong-", "x-original-")))


def normalize(c, routes):
    supplied = c.get("sso", {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
        raise ValueError("sso に未対応の設定があります")
    c["_sso_explicit"] = "enabled" in supplied
    s = dict(DEFAULTS, **supplied)
    if type(s["enabled"]) is not bool:
        raise ValueError("sso.enabled は true / false")
    if not isinstance(s["routes"], dict) or any(k not in routes or v not in ("browser", "api", "public") for k, v in s["routes"].items()):
        raise ValueError("sso.routes はルートIDと browser / api / public の対応を指定してください")
    if not valid_id_token_header(s["id_token_header"]):
        raise ValueError("sso.id_token_header はX-で始まる専用ヘッダー名を指定してください（転送・identity・Kong制御ヘッダーは予約済み）")
    token_routes = s["id_token_routes"]
    if (not isinstance(token_routes, list)
            or any(not isinstance(route, str) or route not in routes for route in token_routes)
            or len(set(token_routes)) != len(token_routes)):
        raise ValueError("sso.id_token_routes は重複のない公開ルートIDの配列を指定してください")
    if any(s["routes"].get(route, "browser") == "public" for route in token_routes):
        raise ValueError("sso.id_token_routes にpublicルートは指定できません")
    if not isinstance(s["secret_file"], str):
        raise ValueError("sso.secret_file はファイルパスです")
    if not isinstance(s["api_paths"], list) or any(not isinstance(p, str) or not re.fullmatch(r"/[a-zA-Z0-9_/-]+", p) or p.endswith("/") for p in s["api_paths"]):
        raise ValueError("sso.api_paths は末尾/のない絶対パスprefixを指定してください")
    c["sso"] = s
    if not s["enabled"]:
        return
    if not c["tls"]["enabled"]:
        raise ValueError("SSO有効時は tls.enabled=true が必須です")
    if not isinstance(s["email_domain"], str) or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in s["email_domain"].split(".")) or "." not in s["email_domain"]:
        raise ValueError("sso.email_domain に許可するGoogle Workspaceメールドメインを指定してください")


def hosts(c):
    return ["hello-test-ssvc." + c["standard_domain"], "hello-test-ssvc." + c["standard_nobuffer_domain"],
            "hello-test-ksvc." + c["knative_domain"], "hello-nobuffer-test-ksvc." + c["knative_nobuffer_domain"],
            "hello-default-test-ssvc." + c[c["default_routes"]["standard"]],
            "hello-default-test-ksvc." + c[c["default_routes"]["knative"]]]


def credentials(c):
    name = c["sso"]["secret_file"]
    if not name:
        return None
    path = Path(name)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("SSO Secretファイルは通常ファイル・権限600で配置してください")
    try:
        data = json.loads(path.read_text())
        if set(data) != set(KEYS) or any(not isinstance(data[k], str) or not data[k].strip() for k in KEYS):
            raise ValueError()
        cookie = base64.b64decode(data[KEYS[2]], validate=True)
        if len(cookie) != 32:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError("SSO Secretファイルは3つの資格情報キーとbase64の32byte Cookie鍵が必要です（値は非表示）") from None
    return data


def preflight(c, cluster):
    if not c["sso"]["enabled"]:
        if not c.get("_sso_explicit", False) and cluster is not None:
            previous = cluster.call("get", "ingress/oauth2-proxy", "-n", "test", "--ignore-not-found", "-o", "name",
                                    capture_output=True, text=True, timeout=15)
            if previous.stdout.strip():
                raise ValueError("既存SSOを解除するにはsso.enabled=falseを明示してください")
        return
    if credentials(c) is not None:
        return
    if cluster is None:
        raise ValueError("新規構築前にsso.secret_fileを用意してください")
    # Report names of nonempty keys only, never query Secret values into logs.
    query = '{{range $k,$v := .data}}{{if $v}}{{$k}}{{"\\n"}}{{end}}{{end}}'
    try:
        result = cluster.call("get", "secret/" + SECRET, "-n", "test", "-o", "go-template=" + query,
                              capture_output=True, text=True, timeout=15)
    except subprocess.SubprocessError:
        raise ValueError("test/google-sso Secretを供給するかsso.secret_fileを指定してください") from None
    if not set(KEYS) <= set(result.stdout.splitlines()):
        raise ValueError("test/google-sso Secretに必要な非空キーが不足しています")


def resource(api, kind, name, namespace, **fields):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": namespace, "labels": OWNER}, **fields}


def render(c, output, route_ids):
    """Public manifests only. RESOURCE_IDENTITIES is parsed without executing Git history."""
    target = output / "sso.json"
    if not c["sso"]["enabled"]:
        target.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": []}) + "\n")
        return {}
    s = c["sso"]
    all_hosts = hosts(c)
    port = c["tls"]["host_https_port"] or 443
    # Public forwarding headers must come from Kong, not client-supplied X-Forwarded-*.
    files = {p.name: p.read_text() for p in (ROOT / "plugins/sso/kong/plugins/session-auth").glob("*.lua")}
    items = [resource("v1", "ConfigMap", "session-auth-code", "kong-system", data=files)]
    def plugin(name, mode, allowed, forward_id_token=False):
        return resource("configuration.konghq.com/v1", "KongPlugin", name, "test", plugin="session-auth",
                        config={"mode": mode, "hosts": allowed, "email_domain": s["email_domain"],
                                "https_port": port, "api_paths": s["api_paths"],
                                "forward_id_token": forward_id_token, "id_token_header": s["id_token_header"]})
    annotations = {}
    for route in route_ids:
        name = "sso-" + route.replace("_", "-")
        items.append(plugin(name, s["routes"].get(route, "browser"), all_hosts, route in s["id_token_routes"]))
        annotations[route] = '    konghq.com/plugins: "' + name + '"'
    items.append(plugin("sso-oauth-endpoints", "oauth", all_hosts))
    args = ["--provider=google", "--http-address=0.0.0.0:4180", "--upstream=static://202",
            "--reverse-proxy=true", "--redirect-url=/oauth2/callback", "--set-xauthrequest=true",
            "--cookie-name=__Host-infra_sso", "--cookie-secure=true", "--cookie-httponly=true",
            "--cookie-samesite=lax", "--cookie-expire=8h", "--cookie-refresh=1h", "--skip-provider-button=true",
            "--email-domain=" + s["email_domain"], "--skip-jwt-bearer-tokens=false",
            "--pass-access-token=false", "--pass-authorization-header=false", "--request-logging=false"]
    args.append("--set-authorization-header=" + str(bool(s["id_token_routes"])).lower())
    args += ["--whitelist-domain=" + h + ("" if port == 443 else ":" + str(port)) for h in all_hosts]
    env = [{"name": name, "valueFrom": {"secretKeyRef": {"name": SECRET, "key": key}}}
           for name, key in zip(("OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET", "OAUTH2_PROXY_COOKIE_SECRET"), KEYS)]
    items.append(resource("apps/v1", "Deployment", "oauth2-proxy", "test", spec={
        "replicas": 1, "selector": {"matchLabels": {"app": "oauth2-proxy"}},
        "template": {"metadata": {"labels": {"app": "oauth2-proxy"}}, "spec": {
            "automountServiceAccountToken": False, "containers": [{"name": "oauth2-proxy",
            "image": "quay.io/oauth2-proxy/oauth2-proxy:v7.15.5", "args": args, "env": env,
            "ports": [{"name": "http", "containerPort": 4180}],
            "readinessProbe": {"httpGet": {"path": "/ready", "port": "http"}},
            "livenessProbe": {"httpGet": {"path": "/ping", "port": "http"}},
            "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
            "securityContext": {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}}))
    items.append(resource("v1", "Service", "oauth2-proxy", "test", spec={"selector": {"app": "oauth2-proxy"},
                        "ports": [{"name": "http", "port": 4180, "targetPort": "http"}]}))
    rules = [{"host": h, "http": {"paths": [{"path": "/oauth2", "pathType": "Prefix", "backend": {
        "service": {"name": "oauth2-proxy", "port": {"number": 4180}}}}]}} for h in all_hosts]
    ingress = resource("networking.k8s.io/v1", "Ingress", "oauth2-proxy", "test", spec={
        "ingressClassName": "kong-ingress", "rules": rules})
    ingress["metadata"]["annotations"] = {"konghq.com/preserve-host": "true", "konghq.com/strip-path": "false",
                                          "konghq.com/plugins": "sso-oauth-endpoints"}
    # App Ingresses supply certificates for the identical hosts. Do not invent a second issuer/callback domain.
    items.append(ingress)
    items.append(resource("networking.k8s.io/v1", "NetworkPolicy", "oauth2-proxy", "test", spec={
        "podSelector": {"matchLabels": {"app": "oauth2-proxy"}}, "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kong-system"}},
                               "podSelector": {"matchLabels": {"app": "kong-ingress"}}}],
                     "ports": [{"protocol": "TCP", "port": 4180}]}]}))
    target.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2) + "\n")
    return annotations


def install(cluster, c, output):
    if not c["sso"]["enabled"]:
        return
    data = credentials(c)
    if data is not None:
        found = cluster.call("get", "secret/" + SECRET, "-n", "test", "--ignore-not-found", "-o", 'go-template={{index .metadata.labels "infra.sasano8.dev/owner"}}',
                             capture_output=True, text=True)
        if found.stdout.strip():
            # Read no Secret data, even on this ownership check.
            if found.stdout.strip() != "infra-kubernetes":
                raise ValueError("外部管理のgoogle-sso Secretをファイルで上書きしません")
        manifest = resource("v1", "Secret", SECRET, "test", type="Opaque",
                            data={k: base64.b64encode(v.encode()).decode() for k, v in data.items()})
        try:
            cluster.call("apply", "--server-side", "--field-manager=sso-secrets", "-f", "-",
                         input=json.dumps(manifest), text=True, capture_output=True)
        except subprocess.SubprocessError:
            raise ValueError("SSO Secret登録失敗（詳細は機密保護のため非表示）") from None
    for item in json.loads((output / "sso.json").read_text())["items"]:
        meta = item["metadata"]
        owner = cluster.call("get", item["kind"] + "/" + meta["name"], "-n", meta["namespace"], "--ignore-not-found",
                             "-o", 'go-template={{index .metadata.labels "infra.sasano8.dev/owner"}}',
                             capture_output=True, text=True)
        if owner.stdout.strip() and owner.stdout.strip() != "infra-kubernetes":
            raise ValueError("所有確認できないSSOリソースは変更しません: " + item["kind"] + "/" + meta["name"])
    cluster.call("apply", "-f", output / "sso.json")
    cluster.call("rollout", "restart", "deployment/oauth2-proxy", "-n", "test")
    cluster.call("rollout", "status", "deployment/oauth2-proxy", "-n", "test", "--timeout=300s")


def cleanup_disabled(cluster, c):
    if c["sso"]["enabled"]:
        return
    for kind, name, ns in (("ingress", "oauth2-proxy", "test"), ("deployment", "oauth2-proxy", "test"),
                           ("service", "oauth2-proxy", "test")):
        result = cluster.call("get", kind + "/" + name, "-n", ns, "--ignore-not-found", "-o",
                              'go-template={{index .metadata.labels "infra.sasano8.dev/owner"}}', capture_output=True, text=True)
        if result.stdout.strip() == "infra-kubernetes":
            cluster.call("delete", kind + "/" + name, "-n", ns, "--ignore-not-found")
    # Secret and inert Plugin/code resources are retained for explicit inventory/prune review.


RESOURCE_IDENTITIES = (
    ("v1", "ConfigMap", "session-auth-code", "kong-system"),
    ("v1", "Secret", "google-sso", "test"),
    ("apps/v1", "Deployment", "oauth2-proxy", "test"),
    ("v1", "Service", "oauth2-proxy", "test"),
    ("networking.k8s.io/v1", "NetworkPolicy", "oauth2-proxy", "test"),
    ("networking.k8s.io/v1", "Ingress", "oauth2-proxy", "test"),
    ("configuration.konghq.com/v1", "KongPlugin", "sso-standard-domain", "test"),
    ("configuration.konghq.com/v1", "KongPlugin", "sso-standard-nobuffer-domain", "test"),
    ("configuration.konghq.com/v1", "KongPlugin", "sso-knative-domain", "test"),
    ("configuration.konghq.com/v1", "KongPlugin", "sso-knative-nobuffer-domain", "test"),
    ("configuration.konghq.com/v1", "KongPlugin", "sso-oauth-endpoints", "test"),
)


def test_plan(c, format):
    if not c["tls"]["enabled"] and not c["node_port"]:
        raise ValueError("外部公開ポートがありません。TLSまたはNodePortを設定してください")
    scheme = "https" if c["tls"]["enabled"] else "http"
    port = (c["tls"]["host_https_port"] or 443) if c["tls"]["enabled"] else c["node_port"]
    suffix = "" if port == (443 if scheme == "https" else 80) else ":" + str(port)
    ids = ["standard_domain", "standard_nobuffer_domain", "knative_domain", "knative_nobuffer_domain",
           c["default_routes"]["standard"], c["default_routes"]["knative"]]
    lines = (["#!/usr/bin/env bash", "set -euo pipefail", "# 別ネットワークの端末で実施。Cookie・トークンは不要。リダイレクトは追わない。",
               "headers=$(mktemp)", "trap " + shlex.quote('rm -f "$headers"') + " EXIT"]
             if format == "shell" else ["# 外部確認プラン", "", "別ネットワークで実施。未認証・認証済み・内部診断を別々に記録する。", ""])
    for host, route in zip(hosts(c), ids):
        mode = c["sso"]["routes"].get(route, "browser") if c["sso"]["enabled"] else "public"
        path = "/get"
        api_request = mode == "api" or any(path == prefix or path.startswith(prefix + "/") for prefix in c["sso"]["api_paths"])
        expected = "200" if mode == "public" else "401" if api_request else "302"
        url = scheme + "://" + host + suffix + path
        curl = ["curl", "--silent", "--show-error", "--max-time", "180", "--output", "/dev/null", "--write-out", "%{http_code}", url]
        if format == "shell":
            lines += ["# " + route + " / " + mode,
                      'status=$(' + shlex.join(curl) + ' --dump-header "$headers")', 'test "$status" = ' + shlex.quote(expected)]
            if expected == "302":
                code = "import pathlib,sys; lines=pathlib.Path(sys.argv[1]).read_text().splitlines(); locations=[l.split(':',1)[1].strip() for l in lines if l.lower().startswith('location:')]; assert len(locations)==1 and locations[0].startswith('/oauth2/start?rd=')"
                lines.append('python3 -c ' + shlex.quote(code) + ' "$headers"')
        else:
            location = " / Location: /oauth2/start?rd=..." if expected == "302" else ""
            lines += ["- " + url + " — 未認証期待HTTP " + expected + location + "（" + mode + "）"]
    if c["sso"]["enabled"]:
        lines += ["# 手動確認: Googleログイン→callback→元URL、期限切れ、ログアウト、許可外403、認証停止503。",
                  "# 認証済みで/get・32MiB upload/download・dripを比較。Cookieやトークンをログに出さない。",
                  "# Knativeはアクセスを止め、内部でPod 0件を確認してから認証済みブラウザで再アクセスする。"]
    lines += ["# DNSとヘアピンNATは別ネットワークで切り分ける。--resolveはDNS診断のみ、TLS検証は無効にしない。",
              "# 記録: コミット / 日時 / 実行場所 / 内部結果 / 外部未認証結果 / 外部認証済み結果 / 未実施項目。"]
    return "\n".join(lines)
