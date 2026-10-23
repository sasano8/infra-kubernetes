# Google Workspace SSO

## 実装方式と範囲

Kong OSS 3.9.1＋KIC 3.4.3に、リポジトリ内のLuaプラグイン `session-auth` を
ConfigMapでマウントする。oauth2-proxyは `v7.15.5` に固定する。
KongのOAuth2プラグインはGoogleログインのforward-auth代用として使わない。
Enterprise OIDCを必須にせず、既存のKongを維持して認証確認だけを接続する方式を選んだ。

```text
ブラウザ → Kong（ルート選択 → session-auth → 許可後に本文転送）
                          │                ├→ 通常Service
                          │                └→ Kourier → Knative
                          └ GET /oauth2/auth → 共通oauth2-proxy ↔ Google
```

認証確認はCookieを付けたGETだけで、クライアントの本文・Bearerトークンを送らない。
Kongのbuffer／nobufferは各ルート定義のまま。認証処理は本文を読み取らない。
SSOセッションCookieはアプリへの転送から除去するため、go-httpbinのレスポンスや
アプリログへセッションを漏らさない。アプリ固有のCookieは維持する。
認証応答の分割Cookie・更新Cookieは、アプリのSet-Cookieと合わせて返す。

この実装はソース・生成物・モックPDKで検証した。Kongコンテナ上の実動作、Googleの実ログイン、
複数Host、セッション更新、HTTPS、スケールゼロ復帰、実通信の非バッファリングは未検証。
この作業環境ではDockerソケットが使用できず、Google資格情報も設定されていない。
自動テスト合格を本番運用検証完了として扱わない。

## 構築時の設定

既存のenvironment JSONへ追加する。

```json
"sso": {
  "enabled": true,
  "email_domain": "your-workspace.example",
  "secret_file": ".local/secrets/google-sso.json",
  "routes": {
    "standard_domain": "browser",
    "standard_nobuffer_domain": "browser",
    "knative_domain": "browser",
    "knative_nobuffer_domain": "browser"
  },
  "api_paths": ["/api", "/upload"]
}
```

`bin/infra configure` でも有効／無効、許可メールドメイン、Secretファイルを設定できる。
有効時はHTTPSが必須で、TLS設定も入力する。`email_domain`は従来の
`SSO_EMAIL_DOMAIN`に対応する非機密設定。

- `enabled=true`：ルートIDごとの認証を適用する。routes省略時は全4ルートがbrowser。
- `browser`：未認証GET／HEADはログインへ302。api_pathsのパスと配下、その他のHTTPメソッド、
  Upgrade要求は401。APIのGETをリダイレクトさせたくない場合はパスまたはapiルートを指定する。
- `api`：未認証は常に401。これはブラウザCookieを検証するAPI用応答であり、Bearer認証ではない。
- `public`：明示したルートを認証なしで公開する。認証済みを示す外部ヘッダーは除去する。
  同じアプリを別のpublicルートでも公開すれば、そのアプリはそこから利用できる。公開先の設計を確認する。
- 認可拒否は403、認証サービス不達・不正応答は503。障害時に無認証転送しない。
- `enabled=false`：SSOは適用しない。Google用Secretは不要。保護していたアプリも公開される。
- 古い設定でssoを省略した場合はfalse互換。ただし既存SSO用Ingressを検出したら、解除には
  `enabled=false` の明示を求める。`sso: {}` や他の項目だけを残してenabledを省略した場合も
  解除は許可しない。

アプリは `infra.sasano8.dev/route` で公開先を選ぶだけで、SSOやbufferingをラベルに埋め込まない。
既定ルートを使うサンプルにも選択先ルートの認証ポリシーを適用する。

## Googleクライアント・Cookie

6つのサンプル公開Hostそれぞれに `/oauth2/callback` を作る。
Google OAuth Webクライアントへ、実際に使うすべてのHTTPS callbackを登録する。
非標準HTTPSポートを使う場合はポートも含める。
設定から生成した `sso.json` のIngress rulesと `test-plan` のHostを照合する。

callbackは固定のパスを指定し、oauth2-proxyは信頼したHostから完全なURLを構成する。
KongプラグインがHTTPS、許可Host、公開してよいOAuthエンドポイントを確認し、
転送ヘッダーを上書きする。戻り先は許可した実Hostと公開ポートのみに限定する。
Kong 3.9.1はNginx変数からX-Forwarded-*を再生成するため、プラグインは
`upstream_x_forwarded_host`／`proto`／`port`も設定する。この連携は採用版のKong内部実装に依存する。
Kong更新時はカスタムHTTPSポートを含むcallbackを実コンテナでも確認する。
`/oauth2/auth`、`/ping`等は外部OAuthルートから公開しない。
Googleの許可ユーザー・組織・同意画面の公開範囲も設定する。メールドメイン制限だけで
アプリ内部の管理権限が付くわけではない。

Cookieは `__Host-infra_sso`、Secure、HttpOnly、SameSite=Lax、Path=/、有効期限8時間、
更新間隔1時間。親ドメイン共有はしない。Hostごとにcallbackとセッションが成立する。
Google側のログイン済みセッションは利用され得るが、全Hostの同時ログイン／ログアウトは保証しない。
ログアウトは対象Hostの `/oauth2/sign_out`。Googleアカウント全体のログアウトではない。

## Secret供給

`test/google-sso` の次の3キーを用意する。値をGit・チャット・ログに貼らない。

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `OAUTH2_PROXY_COOKIE_SECRET`（32 byteの乱数を標準base64で符号化）

