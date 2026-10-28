# Google Workspace SSO

Kong・oauth2-proxy・Googleの役割と通信順序、Google側へのリダイレクトURI追加方法は
[SSOのリダイレクトとcallbackの仕組み](sso-flow.md)を参照。

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
  "auth_host": "auth.knative.tmp.ys-method-dev.com",
  "cookie_domain": "tmp.ys-method-dev.com",
  "cookie_expire_hours": 72,
  "secret_file": ".local/secrets/google-sso.json",
  "api_paths": [
    "/api",
    "/upload"
  ],
  "id_token_routes": [],
  "id_token_header": "X-Forwarded-Id-Token"
},
"routes": {
  "knative_public": {
    "kind": "knative",
    "domain": "knative-public.tmp.ys-method-dev.com",
    "auth": "public",
    "buffering": true
  },
  "standard_domain": {
    "kind": "standard",
    "domain": "standard.tmp.ys-method-dev.com",
    "auth": "sso",
    "buffering": true
  },
  "standard_nobuffer_domain": {
    "kind": "standard",
    "domain": "standard-nobuffer.tmp.ys-method-dev.com",
    "auth": "sso",
    "buffering": false
  },
  "knative_domain": {
    "kind": "knative",
    "domain": "knative.tmp.ys-method-dev.com",
    "auth": "sso",
    "buffering": true
  },
  "knative_nobuffer_domain": {
    "kind": "knative",
    "domain": "knative-nobuffer.tmp.ys-method-dev.com",
    "auth": "sso",
    "buffering": false
  }
}
```

`bin/infra configure` でも有効／無効、許可メールドメイン、Secretファイル、認証ホスト、
Cookie共有親ドメインを設定できる。
有効時はHTTPSが必須で、TLS設定も入力する。`email_domain`は従来の
`SSO_EMAIL_DOMAIN`に対応する非機密設定。

- `enabled=true`：トップレベルの `routes.<経路ID>.auth` に従って認証を適用する。
- `sso`：未認証GET／HEADはログインへ302。api_pathsのパスと配下、その他のHTTPメソッド、
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

認証専用ホストにcallbackを集約する。READMEの構成では、Google OAuth Webクライアントの
**承認済みのリダイレクトURI**へ次の1件だけを登録する。

```text
https://auth.knative.tmp.ys-method-dev.com/oauth2/callback
```

`auth_host` は既定で `auth.<knative_domain>`。公開経路ドメインの直下のホストへ変更できる。
Kongがこのホストの `/oauth2` をoauth2-proxyへ転送し、対応する既存ワイルドカード証明書を
Ingressに明示する。Knative Serviceは作らない。既存のワイルドカードDNSがKongへ向いていれば
認証専用Aレコードは不要。ただし同名レコードがある場合は到達先も確認する。
`cookie_domain` は既定で `base_domain`、省略時は全経路の共通親ドメイン。
認証ホストと全経路がこの共有範囲に入ることを設定検証する。
ログイン・ログアウト画面は [knative_public](knative-public.md) 上の通常のKnativeアプリ。
public経路でもログイン処理は同じoauth2-proxyを使うため、standardなどの保護アプリとセッションを共有する。
非標準HTTPSポートを使う場合はGoogleへ登録するURLにもそのポートを含める。

oauth2-proxyのredirect URLはこの完全なURLに固定する。アプリへの未認証GET／HEADは
`https://<auth_host>/oauth2/start?rd=<元のHTTPS URL>` へ302になり、Googleログイン後に
元のアプリのパス・クエリへ戻る。戻り先は既存の6つの公開Hostと公開ポートだけを
allowlistへ登録し、親ドメイン全体を戻り先として許可しない。
アプリ追加時は公開Hostのallowlistにも追加する。Googleのcallback追加登録は不要。
KongプラグインがHTTPS、許可Host、公開してよいOAuthエンドポイントを確認し、
転送ヘッダーを上書きする。Kong 3.9.1の `upstream_x_forwarded_host`／`proto`／`port` にも
反映するため、Kong更新時はカスタムHTTPSポートを含めて実コンテナでも確認する。
`/oauth2/auth`、`/ping`等は外部OAuthルートから公開しない。
Googleの許可ユーザー・組織・同意画面の公開範囲も設定する。メールドメイン制限だけで
アプリ内部の管理権限が付くわけではない。

