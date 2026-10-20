# Git 履歴と配備記録から旧リソースを削除する

Git でファイルが削除されても、Kubernetes のリソースは自動削除されない。
このスクリプトは「過去に定義があり、現在の定義から消え、実際のクラスタにも残るもの」を
削除候補として管理する。名前変更や Namespace 移動にも対応する。

## 使い方

新構成を配備し、ローカルの経路確認と外部 HTTPS の確認を済ませる。
`up` / `deploy` 成功時には、コミット・クラスタ UID・管理対象の識別情報を
`.local/resource-inventory/deployments/` に自動記録する。

```bash
git pull origin main
sudo ./bin/infra up --require-https
sudo python3 scripts/check-routes.py
```

まず削除計画を作成する。このコマンドはクラスタを変更しない。

```bash
sudo python3 scripts/prune-resources.py plan
```

出力と `.local/resource-inventory/prune-plan.json` を確認する。
plan は名前・種類・Namespace・UID・元のコミット・保留理由のみで、Secret の値を含まない。

| 表示 | 扱い |
| --- | --- |
| `delete` | 所有・参照を確認できた削除候補 |
| `delete-if-empty` | 通常リソースの削除後、配下が空なら削除する Namespace |
| `blocked` | 所有不明・参照中・共有リソース等。削除しない |

内容を確認してから、明示的に削除を実行する。

```bash
sudo python3 scripts/prune-resources.py apply
```

`apply` は実際に削除するコマンド。現在の定義、クラスタ UID、各リソースの UID・所有情報・
宣言的な参照を再確認し、新構成の Deployment / Knative Service と、有効なら証明書が
Ready でない場合は止まる。結果は `.local/resource-inventory/prune-result.json` に保存する。
外部 HTTPS の成功は自動判定しないので、実行前に利用者が確認する。

## 初回・古い環境にも使える

過去の配備記録がない場合も、Git の全祖先履歴から旧リソース名を復元する。
実際には配備していなかったバージョンも含まれるが、クラスタに存在しないものは候補から除く。
履歴内の Python やシェルを実行せず、マニフェストの識別情報と Python の静的な定義を読む。
現在のリポジトリでは、旧 Namespace、Kong、アプリ、RBAC、Issuer、生成 Secret の名前変更を復元できる。
古い設定ファイルやトークンを復元する必要はない。

開始コミットを限定したい場合:

```bash
sudo python3 scripts/prune-resources.py plan --from-ref <開始コミット>
sudo python3 scripts/prune-resources.py apply
```

`--from-ref` はそのコミットを含み、HEAD までの範囲を調べる。
開始前に削除された定義はその範囲では復元できないため、通常は省略する。
浅い clone で全履歴を読めない場合は停止する。必要なら `git fetch --unshallow origin` で履歴を取得する。
管理対象に未コミットの変更がある場合は、意図する版をコミットしてから計画する。
Git HEAD または接続先クラスタが変わった場合は plan を作り直す。

通常の kubectl を使う場合:

```bash
python3 scripts/prune-resources.py plan --kubectl kubectl
python3 scripts/prune-resources.py apply --kubectl kubectl
```

## 削除対象の範囲

- 管理対象は `apps/`、`plugins/kong/manifests/`、旧 `manifests/` と、
  `scripts/infra.py` / `scripts/tls.py` にある Namespace・Issuer・Certificate・Secret の定義。
- 今の定義に残るリソースは削除しない。TLS 無効時も、コードに定義が残る証明書類は維持する。
- 所有ラベルは `infra.sasano8.dev/owner=infra-kubernetes`。旧版の所有ラベルも受け入れる。
  個別ラベルのないアプリは、専用 Namespace の所有ラベルで確認する。
- 現役の Ingress・Certificate・Issuer・ワークロード・RBAC 等から参照されるリソースは保留する。
  保留した旧 Issuer が参照する Secret も連鎖して保留する。
- Secret は識別情報とキー名・参照先だけを扱い、plan / 結果ファイルに値を保存しない。
- UID を条件にした Kubernetes API の DELETE を使う。計画後に同名で再作成された別物は削除しない。
  認証は `kubectl proxy` に任せ、トークンや kubeconfig の値を取り出さない。
  proxy はループバックの一時ポートで起動し、終了時に停止する。
- Namespace は配下を API discovery で調べ、既定の ServiceAccount / CA ConfigMap と
  Events 以外が残る場合は削除しない。未知のカスタムリソースや権限不足も削除を止める。
- CRD、PVC、PV、k0s、Knative / Kourier / cert-manager 本体など共有基盤は自動削除しない。
- Kubernetes の `managedFields` の古い管理 ID は、リソースとは別物なので処分対象にしない。

静的に解決できない名前の定義が見つかった場合は、推測せず停止する。
アプリ内部の DNS 文字列や外部利用者までは参照検査できないため、候補の一覧を確認してから実行する。
コントローラが作った子リソースは親の削除による GC に任せる。
削除の完了待ちがタイムアウトしたら finalizer / コントローラの状態を確認し、強制削除はしない。

スクリプトのローカル検証と、EC2 上での実際の削除は別。実クラスタでの削除はまだ実施していない。
