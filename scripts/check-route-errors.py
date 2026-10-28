#!/usr/bin/env python3
"""Verify deployed Kong/Kourier routing errors from an external client; TLS stays enabled."""
import argparse
import http.client
import json
from pathlib import Path
import secrets
import ssl
from urllib.parse import urlsplit

import infra
import sso


def request(url, host=None):
    parsed = urlsplit(url)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                             context=ssl.create_default_context(), timeout=180)
    try:
        connection.request("GET", parsed.path or "/", headers={"Host": host or parsed.netloc})
        response = connection.getresponse()
        return response.status, response.read(65536), dict(response.getheaders())
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=infra.ROOT / "configs/environment/local.json")
    parser.add_argument("--public-url", help="既存publicアプリのHTTPS URL（省略時はSSOログイン画面）")
    args = parser.parse_args()
    config = infra.load(args.config)
    if not config["tls"]["enabled"]:
        parser.error("この確認にはTLSの有効化が必要です")
    if not args.public_url and not config["sso"]["enabled"]:
        parser.error("SSO無効時は --public-url で既存のpublicアプリを指定してください")
    public = args.public_url or sso.portal_origin(config) + "/"
    parsed = urlsplit(public)
    domain = config["routes"]["knative_public"]["domain"]
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("." + domain)
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        parser.error("--public-url はknative_public配下のHTTPS URLを指定してください")
    origin = "https://" + parsed.netloc
    suffix = ":" + str(parsed.port) if parsed.port else ""
    marker = secrets.token_hex(4)
    unknown = "https://unpublished-" + marker + "." + domain + suffix + "/"
    cases = [(origin + "/", None, 200),
             (origin + "/route-error-check-" + marker, None, 404),
             (unknown, None, 421),
             (origin + "/", "unpublished.invalid", 421)]
    for url, host, expected in cases:
        status, body, headers = request(url, host)
        label = url + (" Host=" + host if host else "")
        if status != expected:
            raise RuntimeError(f"{label}: HTTP {status}、期待値 {expected}")
        if expected == 421:
            problem = json.loads(body)
            content_type = next((value for key, value in headers.items() if key.lower() == "content-type"), "")
            valid_problem = (isinstance(problem, dict) and problem.get("code") == "unknown_host"
                             and problem.get("status") == 421 and isinstance(problem.get("type", "about:blank"), str)
                             and content_type.split(";", 1)[0].strip().lower() == "application/problem+json")
            redirected_or_cookie = any(key.lower() in ("location", "set-cookie") for key in headers)
            if not valid_problem or redirected_or_cookie:
                raise RuntimeError(f"{label}: 421の応答本文・ヘッダーが想定と異なります")
        print(f"PASS HTTP {expected}: {label}")
    print("結果: 4/4 成功。Kong/Kourierの未公開ホスト421と、既存アプリの404を確認しました。")
    print("証明書対象外のホストはTLS段階で失敗するのが期待動作です。この確認ではTLS検証を無効化していません。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        raise SystemExit("エラー: " + str(exc)) from None
