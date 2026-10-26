#!/usr/bin/env bash
# Read-only diagnostics through the current user kubectl context.
set -uo pipefail

if ! command -v kubectl >/dev/null 2>&1; then
  echo "設定済みの kubectl が PATH に必要です。" >&2
  exit 1
fi

failures=0
check() {
  printf '\n+ '
  printf '%q ' "$@"
  printf '\n'
  "$@"
  local status=$?
  if (( status != 0 )); then
    printf 'コマンド失敗（終了コード %s）。次の確認に進みます。\n' "$status" >&2
    failures=$((failures + 1))
  fi
}

# Bound each API request so an unreachable API does not block all diagnostics.
check kubectl --request-timeout=30s -n knative-serving get pods -o wide
check kubectl --request-timeout=30s -n knative-serving get svc webhook -o wide
check kubectl --request-timeout=30s -n knative-serving get endpointslice \
  -l kubernetes.io/service-name=webhook -o wide
check kubectl --request-timeout=30s -n knative-serving logs deployment/webhook --tail=100
check kubectl --request-timeout=30s -n kube-system get pods -o wide

printf '\n確認完了: コマンド失敗 %s 件\n' "$failures"
echo "失敗が0件でも webhook の通信正常性を保証するものではありません。出力を確認してください。"
if (( failures != 0 )); then
  exit 1
fi
