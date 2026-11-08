#!/usr/bin/env python3
"""Prepare or delete the local session Cookie used by HTTPS connectivity tests."""
import argparse
import getpass
import os
from pathlib import Path
import re
import tempfile
import warnings
from urllib.parse import quote

import infra
import sso


def validate_cookie(value):
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    if not value or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError("Cookieは空でない1行のヘッダー値を指定してください（保存していません）")
    cookies = [part.strip().partition("=") for part in value.split(";")]
    if not any(content and (name == sso.COOKIE_NAME or re.fullmatch(re.escape(sso.COOKIE_NAME) + r"_\d+", name))
               for name, _, content in cookies):
        raise ValueError("ログイン用のSSO Cookieが見つかりません。CSRF用だけでなく、ログイン後のCookie全体をコピーしてください（保存していません）")
    return value


def prepare(config, path):
    if not config["tls"]["enabled"] or not config["sso"]["enabled"]:
        raise ValueError("認証後テストの準備には tls.enabled=true と sso.enabled=true が必要です")
    ids = ("standard_domain", "standard_nobuffer_domain", "knative_domain", "knative_nobuffer_domain")
    host = next((host for host, route_id in zip(sso.hosts(config), ids)
                 if config["routes"][route_id]["auth"] != "public"), None)
    if host is None:
        raise ValueError("認証が必要な公開経路がありません")
    port = config["tls"]["host_https_port"] or 443
    url = "https://" + host + ("" if port == 443 else ":" + str(port)) + "/get"
    login_url = sso.origin(config) + "/oauth2/start?rd=" + quote(url, safe="")
    print("接続テスト用Cookieの準備（ブラウザのGoogleログインは手動です）")
    print("1. ブラウザで次のURLを開き、Googleログインしてください：\n   " + login_url)
    print("   ログイン後のアプリ: " + url)
    print("2. 開発者ツールのNetworkを開いた状態で、ログイン後のアプリを再読み込みしてください。")
    print("3. アプリへのリクエストを選択し、Request HeadersのCookie全体をコピーしてください。")
    print("   分割Cookieも含めます。Set-CookieやGoogle側のCookieは使用しません。")
    print("保存先: " + str(path.absolute()))
    if path.exists():
        print("既存Cookieは入力が有効な場合だけ更新します。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass("Cookieを貼り付けてEnter（非表示・空入力で取消）: ")
    except (getpass.GetPassWarning, EOFError):
        raise ValueError("非表示入力できる対話端末から実行してください（保存していません）") from None
    if not value.strip():
        print("取消しました。既存Cookieは変更していません。")
        return
    value = validate_cookie(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".test-connect-cookie-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("Cookieを権限600で保存しました。次に make test-connect を実行してください。")
    print("期限切れで認証後の確認が失敗した場合は、再度 make test-connect-prepare で更新してください。")


def cleanup(path):
    try:
        path.unlink()
    except FileNotFoundError:
        print("接続テスト用Cookieはありません。")
    else:
        print("接続テスト用Cookieを削除しました: " + str(path.absolute()))
    print("ブラウザのCookieやログイン状態は変更していません。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "cleanup"))
    parser.add_argument("--config", type=Path, default=infra.ROOT / "configs/environment/local.json")
    parser.add_argument("--cookie-file", type=Path, default=infra.ROOT / ".local/secrets/test-cookie")
    args = parser.parse_args()
    try:
        if args.action == "cleanup":
            cleanup(args.cookie_file)
        else:
            prepare(infra.load(args.config), args.cookie_file)
    except KeyboardInterrupt:
        print("\n取消しました。既存Cookieは変更していません。")
        return 130
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
