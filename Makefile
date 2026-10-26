.DEFAULT_GOAL := help

CONFIG ?= configs/environment/local.json
KUBECTL ?= kubectl
PYTHON ?= python3
SUDO ?= sudo
EDITOR ?= vi

.PHONY: help install-k0s start-k0s stop-k0s status-k0s reset-k0s install-helm edit-config configure-secret-cloudflare-token configure-secret-google-sso rotate-cookie-secret-google-sso apply-secret-google-sso restart-deployment-oauth2-proxy wait-deployment-oauth2-proxy up

help:
	@printf '%s\n' \
	  '前提: Argo CD・Infisical・ESOにはHelm 3.13以降の3系を別途インストール（推奨v3.19.0）' \
	  'make install-k0s                         sudoでk0sを導入・systemd登録・起動' \
	  'make start-k0s / stop-k0s / status-k0s   sudoでk0sの起動・停止・状態確認' \
	  'make reset-k0s                           sudoで停止・クラスタ破棄・本体削除（バックアップ後に実行）' \
	  'make install-helm                        未導入ならHelm v3.19.0を導入、導入済みならバージョン確認' \
	  'make edit-config                         環境設定JSONを編集' \
	  'make configure-secret-cloudflare-token   保存先を表示してCloudflareトークンを非表示入力・保存' \
	  'make configure-secret-google-sso         Google資格情報を非表示入力して作成・変更（Cookie鍵は維持）' \
	  'make rotate-cookie-secret-google-sso     Cookie鍵も再生成（既存セッションを無効化）' \
	  'make apply-secret-google-sso             Google資格情報をKubernetes Secretへ反映' \
	  'make restart-deployment-oauth2-proxy      認証サービスを再起動してSecretを再読込' \
	  'make wait-deployment-oauth2-proxy         認証サービスのrollout完了を待機' \
	  'make up                                 HTTPS必須で一括構築・配備' \
	  '変数: CONFIG=... KUBECTL=kubectl SUDO=sudo EDITOR=vi'

# Host lifecycle operations require root; Kubernetes deployment uses user kubectl.
install-k0s:
	@$(SUDO) ./bin/infra host install

start-k0s:
	@$(SUDO) ./bin/infra host start

stop-k0s:
	@$(SUDO) ./bin/infra host stop

status-k0s:
	@$(SUDO) ./bin/infra host status

# Destructive host cleanup; reboot remains an explicit administrator operation.
reset-k0s:
	@$(SUDO) ./bin/infra host reset

# Helm requests sudo only when placing its binary under /usr/local/bin.
install-helm:
	@$(PYTHON) scripts/install-helm.py

edit-config:
	@$(EDITOR) "$(CONFIG)"

configure-secret-google-sso:
	@$(PYTHON) scripts/infra.py configure-secret-google-sso --config "$(CONFIG)"

configure-secret-cloudflare-token:
	@$(PYTHON) scripts/infra.py configure-secret-cloudflare-token --config "$(CONFIG)"

rotate-cookie-secret-google-sso:
	@$(PYTHON) scripts/infra.py configure-secret-google-sso --rotate-cookie --config "$(CONFIG)"

apply-secret-google-sso:
	@./bin/infra apply-secret-google-sso --config "$(CONFIG)" --kubectl "$(KUBECTL)"

restart-deployment-oauth2-proxy:
	@$(KUBECTL) -n test rollout restart deployment/oauth2-proxy

wait-deployment-oauth2-proxy:
	@$(KUBECTL) -n test rollout status deployment/oauth2-proxy --timeout=300s

up:
	@./bin/infra up --require-https --config "$(CONFIG)" --kubectl "$(KUBECTL)"
