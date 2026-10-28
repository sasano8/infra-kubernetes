# Host判定、アプリ転送、SSOポータルとエラー生成

現在の実装を示す。簡素化後の構成は後半に分けて記載する。

## 現在の転送経路

```mermaid
flowchart TD
    Client[クライアント] --> TLS[TLS証明書の検証]
    TLS --> Kong{KongのHost・パス経路に一致するか}
    Kong -->|通常Serviceの経路| Standard[通常のService → 業務アプリPod]
    Kong -->|Knativeの経路| Kourier{KourierにアプリのHost経路があるか}
    Kourier -->|完全一致するアプリあり| App[Knative業務アプリ]
    Kourier -->|SSOポータルのHostに一致| Login[ログイン画面への通常アクセス]
    Kourier -->|アプリなし・wildcard fallback| KH[X-Infra-Error: unknown_host]
    Kong -->|一致する経路なし| Classify{生成済みの既知Host一覧にあるか}
    Classify -->|ある| KP[X-Infra-Error: path_not_found]
    Classify -->|ない| KU[X-Infra-Error: unknown_host]
    KP --> Alias[test/error-service alias]
    KU --> Alias
    KH --> Alias
    Alias --> Service[通常のService test/platform-portal:8080]
    Service --> Renderer
    Login --> UI
    subgraph Portal[SSOポータル test/sso：同一Pod・常駐]
        UI[ログイン・ログアウト画面]
        Renderer[エラー生成 application/problem+json]
    end
    Kong -. SSO対象経路ではCookieを内部確認 .-> OAuth[oauth2-proxy：別Deployment]
    UI -. ブラウザを認証ホストへ誘導・Kong経由 .-> OAuth
    OAuth -. Google OAuth .-> Google[Google]
```

通常の業務リクエストの本文は、KongからServiceまたはKourierへ進む。
oauth2-proxyにはCookieによる認証確認を送り、業務リクエストの本文は送らない。
SSO認証不要のpublic経路ではその確認を行わない。

エラー生成とログイン画面は **SSOポータルの同じアプリ・同じPod** に入っている。
Google OAuth・セッション確認を行う **oauth2-proxy本体は別のDeployment**。
「認証サービスに同梱」は、ログイン画面を提供するポータルに同梱する意味であり、
oauth2-proxyのプロセスやコンテナを改変してエラー処理を追加したわけではない。

エラー経路は通常のServiceからユーザーコンテナ8080番へ直結し、Kourier・Activator・queue-proxyへ戻さない。
別の `error_service` を指定した場合も、通常のServiceへ直接接続する。

## アプリ自身の404は上書きしない

```mermaid
sequenceDiagram
    participant C as クライアント
    participant K as Kong
    participant Q as Kourier（Knativeの場合）
    participant A as 業務アプリ
    C->>K: 有効なアプリHostへの要求
    K->>Q: Hostとパスを保持して転送
    Q->>A: 一致したアプリへ転送
    A-->>Q: 404 + アプリ固有Content-Type・JSON
    Q-->>K: 同じステータス・Content-Type・本文
    K-->>C: 同じステータス・Content-Type・本文
    Note over K,A: この応答を理由にエラーサービスを呼ぶ処理はない
```

通常ServiceではKourierを挟まず、Kongからアプリへ転送する。同じくアプリの応答を維持する。
エラーサービスを使うかどうかは、転送前に経路が見つからなかったかで決まる。
アプリのレスポンスコード・JSONを読んでエラーサービスへ振り直すことはしない。
SSOの認証確認とCookie更新などの処理は別であり、業務アプリのJSONを書き換えるものではない。

現在の結合テストは、アプリの404本文がそのまま返ることを確認している。
アプリ固有JSONのContent-Typeを含めた検証拡充は、下記の簡素化計画に含める。

## 現在の余分な処理

Kongの最後のfallbackには、既知Host一覧から404か421かを決めるpre-functionがある。
これは「認証ホストは存在するが `/` を公開していない」といった場合を、
未知Hostと区別しようとして追加したもの。アプリから返ってきた404の処理ではない。

Host不一致だけをエラーサービスへ送る目的なら、この一覧と404分類は不要。
また、新しいHostの入口を別管理で追加した場合に一覧と実際の経路がずれる問題もある。
この処理は [簡素化計画](plans/simplify-host-errors.md) で撤去する対象。

## 簡素化後の目標（未実装）

```mermaid
flowchart TD
    C[クライアント] --> K{Kong：Hostごとの入口に一致するか}
    K -->|通常サービスのHost| S[通常Service → 業務アプリ]
    K -->|KnativeのHost・経路ドメイン| Q{Kourier：アプリHostに一致するか}
    K -->|認証ホスト| O[認証用の入口]
    K -->|Host不一致| E[通常Service error_service]
    Q -->|一致| A[Knative業務アプリ]
    Q -->|ポータルHostに一致| UI[ログイン画面]
    Q -->|Host不一致| E
    E --> P[エラー生成：421のみ]
    subgraph Portal[SSOポータルアプリ：同一Pod]
        UI
        P
    end
```

認証ホストもHost単位の入口へ振り分け、未定義パスはその入口で404にする。
外部公開しない認証用パスの制限は、Host不一致とは別の既存処理として扱う。
業務アプリへ接続した後の404・421・500などは、アプリ自身の応答を返す。
エラーサービス自身に存在しないパスへの404も、そのアプリ自身の通常の404である。

## 実装と検証の参照先

- [Kongの経路とfallback](../plugins/kong/manifests/routes.yaml.tmpl)
- [Kourierのfallback](../apps/route-errors/manifests/apps.yaml.tmpl)
- [ポータルと通常Serviceの生成](../scripts/sso.py)
- [エラー生成とログイン画面のHTTPサーバー](../apps/platform-portal/server.py)
- [接続プロトコル](error-service.md)
- [現行動作・実機確認](routing-errors.md)
- [実プロキシの結合テスト](../tests/check_route_errors.py)
