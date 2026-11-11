# infra-kubernetes

k0s 上で通常の Kubernetes Service と Knative Service を公開する構成。HTTPS の入口は Kong。
Knative へは Kourier が転送し、保護経路では Kong が Google Workspace SSO のセッションを確認する。

| 目的 | 参照先 |
| --- | --- |
| k0s 導入済みの環境に設定を適用する | [導入・設定適用](setup-details.md) |
| DNS を設定して HTTPS で公開する | [HTTPS・DNS・証明書](https.md) |
| アプリの公開先や認証方式を選ぶ | [アプリ公開とルーティング](route-configuration.md)、[SSO](sso.md) |
| バッファリングやアップロード制限を設定する | [ストリーミング](streaming.md) |
| 未認証・認証後の接続を確かめる | [動作確認と障害切り分け](verification.md) |
| 運用する | [追加プラグイン](platform-plugins.md)、[移行・旧リソース整理・撤去](cleanup.md) |
