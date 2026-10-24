.DEFAULT_GOAL := help

CONFIG ?= configs/environment/local.json
KUBECTL ?= k0s kubectl
PYTHON ?= python3
SUDO ?= sudo
EDITOR ?= vi

.PHONY: help edit-config configure-secret-google-sso rotate-cookie-secret-google-sso apply-secret-google-sso restart-deployment-oauth2-proxy wait-deployment-oauth2-proxy up

help:
	@printf '%s\n' \
	  'make edit-config                         環境設定JSONを編集' \
	  'make configure-secret-google-sso         Google資格情報を非表示入力して作成・変更（Cookie鍵は維持）' \
	  'make rotate-cookie-secret-google-sso     Cookie鍵も再生成（既存セッションを無効化）' \
	  'make apply-secret-google-sso             Google資格情報をKubernetes Secretへ反映' \
	  'make restart-deployment-oauth2-proxy      認証サービスを再起動してSecretを再読込' \
	  'make wait-deployment-oauth2-proxy         認証サービスのrollout完了を待機' \
	  'make up                                 HTTPS必須で一括構築・配備' \
	  '変数: CONFIG=... KUBECTL="k0s kubectl" SUDO=sudo EDITOR=vi'

edit-config:
	@$(EDITOR) "$(CONFIG)"

configure-secret-google-sso:
	@$(PYTHON) scripts/infra.py configure-secret-google-sso --config "$(CONFIG)"

rotate-cookie-secret-google-sso:
	@$(PYTHON) scripts/infra.py configure-secret-google-sso --rotate-cookie --config "$(CONFIG)"

apply-secret-google-sso:
	@$(SUDO) ./bin/infra apply-secret-google-sso --config "$(CONFIG)" --kubectl "$(KUBECTL)"

restart-deployment-oauth2-proxy:
	@$(SUDO) $(KUBECTL) -n test rollout restart deployment/oauth2-proxy

wait-deployment-oauth2-proxy:
	@$(SUDO) $(KUBECTL) -n test rollout status deployment/oauth2-proxy --timeout=300s

up:
	@$(SUDO) ./bin/infra up --require-https --config "$(CONFIG)" --kubectl "$(KUBECTL)"
