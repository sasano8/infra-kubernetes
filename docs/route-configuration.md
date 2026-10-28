# 公開経路の設定

公開経路はトップレベルの `routes` にまとめる。アプリは経路IDを選び、
ドメイン・認証・bufferingはその経路の定義に従う。

```json
{
  "routes": {
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
    },
    "knative_public": {
      "kind": "knative",
      "domain": "knative-public.backoffice.ys-method-dev.com",
      "auth": "public",
      "buffering": true
    }
  },
  "default_routes": {
    "standard": "standard_domain",
    "knative": "knative_domain"
  },
  "sso": {
    "enabled": true,
    "email_domain": "ys-method.com",
    "auth_host": "auth.knative.backoffice.ys-method-dev.com",
    "cookie_domain": "backoffice.ys-method-dev.com",
    "cookie_expire_hours": 72,
    "secret_file": ".local/secrets/google-sso.json"
  }
}
```

これは経路・SSO部分の抜粋。`mode`・`tls`・`kourier_service`・`node_port`などは既存設定を維持する。
完全な例は [README](../README.md) と [example.json](../configs/environment/example.json) を参照。

## 項目

| 項目 | 意味 |
| --- | --- |
| `kind` | `standard`／`knative`。現在サポートする5経路の種別は固定 |
| `domain` | その経路の公開ベースドメイン。全5経路で重複不可 |
| `auth` | `sso`／`api`／`public`。`knative_public`は`public`専用 |
| `buffering` | request／response両方のbuffering。JSONの`true`／`false` |

5経路と各4項目をすべて明示する。追加の経路IDや未定義の項目は受け付けない。
`auth=sso` は未認証の画面アクセスをGoogle認証へ誘導し、APIパス・GET／HEAD以外などは401を返す。
`auth=api` は未認証時に常に401。`auth=public` は認証せず転送する。
SSOの共通スイッチ `sso.enabled=false` のときは、経路のauth設定にかかわらず認証を適用しない。

認証基盤のGoogle接続・Cookie・Secretなどは `sso` に残す。`sso.routes` は使わない。
bufferingの初期例は経路名に合わせているが、実際の動作は各 `buffering` の値から生成する。
TLS Secret名は従来どおり維持する。

## アプリの経路選択

Knative Serviceのラベルは変更しない。

```yaml
metadata:
  labels:
    infra.sasano8.dev/route: knative_domain
```

これは `routes.knative_domain` に対応する。`default_routes` の値も従来の経路IDを維持する。

## 旧設定からの移動

| 旧設定 | 新設定 |
| --- | --- |
| トップレベルの `knative_domain` などの文字列 | `routes.<経路ID>.domain` |
| `sso.routes.<経路ID>` | `routes.<経路ID>.auth` |
| コード内のbuffering定義 | `routes.<経路ID>.buffering` |
| 通常／Knativeの種別 | `routes.<経路ID>.kind` |

旧トップレベルのドメイン項目、`domain`・`standard_host`別名、`sso.routes`は削除する。
旧形式の互換読み込み・自動変換は行わず、検証エラーにする。
ドメイン・auth・bufferingの値を維持して移し替えれば、公開URLと認証動作は変わらない。
Google callback、共有Cookie範囲、アプリのラベルを変える必要もない。

```bash
git pull --ff-only origin main
make edit-config
./bin/infra validate --require-https
make up
```
