# 撤去

port-forward を Ctrl-C で終了し、接続先を確認してから実行する。

```bash
# 新規 k0s
sudo ./bin/infra remove --kubectl 'k0s kubectl'

# 既存クラスタ
kubectl config current-context
./bin/infra remove
```

対象は所有ラベル `infra.sasano8.dev/owner=sas47` が付いた
`hello-apps` / `sas47-kong` の2 Namespace と、その配下の全リソース。
両方のラベルを確認してから削除する。所有ラベルがなければ停止する。
この Namespace に検証以外のワークロードを置かないこと。

専用 IngressClass・ClusterRole・ClusterRoleBinding（各 `sas47-kong`）も所有ラベルを確認して削除する。
Caddy、Knative、Kourier、共通設定、共有され得る Kong CRD、cert-manager 本体・CRD、k0s は削除しない。
新規ホストで作った Knative / Kourier も残る。
再配備には同じ設定で `deploy` または `up` を実行する。
生成設定と `.local/` の証跡は調査用に残す。

## 専用 VM 全体を廃棄する場合

VM ごと破棄する方法を推奨する。ホストを残してクラスタ全体を削除する場合のみ、
**本作業で作成した専用ホストであることを確認して**公式の手順を実行する。

```bash
# 破壊的操作。既存クラスタでは実行しない。
sudo k0s stop
sudo k0s reset
sudo reboot
```

全ワークロードとクラスタデータが失われる。
再起動後、不要な `/usr/local/bin/k0s` と
`/var/lib/infra-kubernetes/k0s-version` は管理者が削除する。
インストーラや `remove` はこれらを自動実行しない。

TLS 有効時は検証用 Namespace 内の Issuer・Certificate・Cloudflare トークン Secret も削除する。
TXT レコードの後片付けのため、削除中は cert-manager を起動したままにする。
