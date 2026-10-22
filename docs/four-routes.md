# buffer／nobuffer の4経路

| 種類 | 設定キー | サンプルの公開ホスト | Kong buffering |
| --- | --- | --- | --- |
| 通常 buffer | `standard_domain` | `hello-test-ssvc.<standard_domain>` | request／responseとも有効 |
| 通常 nobuffer | `standard_nobuffer_domain` | `hello-test-ssvc.<standard_nobuffer_domain>` | request／responseとも無効 |
| Knative buffer | `knative_domain` | `hello-test-ksvc.<knative_domain>` | request／responseとも有効 |
| Knative nobuffer | `knative_nobuffer_domain` | `hello-nobuffer-test-ksvc.<knative_nobuffer_domain>` | request／responseとも無効 |

通常の2経路は同じ `hello-ssvc` に転送する。Knativeは同じgo-httpbinイメージを使う
`hello` と `hello-nobuffer` の2サービスを設け、別々にスケールゼロ復帰を検証する。
Kongの既存 `standard`／`hello` Ingressはbuffer有効に戻し、追加する
`standard-nobuffer`／`hello-nobuffer` Ingressは無効とする。全経路でHostを保持する。
現段階のIngressはこのサンプル用の個別ホストであり、任意アプリの自動公開はSAS-68の対象。

## 設定とDNS

4ドメインはすべて必須で、互いに異なる小文字DNS名を指定する。`base_domain`からの
暗黙的な補完はしない。既存local.jsonには次の2項目を明示的に追加する。

```json
{
  "standard_nobuffer_domain": "standard-nobuffer.tmp.ys-method-dev.com",
  "knative_nobuffer_domain": "knative-nobuffer.tmp.ys-method-dev.com"
}
```

これは追加項目だけの抜粋。既存ファイル全体をこの抜粋で置き換えない。
`configs/environment/example.json`には全設定がある。未追加の場合は必須項目エラーで停止する。

DNSでは4経路の実ホスト、または次のワイルドカードをKongホストへ向ける。

- `*.standard.tmp.ys-method-dev.com`
- `*.standard-nobuffer.tmp.ys-method-dev.com`
- `*.knative.tmp.ys-method-dev.com`
- `*.knative-nobuffer.tmp.ys-method-dev.com`

CloudflareはDNS onlyを使用する。HTTPS時は4ドメインとも `tls.cloudflare_zone` 内である必要がある。
各ドメインとワイルドカードを対象に計4つのCertificateを生成し、Readyを待つ。
認証用トークンは既存のSecret供給方式を維持する。

## Knativeのドメイン選択

`config-domain` はラベルなしのRouteに `knative_domain` を、次のServiceラベルがある
Routeに `knative_nobuffer_domain` を割り当てる。

```yaml
metadata:
  labels:
    infra.sasano8.dev/buffering: disabled
```

ラベルはService本体の `metadata.labels` に置く。Revisionの `spec.template.metadata.labels`
だけではドメイン選択にならない。KnativeがServiceラベルをRouteへ引き継ぎ、標準のselectorで
ドメインを選ぶ。Kong側の設定をKnativeへフィードバックしたり、Hostを書き換えたりしない。
ラベルだけでKongのIngressが自動生成されるわけではない（SAS-68とは別）。

## 既存環境への適用

```bash
bin/infra validate --config configs/environment/local.json
bin/infra render --config configs/environment/local.json
```

`mode=new-host` の専用構築環境では、deploy時にも専用field-managerで生成した
`config-domain` を適用する。これにより旧1ドメイン構成からも更新できる。
`mode=existing` の共有クラスタでは、管理者が `.local/rendered/knative-config/domain.yaml` を
既存設定と照合し、必要な2つのドメイン設定を既存の管理方式で整備する。
無関係なドメイン設定を消さない。スクリプトは共有ConfigMapを自動上書きせず、生成値との
一致を事前確認する（selector文字列も生成値と同じ形式にする）。

```bash
sudo bin/infra deploy --kubectl 'k0s kubectl'
sudo python3 scripts/check-routes.py
```

4つのHTTP疎通・Host保持、Knative 2サービスそれぞれのPod 0件→復帰の計6項目が成功すると
`6/6 成功`。各サービスのゼロ待機上限は既定600秒。
外部ネットワークからも4ホストのHTTPS `/get` を確認する。
[ストリーミング検証](streaming.md)を同じパラメータで4経路に実施して比較する。
TLS検証を無効にしない。現在の4経路構成の実クラスタ・外部HTTPS・buffer差の実測は未実施。

追加するKnative Service・Ingress・Certificate・TLS Secretは `test` Namespaceの管理対象。
配備記録とpruneの履歴収集に含まれる。共有CRDや `config-domain` はremoveで削除しない。
