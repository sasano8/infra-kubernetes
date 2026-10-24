# infra-k0s からの取り込み

参照元: `https://gitlab.com/ys-method-dev/infra-k0s`
参照コミット: `933b17aba3dc8ded3cbc8cad2199890d89b3be1b`

クローン済みのファイルを読んで構成を比較した。参照元は変更していない。
今回のリポジトリは SAS-47 の最小検証を対象とし、全機能の移植ではない。

| 参照元 | 今回の構成・扱い |
| --- | --- |
| `configs/kube/k0s.yaml` | 同じ場所にホスト用設定。kube-router の metricsPort=8081 を維持 |
| `plugins/knative/config/` | 同じ場所に network / domain / Kourier Service 設定 |
| `plugins/kong/` | 同じ場所に DB-less Kong の実装を追加 |
| `apps/tutorial/` | `apps/hello/` に用途を限定した2つのサンプル |
| `installer/local_k0s/` | `scripts/infra.py` のホスト構築処理に集約 |
| `.env` / `installer/kvtool.sh` | JSON 設定に置換。設定を shell として実行しない |
| `scripts/plugin-install.sh` | 汎用の全プラグイン導入ではなく、必要な構成だけ順序を固定して配備 |
| `bootstrap/` の順次実行 | `bin/infra up` で明示的な依存順に構築 |
| Docker / Compose / WSL ネットワーク | ホスト VM に不要なため取り込まない |
| Argo CD / Infisical / Pinner | 追加pluginとして導入可能。有効化時のみupで順次導入。同期定義は別管理 |
| External Secrets | 任意pluginとして個別導入のみ。upには含めない |
| Image Updater / Vault | 未移植。自動導入しない |

## 維持する仕様

追加pluginの管理境界・導入順・検証範囲は [プラットフォームplugin](platform-plugins.md)を参照。

- ホスト上で k0s の単一ノード controller / worker を起動する。
- Kourier を Knative の ingress として維持する。
- 元の domain-template は `{{.Namespace}}--{{.Name}}.{{.Domain}}`。
  現在はアプリ名・Namespace・種類を表す
  `{{.Name}}-{{.Namespace}}-ksvc.{{.Domain}}` に変更している。
- upstream の Knative 配布 YAML と追加設定を分離する。
- 大きな CRD は server-side apply で適用する。
- webhook の Deployment 起動に加え、追加設定の server-side dry-run で
  API サーバーからの webhook 呼び出し成功を確認する。
  初回構築時の通信タイムアウト再現を受け、Endpoints の有無だけの待機から変更した。
- 新規ホストに既存 CNI 設定があれば自動削除せず止まる。

## 今回の変更点

参照元のローカル導入経路には k0s の manifests 自動適用と plugin-install の
直接適用がある。二重管理を避けるため、今回は直接適用の1経路にまとめた。
Knative の追加設定は専用 field-manager で管理し、ドメイン変更時に旧キーを除去する。
設定の所有権を上書きする `--force-conflicts` は、専用新規ホストの Knative
追加設定だけに限定した。上流のリソースには使用しない。
Serving の webhook 設定と `webhook-certs` Secret は、存在しなければ作成し、
再実行時は Knative 自身が管理する内容を保持する。
これにより、自動更新された webhook の rules と上流初期値の競合を避ける。
この再実行処理はバージョンアップ手順ではない。

参照元は Kourier に 30080 / 30443 を設定するが、今回は新規 VM の Kourier を
ClusterIP にし、入口を Kong だけにする。上流 Kourier の LoadBalancer 指定は
新規ホストへの適用前に ClusterIP へ置換する。
既存クラスタの Kourier Service や Caddy 経路は変更しない。
Kong も既定は ClusterIP とし、port-forward で検証する。

参照元の Kong は KIC / Helm の検討段階で実装がない。
当初の静的な DB-less Gateway から、現在は DB-less + KIC の Ingress 管理に変更した。
KIC と Gateway は同じ Pod に置き、CRD は公式 chart の検証済みパッケージから取得する。
Gateway API の検証や GitOps 運用整備は含めていない。詳細は [KIC](kic.md) を参照。

参照元の k0s 1.32 / Serving 1.19 系から、新規構築の固定値は
k0s 1.34 / Serving・Kourier 1.20 系にしている。
これは既存クラスタのアップグレード手順ではない。
`mode=existing` ではバージョン・共通設定を変更しない。

ホスト用 k0s 設定は `/etc/k0s/k0s.yaml` に保存してサービスから参照する。
再実行時にリポジトリの設定と異なれば停止し、設定変更や更新を暗黙に適用しない。
既存の kubeconfig を上書きせず、必要なら k0s 内蔵 kubectl を使用する。

今回の差分についてもテスト・インストール・HTTP 確認は依頼に従い未実施。
VM 上で `docs/verification.md` の結果を記録する必要がある。