Cookieは `__Secure-infra_sso`、Domain=`tmp.ys-method-dev.com`、Path=/、Secure、HttpOnly、
SameSite=Lax、有効期限は既定で72時間（3日）、更新間隔1時間。親Domainを指定するため `__Host-` は使わない。
`sso.cookie_expire_hours` で有効期限を時間単位の整数（2〜8760）に変更できる。
`local.json` の既存sso設定へ `"cookie_expire_hours": 72` を追加し、`make up` で反映する。
これはサービスのセッションCookieの期限であり、Google側のログイン期限とは別。
セッション更新に伴いCookieの期限も更新されるため、初回ログインから必ず3日後に
ログアウトする絶対期限ではない。Google側での失効や再認証要求により早くログインが必要になる場合もある。
既存Cookieの期限は配備だけでは変わらず、新規発行・更新後に新しい設定が適用される。
同じCookieで通常／Knative・buffer／nobufferの各アプリを認証する。
アプリ独自のCookieは別名・ホスト限定とし、SSO Cookieと分割CookieはKongでアプリへの転送から除去する。
配下の全アプリを同じ信頼範囲で管理し、アプリから同名の親Domain Cookieを設定しない。
共有Cookieはブラウザから配下全ホストへ送られるため、独立した信頼境界のアプリはこの配下へ置かない。
ログアウトは認証ホストの `/oauth2/sign_out`。全アプリ共通のSSO Cookieを削除するが、
Googleアカウント自体や各アプリ独自のセッションをログアウトするものではない。

### 既存のホスト別callbackから移行する

1. Googleに上記の共通callbackを登録する。旧callbackは切替確認が終わるまで残してよい。
2. `configs/environment/local.json` の既存sso設定へ次の2項目を追加する。

   ```json
   "auth_host": "auth.knative.tmp.ys-method-dev.com",
   "cookie_domain": "tmp.ys-method-dev.com"
   ```

3. ソースを更新し、メンテナンス中に設定・Lua・Kongをまとめて反映する。

   ```bash
   ./bin/infra up --require-https
   ```

4. シークレットウィンドウで通常アプリからログインし、元の `/get` に戻ることを確認する。
   続いてKnative・nobufferの各アプリを開き、Googleへの再リダイレクトなしで表示できることを確認する。
   共通ログアウトと、再度アクセスしたときのログイン誘導も確認する。
5. 確認後にGoogle側の旧アプリ別callback登録を削除できる。

GoogleクライアントとCookie暗号鍵の変更は不要。Cookie名が変わるので初回は再ログインになる。
残った旧 `__Host-infra_sso` Cookieは認証に利用せず、アプリへの転送からも除去する。
新しい設定・コードの切替は原子的ではないため、反映途中の挙動を運用成功と判断しない。
この作業環境から実クラスタへの反映とGoogleログイン確認は実施していない。

## IDトークンの転送

Google IDトークンをアプリ自身で検証する場合は、転送する公開ルートを明示する。
既定は `id_token_routes: []` で転送しない。例えばKnative nobufferルートだけを対象にする場合、
既存environment JSONのsso設定へ次の項目を追加する。

```json
"id_token_routes": ["knative_nobuffer_domain"],
"id_token_header": "X-Forwarded-Id-Token"
```

指定できるのは通常／Knativeの4つの公開ルートID。sso／apiルートが対象で、
publicルートの指定は設定エラーにする。既定ルートを使うアプリにも選択先の設定を適用する。
転送の単位は公開ルートであり、そのルートの対象アプリすべてへ渡す。
認証、buffering、APIの未認証応答の設定は維持する。

oauth2-proxy v7.15.5の `--set-authorization-header=true` を使い、内部の `/oauth2/auth` 応答から
`Authorization: Bearer <Google ID token>` を受け取る。Kongは認証・メールドメインの許可確認後、
Bearer接頭辞を外して専用ヘッダーへ付与する。Google API用アクセストークンは渡さない。
選択したルートでIDトークンが欠落・複数値・不正形式なら503としてアプリへ転送しない。
署名などの暗号学的検証をKongで追加する処理ではなく、信頼する認証サービスの結果を受け渡す機能。

クライアント由来の既定ヘッダーと設定したヘッダーは先に除去する。
SSO有効時は非対象ルート、publicルート、OAuth入口でも除去し、認証結果の偽装を防ぐ。
アプリ独自のAPI認証に使うリクエストの `Authorization` は上書きしない。
認証問い合わせへクライアントのAuthorizationや本文を送らず、認証サービスのAuthorizationを
クライアントへのレスポンスにコピーする処理も追加しない。

ヘッダー名は既定の `X-Forwarded-Id-Token` または `X-SSO-ID-Token` 等の専用名を使う。
`X-`で始まる英数字・ハイフンを許可し、既定名を除く `X-Forwarded-*`、`X-Auth-Request-*`、
`X-Kong-*`、`X-Original-*`、`X-Real-IP` は予約済みとして拒否する。

