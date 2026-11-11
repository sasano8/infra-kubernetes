# 移行・旧リソース整理・撤去

| 作業 | コマンド |
| --- | --- |
| 更新後の削除候補を確認する | `python3 scripts/prune-resources.py plan` |
| 確認済みの旧リソースを削除する | `python3 scripts/prune-resources.py apply` |
| このプロジェクトの配備を撤去する | `./bin/infra remove` |
| 専用ホストの k0s クラスタ全体を破棄する | `make reset-k0s` |

## Git 履歴と配備記録から旧リソースを整理する

Git でファイルが削除されても、Kubernetes のリソースは自動削除されない。
このスクリプトは「過去に定義があり、現在の定義から消え、実際のクラスタにも残るもの」を
削除候補として管理する。名前変更や Namespace 移動にも対応する。

### 使い方

新構成を配備し、ローカルの経路確認と外部 HTTPS の確認を済ませる。
`up` / `deploy` 成功時には、コミット・クラスタ UID・管理対象の識別情報を
`.local/resource-inventory/deployments/` に自動記録する。

```bash
git pull origin main
./bin/infra up --require-https
python3 scripts/check-routes.py
```

まず削除計画を作成する。このコマンドはクラスタを変更しない。

```bash
python3 scripts/prune-resources.py plan
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
python3 scripts/prune-resources.py apply
```

`apply` は実際に削除するコマンド。現在の定義、クラスタ UID、各リソースの UID・所有情報・
宣言的な参照を再確認し、新構成の Deployment / Knative Service と、有効なら証明書が
Ready でない場合は止まる。結果は `.local/resource-inventory/prune-result.json` に保存する。
外部 HTTPS の成功は自動判定しないので、実行前に利用者が確認する。

### 初回・古い環境にも使える

過去の配備記録がない場合も、Git の全祖先履歴から旧リソース名を復元する。
実際には配備していなかったバージョンも含まれるが、クラスタに存在しないものは候補から除く。
履歴内の Python やシェルを実行せず、マニフェストの識別情報と Python の静的な定義を読む。
現在のリポジトリでは、旧 Namespace、Kong、アプリ、RBAC、Issuer、生成 Secret の名前変更を復元できる。
古い設定ファイルやトークンを復元する必要はない。

開始コミットを限定したい場合:

