# go-httpbin による非バッファリング検証

通常 Service (`test/hello-ssvc`) と Knative (`test/hello`・`test/hello-nobuffer`) は
`ghcr.io/mccutchen/go-httpbin:2.21.0` を使用する。既存の名前・URLを維持し、
`/get` が疎通確認、`/headers` が受信ヘッダー確認、`/` がエンドポイント一覧になる。

nobuffer 側の2つの Kong Ingress に `konghq.com/request-buffering: "false"` と
`konghq.com/response-buffering: "false"` を指定する。Kourier への Host 保持も維持する。
buffer 側は両設定を `"true"` にする。[4経路の構成](four-routes.md)を参照。
これはサンプルのルート設定であり、Knative の公開ルート選択を一般アプリの公開へ連携する機能
(SAS-68) は含まない。指定しただけで経路全体の非バッファリングが実証されたとは扱わない。

## アプリからレスポンス単位でバッファリングを無効にする

SSE など、特定のレスポンスを逐次配信する場合は、アプリのレスポンスに次のヘッダーを付ける。

```http
X-Accel-Buffering: no
```

Kong の既定構成では、buffer 経路でもこのヘッダーを受け取ったレスポンスのバッファリングを無効にできる。
この設定が作用するのは Kong のレスポンス側で、アップロードなどのリクエスト本文は
Ingress の `konghq.com/request-buffering` で制御する。
逐次配信は、[少しずつ返るレスポンス](#少しずつ返るレスポンス)の手順でデータの到着時刻を確認する。
詳細は [Kong の SSE 設定ガイド](https://developer.konghq.com/support/how-can-i-use-kong-to-allow-sse-server-sent-events/) を参照。

### Knative 経路での Kourier と Kong の動作

Knative のレスポンスは、アプリから Kourier（Envoy）と Kong を経由してクライアントに届く。
Kourier v1.20 の既定構成では、レスポンス全体を蓄積する HTTP フィルターがなく、
レスポンスの完了を待たずにデータを Kong へ順次転送する。
[Kourier v1.20 の Envoy 設定](https://github.com/knative-extensions/net-kourier/blob/knative-v1.20.0/pkg/envoy/api/http_connection_manager.go)

Kong は受け取ったレスポンスを自身のルート設定に従って処理する。
アプリが `X-Accel-Buffering: no` を返すと、Kourier はそのヘッダーを Kong へ転送し、
Kong がそのレスポンスのバッファリングを無効にする。

## 配備と前提

既存環境では設定を確認してから `bin/infra deploy` で再配備する。
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

### S3 のマルチパートアップロードとサイズ上限

boto3 の `upload_file()` などの転送 API は、設定したしきい値に達したファイルを分割して送る。
S3 の `UploadPart` はパートごとに独立した HTTP リクエストなので、Kong を経由する場合、
本文上限・タイムアウト・リクエストバッファリングは各パートに適用される。
たとえば 2 GiB のファイルを 64 MiB ずつ送れば、Kong の本文上限が 105 MiB でも、
各パートはサイズ上限内に収まる。完成するオブジェクトの上限はストレージ側の仕様に従う。
署名付き URL でストレージへ直接送る場合、ファイル本体には Kong の制限がかからない。
[boto3 の転送設定](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3.html)・[S3 のパートとオブジェクトの上限](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html)

アプリ独自の最大サイズはアップロード方式に応じて設ける。Amazon S3 の署名付き POST では
`content-length-range` でサイズの範囲を指定できる。マルチパートでは、アプリがアップロードの完了を管理し、
送信済みパートの合計を確認してから完了させる。Amazon S3 では完了時に `MpuObjectSize` を指定すると、
期待した合計サイズとの一致も検証できる。署名やサイズ検証の互換性は採用する実ストレージで確認する。
[S3 の POST ポリシー](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sigv4-HTTPPOSTConstructPolicy.html)・[S3 の完了 API](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CompleteMultipartUpload.html)

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
