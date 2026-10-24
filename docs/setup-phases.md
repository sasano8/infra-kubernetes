# 導入・設定適用・待機の個別実行

## 一括実行

新規ホストの構築は、引き続きリポジトリのルートで実行する。

```bash
sudo ./bin/infra up --require-https
```

設定・資格情報の事前検証と生成、k0s、Knative／Kourier、Namespace／KIC、証明書、
SSO、アプリ、Kong／ルート、配備記録の順に進める。
`up` と個別コマンドは同じPython関数を利用する。
`mode=existing` は k0s／Knative本体・共通設定を自動変更しない。

## 標準導入・設定注入・個別設定

| 層 | 内容 |
| --- | --- |
| 標準導入 | 固定版の本体、CRD、RBAC、コントローラー。`plugin install`で対象を選ぶ |
| 設定注入 | environment JSONから生成したドメイン、ルート、ポート、TLS、SSO等。`apply-*`で対象を反映 |
| 個別設定 | DNS・公開IP・ファイアウォール、トークンの権限・接続元IP、Googleクライアント／callback、アプリSecretを用意 |
| 待機・確認 | `wait-*`で既存リソースのReadyやURLを確認。設定適用・Secret更新・本体導入はしない |

`apply-*`は対象設定を適用し、証明書発行やアプリのReady完了を待たずに終了する。
CRD作成後の確立確認、導入に必要なwebhook起動、設定適用時のwebhook検証・通信再試行は、
APIの前提条件を満たすため導入／適用処理に残す。
`wait-*`はクラスターを変更せず、資格情報ファイルの読込や設定生成も行わない。
各待機には上限があり、証明書はIssuer最大180秒・各Certificate最大600秒。

## 個別コマンド

すべて同じenvironment JSON（既定 `configs/environment/local.json`）を使う。
別ファイルは `--config`、接続先は `--kubectl` で指定する。
既存k0sホストでの各コマンドには `--kubectl 'k0s kubectl'` を付ける。

| コマンド | 対象 |
| --- | --- |
| `apply-namespaces` | 所有確認済みのtest／kong-system Namespaceを用意 |
| `plugin install knative` | Knative Serving／Kourier本体。環境固有の追加設定は別適用 |
| `plugin install cert-manager` | cert-manager本体・CRD。Issuer／Certificateは別適用 |
| `plugin install kong` | KICのCRD・RBAC・コントローラー。Kong本体の設定はapply-kong |
| `apply-config-knative` / `wait-knative` | ドメイン・Ingressクラス・Kourier設定の適用／Deployment待機 |
| `wait-cert-manager` | cert-managerのCRD確立・Deployment待機 |
| `apply-secret-cloudflare-token` | ローカルトークンをtest/cloudflare-api-tokenへ登録 |
| `apply-certificates` / `wait-certificates` | Issuer・Certificateの適用／発行待機 |
| `apply-sso` / `wait-sso` | SSO有効時のSecret・Plugin・認証基盤の適用／認証基盤待機 |
| `apply-apps` / `wait-apps` | アプリの適用／Ready・Knative URL確認 |
| `apply-kong` / `wait-kong` | Kong Deployment・Service設定の適用／rollout待機 |
| `apply-routes` | 公開Ingressの適用 |
| `record-inventory` | 現在の配備リソースをローカルに記録 |

`apply-namespaces`を実行してから、各Namespaceを使う設定を適用する。
初回導入の順序は一括実行と同じ。例えばKnativeを導入し、設定を反映する場合：

```bash
sudo ./bin/infra plugin install knative --kubectl 'k0s kubectl'
sudo ./bin/infra apply-config-knative --kubectl 'k0s kubectl'
sudo ./bin/infra wait-knative --kubectl 'k0s kubectl'
```

## 失敗した工程だけ再実行する

トークンを修正した場合は、Secretを更新して証明書発行を待つ。
Issuer／Certificateの定義を変更していなければ、その再適用は不要。

```bash
sudo ./bin/infra apply-secret-cloudflare-token --kubectl 'k0s kubectl'
sudo ./bin/infra wait-certificates --kubectl 'k0s kubectl'
```

ドメインなど証明書の設定を変更した場合：

```bash
sudo ./bin/infra apply-certificates --kubectl 'k0s kubectl'
sudo ./bin/infra wait-certificates --kubectl 'k0s kubectl'
```

アプリの定義を修正した場合：

```bash
sudo ./bin/infra apply-apps --kubectl 'k0s kubectl'
sudo ./bin/infra wait-apps --kubectl 'k0s kubectl'
```

SSO有効時の初回個別適用は、KIC導入・証明書発行後に `apply-sso` → `wait-sso` →
`apply-apps` → `wait-apps` → `apply-kong` → `wait-kong` → `apply-routes` の順に行う。
KongへマウントしたLuaの版とrollout状態が揃うまでは、SSO付きルートの適用を拒否する。
全工程の個別適用後は `record-inventory` で配備記録を更新する。

SSO有効／無効の切替と旧Kongからの移行は、参照解除・rollout・経路切替の順序を保つため
`up` または `deploy` で一括実行する。個別コマンドはこの切替を拒否する。
個別の `apply-sso` でSSOを無効化することもできない。

個別コマンドは、今回の変更で自動実行を分離したもの。実機での全工程・各再実行経路の検証は未実施。
