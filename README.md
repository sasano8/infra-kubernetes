# infra-kubernetes

SAS-47: Kong を入口に通常の Kubernetes Service と Knative Service の2経路を
検証する最小構成。Linux ホストに k0s を直接導入する。
Docker は不要で、コンテナ実行には k0s 内蔵 containerd を使う。

```text
HTTP + Host → Kong (DB-less)
                ├─ standard.example.test
                │    └─ Service → Deployment → "standard-service"
                └─ sas47-apps--serverless.example.test
                     └─ Kourier → Knative → "knative-service"
```

Kong の `preserve_host: true` と Knative の
`{{.Namespace}}--{{.Name}}.{{.Domain}}` を使う。
Revision / Activator と連携する Kourier は維持する。
Kong は宣言ファイルを読む単独 Gateway とし、DB・Kong Ingress Controller・
Gateway API CRD・Helm は追加しない。Gateway API 自体の評価は含まない。

## 新規 VM に一括インストール

AWS の `dnf` 環境で使う、設定をリダイレクトで配置する手順は
[AWS VM での初回構築](docs/aws-setup.md)を参照。

対象は **専用の新規 Linux VM（systemd、amd64 / arm64）**。
Ubuntu 24.04 LTS を想定。目安は 4 vCPU / 8 GiB RAM / 30 GiB ディスク。
Python 3.10 以上、curl、CA 証明書、Git、sudo を事前に用意する。
ネットワーク・カーネル条件は [k0s の要件](https://docs.k0sproject.io/stable/system-requirements/)
に従う。Pod CIDR `10.244.0.0/16` / Service CIDR `10.96.0.0/12` が
ホスト・VPN のネットワークと重ならないこと。

```bash
git clone https://github.com/sasano8/infra-kubernetes.git
cd infra-kubernetes
./bin/infra configure
sudo ./bin/infra up
```

非対話の場合は `configs/environment/example.json` を `configs/environment/local.json` にコピーして編集し、
`sudo ./bin/infra up` の1コマンドで構築できる。
`configure` は設定だけを生成し、インストールは行わない。既存ファイルは上書きしない。

`up` は公式 Release のバイナリと SHA-256 リストを照合して k0s を導入し、
systemd サービス化、Knative / Kourier 導入、サンプルと Kong の配備まで行う。
API・Node・Deployment・Knative Service の起動待ちは行うが HTTP 検証は行わない。
起動失敗時は `sudo journalctl -u k0scontroller` を確認する。
自分で作成した k0s だけ再実行を許可し、既存 k0s の採用・更新・reset はしない。
通信などの問題を解消後、同じコマンドで再開できる。

| コンポーネント | 固定バージョン |
| --- | --- |
| k0s | v1.34.3+k0s.0 |
| Knative Serving / Kourier | knative-v1.20.0 |
| Kong | 3.9.1 |
| サンプル | hashicorp/http-echo:1.0.0 |

バージョンは `scripts/infra.py`、`plugins/`、`apps/` で管理する。
上流 Kourier の Envoy タグを含め、全イメージの digest 固定は行っていない。
Release マニフェストは HTTPS で取得し、k0s バイナリは SHA-256 も検証する。
更新時は組み合わせの互換性を確認する。

## 既存クラスタに追加

設定の `mode` を `existing` にし、`domain` を既存 Knative のドメインに合わせる。
`kourier_service` は **外部向け HTTP listener（80番）を持つ** Kourier Service の
クラスタ内 DNS 名にする。`kourier-internal` には向けない。
Knative の上記 domain-template、HTTP、スケールゼロが有効な環境を前提にする。
namespace selector や NetworkPolicy の影響も考慮する。

```bash
# 既存 kubeconfig と現在の context を使う。接続先を確認してから実行する。
kubectl config current-context
./bin/infra up

# k0s 内蔵 kubectl を使う場合
sudo ./bin/infra up --kubectl 'k0s kubectl'
```

`existing` では k0s・Knative・Kourier の導入や共通設定の変更を行わない。
domain-template が前提と異なれば停止し、自動修正しない。
作成した Knative Service の実際の URL も照合する。
既存 Caddy、本番経路、DNS、TLS、認証、Kestra、Argo CD は変更しない。

| 設定項目 | 意味 |
| --- | --- |
| `mode` | `new-host` / `existing` |
| `domain` | Knative ドメイン。新規構築時は config-domain に設定 |
| `standard_host` | 通常 Service の Host |
| `kourier_service` | Kong から接続する Kourier の DNS 名 |
| `node_port` | `null` なら非公開。必要なら 30000〜32767 |

```bash
# クラスタに触れずにマニフェストだけ出力
./bin/infra render

# 起動済みクラスタにサンプルと Kong だけ再配備
sudo ./bin/infra deploy --kubectl 'k0s kubectl'
```

`configs/environment/local.json` と `.local/` は Git 管理外。
生成先は `.local/rendered/{apps,kong}.yaml`。
新規ホスト向け Knative 設定も `.local/rendered/knative-config/` に生成するが、
既存クラスタには適用しない。
Kong の設定変更時は Pod の注釈ハッシュを更新して再起動する。
専用 Namespace `sas47-apps` / `sas47-kong` を作成する。
同名で所有ラベルのない Namespace があれば停止する。
これらの Namespace に他のワークロードを追加しないこと。

## 公開と確認

デフォルトは ClusterIP で外部へポートを開かない。Kong Admin API は無効。
Caddy の80/443番とは競合させない。VM のターミナルで以下を実行する。

```bash
sudo k0s kubectl -n sas47-kong port-forward service/kong-proxy 8080:80
```

別ターミナルから curl を送る。PC から使う場合は SSH トンネルで
VM の `127.0.0.1:8080` を転送する。
NodePort を選ぶ場合は VM のファイアウォールで検証元だけを許可する。
公開 DNS や本番 TLS は不要で、Host ヘッダーで振り分ける。

### 1コマンドで動作確認

VM 上のリポジトリのルートで実行する。Python 標準ライブラリのみを使用する。

```bash
sudo python3 scripts/check-routes.py
```

設定ファイルを読み、自動で Kong の port-forward を起動して、次の3項目を確認する。

1. **通常 Service の経路**: Kong → Kubernetes Service → Deployment にアクセスし、
   HTTP 200 と本文 `standard-service` を確認する。
2. **Knative の経路**: Kong → Kourier → Knative Service にアクセスし、
   HTTP 200 と本文 `knative-service` を確認する。
3. **スケールゼロからの復帰**: Knative へのアクセスを止め、検証用 Knative Service の
   Pod が0件になるまで待つ。その後、同じ Kong 入口に再アクセスし、
   HTTP 200・本文 `knative-service`・Ready Pod の再生成を確認する。

3では次のように表示する。5秒ごとに Pod 数を取得し、最大600秒待機する。
Terminating / Error の Pod も削除されて0件になるまで待つ。

```text
[scale-zero] Pod がゼロ件になるまで待機します（最大 600 秒）。Knative への別のアクセスも停止してください。
```

待機中は別ターミナルや監視からも検証用 Knative Service にアクセスしないこと。
0件になる前には復帰リクエストを送らず、期限内に0件にならなければ失敗とする。
すべて成功すると `3/3 成功` と表示し、終了コード0を返す。
途中の失敗やスキップがあれば終了コード1を返す。
終了時には自身が起動した port-forward を停止する。

待機時間を延ばす場合:

```bash
sudo python3 scripts/check-routes.py --scale-zero-timeout 900
```

ユーザーの AWS VM で、2経路の HTTP 200・期待する本文を確認済み。
手動で Pod が0件になった後の再アクセスも HTTP 200 が報告されている。
スケールゼロ復帰を追加した版のスクリプトは、VM 上での実行結果待ち。
[HTTP・スケールゼロ復帰・結果記録](docs/verification.md)と
[撤去手順](docs/cleanup.md)を参照。

## 管理構成と移植

```text
bin/infra                       # 操作入口
configs/environment/            # 環境ごとの入力例とローカル設定
configs/kube/k0s.yaml            # ホスト上の k0s 設定
plugins/knative/config/          # Knative・Kourier の追加設定
plugins/kong/manifests/          # Kong 宣言設定と Gateway
apps/sas47/manifests/            # 検証用アプリ
scripts/infra.py                # 設定生成・構築・配備・撤去
docs/                           # 確認・撤去・移植方針
```

`ys-method-dev/infra-k0s` を参照し、`configs/`・`plugins/`・`apps/` の区分、
k0s の kube-router 設定、Knative 追加設定、CRD の server-side apply、
webhook の Endpoints 待機、ホスト上の既存 CNI 設定の検出を取り込んだ。
詳細は [参照元との対応](docs/migration.md)を参照。
Docker、Argo CD、Vault などは最小検証に含めない。

公式資料:

- [k0s インストール](https://github.com/k0sproject/k0s/blob/main/docs/install.md)
- [Kourier](https://github.com/knative-extensions/net-kourier)
- [Kong DB-less](https://developer.konghq.com/gateway/db-less-mode/)
- [Kong Route / preserve_host](https://developer.konghq.com/gateway/entities/route/)
