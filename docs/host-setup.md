# Linux ホストでの初回構築

プロビジョニング時は、[README の cloud-init 設定例](../README.md#ホストのプロビジョニング)を使う。
cloud-init 対応イメージの既定ユーザーに SSH 公開鍵を登録する。
公開鍵の準備と、コンソールからシェルで鍵を追加する方法は
[SSH 公開鍵の登録](provisioning/aws-setup.md)を参照。

初回構築の手順は [README](../README.md#インストール) にまとめている。
k0sホストは`make install-k0s`でsudo付き導入する。作業ユーザーのkubectl接続を設定した後、
設定ファイルを配置して`make up`をsudoなしで実行する。

## 停止・撤去・再構築

```bash
make stop-k0s     # 停止だけ。クラスタを保持
make start-k0s    # 再開
```

旧infra-k0sのホスト直接導入版も、次のコマンドで撤去できる。
**クラスタ全体の状態を削除するため、必要な定義・Secret・永続データは先にバックアップする。**

```bash
make reset-k0s
sudo reboot
```

sudoで停止→k0s reset→成功後の本体バイナリ・導入記録削除を行う。再起動は別操作。
旧リポジトリやユーザーのkubeconfig、クラスタ外データは残す。
再構築時は`make install-k0s`とユーザー用kubeconfigの再取得を行う。

構築後はリポジトリのルートで実行する。

```bash
python3 scripts/check-routes.py
```

[構築の詳細](setup-details.md) / [動作確認の詳細](verification.md)
