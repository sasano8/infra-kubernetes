# Hostによる転送とエラー生成の配置

入口の判定はHostに絞る。公開されたHostなら転送先へ渡し、未公開のHostなら421を返す。
パスが存在するかは転送先が判断する。SSOポータルも、この点では他のアプリと同じである。

以下は **簡素化後の設計（未実装）**。現行との差分は末尾に記載する。

## Hostによる転送

```mermaid
flowchart TD
    C[クライアント] --> K{Kong：公開Hostの条件に一致するか}
    K -->|通常ServiceのHostに一致| S[通常Service → アプリ]
    K -->|KnativeのHost・ドメイン条件に一致| Q{Kourier：完全一致するアプリHostがあるか}
    K -->|一致なし| E[エラーサービス → 421]
    Q -->|あり| A[Knativeアプリ]
    Q -->|なし・wildcard fallback| E
```

Kongでは公開するHostの入口を `/` Prefixに揃え、すべてのパスを転送先へ渡す。
Kongの経路設定にはパス条件が残るが、`/` が全パスを受けるため、入口の振り分けはHostで決まる。
既存のbuffering／no-buffering設定は各経路で維持する。

Knativeのワイルドカード経路はKourierへ転送するための入口であり、アプリの存在を保証するものではない。
Kourierが完全一致するアプリHostへ転送し、アプリがなければwildcard fallbackからエラーサービスへ渡す。
Kong側で個別Hostだけを公開する経路では、その公開設定への一致も必要になる。

ログイン画面を提供するポータルも、通常のKnativeアプリとして「あり」の経路を通る。
ポータル専用のHost判定や分岐は設けない。
認証の要否は各経路の設定に従うが、この図はHostによる転送だけを示している。
SSOの処理は [SSOの構成とリダイレクト](sso-flow.md) を参照。

## パスとアプリの応答

Hostが一致すれば、パスは転送先の責任とする。
転送先が404を返しても、Kong・Kourierはその応答をエラーサービスへ振り直さない。

| 状況 | 応答する主体・結果 |
| --- | --- |
| 公開Hostに一致しない | エラーサービスから421、`unknown_host` |
| Knativeドメインには一致するが、アプリHostが存在しない | 同上 |
| アプリHostが存在し、パスが存在しない | アプリ自身の404 |
| アプリが独自JSONで404・421・500などを返す | アプリのステータス・Content-Type・本文を維持 |

**プロキシのfallbackで `path_not_found` と `unknown_host` を区別する必要はない。**
アプリ自身が404の本文として `path_not_found` を使うことは、この入口判定とは別である。
認証ホストもHost全体の入口を用意し、未公開の認証用パスはその転送先の処理で拒否する。
`/oauth2/auth` などの外部公開制限を解除するという意味ではない。

TLS証明書の検証はHTTPより前に行われる。証明書対象外のHostでは、421を見る前にTLS検証で失敗し得る。

## エラー生成サービスの配置

こちらはHost判定の図ではなく、エラーを生成するプログラムがどこで動くかを示す。

```mermaid
flowchart TD
    K[KongのHost不一致] --> Alias[通常Service test/error-service：alias]
    Q[KourierのHost不一致] --> Alias
    Alias --> S[通常Service test/platform-portal:8080]
    S --> E
    subgraph Pod[ポータルの同一Pod・常駐]
        E[エラー生成：application/problem+json]
        UI[ログイン・ログアウト画面]
    end
```

既定では、エラー生成プログラムをログイン画面のポータルに同梱している。
同じPodを利用する配置上の関係であり、エラー生成のためにSSO認証を行うわけではない。
SSO無効時も、既定のエラー生成に必要なPodは起動する。
Google OAuth・セッション確認を行うoauth2-proxy本体は別Deploymentである。

エラー経路は通常のServiceからユーザーコンテナへ直接転送し、Kourier・Activator・queue-proxyへ戻さない。
そのため転送が循環しない。設定で別の `error_service` を指定した場合も、通常のServiceへ直接接続する。

## 現行実装との差分

上の配置図は現行どおりだが、Hostだけに絞る入口判定はまだ実装していない。

- 通常アプリの入口はすでに `/` Prefixだが、認証ホストの入口は現在 `/oauth2` に限定されている。
- Kongの最後のfallbackには、生成済みの既知Host一覧から `path_not_found` の404と `unknown_host` の421を分ける処理が残っている。
- 簡素化では認証ホストも `/` Prefixで受け、既知Host一覧とfallbackの404分類を削除する。
- アプリ自身の404本文を上書きしない動作は、現在も維持されている。

実施順序と受け入れ条件は [簡素化計画](plans/simplify-host-errors.md) を参照。

## 実装と検証の参照先

- [Kongの経路とfallback](../plugins/kong/manifests/routes.yaml.tmpl)
- [Kourierのfallback](../apps/route-errors/manifests/apps.yaml.tmpl)
- [ポータルと通常Serviceの生成](../scripts/sso.py)
- [エラー生成とログイン画面のHTTPサーバー](../apps/platform-portal/server.py)
- [接続プロトコル](error-service.md)
- [現行動作・実機確認](routing-errors.md)
- [実プロキシの結合テスト](../tests/check_route_errors.py)
