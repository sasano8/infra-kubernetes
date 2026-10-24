# SSH 公開鍵の登録

新規ホストは cloud-init、すでにコンソールへ接続できるホストはシェルで公開鍵を登録する。
以下の設定例はドキュメント内で提示し、cloud-init のファイルはリポジトリへ配置しない。

## 1. PC の公開鍵を確認する

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

## 2-A. 新規ホスト：cloud-init で登録する

cloud-init 対応の Linux イメージを使い、プロビジョニング時に以下を渡す。
公開鍵は PC で表示したものに置き換える。

```yaml
#cloud-config
ssh_authorized_keys:
  - ssh-ed25519 AAAA...あなたの公開鍵... user@pc
```

イメージの既定ユーザーに公開鍵を登録する。UTF-8・BOMなし・LFで指定する。
すでに起動済みのホストへ鍵を追加する場合は、次のシェル手順を使う。

## 2-B. 起動済みホスト：シェルで追加する

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

## 3. PC から SSH 接続する

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

SSH 接続後は [README のインストール手順](../../README.md#インストール)へ進む。
