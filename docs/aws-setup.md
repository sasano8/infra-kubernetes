# AWS VM での初回構築

ユーザー指定の `dnf` を使用する環境向け手順。
Python 3.10 以上、curl、CA 証明書、sudo、systemd は事前に必要。
新規の専用 VM を対象とする。動作確認結果はまだ記録していない。

## リポジトリの取得

```bash
sudo dnf install -y git
mkdir projects
cd projects
git clone https://github.com/sasano8/infra-kubernetes.git
cd infra-kubernetes
```

## 設定ファイルの配置

対話スクリプトは使わず、リダイレクトで配置する。
以下は既存のローカル設定ファイルがあれば上書きする。

```bash
cat > configs/environment/local.json <<'EOF'
{
  "mode": "new-host",
  "domain": "knative.tmp.ys-method-dev.com",
  "standard_host": "standard.tmp.ys-method-dev.com",
  "kourier_service": "kourier.kourier-system.svc.cluster.local",
  "node_port": null
}
EOF
```

## 構築

```bash
sudo ./bin/infra up
```

`node_port: null` のため入口は ClusterIP。
[確認手順](verification.md)に沿い、Kong を port-forward して Host 指定で確認する。
この設定での Knative Host は
`sas47-apps--serverless.knative.tmp.ys-method-dev.com`、
通常 Service の Host は `standard.tmp.ys-method-dev.com`。
これらの Host を指定する確認だけなら公開 DNS の変更は不要。
