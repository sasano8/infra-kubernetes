# 未公開ホストは421、アプリのリソース不在は404

ワイルドカードDNSは、アプリが存在しない名前もKongへ到達させる。
DNSのNXDOMAINをHTTPで返すことはできないため、HTTPまで到達した未公開ホストには
`421 Misdirected Request` と `application/problem+json` の `code: unknown_host` を返す。別URLやGoogleログインへは転送しない。

| 要求 | 期待する動作 |
| --- | --- |
| 正しいpublicアプリのURL | 200 |
| 正しいアプリ内の存在しないAPI・パス | アプリ自身の404をそのまま返す |
| `knative_public`配下の存在しないアプリ名 | ワイルドカード証明書でTLS検証が成功した後、421 |
| Kongに経路がない未知のHost | HTTPまで到達すれば421 |
| 既知HostでKongに該当パスがない場合（認証ホストの `/` など） | 404、Problem Detailsの `code: path_not_found` |
| DNSに存在しない名前 | 名前解決に失敗。HTTP応答なし |
| DNSが解決しても証明書の対象外の名前 | TLS検証で失敗。HTTP応答なし |
| 有効なTLS接続に未知のHostを指定 | 421 |
| 存在するサービスの障害・起動待ち | 従来の503などを維持。404・5xxの一律変換はしない |

例えば `asdfasfasdfasdfsso-test-ksvc.knative-public.backoffice.ys-method-dev.com` は、
`*.knative-public.backoffice.ys-method-dev.com` の証明書に含まれる。
対応アプリがないので、TLS接続成功後に421を返す。

一方、`sso-test-ksvc.knative-publicasdfasdfasdfas.backoffice.ys-method-dev.com` は証明書対象外。
DNSが解決する場合も、ブラウザは証明書エラーで停止するのが期待動作。
ブラウザの `ERR_CERT_AUTHORITY_INVALID` などをHTTPステータスに置き換えることはできない。
証明書検証を省略した診断では、その後Kongから421が返るが、通常運用で検証は無効化しない。

## 実装

- KongにHost条件なしの最後のIngressを置く。既存のHost付きIngressを優先し、未一致要求だけをエラー用Serviceへ送る。
- Kongのfallback用pre-functionが生成済みの既知Host一覧を見て、`X-Infra-Error: path_not_found` または `unknown_host` を上書きする。
- Kourierには、管理対象の各Knative経路ドメインのワイルドカードを受ける内部Ingressを置く。
  実在するKnativeアプリの完全一致Hostを優先し、未一致時だけ `X-Infra-Error: unknown_host` を上書きして転送する。
- 転送先は `error_service` の通常のKubernetes Service・単一ポート。呼び出し元ごとのポートは使わない。
- 既定はログイン画面 `test/sso` と同居するアプリ。`min-scale: 1` で常駐し、
  `test/platform-portal` Serviceからユーザーコンテナ8080番へ直接接続する。Kourier／queue-proxyへ戻さない。
- 通常のアプリ経路ではrequest-transformerでクライアントの `X-Infra-Error` を除去する。
  本文の変換を設定しないため、このヘッダー処理で本文を読み込まない。
- 以前の独立した `route-errors` Deployment・Service・ConfigMapは、所有ラベルを確認して`up`で撤去する。
- アプリが返した404・500などは変更しない。エラー生成サービスの形式・設定・probe仕様は
  [エラーサービスの接続プロトコル](error-service.md)を参照。

Knative内部Ingressとprobeの形式は、導入版 `knative-v1.20.0` に合わせている。
upstreamの [KourierのHost生成](https://github.com/knative-extensions/net-kourier/blob/knative-v1.20.0/pkg/generator/ingress_translator.go) と
[probeの挿入](https://github.com/knative/networking/blob/release-1.20/pkg/ingress/ingress.go) を前提とする。

## 確認

反映後、公開URLへ接続できる外部端末で実行する。
認証情報は不要。TLS検証を有効にし、リダイレクトは追わない。

```bash
python3 scripts/check-route-errors.py
```

設定ファイルが別の場所にある場合は `--config` を指定する。
SSO無効時、または別のpublicアプリで確認する場合は、ルートパスが200を返すアプリを指定する。

```bash
python3 scripts/check-route-errors.py \
  --public-url https://sso-test-ksvc.knative-public.backoffice.ys-method-dev.com/
```

この確認は、ログイン画面200・アプリ内の未定義パス404・存在しないアプリ421・未知のHost421の4件。
テスト用の未公開ホスト名は実行ごとに生成する。publicアプリで全パスを200にする場合は、
既定のログイン画面など、未定義パスで404を返すアプリを使う。

証明書対象外のドメインのHTTP処理だけを調べたい場合も、検証を無効化する代わりに、
正しい証明書が使われるURLへ接続してHostだけ変えられる。

```bash
curl --silent --show-error --dump-header - \
  --header 'Host: unpublished.invalid' \
  https://sso-test-ksvc.knative-public.backoffice.ys-method-dev.com/
```

期待値は421。これは有効なSNIで接続した後のHost振り分けの確認であり、誤ったドメインの証明書が有効になったという意味ではない。

## 自動テストと実機で確認する範囲

- `tests/test_route_errors.py`：実際のHTTPサーバーで421／404、HEAD、任意メソッド、転送しないこと、probe hash応答、生成マニフェストとinventoryを検証する。
- `tests/check_route_errors.py`：実際のKong 3.9.1・Envoy 1.34をDockerで起動し、完全一致Hostの優先、未一致Host421、アプリ404の維持、TLS検証成功／失敗と診断時421を検証する。CIでも実行する。
- 上のDockerテストはKourierが生成するHostルールを再現するが、KIC／Knative controllerのreconcile自体は実行しない。
  実配備への反映後は `scripts/check-route-errors.py` と次のReady確認で補う。

```bash
kubectl -n test wait ksvc/sso --for=condition=Ready --timeout=300s
kubectl -n test wait ingresses.networking.internal.knative.dev/route-errors \
  --for=condition=Ready --timeout=300s
```

将来クラスタを使うCIを追加する場合は、上記4件に加えて、publicアプリの作成→スケールゼロ復帰→削除後421、
SSO有効／無効、Kourier更新後のprobe hashとReadyを検証する。
