# Linux ホストでの初回構築

プロビジョニング時は、[README の cloud-init 設定例](../README.md#ホストのプロビジョニング)を使う。
cloud-init 対応イメージの既定ユーザーに SSH 公開鍵を登録する。
公開鍵の準備と、コンソールからシェルで鍵を追加する方法は
[SSH 公開鍵の登録](provisioning/aws-setup.md)を参照。

初回構築の手順は [README](../README.md#インストール) にまとめている。
設定ファイルをリダイレクトで配置し、`sudo ./bin/infra up` で構築する。

構築後はリポジトリのルートで実行する。

```bash
sudo python3 scripts/check-routes.py
```

[構築の詳細](setup-details.md) / [動作確認の詳細](verification.md)
