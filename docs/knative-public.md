# SSOを要求しないKnative経路

`knative_public` はpublic専用の公開ルート。request／response bufferingは有効。
通常のKnativeラベルで選択し、KongはドメインのワイルドカードをKourierへ転送する。
既存のstandard／knativeドメイン、Google callback、共有Cookie設定は維持する。

## 設定とDNS

`configs/environment/local.json` に次の項目を追加する（既存設定への追加部分のみ）。

```json
{
  "knative_public": "knative-public.backoffice.ys-method-dev.com"
}
```

省略時は `knative_domain` の先頭ラベルを `knative-public` に置き換えたドメインになる。
既存4経路と異なるドメインで、TLSのCloudflare zoneとSSO Cookieの共有範囲内に配置する。
Cloudflareに次のAレコードをDNS onlyで追加し、Kongホストの公開IPを指定する。

| 名前 | 用途 |
| --- | --- |
| `*.knative-public.backoffice.ys-method-dev.com` | 各Knativeアプリ |
| `knative-public.backoffice.ys-method-dev.com` | ベースドメイン |

証明書 `test/knative-public-tls` はcert-managerが発行する。

```bash
git pull --ff-only origin main
make up
./bin/infra test-plan
```

`mode=new-host`ではKnativeのconfig-domainも更新する。`mode=existing`では生成された
`.local/rendered/knative-config/domain.yaml`と既存config-domainを照合し、管理者が設定を追加する。

## アプリの経路選択

Knative Service本体にラベルを付ける。

```yaml
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: example
  namespace: test
  labels:
    infra.sasano8.dev/route: knative_public
spec:
  template:
    spec:
      containers:
        - image: ghcr.io/mccutchen/go-httpbin:2.21.0
          ports:
            - containerPort: 8080
```

公開URLは `https://example-test-ksvc.knative-public.backoffice.ys-method-dev.com/`。
アプリごとのKong設定は不要。`sso.routes`でsso／apiへ変更したり、IDトークン転送を有効にしたりはできない。
SSO有効時も未認証でアプリへ到達する。Kongは共有SSO Cookieと外部からのidentityヘッダーをアプリへ渡さない。

## ログイン・ログアウト画面

SSO有効時は `test/sso` をこの経路に配備する。

```text
https://sso-test-ksvc.knative-public.backoffice.ys-method-dev.com/
```

画面は `apps/sso/index.html.tmpl`。ログイン・ログアウトボタンは既存の認証ホストの
`/oauth2/start`／`/oauth2/sign_out`へ進み、完了後はこの画面へ戻る。
ページを開くだけでは認証を要求しない。Google callback URLの追加は不要。
同じoauth2-proxy・Cookieを使うため、ログイン後はstandardなどの保護アプリでも認証が有効になる。
ログアウトは共有SSO Cookieを削除するが、Google自体からはログアウトしない。

以前のKong内蔵ログインページは削除する。認証ホストの `/` にはルートを定義せず、
`/oauth2/*` のOAuth処理だけを残す。ログイン画面は上記URLまたは `test-plan` で確認する。
