# infra-kubernetes

EC2 上に k0s・Kong・Knative を構築し、通常 Service と Knative の2経路を確認する。
Kong は DB-less のまま、KIC（Kong Ingress Controller）で Ingress を反映する。

```mermaid
flowchart LR
    HTTP[HTTP リクエスト] -->|VM 内での確認| Kong
    Internet[インターネット] -->|HTTPS :443 / DNS only| Kong
    Kong --> Service[通常の Kubernetes Service]
    Kong --> Kourier --> Knative[Knative Service]
```

Ingress はホスト・パスと転送先を定義し、KIC が Kong に反映する設定リソース。
通信を中継するコンポーネントではないため、上図には含めていない。
現在は Ingress を使用し、Gateway API の HTTPRoute は使用していない。
Kong の Namespace は `kong-system`、アプリは `test`。
証明書用 Secret は `cloudflare-api-token`、Issuer は `cloudflare-letsencrypt` を使う。

## インストール

`dnf` が使える新規の専用 EC2 を使用する。
Python 3.10 以上・curl・CA 証明書・sudo・systemd が必要。

```bash
sudo dnf install -y git
mkdir projects
cd projects
git clone https://github.com/sasano8/infra-kubernetes.git
cd infra-kubernetes
```

HTTPS 公開用の設定ファイルを配置する。`email` は受信できる自分のメールアドレスに変更する。

```bash
cat > configs/environment/local.json <<'EOF'
{
  "mode": "new-host",
  "base_domain": "tmp.ys-method-dev.com",
  "knative_domain": "knative.tmp.ys-method-dev.com",
  "standard_domain": "standard.tmp.ys-method-dev.com",
  "kourier_service": "kourier.kourier-system.svc.cluster.local",
  "node_port": null,
  "tls": {
    "enabled": true,
    "email": "you@example.com",
    "cloudflare_zone": "ys-method-dev.com",
    "host_https_port": 443
  }
}
EOF
```

`base_domain` は共通の参照情報。`knative_domain` は Knative 用、
`standard_domain` は通常経路のベースドメインとして個別指定する。

構築前に [Cloudflare API トークンを配置](docs/https.md#2-cloudflare-api-トークンを配置)し、
下の「HTTPS で公開」の DNS・EC2 設定を済ませる。

```bash
sudo ./bin/infra validate --require-https
sudo ./bin/infra up --require-https
```

設定だけを確認する場合は `sudo ./bin/infra validate --require-https`。
`up` も開始時に設定と必要なトークンを検証し、不足していれば構築前に停止する。

Namespace は `test`。公開ホスト名はアプリ名・Namespace・種類の順に揃える。

| 種類 | リソース名 | 公開ホスト名 |
| --- | --- | --- |
| Knative Service（ksvc） | `hello` | `hello-test-ksvc.<knative_domain>` |
| 通常 Service / Deployment（ssvc） | `hello-ssvc` | `hello-test-ssvc.<standard_domain>` |

## 動作確認

```bash
sudo python3 scripts/check-routes.py
```

port-forward を自動で起動し、次を確認する。

- Kong → 通常 Service：HTTP 200・`standard-service`
- Kong → Kourier → Knative：HTTP 200・`knative-service`
- Knative の Pod が0件になるまで待ち、再アクセスによる復帰を確認

ゼロになるまでの目安は最後のアクセスから1〜2分、最大10分待機する。
待機中は別のアクセスを止める。すべて成功すると `3/3 成功` と表示する。

## HTTPS で公開（Cloudflare DNS-01）

Caddy と同じ方式で、cert-manager が証明書を自動発行・更新し、KIC が Kong に反映する。
Cloudflare Tunnel は使わず、DNS は **DNS only** で EC2 のパブリック IP へ向ける。
通常・Knative の両ドメインと、それぞれの `*.` の証明書を発行する。
公開するホスト・パスは別途 Ingress で指定し、KIC が Kong に動的に反映する。

証明書の発行元は **Let's Encrypt**。Cloudflare は DNS-01 認証用の DNS を管理する。

```mermaid
flowchart LR
    CM[cert-manager] -->|API で認証用 TXT を作成・削除| CF[Cloudflare DNS]
    CM -->|証明書を申請・更新| LE[Let's Encrypt]
    LE -->|DNS-01 認証の TXT を確認| CF
    LE -->|証明書を発行| CM
    CM -->|証明書・秘密鍵を保存| Secret[TLS Secret]
    Secret -->|KIC が読み取り・反映| Kong[Kong / HTTPS 終端]
```

1. 上記の `tls` 設定と [Cloudflare API トークンの配置](docs/https.md#2-cloudflare-api-トークンを配置)を済ませる。
2. `standard.tmp.ys-method-dev.com`、`*.standard.tmp.ys-method-dev.com` と `*.knative.tmp.ys-method-dev.com`、
   `knative.tmp.ys-method-dev.com` の A レコードを登録し、EC2 の443/TCPを許可する。
3. 構築・更新して確認する。

   ```bash
   sudo ./bin/infra validate --require-https
   sudo ./bin/infra up --require-https
   curl --fail-with-body https://hello-test-ssvc.standard.tmp.ys-method-dev.com/
   curl --fail-with-body https://hello-test-ksvc.knative.tmp.ys-method-dev.com/
   ```

DNS-01 のため証明書発行用に80番を開ける必要はない。
既存 Caddy が443番を使用していれば自動停止せずエラーにする。
[Cloudflare API の取得・設定手順](docs/cloudflare.md) / [証明書・更新の詳細](docs/https.md)

## 詳細

古いリソースは Git 履歴との差分から確認・削除できる（`apply` は削除を実行する）。

```bash
sudo python3 scripts/prune-resources.py plan
sudo python3 scripts/prune-resources.py apply
```

[削除候補・保留条件・配備記録の詳細](docs/pruning.md)

- [構成・設定・既存クラスタへの追加](docs/setup-details.md)
- [HTTPS・証明書の自動発行と更新](docs/https.md)
- [KIC 構成と既存環境の移行](docs/kic.md)
- [確認手順とオプション](docs/verification.md)
- [webhook のトラブルシューティング](docs/webhook-diagnostics.md)
- [撤去](docs/cleanup.md)
- [infra-k0s との対応](docs/migration.md)