```bash
python3 scripts/prune-resources.py plan --from-ref <開始コミット>
python3 scripts/prune-resources.py apply
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

### 削除対象の範囲

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

## Kong・KIC と TLS リソースの移行

起動済みの環境では以下を実行する。

```bash
git pull origin main
./bin/infra deploy
python3 scripts/check-routes.py
```

Kong の設定が ConfigMap 管理から Ingress 管理へ切り替わり、Pod は更新される。
単一 Replica の検証環境のため、移行中に一時的な通信断が生じる場合がある。
新しい入口は `kong-system/kong-proxy`。HTTP ポートと `node_port` 設定は維持する。
KIC 付き Pod の rollout 成功後、旧 `kong-declarative` ConfigMap を削除する。
ルート設定の変更は KIC が反映するため、以後は ConfigMap のハッシュによる再起動は不要。

CRD は公式 Kong Helm chart 2.47.0 のパッケージから取り出す。
固定 SHA-256 を公式チャート index の digest と照合し、CRD だけを server-side apply する。
Helm CLI・Helm release・PostgreSQL は追加しない。
取得したパッケージは `.local/downloads/` にキャッシュし、再利用時にも digest を確認する。
既存の異なる管理者と CRD のフィールドが競合した場合は強制上書きせず停止する。
IngressClass・ClusterRole・ClusterRoleBinding の同名既存リソースは所有ラベルを確認する。

| 用途 | 名前 |
| --- | --- |
| Kong Namespace | `kong-system` |
| IngressClass / ServiceAccount / ClusterRole / ClusterRoleBinding | `kong-ingress` |
| Cloudflare API Secret（Namespace: test） | `cloudflare-api-token` |
| DNS-01 Issuer（Namespace: test） | `cloudflare-letsencrypt` |
| ACME アカウント Secret（Namespace: test） | `letsencrypt-account` |
| 所有ラベル値 | `infra-kubernetes` |

既存環境は同じ `up` で移行する。旧 Secret は所有ラベル確認後にコピーし、値は表示しない。
トークンファイルがある場合は、新しい Secret にその値を登録する。
所有確認済みの旧 Issuer が参照する ACME アカウントキーも引き継ぐ。
既存 Certificate の参照更新では、所有確認済みの対象に限り旧 field-manager から管理を移す。
Issuer 名の変更により証明書が再発行される場合がある。

旧 Kong は証明書の準備とアプリの Ready 確認後に0 Replicaへ変更し、
Pod の消滅を待ってから新 Namespace の Kong を起動する。
この切り替え中は一時的な通信断がある。旧 Namespace 自体や旧 Secret は自動削除しない。
移行後は `python3 scripts/check-routes.py` と外部 HTTPS の両方を確認する。
旧リソースの名前は移行用の検出処理だけに残し、新規リソースには使用しない。

Knative の旧 field-manager はリソース名ではなく Kubernetes の内部管理 ID。
既存 Deployment にその管理履歴があれば同じ ID を引き継ぎ、フィールドの競合を避ける。
新規環境では `knative-upstream` を使う。既存環境で旧 ID がログに出ても、
旧名の Secret や Namespace を新しく作るわけではない。
他の管理者との競合を一括 `--force-conflicts` で上書きしない。

## 旧ホスト名・Namespace からの移行

旧 Namespace（`sas47-apps` / `hello-apps`）は自動削除せず、旧リソースを残す。
新しい経路が動作することを確認し、旧 Namespace に残すべきリソースがないと確認できたら
管理者が旧 Namespace を削除する。新しい `remove` は旧 Namespace を対象にしない。
既存の所有ラベルのない `test` Namespace があれば、上書きせず停止する。
TLS 有効時は新 Namespace に Issuer・証明書・トークン Secret を作り直す。
旧 Secret を自動コピーしないため、トークンファイルを削除済みなら再配置が必要。
新 Namespace で証明書を再発行する場合は ACME の発行制限に注意する。

Knative の Namespace とサービス名の区切りを `--` から `-` に変更した。
新規構築、Kong Ingress、確認スクリプトは同じ新形式を使用する。
本リポジトリで作成した専用 VM は `./bin/infra up` で設定を更新する。
URL の反映と Ready を最大180秒待ってから、新しい Ingress を反映する。
Cloudflare の公開ホスト名も新しい名前へ変更する。

`mode=existing` では共通の domain-template を自動変更しない。
共有クラスタでは他の Knative Service の URL にも影響するため、管理者が移行範囲を確認する。
また、一般には Namespace とサービス名の組み合わせがハイフン1つだと衝突する場合がある。
この検証の固定名では衝突しないが、複数サービスを追加するときは命名規則を確認する。

## プロジェクトの配備を撤去する

port-forward を Ctrl-C で終了し、接続先を確認してから実行する。

```bash
# 新規 k0s
./bin/infra remove

# 既存クラスタ
kubectl config current-context
./bin/infra remove
```

対象は所有ラベル `infra.sasano8.dev/owner=infra-kubernetes` が付いた
`test` / `kong-system` の2 Namespace と、その配下の全リソース。
両方のラベルを確認してから削除する。所有ラベルがなければ停止する。
この Namespace に検証以外のワークロードを置かないこと。

専用 IngressClass・ClusterRole・ClusterRoleBinding（各 `kong-ingress`）も所有ラベルを確認して削除する。
Caddy、Knative、Kourier、共通設定、共有され得る Kong CRD、cert-manager 本体・CRD、k0s は削除しない。
新規ホストで作った Knative / Kourier も残る。
再配備には同じ設定で `deploy` または `up` を実行する。
生成設定と `.local/` の証跡は調査用に残す。

TLS 有効時は `test` 内の Issuer・Certificate・Cloudflare トークン Secret も削除する。
TXT レコードの後片付け中は cert-manager を起動しておく。
共有する `knative-serving/config-domain` は `remove` の対象外。
SSO だけを無効にする場合は [SSO の配備・無効化](sso.md) を参照。

## 専用ホストの k0s クラスタを破棄する

必要なアプリ定義・Secret・永続データをバックアップする。
**`make reset-k0s` はホスト上の k0s クラスタ全体を破棄する。共有クラスタには使わない。**

```bash
make reset-k0s
sudo reboot
```

リセット成功後、導入記録と k0s バイナリ、本プロジェクトと内容が一致する k0s 設定を削除する。
ホストを再構築する場合は [新規ホストでの k0s 導入](setup-details.md) に戻る。停止と再開だけなら
`make stop-k0s` と `make start-k0s` を使う。
