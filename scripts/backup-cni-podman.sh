#!/usr/bin/env bash
set -euo pipefail

# Run as the Podman user so both rootless and rootful backends are checked.
if (( EUID == 0 )); then
  echo 'エラー: sudoを付けず、Podmanを利用するユーザーで実行してください。' >&2
  exit 1
fi

source_path=/etc/cni/net.d/87-podman-bridge.conflist
backup_dir=/etc/cni/reset-backup
backup_path="$backup_dir/87-podman-bridge.conflist"

if ! sudo test -e "$source_path" && ! sudo test -L "$source_path"; then
  echo "退避対象はありません: $source_path"
  exit 0
fi

user_backend=$(podman info --format '{{.Host.NetworkBackend}}')
root_backend=$(sudo podman info --format '{{.Host.NetworkBackend}}')
printf 'Podman backend: user=%s / root=%s\n' "$user_backend" "$root_backend"
if [[ "$user_backend" != netavark || "$root_backend" != netavark ]]; then
  echo 'エラー: 両方がnetavarkの場合だけ退避できます。CNI設定は変更していません。' >&2
  exit 1
fi
if sudo test -e "$backup_path" || sudo test -L "$backup_path"; then
  echo "エラー: 退避先が既に存在します（上書きしません）: $backup_path" >&2
  exit 1
fi

printf '退避元: %s\n退避先: %s\n' "$source_path" "$backup_path"
sudo install -d -m 0700 "$backup_dir"
sudo mv -n -- "$source_path" "$backup_path"
if sudo test -e "$source_path" || sudo test -L "$source_path"; then
  echo 'エラー: 退避できませんでした。退避先と元ファイルを確認してください。' >&2
  exit 1
fi
echo '旧Podman CNI設定を退避しました。make install-k0sを再実行してください。'
