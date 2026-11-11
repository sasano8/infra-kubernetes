# infra-kubernetes

k0s 上で通常の Kubernetes Service と Knative Service を Kong から公開します。
Knative の入口には Kourier を使い、保護経路では Google Workspace SSO を利用できます。

## インストール

Kong を動かす Linux ホストで、対象クラスタに `kubectl` で接続できることを確認してから実行します。
k0s が未導入の場合は [新規ホストの準備](docs/setup-details.md)から始めます。

```bash
git clone https://github.com/sasano8/infra-kubernetes.git
cd infra-kubernetes
```

### 環境設定を作る

次のブロックをまとめて貼り付けると、HTTPS・Google Workspace SSO・Argo CD の導入と Knative 経由の公開を含む設定を作成します。
既存の `configs/environment/local.json` がある場合は `make edit-config` で必要な値を反映してください。
このブロックは既存ファイルの上書きを拒否します。

```bash
(
  set -o noclobber
  cat > configs/environment/local.json <<'JSON'
{
  "mode": "new-host",
  "base_domain": "backoffice.ys-method-dev.com",
  "node_port": null,
  "kourier_service": "kourier.kourier-system.svc.cluster.local",
  "tls": {
    "enabled": true,
    "email": "you@example.com",
    "cloudflare_zone": "ys-method-dev.com",
    "cloudflare_token_file": ".local/secrets/cloudflare-api-token",
    "host_https_port": 443
  },
  "sso": {
    "enabled": true,
    "email_domain": "ys-method.com",
    "auth_host": "auth.knative.backoffice.ys-method-dev.com",
    "cookie_domain": "backoffice.ys-method-dev.com",
    "cookie_expire_hours": 72,
    "secret_file": ".local/secrets/google-sso.json"
  },
  "plugins": {
    "argocd": {"enabled": true},
    "infisical": {"enabled": false},
    "pinner": {"enabled": false}
  },
  "upload_limits": {
    "max_body_bytes": 110100480,
    "client_body_idle_timeout_seconds": 30,
    "upstream_connect_timeout_seconds": 5,
    "upstream_send_idle_timeout_seconds": 30,
    "upstream_read_idle_timeout_seconds": 330,
    "max_concurrent_upload_requests": 5
  },
  "error_service": {"name": "platform-portal", "namespace": "test", "port": 8080},
  "default_routes": {
    "standard": "standard_domain",
    "knative": "knative_domain"
  },
  "routes": {
    "knative_public": {
      "kind": "knative",
      "domain": "knative-public.backoffice.ys-method-dev.com",
      "auth": "public",
      "buffering": true
    },
    "standard_domain": {
      "kind": "standard",
      "domain": "standard.backoffice.ys-method-dev.com",
      "auth": "sso",
      "buffering": true
    },
    "standard_nobuffer_domain": {
      "kind": "standard",
      "domain": "standard-nobuffer.backoffice.ys-method-dev.com",
      "auth": "sso",
      "buffering": false
    },
    "knative_domain": {
      "kind": "knative",
      "domain": "knative.backoffice.ys-method-dev.com",
      "auth": "sso",
      "buffering": true
    },
    "knative_nobuffer_domain": {
      "kind": "knative",
      "domain": "knative-nobuffer.backoffice.ys-method-dev.com",
      "auth": "sso",
      "buffering": false
    }
  }
}
JSON
)
```

`make edit-config` で `tls.email` を ACME の連絡先、`sso.email_domain` を許可する Google Workspace のメールドメインにします。
既存設定から切り替える場合は、`plugins.argocd` を `{"enabled": true}` にします。
ドメインを変える場合は、`base_domain`、各 `routes.*.domain`、`tls.cloudflare_zone`、`sso.auth_host`、`sso.cookie_domain` を合わせて変更します。
k0s だけを導入済みの専用クラスタは `mode: new-host`、稼働中の Knative／Kourier を流用する場合は `mode: existing` を指定します。

### 認証情報を用意して配備する

- Cloudflare で各 `routes.*.domain` とそのワイルドカードを DNS only で Kong の公開 IP に向け、ホストの TCP 443 番に到達できるようにします。 [DNS・トークンの設定](docs/https.md)
- Google OAuth クライアントの承認済みリダイレクト URI に `https://auth.knative.backoffice.ys-method-dev.com/oauth2/callback` を登録します。 [Google OAuth の設定](docs/sso.md)

```bash
make install-helm
make configure-secret-cloudflare-token
make configure-secret-google-sso
./bin/infra validate --require-https
make up  # 設定を適用し、サービスを起動・更新する
```

保存済みの認証情報を使う場合は、該当する `configure-secret` コマンドを省略します。
配備後は [Argo CD](https://argocd-server-argocd-ksvc.knative-nobuffer.backoffice.ys-method-dev.com/) を開いて確認します。 [Argo CD の確認手順](docs/platform-plugins.md)

## 動作確認

```bash
make test-connect-prepare  # 認証済み Cookie を準備
make test-connect          # 未認証・認証後の HTTPS 接続を確認
make test-connect-cleanup  # テスト用 Cookie を削除
```

## ドキュメント

[ドキュメントサイト](https://sasano8.github.io/infra-kubernetes/)

- [導入・設定適用](docs/setup-details.md)、[HTTPS・DNS・証明書](docs/https.md)
- [アプリ公開とルーティング](docs/route-configuration.md)、[SSO](docs/sso.md)、[ストリーミング](docs/streaming.md)
- [動作確認と障害切り分け](docs/verification.md)、[追加プラグイン](docs/platform-plugins.md)、[移行・旧リソース整理・撤去](docs/cleanup.md)

MkDocs サイトは [uv](https://docs.astral.sh/uv/) を使って `make docs` でビルドできます。
`make docs-serve` で [ローカルのプレビュー](http://127.0.0.1:8000/infra-kubernetes/) を開けます。
