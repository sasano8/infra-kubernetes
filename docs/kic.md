# Kong Ingress Controller

Kong Gateway 3.9.1 と KIC 3.4.3 を同じ Pod に配置する。
Kong は DB-less のまま。KIC は Kubernetes の Ingress・Service・TLS Secret を読み、
Pod 内の `http://127.0.0.1:8001` Admin API に設定を反映する。
Admin API の Service は作成せず、外部インターフェースでも待ち受けない。
API トークンは KIC コンテナだけにマウントする。

## 現在の2経路

`plugins/kong/manifests/routes.yaml.tmpl` に次の Ingress を定義する。

| Ingress | ホスト名 | バックエンド |
| --- | --- | --- |
| `test/standard` | `hello-test-ssvc.<standard_domain>` | `hello-ssvc` Service |
| `test/hello` | `hello-test-ksvc.<knative_domain>` | `kourier-proxy` Service |

`kourier-proxy` は `kourier_service` を指す ExternalName Service。
Ingress とバックエンドを同じ Namespace に置き、既存 Kourier の Service は変更しない。
両 Ingress で Host を保持し、Knative の ingress は Kourier のままにする。

専用 IngressClass は `kong-ingress`。デフォルトの IngressClass には設定しない。
KIC の監視対象 Namespace は `test` と `kong-system` に限定する。
RBAC は公式 KIC v3.4.3 の権限定義を基にしており、ClusterRole を含む。
既存の Caddy・別 IngressClass のルートは採用しない。
Gateway API のコントローラは無効にし、Gateway API CRD は追加しない。

## 導入・既存構成からの移行

新規構築は従来どおり `sudo ./bin/infra up`。
起動済みの環境では以下を実行する。

```bash
git pull origin main
sudo ./bin/infra deploy --kubectl 'k0s kubectl'
sudo python3 scripts/check-routes.py
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

## ホスト名の区切り変更

公開名は `hello-test-ksvc.<knative_domain>` / `hello-test-ssvc.<standard_domain>`。
`ksvc` は Knative Service、`ssvc` は standard service の略。
Knative の domain-template は `{{.Name}}-{{.Namespace}}-ksvc.{{.Domain}}` とする。
通常経路は Kong Ingress に同じ順序のホスト名を明示指定する。
通常 Service 名は `hello-ssvc` とし、Knative が作る内部 Service `hello` との衝突を避ける。
Ingress 自体の名前は `standard` / `hello` を維持し、旧ホストのルートを上書きする。
旧 `standard` Deployment / Service は自動削除しない。不要なら新経路の確認後に撤去する。

アプリ用 Namespace は `test`、Knative Service 名は `hello`。
ホスト名は `hello-test-ksvc.<knative_domain>` となる。
ファイル配置はアプリ名を表す `apps/hello/`。
`git pull origin main` 後に `sudo ./bin/infra up` を実行し、
`sudo python3 scripts/check-routes.py` で新しい Namespace の経路を確認する。
KIC の監視対象・Ingress・証明書の配置・確認スクリプトも新しい名前を使用する。
通常経路の Kubernetes Service / Deployment 名は `hello-ssvc` とし、同じ `test` Namespace に置く。
Cloudflare Tunnel を使っている場合は公開ホスト名も変更する。

旧 Namespace（`sas47-apps` / `hello-apps`）は自動削除せず、旧リソースを残す。
新しい経路が動作することを確認し、旧 Namespace に残すべきリソースがないと確認できたら
管理者が旧 Namespace を削除する。新しい `remove` は旧 Namespace を対象にしない。
既存の所有ラベルのない `test` Namespace があれば、上書きせず停止する。
TLS 有効時は新 Namespace に Issuer・証明書・トークン Secret を作り直す。
旧 Secret を自動コピーしないため、トークンファイルを削除済みなら再配置が必要。
新 Namespace で証明書を再発行する場合は ACME の発行制限に注意する。

Knative の Namespace とサービス名の区切りを `--` から `-` に変更した。
新規構築、Kong Ingress、確認スクリプトは同じ新形式を使用する。
本リポジトリで作成した専用 VM は `sudo ./bin/infra up` で設定を更新する。
URL の反映と Ready を最大180秒待ってから、新しい Ingress を反映する。
Cloudflare の公開ホスト名も新しい名前へ変更する。

`mode=existing` では共通の domain-template を自動変更しない。
共有クラスタでは他の Knative Service の URL にも影響するため、管理者が移行範囲を確認する。
また、一般には Namespace とサービス名の組み合わせがハイフン1つだと衝突する場合がある。
この検証の固定名では衝突しないが、複数サービスを追加するときは命名規則を確認する。

## HTTPS と cert-manager

Kong は TLS listener 8443 と Service 443 を用意する。
`tls.enabled` を有効にすると cert-manager を導入し、Cloudflare DNS-01 で
通常経路用と Knative ワイルドカード用の証明書を自動発行・更新する。
Issuer・Certificate・TLS Secret は `test` に置き、KIC が Kong に反映する。
ホスト443番への公開も設定できる。詳細は [HTTPS](https.md) を参照。

## 状態確認

```bash
sudo k0s kubectl -n kong-system get pods
sudo k0s kubectl -n kong-system logs deployment/kong -c ingress-controller --tail=100
sudo k0s kubectl -n kong-system logs deployment/kong -c kong --tail=100
sudo k0s kubectl -n test get ingress
sudo k0s kubectl get ingressclass kong-ingress
```

KIC 移行後はログ取得時にコンテナ名を指定する。
この移行の VM 上の動作確認は未実施。既存の確認スクリプトで2経路とスケールゼロ復帰を確認する。

## 撤去

`bin/infra remove` は検証用 Namespace と専用 IngressClass・ClusterRole・ClusterRoleBinding を削除する。
共有され得る Kong CRD は残し、Knative・Kourier・他のコントローラは削除しない。
KIC の RBAC は [公式 v3.4.3](https://github.com/Kong/kubernetes-ingress-controller/tree/v3.4.3/config/rbac)
を参照しており、ライセンスは `plugins/kong/LICENSE.kic` に保存している。

## 用途に基づくリソース名

課題番号をリソース名に使わず、以下へ統一した。

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
移行後は `sudo python3 scripts/check-routes.py` と外部 HTTPS の両方を確認する。
旧リソースの名前は移行用の検出処理だけに残し、新規リソースには使用しない。

Knative の旧 field-manager はリソース名ではなく Kubernetes の内部管理 ID。
既存 Deployment にその管理履歴があれば同じ ID を引き継ぎ、フィールドの競合を避ける。
新規環境では `knative-upstream` を使う。既存環境で旧 ID がログに出ても、
旧名の Secret や Namespace を新しく作るわけではない。
他の管理者との競合を一括 `--force-conflicts` で上書きしない。
