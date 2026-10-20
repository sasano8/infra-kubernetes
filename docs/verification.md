# SAS-47 手動確認

VM で実施する手順。ユーザーの AWS VM で2経路の HTTP 200・期待する本文を確認済み。
手動で Pod が0件になった後の再アクセスも HTTP 200 が報告されている。
スケールゼロ復帰を追加した版のスクリプトは、VM 上での実行結果待ち。
インストール時の Ready 待ちだけでは2経路やスケールゼロ復帰の検証にならない。

新規 k0s とデフォルト設定を例にする。既存クラスタでは関数 `k()` の中身を
`kubectl "$@"` に置き換える。Bash、curl、Python 3 を使用する。

## 入口と通常リクエスト

### 1コマンドで2経路とスケールゼロ復帰を確認する

リポジトリのルートで次を実行する。Python 標準ライブラリのみを使用する。

```bash
git pull origin main
sudo python3 scripts/check-routes.py
```

`configs/environment/local.json` のドメイン設定を読み、Kong の port-forward を
127.0.0.1:18080 で起動する。ログは別スレッドで読み取り、転送開始を待ってから
両経路の HTTP ステータスと本文を検証する。
両方が200で、本文がそれぞれ `standard-service` / `knative-service` なら成功。
どちらかが失敗したら終了コード1。終了・失敗・Ctrl-C 時には自身が起動した
port-forward を停止する。既存の8080番の port-forward は停止しない。
続いて「Pod がゼロ件になるまで待機します」と表示し、5秒間隔で Pod 数を取得する。
Terminating / Error の Pod も残っている間はゼロと扱わない。
待機中は Knative に HTTP リクエストを送らず、Pod が0件になった後、同じ Kong 入口へ
リクエストして HTTP 200・`knative-service` と Ready Pod の再生成を確認する。
待機時間の目安は最後のアクセスから通常1〜2分程度。
autoscaling window の30秒に加え、スケールゼロの猶予期間や Pod の終了処理があるため前後する。
すべて成功すれば `3/3 成功`。待機上限は既定600秒で、API エラーや期限切れは失敗とする。
Knative の初回リクエストが失敗した場合、復帰確認はスキップし終了コード1を返す。
実行中は別ターミナルや監視から Knative にアクセスしないこと。

```bash
# ポート変更、または通常の kubectl を使う場合
sudo python3 scripts/check-routes.py --port 18081
python3 scripts/check-routes.py --kubectl kubectl

# ゼロになるまで最大15分待機する場合
sudo python3 scripts/check-routes.py --scale-zero-timeout 900

# 証跡を残す場合
mkdir -p .local/results
set -o pipefail
sudo python3 scripts/check-routes.py 2>&1 | tee .local/results/routes.log
```

### 手動で確認する場合

VM のターミナル A:

```bash
sudo k0s kubectl -n sas47-kong port-forward service/kong-proxy 8080:80
```

VM のターミナル B（リポジトリのルート）:

```bash
set -euo pipefail
k() { sudo k0s kubectl "$@"; }
mkdir -p .local/results
date -u +%FT%TZ > .local/results/started-at.txt
CONFIG=configs/environment/local.json
STANDARD_HOST=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c.get("standard_domain", c.get("standard_host")))' "$CONFIG")
KNATIVE_HOST=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print("hello-apps-serverless." + c.get("knative_domain", c.get("domain")))' "$CONFIG")
BASE=http://127.0.0.1:8080

curl --fail-with-body --silent --show-error --max-time 180 \
  -D .local/results/standard.headers -H "Host: $STANDARD_HOST" "$BASE/" \
  -o .local/results/standard.body
cat .local/results/standard.body
# 期待: HTTP 200 / standard-service

curl --fail-with-body --silent --show-error --max-time 180 \
  -D .local/results/knative.headers -H "Host: $KNATIVE_HOST" "$BASE/" \
  -o .local/results/knative.body
cat .local/results/knative.body
# 期待: HTTP 200 / knative-service

k -n hello-apps get ksvc serverless -o yaml > .local/results/knative-service.yaml
k -n sas47-kong logs deployment/kong -c kong --tail=100 > .local/results/kong.log
```

HTTP が両方200で、本文が別々の期待値であることを確認する。
Knative の `status.url` は `http://$KNATIVE_HOST` と一致すること。
Kong の access log に両リクエストが記録される。
Kong に送る Host と、Kourier が期待する Knative URL の Host は同一にする。
NodePort を選んだ場合のみ `BASE=http://VMのIP:設定したNodePort` も使用できる。

## スケールゼロからの復帰

Knative への curl・監視アクセスを止める。Knative Pod 自体には port-forward しない。
window は30秒だが、autoscaler 全体の猶予期間もあるため数分待つ場合がある。

```bash
# 約10分を上限に実際の Pod 消滅を待つ。
zero=false
for ((i=0; i<120; i++)); do
  count=$(k -n hello-apps get pods \
    -l serving.knative.dev/service=serverless -o name | wc -l)
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
k -n hello-apps get pods -l serving.knative.dev/service=serverless \
  -o json > .local/results/zero-pods.json
k -n hello-apps get deployments -l serving.knative.dev/service=serverless \
  -o yaml > .local/results/zero-deployments.yaml

# 同じ Kong 入口にリクエストして復帰させる。
curl --fail-with-body --silent --show-error --max-time 180 \
  -D .local/results/cold-start.headers -H "Host: $KNATIVE_HOST" "$BASE/" \
  -o .local/results/cold-start.body
cat .local/results/cold-start.body
# 期待: HTTP 200 / knative-service
k -n hello-apps get pods -l serving.knative.dev/service=serverless \
  -o json > .local/results/recovered-pods.json
date -u +%FT%TZ > .local/results/recovered-at.txt
```

ゼロ時点の Pod が0件、Revision Deployment の replicas が0、復帰後が200で
本文が `knative-service`、Pod が再生成されていることを確認する。
ゼロになる前のリクエストは復帰確認の成功に数えない。

## 問題の切り分け

webhook のタイムアウト時は [確認スクリプト](webhook-diagnostics.md)で状態をまとめて取得できる。

```bash
k -n sas47-kong get pods,svc
k -n sas47-kong logs deployment/kong -c kong --tail=100
k -n hello-apps get ksvc,revision,deploy,pod,svc
k -n hello-apps describe ksvc serverless
k -n hello-apps get events --sort-by=.lastTimestamp
k -n knative-serving get pods
k -n kourier-system get pods,svc
k -n knative-serving get cm config-network config-domain config-autoscaler -o yaml
```

- Kong の404: Host とルートの一致を確認。
- Kourier の404: Knative URL、domain-template、Host 保持を確認。
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
