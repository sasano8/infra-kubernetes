# 動作確認と障害切り分け

## HTTPS・認証・未公開ホストの接続テスト

Python標準モジュールだけで実行できる。`configs/environment/local.json` の5経路について、
存在するアプリ／存在しないアプリ、未認証／ログイン済みCookie、公開DNS経由／127.0.0.1経由を確認する。
既定は計40ケース。ローカル経由でもHost・TLS SNIは元のホスト名を使用し、証明書を検証する。
リダイレクトは追わず、Cookie・応答本文・認証ヘッダーはログに表示しない。

まずk0s/Kongホストで未認証だけ確認する：

```bash
python3 scripts/check-https-routes.py --anonymous-only
```

通常は次のMakeターゲットを順に実行する（sudo不要）：

```bash
make test-connect-prepare
make test-connect
make test-connect-cleanup
```

`test-connect-prepare` は環境設定からログイン用URLと対象アプリのURLを表示し、
ブラウザの開発者ツールでCookieを取得する方法を案内する。ログイン後に対象アプリへの
リクエストのRequest HeadersからCookie全体をコピーし、対話端末へ貼り付ける。
入力は表示されない。`Cookie:` の接頭辞はあってもよい。分割されたセッションCookieにも対応する。
Set-CookieやGoogle側のCookie、CSRF用Cookieだけでは準備できない。

既定の保存先はGit管理外の `.local/secrets/test-cookie`。一時ファイルを使い、
権限600で保存する。空入力・入力取消・入力エラーでは既存Cookieを変更しない。
CookieをチャットやGitに貼らない。

`test-connect` は保存したCookieを使い、未認証と認証後の両方を計40ケース確認する。
未認証（Cookieなし）の全20件を実行してから、認証後（保存済みCookieあり）の全20件を実行し、
それぞれの小計と全体の結果を表示する。各一式の中では公開DNS経由、127.0.0.1経由の順に確認する。
Cookieがない場合は `make test-connect-prepare` を案内して終了する。
期限切れ等で認証後の確認が失敗したら、ブラウザで再ログインしてprepareを再実行する。

`test-connect-cleanup` はテスト用Cookieファイルだけを削除し、ファイルがなくても成功する。
ブラウザのCookie・ログイン状態や、他のSecretは変更しない。

環境設定・保存先・接続オプションも指定できる。保存先を変更した場合は3ターゲットで同じ値を使う：

```bash
make test-connect-prepare CONFIG=configs/environment/local.json \
  TEST_CONNECT_COOKIE_FILE=.local/secrets/custom-test-cookie
make test-connect CONFIG=configs/environment/local.json \
  TEST_CONNECT_COOKIE_FILE=.local/secrets/custom-test-cookie \
  TEST_CONNECT_ARGS="--via dns --timeout 180"
make test-connect-cleanup TEST_CONNECT_COOKIE_FILE=.local/secrets/custom-test-cookie
```

Pythonスクリプトを直接実行する場合は次と同じ：

```bash
python3 scripts/check-https-routes.py --cookie-file .local/secrets/test-cookie
```

Googleログイン自体は自動化せず、既存セッションを使ったアプリ接続を検証する。
認証済みで200を期待した接続が302/401/403になれば失敗する。
未認証の既存アプリは、経路のauthとSSO設定に従いpublic=200、sso=302、api=401を期待する。
未知ホストも保護経路では認証を先に要求する。SSO経路では未認証は302、認証済みは421。
API経路では未認証は401、認証済みは421。public経路やSSO無効時は未認証でも421。
既定の `--unknown-auth required` がこの挙動を検証する。
移行前の実装を診断する場合だけ `--unknown-auth current` で旧仕様の未認証421を期待できる。
この指定は期待値だけを切り替え、サーバー設定は変更しない。

既存アプリの既定値はリポジトリのhelloサンプル。knative_publicはSSOポータルを使う。
別アプリを確認したい場合や、SSO無効でポータルがない場合は経路ごとに上書きする：

