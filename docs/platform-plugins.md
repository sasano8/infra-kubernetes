# Argo CD・Infisical・任意ESO・pinnerの導入

このリポジトリの責務は、各コントローラーを導入して使用可能にするところまで。
Argo CDのApplication／AppProject／Git接続情報、Infisicalの接続先・認証・同期定義、
ESOのSecretStore／ExternalSecret、アプリのマニフェストは別リポジトリ等で管理する。
既存のKong・Knative・SSO・SecretをArgo CDへ自動移管しない。

導入順は **Argo CD → Infisical → pinner**。ESOは任意pluginとして保持し、
**`up`／`deploy`では常に除外する**。Infisicalやpinnerの前提にしない。

## 本体導入と待機

通常の環境設定 `configs/environment/local.json` を使う。
Argo CD・Infisical・ESOには、sudoで実行する場合もPATHから利用できるHelm **3.13以降の3系**が必要。
[Helm公式の導入手順](https://helm.sh/docs/intro/install/)に従って配置する。
ホストでは本体を自動ダウンロードしてroot実行するスクリプトを追加しない。

```bash
helm version --short
sudo helm version --short

sudo ./bin/infra plugin install argocd --kubectl 'k0s kubectl'
sudo ./bin/infra plugin wait argocd --kubectl 'k0s kubectl'

sudo ./bin/infra plugin install infisical --kubectl 'k0s kubectl'
sudo ./bin/infra plugin wait infisical --kubectl 'k0s kubectl'

# 任意。使わなければ実行しない。
sudo ./bin/infra plugin install external-secrets --kubectl 'k0s kubectl'
sudo ./bin/infra plugin wait external-secrets --kubectl 'k0s kubectl'

sudo ./bin/infra plugin install pinner --kubectl 'k0s kubectl'
sudo ./bin/infra plugin wait pinner --kubectl 'k0s kubectl'
```

`install`は本体を適用し、Deployment／StatefulSetのrollout完了は`wait`で確認する。
HelmのCRD作成やインストールhookの完了など、本体導入に必要な処理はinstallに含む。
`wait`はクラスタを変更せず、チャートの取得・Helm実行・Secret取得も行わない。
`install`を明示した場合、以下の`enabled`がfalseでも対象pluginを導入する。

Helmは`--kubectl`が選択したクラスタのkubeconfigを権限600の一時ファイルへ取得して使う。
既存の別クラスタ用`KUBECONFIG`を誤って使わず、終了時に一時ファイルを削除する。
既存Namespace／Helm releaseは所有確認できなければ変更しない。
Helm管理のreleaseラベルで再実行を判定し、手動導入やk0s Helm拡張による既存releaseを引き継がない。
`--force`や`--reuse-values`は使用しない。設定値はリポジトリ内の公開valuesから再現する。

| plugin | 導入先 | 固定する本体 | 公開方式 |
| --- | --- | --- | --- |
| argocd | `argocd` | `argo-cd` chart `10.9.2` | ClusterIP。UIのIngress／NodePortは作らない |
| infisical | `infisical-system` | `secrets-operator` chart `0.11.10` | Operatorのみ。Infisicalサーバーは導入しない |
| external-secrets | `external-secrets` | `external-secrets` chart `2.11.0` | 任意のOperator |
| pinner | `pinner-system` | `plugins.pinner.image` | 内部のcontrollerのみ |

旧 `infra-k0s` の `933b17aba3dc8ded3cbc8cad2199890d89b3be1b` のpluginから
チャート・version・release名を引き継いだ。Argo CDの旧NodePortは引き継がず、
Infisical／ESOはHelmの`--namespace`でRoleBindingやwebhookのnamespaceも揃える。
本体の将来のバージョン更新、特にHelmがupgradeで自動更新しないCRDは別途互換性を確認する。

## upへ組み込む設定

既存JSONへ次の`plugins`を追加する。旧設定ではすべて無効で、勝手に導入しない。
個別導入済みのpluginも、enabledがfalseなら`up`で変更しない。

```json
"plugins": {
  "argocd": {"enabled": true},
  "infisical": {"enabled": true},
  "external-secrets": {},
  "pinner": {
    "enabled": true,
    "namespaces": ["test"],
    "image": "ghcr.io/sasano8/pinner:latest",
    "dry_run": true,
    "interval": "5m",
    "registry_secret": ""
  }
}
```

```bash
sudo ./bin/infra up --require-https
```

既存のKong／サンプル配備後に、有効な標準pluginをArgo CD → Infisical → pinnerの順で導入・待機する。
既定ではpinnerのpull／triggerともdry-runで動き、変更を加えず観測する。
`enabled=false`は自動削除の指示ではない。ESOの`enabled`指定は設定エラーにする。

## Argo CDを使い始める

サーバー上でローカル転送を起動する。

```bash
sudo k0s kubectl -n argocd port-forward --address=127.0.0.1 service/argo-cd-argocd-server 8443:443
```

外部PCからはSSHトンネル等でこのローカルポートへ接続する。FWで8443を公開する必要はない。
Argo CD自身の管理者認証を使用し、初期管理者パスワードはArgo CD公式手順に従って
管理者の端末でのみ確認・変更する。共有ログやGitへ保存しない。
既存アプリ用SSOとは別の認証で、管理UIを無認証で公開しない。

Git接続・Applicationはこの導入後、別管理の構成で登録する。
最初は専用のアプリNamespaceに限定し、手動配備済みの`test`・Kong・Knative本体を
無条件に同期・pruneするApplicationを作らない。

## Infisical／ESOの同期設定

InfisicalはOperatorのみを用意する。Machine Identity等の資格情報、接続先URL、project／environment、
`InfisicalSecret`等のCRは利用側で設定する。導入だけではまだSecretは同期されない。
OperatorのReadyと実際のSecret取得・rotation成功を区別する。

ESOも同様に本体だけを提供し、Store・認証・同期CRは利用側が用意する。
同じSecretをInfisical・ESO・手動apply・Argo CDで同時に管理しない。
Operator生成のSecretをArgo CDで別途作成・pruneしない。

## pinnerとArgo CDの共存

pinnerは指定Namespaceのannotation付きワークロードだけを検出する。
Namespaceは先に用意する。各対象NamespaceへRole／RoleBindingを作り、
クラスタ全体でのpatch・Pod削除は許可しない。Nodeのget/listだけはクラスタ権限を必要とする。
Namespaceごとにpull／triggerのプロセスを分け、対象範囲をCLIでも制限する。
単一replica・Recreateとし、pinner自身には自動更新annotationを付けない。

利用側のDeployment／StatefulSet／DaemonSet／Knative Service本体へ次を設定する。

```yaml
metadata:
  annotations:
    floatingtag-pinner/enabled: "true"
    floatingtag-pinner/on-mismatch: rollout
```

通常ワークロードは可変タグを再取得できるよう`imagePullPolicy: Always`を設定する。
Knativeは新Revisionを作ってタグを解決し直す必要があり、Pod削除で代用しない。
pinnerはイメージ文字列を書き換えず、`spec.template.metadata.annotations`の
`floatingtag-pinner/restartedAt`だけを変更する（field-manager：`floatingtag-pinner`）。

Argo CDのApplicationには [ignoreDifferencesの設定例](../plugins/pinner/argocd-ignore-differences.yaml)を
取り込む。このannotationだけを無視し、`RespectIgnoreDifferences=true`も設定して
selfHealと手動syncの両方で巻き戻さないようにする。既存のignoreDifferences／syncOptionsは保持する。
イメージやmetadata全体を無視せず、通常のGit変更による更新は維持する。
Applicationは別管理のため、このCLIから自動patchしない。

dry-runのログ確認と上記Application設定後、`plugins.pinner.dry_run=false`にして
`plugin install pinner`で再適用する。`plugin wait pinner`はPodの稼働確認であり、
レジストリ照会・タグ更新の反映成功まで保証しない。
タグの更新→検出→rollout／Revision→アプリ応答、無変更時の再起動抑止、Argo CDによる
巻き戻しがないことを、利用側アプリで実測する。

旧構成と同じ`latest`を初期値にしているため、本番では検証済みのタグまたはdigestを`image`へ指定する。
`registry_secret`を指定すると、`pinner-system`の既存docker-registry型Secretの
`.dockerconfigjson`をpullコンテナの`/credential/config.json`へマウントする。
これはdigest照会の認証用で、ノードによるイメージpullの認証は別途必要。
pinnerの短命pull PodへアプリのimagePullSecretsをコピーする実装ではない。

`namespaces`から対象を外して再適用すると、配備記録にある旧RoleBinding／Roleを所有確認して削除する。
対象Namespace・アプリ・Secretそのものは削除しない。

## 管理と検証の範囲

Helm releaseとpinnerのリソースは、従来のサンプル用prune／removeへ自動追加しない。
`remove`は従来どおり所有する`test`／`kong-system`を対象とし、追加pluginのNamespaceは残す。
対象Namespaceの削除・変更時は、別管理のアプリ・Secret・CRとplugin権限も確認する。
Helm本体の撤去・共有CRD削除はこのCLIに追加しない。

自動テストで導入順、ESO除外、所有権、kubeconfig一時ファイル、pinnerのNamespace権限と
権限撤回、待機の非変更性を確認する。CIでは実際の固定チャートをレンダリングし、
Namespace・ServiceAccount参照・ClusterIPを検証する。
実クラスタでの導入、Git同期、Infisical/ESOの実Secret同期、pinnerによる実タグ更新は別途確認が必要。
