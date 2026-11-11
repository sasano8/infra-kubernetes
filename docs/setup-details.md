# 導入・設定適用

Kong を動かす Linux ホストのリポジトリルートで実行する。Git、make、Python 3.10 以上、
curl、CA 証明書、接続先を設定した kubectl を用意する。k0s を新規導入するホストでは sudo と systemd も使う。

Pod CIDR `10.244.0.0/16`、Service CIDR `10.96.0.0/12` がホストや VPN と重ならないようにする。
ホストの要件は [k0s の要件](https://docs.k0sproject.io/stable/system-requirements/)を参照。

## 新規ホストでの k0s 導入

新しいk0sホストを作る場合は、配備と分けて実行する。

```bash
make install-k0s   # sudoでバイナリ配置・設定・systemd登録・起動
```

`既存 CNI 設定と競合する可能性があります: 87-podman-bridge.conflist`で止まり、
PodmanがNetavarkを使用している場合は、旧CNI設定を退避して再実行する。
Podmanを利用する通常ユーザーで、sudoを付けずに実行する（必要な操作だけ内部でsudoを使用）。

```bash
make backup-cni-podman
make install-k0s
```

スクリプトは通常ユーザーとrootのPodmanバックエンドが両方とも`netavark`であることを確認し、
`/etc/cni/net.d/87-podman-bridge.conflist`だけを
`/etc/cni/reset-backup/87-podman-bridge.conflist`へ退避する。
CNI使用中・確認失敗・退避先が既に存在する場合は変更せず終了する。
退避済みで元ファイルがなければ再実行しても変更しない。他のユーザーがPodmanを使うホストでは、
そのユーザーもNetavarkを使用していることを事前に確認する。

kubectlが既に対象クラスタへ接続できる場合は、その設定を使う。
新規ホストで接続設定を作る場合は、管理者用kubeconfigを作業ユーザーの専用ファイルへ保存する。
既存の`~/.kube/config`は上書きしない。このkubeconfigにはクラスタ管理者権限がある。

```bash
mkdir -p ~/.kube
(
  umask 077
  sudo ./bin/infra host kubeconfig > ~/.kube/infra-kubernetes.yaml
)
export KUBECONFIG="$HOME/.kube/infra-kubernetes.yaml"
kubectl config current-context
kubectl get nodes
```

以降の`make up`・plugin操作・Secret適用・疎通確認はsudoなしで実行する。
`make start-k0s`／`make stop-k0s`／`make status-k0s`はsudoでk0sホストを操作する。
`mode=new-host`はKnativeの導入・共通設定を管理する指定として残るが、`up`からk0sの
インストールやsystemd操作は行わない。

## 既存クラスタに追加

稼働中の Knative／Kourier とその共通設定を流用する場合は `mode=existing` を選ぶ。
k0s が起動済みで、Knative／Kourier も今回導入する専用クラスタでは `mode=new-host` を選ぶ。

設定の `mode` を `existing` にし、`routes.knative_domain.domain` を既存 Knative のドメインに合わせる。
`kourier_service` は **外部向け HTTP listener（80番）を持つ** Kourier Service の
クラスタ内 DNS 名にする。`kourier-internal` には向けない。
Knative の `{{.Name}}-{{.Namespace}}-ksvc.{{.Domain}}` 形式の domain-template、HTTP、スケールゼロが有効な環境を前提にする。
namespace selector や NetworkPolicy の影響も考慮する。



`existing` では k0s・Knative・Kourier の導入や共通設定の変更を行わない。
domain-template が前提と異なれば停止し、自動修正しない。
作成した Knative Service の実際の URL も照合する。
`kubectl config current-context` と `kubectl get nodes` で接続先を確認し、以下の設定と配備に進む。

## 環境設定と配備

初回は `./bin/infra configure` で対話式に `configs/environment/local.json` を作る。
既存設定を使う場合は `configure` を省略し、変更するときは `make edit-config` を使う。
非対話の場合は [example.json](https://github.com/sasano8/infra-kubernetes/blob/main/configs/environment/example.json) をコピーして編集する。この例の TLS と SSO は無効。
`configure` で SSO を無効のままにした場合も、HTTPS で配備するには `tls.enabled`、`tls.email`、`tls.cloudflare_zone` を設定する。

| 設定 | 詳細 |
| --- | --- |
| `routes`、`default_routes`、`base_domain` | [公開経路](route-configuration.md) |
| `tls` | [DNS・証明書・Cloudflare トークン](https.md) |
| `sso` | [Google OAuth クライアント・Secret](sso.md) |
| `plugins` | [追加プラグインと Helm](platform-plugins.md) |
| `upload_limits` | [本文サイズ・タイムアウト・同時数](streaming.md#アップロード制限) |
| `error_service` | [エラー生成サービスの接続仕様](error-service.md) |

公開経路の DNS と [HTTPS の設定](https.md)を済ませる。SSO を有効にする場合は Google 側に callback を登録し、認証情報を配置する。
新規に Google の認証情報を保存する場合、`configure` の SSO Secret JSON ファイルには `.local/secrets/google-sso.json` を入力する。既存 Kubernetes Secret を使う場合は空欄にする。
Argo CD や Infisical を有効にする場合は `make install-helm` で Helm を準備できる。

```bash
make configure-secret-cloudflare-token
make configure-secret-google-sso  # SSO を有効にする場合
./bin/infra validate --require-https
make up  # 設定を適用し、サービスを起動・更新する
```

`make up` と `./bin/infra up --require-https` は同じ一括配備を行う。
事前検証後、Knative／Kourier、Namespace／KIC、証明書、SSO、アプリ、Kong、公開 Ingress の順に反映して配備を記録する。
別設定で実行する場合は `make up CONFIG=...`、別の kubectl は `KUBECTL=...` で指定する。

`./bin/infra render` はクラスタを変更せず、`.local/rendered/` にマニフェストを出力する。
`./bin/infra deploy` は基盤が起動済みの環境へアプリと Kong の設定を再配備する。
`configs/environment/local.json` と `.local/` は Git 管理外。
以前 sudo で実行して `.local` が root 所有になっている場合は作業ユーザーへ所有権を戻す。

配備後は [動作確認](verification.md) へ進む。

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
すべて作業ユーザーの設定済みkubectlを使用し、sudoは付けない。

| コマンド | 対象 |
| --- | --- |
| `apply-namespaces` | 所有確認済みのtest／kong-system Namespaceを用意 |
| `plugin install knative` | Knative Serving／Kourier本体。環境固有の追加設定は別適用 |
| `plugin install cert-manager` | cert-manager本体・CRD。Issuer／Certificateは別適用 |
| `plugin install kong` | KICのCRD・RBAC・コントローラー。Kong本体の設定はapply-kong |
| `plugin install argocd/infisical/external-secrets/pinner` | 指定pluginの本体導入。ESOは個別導入のみ |
| `plugin wait argocd/infisical/external-secrets/pinner` | 指定pluginのDeployment／StatefulSetの待機 |
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
./bin/infra plugin install knative
./bin/infra apply-config-knative
./bin/infra wait-knative
```

## 失敗した工程だけ再実行する

トークンを修正した場合は、Secretを更新して証明書発行を待つ。
Issuer／Certificateの定義を変更していなければ、その再適用は不要。

```bash
./bin/infra apply-secret-cloudflare-token
./bin/infra wait-certificates
```

ドメインなど証明書の設定を変更した場合：

```bash
./bin/infra apply-certificates
./bin/infra wait-certificates
```

アプリの定義を修正した場合：

```bash
./bin/infra apply-apps
./bin/infra wait-apps
```

SSO有効時の初回個別適用は、KIC導入・証明書発行後に `apply-sso` → `wait-sso` →
`apply-apps` → `wait-apps` → `apply-kong` → `wait-kong` → `apply-routes` の順に行う。
KongへマウントしたLuaの版とrollout状態が揃うまでは、SSO付きルートの適用を拒否する。
全工程の個別適用後は `record-inventory` で配備記録を更新する。

SSO有効／無効の切替と旧Kongからの移行は、参照解除・rollout・経路切替の順序を保つため
`up` または `deploy` で一括実行する。個別コマンドはこの切替を拒否する。
個別の `apply-sso` でSSOを無効化することもできない。

## SSH 公開鍵の登録

新規 VM は cloud-init の対応イメージを使い、プロビジョニング時に公開鍵を登録できる。
起動済みホストへ鍵を追加する場合は、既にログイン可能なアカウントを使う。

### 1. PC の公開鍵を確認する

PC のターミナルで実行する。

```bash
cat ~/.ssh/id_ed25519.pub
```

鍵がない場合だけ、次で作成してから公開鍵を表示する。既存の鍵は上書きしない。

```bash
ssh-keygen -t ed25519
cat ~/.ssh/id_ed25519.pub
```

`ssh-ed25519` から始まる公開鍵の1行を使う。
秘密鍵 `~/.ssh/id_ed25519` は PC に置いたままにする。

### 2-A. 新規ホスト：cloud-init で登録する

cloud-init 対応の Linux イメージを使い、プロビジョニング時に以下を渡す。
公開鍵は PC で表示したものに置き換える。

```yaml
#cloud-config
ssh_authorized_keys:
  - ssh-ed25519 AAAA...あなたの公開鍵... user@pc
```

イメージの既定ユーザーに公開鍵を登録する。UTF-8・BOMなし・LFで指定する。
すでに起動済みのホストへ鍵を追加する場合は、次のシェル手順を使う。

### 2-B. 起動済みホスト：シェルで追加する

接続済みのコンソールで、PC から SSH ログインするユーザーとして実行する。

```bash
whoami
```

root や別のユーザーになっている場合は、`sudo -iu ログインユーザー名` で対象ユーザーへ
切り替える。以降の `~` は、そのユーザーのホームディレクトリを指す。

```bash
install -d -m 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

printf '\n' >> ~/.ssh/authorized_keys
cat >> ~/.ssh/authorized_keys <<'EOF'
ssh-ed25519 AAAA...PCの公開鍵... user@pc
EOF
```

公開鍵は PC で表示したものに置き換え、1行のまま貼り付ける。
`>>` で既存の鍵を残して追記する。先頭の改行は、既存ファイルの末尾に改行がない場合に
鍵同士が連結されることを防ぐ。SSH サービスの再起動は不要。

### 3. PC から SSH 接続する

接続元 PC からホストの TCP 22 へ到達できるよう、ファイアウォールの受信ルールを設定する。
IP 制限を使う場合は、接続元 PC のグローバル IP `/32` を許可する。

Linux の PC では、先にポートの疎通を確認できる。

```bash
nc -vz -w 5 サーバーの公開IP 22
```

PC の秘密鍵と、登録した公開鍵の対象ユーザーで接続する。

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ログインユーザー名@サーバーの公開IP
```

`-i` には `.pub` ではなく対応する秘密鍵を指定する。
ユーザー名はイメージの設定に合わせる。例えば `ec2-user` や `ubuntu`。

| エラー | 確認するもの |
| --- | --- |
| Connection timed out | 現在の公開 IP、接続元 IP の受信許可、ネットワーク経路 |
| Connection refused | SSH サービスの起動と22番の待受 |
| Permission denied (publickey) | ユーザー名、鍵の対応、対象ユーザーのファイル所有者・権限 |