```bash
python3 scripts/check-https-routes.py --cookie-file .local/secrets/test-cookie \
  --app knative_domain=https://my-app.knative.example.test/get \
  --app knative_public=https://my-public-app.knative-public.example.test/
```

`--config PATH` で別の環境設定を使える。URLは指定経路のドメイン配下のみ受け付ける。
未知ホストは実行ごとにランダムな名前を作り、公開アプリに衝突する可能性を下げる。

ヘアピンNATやDNSの問題で公開URLだけ失敗する場合も、ローカル接続の結果は独立して出力する。
全ケースを継続し、1件でも失敗すれば終了コード1を返す。接続失敗は成功やスキップにしない。
外部PCから実行する場合、127.0.0.1はそのPC自身なので、`--via dns` を指定する。
ローカル経由だけなら `--via loopback` を指定する。
接続ポートは設定の `tls.host_https_port`（nullなら443）を使用する。
HTTPSのport-forward等を使う場合は `--loopback-port 8443` でローカル接続先ポートだけ変更できる。
タイムアウトは1リクエスト30秒で、Knativeのコールドスタートが長い場合は `--timeout 180` を指定する。
実クラスタ・実Cookieでの成功は利用環境で確認する必要がある。

## HTTP とスケールゼロ復帰

SSO有効時は [SSO検証手順](sso.md)を使用する。以下のKong HTTP疎通はSSO無効時用。
SSO有効時は内部経路診断と、外部未認証／認証済み確認を分離する。

新規 k0s とデフォルト設定を例にする。既存クラスタでは関数 `k()` の中身を
`kubectl "$@"` に置き換える。Bash、curl、Python 3 を使用する。

### 1コマンドで公開経路とスケールゼロ復帰を確認する

リポジトリのルートで次を実行する。Python 標準ライブラリのみを使用する。

```bash
git pull origin main
python3 scripts/check-routes.py
```

`configs/environment/local.json` のドメイン設定を読み、Kong の port-forward を
127.0.0.1:18080 で起動する。ログは別スレッドで読み取り、転送開始を待ってから
明示4経路と既定2経路の HTTP ステータスと本文を検証する。
6つの公開ホストが200で、本文がそれぞれ go-httpbin の JSON（`headers.Host` が各公開ホスト） なら成功。
どちらかが失敗したら終了コード1。終了・失敗・Ctrl-C 時には自身が起動した
port-forward を停止する。既存の8080番の port-forward は停止しない。
続いて Knative の `hello`・`hello-nobuffer`・`hello-default` のそれぞれについて、
「Pod がゼロ件になるまで待機します」と表示し、5秒間隔で Pod 数を取得する。
Terminating / Error の Pod も残っている間はゼロと扱わない。
待機中は Knative に HTTP リクエストを送らず、Pod が0件になった後、同じ Kong 入口へ
リクエストして HTTP 200・go-httpbin JSON の Knative 側 Host と Ready Pod の再生成を確認する。
待機時間の目安は最後のアクセスから通常1〜2分程度。
autoscaling window の30秒に加え、スケールゼロの猶予期間や Pod の終了処理があるため前後する。
すべて成功すれば `9/9 成功`。各Knativeサービスの待機上限は既定600秒で、API エラーや期限切れは失敗とする。
Knative の初回リクエストが失敗した場合、復帰確認はスキップし終了コード1を返す。
実行中は別ターミナルや監視から Knative にアクセスしないこと。

```bash
# ポート変更、または通常の kubectl を使う場合
python3 scripts/check-routes.py --port 18081
python3 scripts/check-routes.py --kubectl kubectl

# ゼロになるまで最大15分待機する場合
python3 scripts/check-routes.py --scale-zero-timeout 900

# 証跡を残す場合
mkdir -p .local/results
set -o pipefail
python3 scripts/check-routes.py 2>&1 | tee .local/results/routes.log
```

### 手動で確認する場合

