# go-httpbin による非バッファリング検証

通常 Service (`test/hello-ssvc`) と Knative (`test/hello`・`test/hello-nobuffer`) は
`ghcr.io/mccutchen/go-httpbin:2.21.0` を使用する。既存の名前・URLを維持し、
`/get` が疎通確認、`/headers` が受信ヘッダー確認、`/` がエンドポイント一覧になる。

nobuffer 側の2つの Kong Ingress に `konghq.com/request-buffering: "false"` と
`konghq.com/response-buffering: "false"` を指定する。Kourier への Host 保持も維持する。
buffer 側は両設定を `"true"` にする。[4経路の構成](four-routes.md)を参照。
これはサンプルのルート設定であり、Knative の公開ルート選択を一般アプリの公開へ連携する機能
(SAS-68) は含まない。指定しただけで経路全体の非バッファリングが実証されたとは扱わない。

## 配備と前提

既存環境では設定を確認してから `sudo bin/infra deploy --kubectl 'k0s kubectl'` で再配備する。
[疎通確認](verification.md)の4経路・Host・スケールゼロ復帰を先に実施する。
本文上限は64 MiB、遅延レスポンス上限は60秒、アプリの読み取り期限とKnativeの
リクエスト期限は120秒、Kongの読み書き期限は180秒。
上限を超えるテストは設定を見直してから行う。大容量データをJSONへ展開する
`/post` や `/anything` は使わず、本文を逐次破棄する `/upload` を使う。

以下はリポジトリルートで Bash、curl、Python 3 を使う。
外部公開の確認は別ネットワークの端末で実行する。TLS検証を無効にしない。
SSO導入後は保護方式に従い、Cookie・トークンや認証ヘッダーを証跡に出力しない。

```bash
set -euo pipefail
mkdir -p .local/results
# 自分の通常 nobuffer 公開URLを指定。4経路それぞれで同じ試験を実施する。
BASE=https://hello-test-ssvc.standard-nobuffer.tmp.ys-method-dev.com
curl --fail-with-body --silent --show-error --max-time 180 "$BASE/get"
curl --fail-with-body --silent --show-error --max-time 180 "$BASE/headers"
```

内部の切り分けではKongのport-forwardを使用し、URLを `http://127.0.0.1:8080` に、
curlに `-H 'Host: 対象の公開ホスト'` を追加する。内部結果と外部結果は分けて記録する。
Knativeは先に `/get` で起動させ、コールドスタート時間と転送遅延を分離する。

## 少しずつ返るレスポンス

```bash
curl --fail-with-body --silent --show-error --no-buffer --max-time 30 \
  "$BASE/drip?duration=10&numbytes=20&delay=0" |
  python3 -u -c 'import sys,time
start=time.monotonic()
while True:
    data=sys.stdin.buffer.read1(4096)
    if not data:
        break
    print("%.3fs: %d bytes" % (time.monotonic()-start, len(data)))' |
  tee .local/results/drip-arrivals.log
```

10秒の終了時にまとめて届くのではなく、途中から継続的に到着するかを確認する。
HTTPヘッダーの到着時間だけで判定しない。小さなチャンクがネットワークで合流する
ことはあるため、20回の到着を必須にしない。疑わしければアプリ直結・Kourier経由・
Kong経由を比較し、どの段階で遅延するかを切り分ける。

## 大容量ダウンロード・アップロード

```bash
curl --fail-with-body --silent --show-error --max-time 90 \
  "$BASE/stream-bytes/33554432?chunk_size=16384" \
  -o .local/results/download.bin \
  -w 'status=%{http_code} bytes=%{size_download} first_header=%{time_starttransfer} total=%{time_total}\n'
test "$(wc -c < .local/results/download.bin)" -eq 33554432

# 32 MiBを約32秒で送る。途中で本文をJSONへ展開させない。
dd if=/dev/zero of=.local/results/upload.bin bs=1048576 count=32
curl --fail-with-body --silent --show-error --max-time 90 \
  --limit-rate 1M -H 'Content-Type: application/octet-stream' \
  --data-binary @.local/results/upload.bin "$BASE/upload" \
  -o .local/results/upload.json
python3 -c 'import json; r=json.load(open(".local/results/upload.json")); assert r["bytes_received"] == 33554432; print("upload: 32 MiB received")'
```

ファイルサイズ一致・受信バイト数一致は転送成功の確認であり、非バッファリングの証明ではない。
アップロードの逐次到着は、低速送信中のバックエンド側受信をパケット観測等で確認する。
go-httpbinの完了ログだけでは逐次受信を判定できない。観測していなければ
「転送成功／request非バッファリング未確認」と記録する。
同時にKong・Kourier・queue-proxy・アプリのメモリや一時ディスクも観測する。
S3署名やmultipart uploadの互換性は採用する実ストレージで別途検証する。

## 証跡

通常／Knative、buffer／nobuffer、内部／外部、HTTP成功／逐次到着確認／未実施を分け、実行コミット、
日時、転送量、到着ログと実際のIngress設定を記録する。現時点で本版の実クラスタ検証は未実施。

参考: [go-httpbin v2.21.0](https://github.com/mccutchen/go-httpbin/tree/v2.21.0)、
[Kong annotations](https://developer.konghq.com/kubernetes-ingress-controller/reference/annotations/)

buffer 有効側も同じ試験を行い、到着時刻・転送量・メモリを比較する。
Kongのbuffer有効は必ず全レスポンス完了まで待つという保証ではない。
HTTPバージョンや他のプロキシも記録し、期待した差が出ない場合も実測として残す。

SSO有効時は [SSO手順](sso.md)に従い先にログインする。以下のcurl例は
認証なしで通ることを保証しない。セッションCookieを共有ログへ出さず、認証済みブラウザ等で
実行・観測する。未認証のuploadは401、認証サービス障害は503で転送しない。
