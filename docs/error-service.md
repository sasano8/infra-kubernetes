# エラー生成サービスの接続プロトコル

エラー応答は [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) のProblem Details形式で生成する。
KongとKourierは同じ通常のKubernetes Service・単一ポートを呼ぶ。
エラー生成側は呼び出し元のプロキシを判定せず、内部ヘッダーで指定された理由を表示する。

## 設定

environment JSONのトップレベルに指定する。省略時も以下が既定値。

```json
"error_service": {
  "name": "platform-portal",
  "namespace": "test",
  "port": 8080
}
```

`port` はKubernetes Serviceの `spec.ports[].port`。プロキシごとのポートや、PodのtargetPortを指定する項目ではない。
既定の `platform-portal/test` を指定すると、組み込みのログイン画面兼エラー生成アプリを使う。
このServiceの公開ポートは設定値、転送先のユーザーコンテナは8080番で固定する。

別のServiceを指定した場合、そのServiceとアプリ本体は別管理。インストーラーは接続先を作成・変更しない。
接続先は常時ReadyなPodへ到達する通常のServiceとし、下のHTTP仕様を実装する。
Kong／Kourierに戻るServiceを指定すると循環するため使用しない。

```json
"error_service": {
  "name": "error-renderer",
  "namespace": "platform",
  "port": 8080
}
```

Knative内部IngressのService参照は同じNamespaceに限られるため、`test/error-service` という
ExternalName aliasを生成して指定先の `<name>.<namespace>.svc.cluster.local` へ接続する。
Kong・Kourierともこのaliasの同じポートを使う。aliasにPodはなく、追加のプロキシも動作しない。

## リクエスト

```http
GET /original/path HTTP/1.1
Host: requested.example.com
X-Infra-Error: unknown_host
```

元のHost・パス・メソッドを保持する。プロキシから指定する理由は `unknown_host` だけ。

| `X-Infra-Error` | HTTP応答 | 意味 |
| --- | --- | --- |
| `unknown_host` | 421 | 該当する公開ホストがない |

KongのfallbackはHost一覧を持たず、built-in request-transformerで `unknown_host` を固定設定する。
Kourierのfallbackは `unknown_host` を上書きする。
通常のアプリ経路は、外部クライアントが付けたこのヘッダーを削除する。
`X-Infra-Error` は内部予約ヘッダーで、IDトークンの転送先ヘッダー名にも指定できない。
エラー生成サービスはHost一覧を持たず、ホストの存在確認・追加転送・ログイン誘導を行わない。
リクエスト本文・Cookie・認証トークンは読み取らず、応答後に接続を閉じる。

## レスポンス

```http
HTTP/1.1 421 Misdirected Request
Content-Type: application/problem+json
Cache-Control: no-store

{
  "type": "about:blank",
  "title": "Misdirected Request",
  "status": 421,
  "detail": "No published service matches the requested host.",
  "code": "unknown_host"
}
```

- `type`・`title`・`status`・`detail`はRFC 9457のフィールド。`title`はHTTPステータスの標準表記。
- `code`は本プロジェクトの拡張フィールド。機械判定では説明文の文字列を解析しない。
- HTTPステータスと本文の `status` を一致させる。
- Host、クエリ、Cookie、トークン、例外内容を本文に反映しない。`instance`も生成しない。
- HEADは同じステータス・Content-Type・Content-Lengthを返し、本文を送らない。
- LocationやSet-Cookieは生成しない。対応外の理由の値や重複した理由ヘッダーは400 `invalid_error_reason`。

組み込みアプリはエラーヘッダーなしのGET／HEAD `/` でログイン画面を返す。
未定義パスは404のProblem Details、`/` のその他メソッドは405のProblem Detailsを返す。
他のアプリやoauth2-proxy、SSO認証プラグインのエラー本文をこのサービスで書き換えることはない。

## Knativeネットワークprobe

Kourierから `X-Infra-Error: unknown_host` とともに次のヘッダーが届く場合は、
ネットワークprobeとして200を返し、受け取ったhashを応答ヘッダーへコピーする。

```http
K-Network-Probe: probe
K-Network-Hash: <64文字の小文字16進hash>
```

Kourierは `override` を実際のIngress hashへ上書きしてから送る。
新規アプリの経路が反映される前はfallbackのhashになるため、アプリ側のprobeが成功したと誤認しない。
Kongのfallbackは外部からのprobeヘッダーを削除する。

## 組み込みアプリと移行

実装は [platform-portal/server.py](../apps/platform-portal/server.py)。Python標準ライブラリだけで動作する。
ローカルでプロトコルだけを試す場合は、次のサーバーを起動し、別ターミナルから呼び出せる。

```bash
python3 apps/platform-portal/server.py
```

```bash
curl -i --header 'X-Infra-Error: unknown_host' http://127.0.0.1:8080/
```

`test/sso` のKnativeアプリにログイン画面と同居し、既定は `min-scale: 1` で常駐する。
通常のService `test/platform-portal` は、そのユーザーコンテナ8080番へ直接接続する。
Kourier・Activator・queue-proxyを経由しないので、エラー経路への循環は発生しない。
別管理のエラーServiceを指定した場合、ログイン画面はスケールゼロ可能に戻す。

```bash
git pull --ff-only origin main
make up
python3 scripts/check-route-errors.py
```

`up` は新しい転送設定を適用後、所有確認済みの旧 `route-errors` Deployment・Service・ConfigMapを削除する。
入力設定にプロキシ別ポートの互換形式は設けない。旧 `{"error":"unknown_host"}` 形式の応答も維持しない。
実クラスタのKIC／Knative反映確認は [ルーティングエラーの動作とテスト](routing-errors.md)を参照。