以下は buffer 側の手順。nobuffer 側も [4経路の構成](route-configuration.md)のホストで
同じ確認を行う。Knative nobuffer は `ksvc/hello-nobuffer` と
`serving.knative.dev/service=hello-nobuffer` を使い、個別にPod 0件と復帰を確認する。

VM のターミナル A:

```bash
kubectl -n kong-system port-forward service/kong-proxy 8080:80
```

VM のターミナル B（リポジトリのルート）:

```bash
set -euo pipefail
k() { kubectl "$@"; }
mkdir -p .local/results
date -u +%FT%TZ > .local/results/started-at.txt
CONFIG=configs/environment/local.json
STANDARD_HOST=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print("hello-test-ssvc." + c["routes"]["standard_domain"]["domain"])' "$CONFIG")
KNATIVE_HOST=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print("hello-test-ksvc." + c["routes"]["knative_domain"]["domain"])' "$CONFIG")
BASE=http://127.0.0.1:8080

curl --fail-with-body --silent --show-error --max-time 180 \
  -D .local/results/standard.headers -H "Host: $STANDARD_HOST" "$BASE/get" \
  -o .local/results/standard.body
cat .local/results/standard.body
# 期待: HTTP 200 / JSON の headers.Host が STANDARD_HOST

curl --fail-with-body --silent --show-error --max-time 180 \
  -D .local/results/knative.headers -H "Host: $KNATIVE_HOST" "$BASE/get" \
  -o .local/results/knative.body
cat .local/results/knative.body
# 期待: HTTP 200 / JSON の headers.Host が KNATIVE_HOST

k -n test get ksvc hello -o yaml > .local/results/knative-service.yaml
k -n kong-system logs deployment/kong -c kong --tail=100 > .local/results/kong.log
```

HTTP が両方200で、JSON の `headers.Host` がそれぞれの公開ホストと一致することを確認する。
Knative の `status.url` は `http://$KNATIVE_HOST` と一致すること。
Kong の access log に両リクエストが記録される。
Kong に送る Host と、Kourier が期待する Knative URL の Host は同一にする。
NodePort を選んだ場合のみ `BASE=http://VMのIP:設定したNodePort` も使用できる。

### 手動でスケールゼロからの復帰を確認する

Knative への curl・監視アクセスを止める。Knative Pod 自体には port-forward しない。
window は30秒だが、autoscaler 全体の猶予期間もあるため数分待つ場合がある。

```bash
# 約10分を上限に実際の Pod 消滅を待つ。
zero=false
for ((i=0; i<120; i++)); do
  count=$(k -n test get pods \
    -l serving.knative.dev/service=hello -o name | wc -l)
  if [ "$count" -eq 0 ]; then
    zero=true
    break
  fi
  sleep 5
done
if [ "$zero" != true ]; then
  echo 'スケールゼロ未達。復帰確認には進まない。' >&2
  exit 1
fi
date -u +%FT%TZ > .local/results/zero-at.txt
k -n test get pods -l serving.knative.dev/service=hello \
  -o json > .local/results/zero-pods.json
k -n test get deployments -l serving.knative.dev/service=hello \
  -o yaml > .local/results/zero-deployments.yaml

# 同じ Kong 入口にリクエストして復帰させる。
curl --fail-with-body --silent --show-error --max-time 180 \
  -D .local/results/cold-start.headers -H "Host: $KNATIVE_HOST" "$BASE/get" \
  -o .local/results/cold-start.body
cat .local/results/cold-start.body
# 期待: HTTP 200 / JSON の headers.Host が KNATIVE_HOST
k -n test get pods -l serving.knative.dev/service=hello \
  -o json > .local/results/recovered-pods.json
date -u +%FT%TZ > .local/results/recovered-at.txt
```

ゼロ時点の Pod が0件、Revision Deployment の replicas が0、復帰後が200で
本文が go-httpbin JSON の Knative 側 Host、Pod が再生成されていることを確認する。
ゼロになる前のリクエストは復帰確認の成功に数えない。

