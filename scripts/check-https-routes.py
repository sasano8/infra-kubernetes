#!/usr/bin/env python3
"""Check configured HTTPS routes via DNS and loopback, without third-party modules."""
import argparse
import http.client
import json
from pathlib import Path
import secrets
import socket
import ssl
from urllib.parse import urlsplit

import infra
import sso


class HTTPSConnection(http.client.HTTPSConnection):
    """Change only the TCP destination; preserve certificate verification and SNI."""
    def __init__(self, host, port, address, timeout, connect_port=None):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address
        self.connect_port = connect_port or port

    def connect(self):
        raw = socket.create_connection((self.address or self.host, self.connect_port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def request(url, address, cookie, timeout, connect_port=None):
    parsed = urlsplit(url)
    conn = HTTPSConnection(parsed.hostname, parsed.port or 443, address, timeout, connect_port)
    try:
        headers = {"Host": parsed.netloc}
        if cookie:
            headers["Cookie"] = cookie
        conn.request("GET", parsed.path or "/", headers=headers)
        response = conn.getresponse()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read(65537)
    finally:
        conn.close()


def cases(config, overrides, marker, unknown_auth):
    samples = {"standard_domain": ("hello-test-ssvc", "/get"),
               "standard_nobuffer_domain": ("hello-test-ssvc", "/get"),
               "knative_domain": ("hello-test-ksvc", "/get"),
               "knative_nobuffer_domain": ("hello-nobuffer-test-ksvc", "/get"),
               "knative_public": ("sso-test-ksvc", "/")}
    port = config["tls"]["host_https_port"] or 443
    suffix = "" if port == 443 else f":{port}"
    for route_id, route in config["routes"].items():
        domain = route["domain"]
        policy = route["auth"] if config["sso"]["enabled"] else "public"
        prefix, path = samples[route_id]
        existing = overrides.get(route_id, "https://" + prefix + "." + domain + suffix + path)
        if route_id == "knative_public" and not config["sso"]["enabled"] and route_id not in overrides:
            raise ValueError("SSO無効時は --app knative_public=https://既存アプリ/ を指定してください")
        parsed = urlsplit(existing)
        if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("." + domain)
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("--app は指定経路配下のHTTPS URL（queryなし）にしてください")
        # /api and configured API paths use 401 instead of browser redirects.
        api = policy == "api" or any(parsed.path == p or parsed.path.startswith(p + "/")
                                    for p in config["sso"]["api_paths"])
        auth_status = 401 if api else 302
        for kind, url in (("existing", existing),
                          ("missing", "https://unpublished-" + marker + "." + domain + suffix + "/")):
            for authenticated in (False, True):
                expected = 200 if kind == "existing" else 421
                if not authenticated and policy != "public" and (kind == "existing" or unknown_auth == "required"):
                    expected = auth_status if kind == "existing" else (401 if policy == "api" else 302)
                yield route_id, kind, authenticated, url, expected


def verify(config, result, expected):
    status, headers, body = result
    if status != expected:
        return f"HTTP {status} (expected {expected})"
    if expected == 302 and not headers.get("location", "").startswith(sso.origin(config) + "/oauth2/start?rd="):
        return "unexpected login redirect"
    if expected == 421:
        try:
            problem = json.loads(body)
        except (ValueError, UnicodeError):
            return "421 response is not JSON"
        if (not isinstance(problem, dict) or problem.get("code") != "unknown_host"
                or problem.get("status") != 421
                or headers.get("content-type", "").split(";")[0].strip() != "application/problem+json"):
            return "unexpected 421 problem response"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=infra.ROOT / "configs/environment/local.json")
    parser.add_argument("--cookie-file", type=Path, help="Cookieヘッダーの値だけを保存したファイル。値は出力しません")
    parser.add_argument("--anonymous-only", action="store_true", help="認証後の確認を明示的に省略")
    parser.add_argument("--via", choices=("both", "dns", "loopback"), default="both")
    parser.add_argument("--loopback-port", type=int, help="ローカル接続のポートだけ変更（port-forward等）")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--app", action="append", default=[], metavar="ROUTE_ID=HTTPS_URL")
    parser.add_argument("--unknown-auth", choices=("current", "required"), default="required",
                        help="required: 保護経路は認証を先に要求（既定）。current: 旧仕様の未認証421を確認")
    args = parser.parse_args()
    try:
        config = infra.load(args.config)
        if not config["tls"]["enabled"]:
            raise ValueError("tls.enabled=true が必要です")
        if args.timeout <= 0 or (args.loopback_port is not None and not 1 <= args.loopback_port <= 65535):
            raise ValueError("timeout/loopback-port が範囲外です")
        if args.anonymous_only and args.cookie_file:
            raise ValueError("--anonymous-only と --cookie-file は同時指定できません")
        if not args.anonymous_only and not args.cookie_file:
            raise ValueError("認証後の確認には --cookie-file が必要です（未認証のみは --anonymous-only）")
        cookie = args.cookie_file.read_text().strip() if args.cookie_file else ""
        if args.cookie_file and (not cookie or "\r" in cookie or "\n" in cookie or cookie.lower().startswith("cookie:")):
            raise ValueError("Cookieファイルは空でない1行のCookie値だけにしてください")
        overrides = {}
        for item in args.app:
            key, sep, value = item.partition("=")
            if not sep or key not in config["routes"] or key in overrides:
                raise ValueError("--app は重複のない ROUTE_ID=HTTPS_URL 形式で指定してください")
            overrides[key] = value
        plan = list(cases(config, overrides, secrets.token_hex(6), args.unknown_auth))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    passed = total = 0
    modes = ("dns", "loopback") if args.via == "both" else (args.via,)
    print("未公開ホストの期待値: " + args.unknown_auth + " / リダイレクト追跡なし / TLS検証あり")
    for phase in ((False,) if args.anonymous_only else (False, True)):
        phase_name = "認証後" if phase else "未認証"
        print("\n=== " + phase_name + ("（保存済みCookieあり）" if phase else "（Cookieなし）") + " ===")
        before_passed, before_total = passed, total
        for mode in modes:
            for route, kind, authenticated, url, expected in plan:
                if authenticated != phase:
                    continue
                total += 1
                label = f"{mode} {route} {kind} {'authenticated' if authenticated else 'anonymous'}"
                label += " " + url
                if mode == "loopback":
                    port = args.loopback_port or urlsplit(url).port or 443
                    label += f" (TCP 127.0.0.1:{port})"
                try:
                    result = request(url, "127.0.0.1" if mode == "loopback" else None,
                                     cookie if authenticated else "", args.timeout,
                                     **({"connect_port": args.loopback_port} if mode == "loopback" else {}))
                    error = verify(config, result, expected)
                    if error:
                        print(f"FAIL {label}: {error}")
                    else:
                        passed += 1
                        print(f"PASS {label}: HTTP {expected}")
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    # Never echo exception messages, response headers/body or Cookie values.
                    print(f"FAIL {label}: {type(exc).__name__} (DNS/TCP/TLS/HTTP接続失敗)")
        print(f"{phase_name}結果: {passed - before_passed}/{total - before_total} 成功")
    print(f"結果: {passed}/{total} 成功" + ("（未認証のみ）" if args.anonymous_only else ""))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
