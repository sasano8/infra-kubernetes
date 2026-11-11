# infra-kubernetes

k0s 上で通常の Kubernetes Service と Knative Service を Kong から公開します。
Knative の入口には Kourier を使い、保護経路では Google Workspace SSO を利用できます。

## インストール

Kong を動かす Linux ホストで、対象クラスタに `kubectl` で接続できることを確認してから実行します。
k0s が未導入の場合は [新規ホストの準備](docs/setup-details.md)から始めます。

```bash
git clone https://github.com/sasano8/infra-kubernetes.git
cd infra-kubernetes
./bin/infra configure  # 初回のみ。既存設定を使う場合は省略
```

[HTTPS](docs/https.md) の DNS と Cloudflare トークンを準備し、SSO を有効にした場合は [Google callback と認証情報](docs/sso.md)も設定します。
設定ファイルの全体例は [example.json](configs/environment/example.json) にあります。

```bash
./bin/infra validate --require-https
make up  # 設定を適用し、サービスを起動・更新する
```

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
