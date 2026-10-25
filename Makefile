.DEFAULT_GOAL := help

CONFIG ?= configs/environment/local.json
KUBECTL ?= k0s kubectl
PYTHON ?= python3
SUDO ?= sudo
EDITOR ?= vi

.PHONY: help help-install-helm check-helm edit-config configure-secret-google-sso rotate-cookie-secret-google-sso apply-secret-google-sso restart-deployment-oauth2-proxy wait-deployment-oauth2-proxy up

help:
	@printf '%s\n' \
	  '前提: Argo CD・Infisical・ESOにはHelm 3.13以降の3系を別途インストール（推奨v3.19.0）' \
	  'make help-install-helm                   Helmの導入先・バージョン・手順を表示' \
	  'make check-helm                          配備時と同じsudo環境でHelmを確認' \
	  'make edit-config                         環境設定JSONを編集' \
	  'make configure-secret-google-sso         Google資格情報を非表示入力して作成・変更（Cookie鍵は維持）' \
	  'make rotate-cookie-secret-google-sso     Cookie鍵も再生成（既存セッションを無効化）' \
	  'make apply-secret-google-sso             Google資格情報をKubernetes Secretへ反映' \
	  'make restart-deployment-oauth2-proxy      認証サービスを再起動してSecretを再読込' \
	  'make wait-deployment-oauth2-proxy         認証サービスのrollout完了を待機' \
	  'make up                                 HTTPS必須で一括構築・配備' \
	  '変数: CONFIG=... KUBECTL="k0s kubectl" SUDO=sudo EDITOR=vi'

# Helm is a separately installed host prerequisite, not installed by make up.
help-install-helm:
	@printf '%s\n' \
	  '必要: Helm 3.13以降の3系（推奨v3.19.0）' \
	  '導入手順: https://helm.sh/docs/intro/install/' \
	  '公式バイナリ: https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz（arm64はamd64をarm64へ変更）' \
	  '同じURLに.sha256sumを付けたチェックサムで検証してから、/usr/local/bin/helm等へ配置してください。' \
	  'sudoからも利用できる場所に配置し、make check-helmで確認してください。' \
	  '詳細: docs/platform-plugins.md'

check-helm:
	@helm_version=$$($(SUDO) helm version --short) || { \
	  printf '%s\n' 'Helmを配備時の環境から実行できません。make help-install-helmを参照してください。' >&2; exit 1; \
	}; \
	printf '%s\n' "$$helm_version" | $(PYTHON) -c 'import re,sys; version=sys.stdin.read().strip(); match=re.match(r"v3\.([0-9]+)\.", version); valid=match is not None and int(match[1]) >= 13; print("Helm確認: " + version if valid else "Helm 3.13以降の3系が必要です。make help-install-helmを参照してください。"); sys.exit(0 if valid else 1)'

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