[app-authorization-demo](https://github.com/sasano8/app-authorization-demo) 側は次を設定する。

```text
SSO_ID_TOKEN_HEADER=X-Forwarded-Id-Token
SSO_GOOGLE_CLIENT_ID=<このSSOで使用するGoogle OAuthクライアントID>
APP_ALLOW_LOCAL_LOGIN=false
```

アプリ独自の署名鍵等の必須設定も別途用意する。受信アプリでGoogle署名・issuer・audience・期限を
検証し、ヘッダーが存在するだけでログイン済みとは判断しない。ヘッダー名を変える場合は
送信側のid_token_headerと受信側のSSO_ID_TOKEN_HEADERを揃える。
Google公開鍵取得のためのHTTPS接続もアプリ側に必要。

トークンはアプリログへ出力しない。go-httpbinの `/get` は受信ヘッダーをレスポンスへ含めるため、
実トークンを転送するルートで使うとトークンが応答にも含まれる。受信・検証結果だけを返すアプリで確認する。
実Kong／Google／受信アプリの結合検証は未実施。共有セッションでも、IDトークンを転送する
公開ルートは `id_token_routes` に指定したものだけ。

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

### Makefileで作成・変更する

リポジトリのルートで、sudoを付けずに実行する。

```bash
make configure-secret-google-sso
```

`local.json` の `sso.secret_file` へGoogleクライアントID・シークレットを非表示入力して保存する。
初回は32byteのCookie鍵を自動生成する。変更時の空入力は現在値を維持し、Cookie鍵も維持する。
ファイルは権限600の一時ファイルから置き換え、入力取消や保存失敗で既存JSONを壊さない。
`secret_file` を省略した外部管理構成ではローカルファイルを作成しない。

Google側の共通callback登録も済ませてから構築する。
READMEの例では認証専用ホストの `/oauth2/callback` を1つ登録する。
初回の一括構築は `make up` で行える。

### 資格情報だけを既存クラスタへ反映する

```bash
make apply-secret-google-sso
make restart-deployment-oauth2-proxy
make wait-deployment-oauth2-proxy
```

1つ目は `test/google-sso` Secretだけを更新する。既存Secretの所有ラベルを確認し、
外部管理・所有確認できないSecretは上書きしない。トークン値やAPIエラー詳細は表示しない。
2つ目で認証サービスを再起動して環境変数を読み直し、3つ目でrollout完了を待つ。
適用・再起動・待機はそれぞれ独立しているため、上の順に実行する。
Cloudflareのトークン、Kongやアプリの配備をこの更新に含めない。

既定の接続先は `kubectl`。別クラスタは例えば次のように指定する。

```bash
make apply-secret-google-sso KUBECTL=kubectl SUDO=
```

### Cookie鍵を変更する

```bash
make rotate-cookie-secret-google-sso
```

Cookie鍵も変更したい場合だけ実行する。Secretへの反映・認証サービス再起動後、既存セッションは無効になる。
通常のクライアント情報変更ではこのターゲットは不要。

### 許可ドメイン・公開ルートなどを変更する

```bash
make edit-config
make up
```

`edit-config` はenvironment JSONをエディタで開く。`EDITOR=nano` 等でエディタを選択できる。
許可メールドメイン・SSO有効／無効・公開ルート設定はSecretだけの更新では反映されないため、
一括配備で必要なリソースと参照順序を反映する。
別の設定ファイルは各ターゲットへ `CONFIG=/path/to/environment.json` を指定する。
`make help` で全ターゲットと変数を確認できる。

ファイル供給ではCookie鍵の形式も事前検証する。既存Secretはキーの有無を事前検証し、
値の妥当性はoauth2-proxy起動で確認する。不正ならrolloutが失敗し、保護ルートは認証を解除しない。
Secret更新後は `make restart-deployment-oauth2-proxy` で環境変数を読み直す。
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
ブラウザでは共通callback、元URL／クエリ、別Hostへの認証共有、許可外、期限切れ、共通ログアウトを確認する。
認証済みで [ストリーミング検証](streaming.md)を実施する。API送信は先にログインし、
未認証本文の自動再送・再開を期待しない。S3署名認証やBearer認証は別方式として未実装。
ブラウザCookieをcurlへコピーして共有ログに残す手順は用意しない。

内部診断は次を別々に実行する。認証やKongを迂回するので、外部SSO成功の証跡にはしない。
いずれも127.0.0.1へのport-forwardだけを起動し、公開バイパスは作らない。

```bash
python3 scripts/check-routes.py --internal standard
python3 scripts/check-routes.py --internal knative
```

通常は3ホスト、Knativeは3ホストと3サービスのPodゼロ→復帰を確認する。
認証済みの外部スケールゼロ復帰は、アクセス停止→内部でPodゼロ確認→ブラウザ再アクセスを行う。
内部成功・外部未認証・外部認証済み・未実施を分けて日時・コミット・実行場所とともに記録する。
ヘアピンNATは別ネットワークで切り分け、--resolveを公開DNSの検証成功と扱わない。

## 仕様の確認先

- [oauth2-proxy integration](https://oauth2-proxy.github.io/oauth2-proxy/configuration/integration/)
- [採用版callback生成とセッション検証](https://github.com/oauth2-proxy/oauth2-proxy/blob/v7.15.5/oauthproxy.go)
- [Kongのカスタムプラグイン配備](https://developer.konghq.com/kubernetes-ingress-controller/custom-plugins/)