## 問題の切り分け

webhook のタイムアウト時は [Knative webhook の状態確認](#knative-webhook-の状態確認)へ進む。

```bash
k -n kong-system get pods,svc
k -n kong-system logs deployment/kong -c kong --tail=100
k -n test get ksvc,revision,deploy,pod,svc
k -n test describe ksvc hello
k -n test get events --sort-by=.lastTimestamp
k -n knative-serving get pods
k -n kourier-system get pods,svc
k -n knative-serving get cm config-network config-domain config-autoscaler -o yaml
```

- 未公開 Host の421: Kong Ingress、Knative URL、domain-template、Host 保持を確認。
- アプリの404: 転送先のパスとアプリログを確認。
- Kong の502/503: Kourier の DNS 名・80番 listener、CoreDNS、NetworkPolicy を確認。
- Knative が未 Ready: image pull、webhook、Kourier、メモリ不足を確認。
- ゼロにならない: 定期アクセス、グローバルな scale-to-zero 無効化、autoscaler を確認。
  既存クラスタの共通設定は自動変更しない。

## 結果記録用テンプレート

実測値を埋めて Issue に記録する。未実施は未実施のまま残す。

| 項目 | 状態 | 証跡 |
| --- | --- | --- |
| VM OS / CPU / メモリ | 未記録 | |
| インストールと Ready | 未実施 | |
| Kong → 通常 Service | 未実施 | status / body / Kong log |
| Kong → Kourier → Knative | 未実施 | status / body / URL |
| スケールゼロ | 未実施 | 時刻 / Pod 0 / replicas 0 |
| ゼロから同じ入口で復帰 | 未実施 | status / body / Pod / 時刻 |
| 検証リソースの撤去 | 未実施 | |

設定、バージョン、実行コミットも添付する。
既存クラスタの証跡に機密情報が含まれないか確認してから共有する。

大容量転送・逐次レスポンスは [ストリーミング検証](streaming.md)を参照。

既定利用の2ホストも [既定ルート設定](route-configuration.md)のURLで `/get` を確認する。
Knative既定のスケールゼロ確認は `ksvc/hello-default` と
`serving.knative.dev/service=hello-default` を使う。
既定ルートを変更した場合は新しいホスト・URLで確認し、明示ルートの公開先が変わらないことも確認する。

## Knative webhook の状態確認

初回構築は、Pod が Ready になった後も webhook の呼び出しがタイムアウトする場合がある。
`up` は追加設定を server-side dry-run で検証し、成功後に適用する。
webhook 通信の一時的な失敗は最大180秒待機する。
これは通信原因そのものの解消を保証するものではなく、期限内に成功しなければ停止する。
設定の不正や権限不足は待機せずエラーとして扱う。

再実行時、上流 Serving マニフェストの webhook 設定と証明書 Secret は
既存なら適用対象から除外し、Knative 自身による更新を保持する。
`.webhooks[].rules` の競合を解消するために、上流マニフェストへ
`--force-conflicts` を追加して webhook の内容を初期化する必要はない。

API サーバーから webhook への接続がタイムアウトしたときに、Pod、Service、
EndpointSlice、webhook の直近100行のログ、kube-system の Pod をまとめて表示する。
クラスタ設定やリソースは変更しない。途中で失敗しても残りの確認を続ける。
API リクエストのタイムアウトは30秒。コマンド失敗があればスクリプトは終了コード1を返す。
失敗0件は webhook への通信成功を意味せず、収集した出力から原因を切り分ける。

VM 上で、リポジトリのルートから実行する。

```bash
git pull origin main
bash ./scripts/check-knative-webhook.sh
```

ファイルにも残す場合:

```bash
mkdir -p .local/results
set -o pipefail
bash ./scripts/check-knative-webhook.sh 2>&1 | tee .local/results/webhook-diagnostics.log
```

出力を共有して原因を切り分ける。ログに機密情報がある場合は伏せてから共有する。
