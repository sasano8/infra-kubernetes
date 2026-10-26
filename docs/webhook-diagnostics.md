# Knative webhook の状態確認

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
