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
| `sas47-apps/standard` | `standard_domain` | `standard` Service |
| `sas47-apps/serverless` | `sas47-apps-serverless.<knative_domain>` | `kourier-proxy` Service |

`kourier-proxy` は `kourier_service` を指す ExternalName Service。
Ingress とバックエンドを同じ Namespace に置き、既存 Kourier の Service は変更しない。
両 Ingress で Host を保持し、Knative の ingress は Kourier のままにする。

専用 IngressClass は `sas47-kong`。デフォルトの IngressClass には設定しない。
KIC の監視対象 Namespace は `sas47-apps` と `sas47-kong` に限定する。
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
既存の `sas47-kong/kong-proxy` Service 名、HTTP ポート、`node_port` 設定は維持する。
KIC 付き Pod の rollout 成功後、旧 `kong-declarative` ConfigMap を削除する。
ルート設定の変更は KIC が反映するため、以後は ConfigMap のハッシュによる再起動は不要。

CRD は公式 Kong Helm chart 2.47.0 のパッケージから取り出す。
固定 SHA-256 を公式チャート index の digest と照合し、CRD だけを server-side apply する。
Helm CLI・Helm release・PostgreSQL は追加しない。
取得したパッケージは `.local/downloads/` にキャッシュし、再利用時にも digest を確認する。
既存の異なる管理者と CRD のフィールドが競合した場合は強制上書きせず停止する。
IngressClass・ClusterRole・ClusterRoleBinding の同名既存リソースは所有ラベルを確認する。

## ホスト名の区切り変更

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
cert-manager の導入・Issuer・証明書発行・外部80/443番からの到達性は、この KIC 切り替えには含まない。
まだ有効な公開証明書は発行していないため、この変更だけでは信頼される HTTPS は完成しない。

次の段階で、各 Ingress に `spec.tls` と cert-manager の Issuer 注釈を追加する。
TLS Secret は対応する Ingress と同じ `sas47-apps` Namespace に作成する。
KIC がその Secret の証明書と更新を Kong に反映する。
手作業で証明書ファイルを Kong にコピーしたり、Pod を再起動したりする必要はない。
NodePort モードでは443番用の NodePort は Kubernetes が自動採番する。
HTTP-01 を使う場合、単に NodePort を追加するだけでは外部80番への到達性は満たさない。

## 状態確認

```bash
sudo k0s kubectl -n sas47-kong get pods
sudo k0s kubectl -n sas47-kong logs deployment/kong -c ingress-controller --tail=100
sudo k0s kubectl -n sas47-kong logs deployment/kong -c kong --tail=100
sudo k0s kubectl -n sas47-apps get ingress
sudo k0s kubectl get ingressclass sas47-kong
```

KIC 移行後はログ取得時にコンテナ名を指定する。
この移行の VM 上の動作確認は未実施。既存の確認スクリプトで2経路とスケールゼロ復帰を確認する。

## 撤去

`bin/infra remove` は検証用 Namespace と専用 IngressClass・ClusterRole・ClusterRoleBinding を削除する。
共有され得る Kong CRD は残し、Knative・Kourier・他のコントローラは削除しない。
KIC の RBAC は [公式 v3.4.3](https://github.com/Kong/kubernetes-ingress-controller/tree/v3.4.3/config/rbac)
を参照しており、ライセンスは `plugins/kong/LICENSE.kic` に保存している。