Infisical／External Secrets等でこのSecretを供給する場合、`secret_file`は省略する。
既存Secretの検証では非空キー名だけを取得し、値を出力しない。
最初から構築する場合は、上記キーを持つJSONファイルを指定してbootstrapできる。
通常ファイル・権限600が必須で、ファイルの値は生成マニフェストへ保存しない。
登録APIのエラー本文も表示しない。外部管理・所有確認できないSecretをファイルで上書きしない。

```bash
mkdir -p .local/secrets
# 安全なエディタ等で資格情報JSONを作成する。シェル履歴や共有ターミナルへ値を貼らない。
chmod 600 .local/secrets/google-sso.json
bin/infra validate
bin/infra render
sudo bin/infra deploy --kubectl 'k0s kubectl'
```

ファイル供給ではCookie鍵の形式も事前検証する。既存Secretはキーの有無を事前検証し、
値の妥当性はoauth2-proxy起動で確認する。不正ならrolloutが失敗し、保護ルートは認証を解除しない。
Secret更新後はdeployでoauth2-proxyを再起動して環境変数を読み直す。
Cookie鍵変更は既存セッションを無効化する。OAuth client secret変更はGoogle側と同期させる。

## 配備・無効化・復旧

有効時はコードConfigMap、KongPlugin、oauth2-proxy Deployment／Service、OAuth用Ingress、
NetworkPolicyを生成する。認証サービスのIngress受信はKong Podへ限定する。
CNIのNetworkPolicy実装、KourierのClusterIP、ホスト・クラウドFW、既存の別IngressやNodePortも確認する。
ルート単位の入口認証だけで、クラスタ内のすべてのアプリ直アクセスを遮断したとは扱わない。

Luaコードの変更ではハッシュannotationでKongをrolloutする。無効化はルートからPlugin参照を
除き、所有確認済みのOAuth Ingress・Deployment・Serviceを外してからKongのLuaをアンロードする。
Secret、無効なKongPlugin／コードConfigMap／NetworkPolicyは再有効化・調査のため保持する。
定義に残る資源はpruneが自動削除する対象ではない。不要なら参照と所有権を確認して個別整理する。
`remove`は既存仕様どおり所有するtest／kong-system Namespaceを撤去するため、この中のSecretも対象になる。

更新は単一replicaの構成であり、認証の有効／無効切替は無停止・原子的切替を保証しない。
設定反映中には旧ルート設定が残る時間があるため、外部入口を閉じた検証環境または
メンテナンス中に実施し、外部未認証確認を完了してから入口を開く。
Kong／oauth2-proxyのrollout完了だけをSSO検証成功と扱わない。

障害時はKongのプラグイン読込、KICの設定反映、oauth2-proxyのReady、Service DNS、
NetworkPolicy、Secretのキー、Google callback登録を確認する。ログにはCookie・トークンを出さない。
SSO有効時のKong access logはクエリを記録しない形式にし、OAuthコードのログ流出を避ける。
oauth2-proxyのrequest loggingも無効にする。

## 検証手順

```bash
REQUIRE_LUA_TESTS=1 python3 -m unittest discover -s tests -v
bin/infra test-plan --format markdown
bin/infra test-plan --format shell > .local/external-sso-check.sh
```

自動テストにはPython 3.10以上とLua実行環境が必要。`lua5.1`、`luajit`、`lua`、`texlua`の
いずれかを用意する。上のコマンドはLuaがない場合も失敗し、認証プラグインのテストを
スキップしたまま成功としない。GitHub ActionsもPython／Luaの全テストを実行する。

[SAS-73](https://linear.app/sasano8/issue/SAS-73)にレビューで判明したバグと再現条件を記録した。
回帰テストはenabled省略による既存SSO解除、複数アプリCookieとSSO更新Cookieの両立、
HTTPS公開ポートを含む実効転送ヘッダー、api_pathsに応じた外部プランの期待値を検証する。
Luaテストは実handlerを実行し、Kong 3.9.1のPDKとNginxのヘッダー転送規則をモックで再現する。
KongコンテナやGoogleとの実通信はこのテストの対象外。

プラン生成は通信・Secret取得を行わない。生成したshellを別ネットワークの端末で実行する。
リダイレクトを追わず期待ステータスを確認し、TLS検証を無効にしない。
ブラウザでは各Hostのログイン、callback、元URL／クエリ、許可外、期限切れ、ログアウトを確認する。
認証済みで [ストリーミング検証](streaming.md)を実施する。API送信は先にログインし、
未認証本文の自動再送・再開を期待しない。S3署名認証やBearer認証は別方式として未実装。
ブラウザCookieをcurlへコピーして共有ログに残す手順は用意しない。

内部診断は次を別々に実行する。認証やKongを迂回するので、外部SSO成功の証跡にはしない。
いずれも127.0.0.1へのport-forwardだけを起動し、公開バイパスは作らない。

```bash
sudo python3 scripts/check-routes.py --internal standard
sudo python3 scripts/check-routes.py --internal knative
```

通常は3ホスト、Knativeは3ホストと3サービスのPodゼロ→復帰を確認する。
認証済みの外部スケールゼロ復帰は、アクセス停止→内部でPodゼロ確認→ブラウザ再アクセスを行う。
内部成功・外部未認証・外部認証済み・未実施を分けて日時・コミット・実行場所とともに記録する。
ヘアピンNATは別ネットワークで切り分け、--resolveを公開DNSの検証成功と扱わない。

## 仕様の確認先

- [oauth2-proxy integration](https://oauth2-proxy.github.io/oauth2-proxy/configuration/integration/)
- [採用版callback生成とセッション検証](https://github.com/oauth2-proxy/oauth2-proxy/blob/v7.15.5/oauthproxy.go)
- [Kongのカスタムプラグイン配備](https://developer.konghq.com/kubernetes-ingress-controller/custom-plugins/)
