#!/usr/bin/env python3
"""Install the official Helm binary if absent; validate an existing Helm first."""
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

import platform_plugins

VERSION = "v3.19.0"
DESTINATION = Path("/usr/local/bin/helm")


def main():
    if shutil.which("helm"):
        platform_plugins.require_helm()
        print("Helmは導入済みで、対応バージョンです。")
        return
    if platform.system() != "Linux":
        raise ValueError("自動導入はLinux向けです。Helm公式手順で3.13以降の3系を導入してください")
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if not arch:
        raise ValueError("対応CPUはamd64 / arm64です")
    with tempfile.TemporaryDirectory(prefix="infra-helm-install-") as directory:
        directory = Path(directory)
        name = f"helm-{VERSION}-linux-{arch}.tar.gz"
        archive, checksum = directory / name, directory / (name + ".sha256sum")
        for path in (archive, checksum):
            subprocess.run(["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "20",
                            "--max-time", "300", "--proto", "=https", "--tlsv1.2", "--output", str(path),
                            "https://get.helm.sh/" + path.name], check=True)
        fields = checksum.read_text().split()
        if (not fields or not re.fullmatch(r"[a-fA-F0-9]{64}", fields[0])
                or fields[0].lower() != hashlib.sha256(archive.read_bytes()).hexdigest()):
            raise ValueError("HelmのSHA-256検証に失敗しました（インストールしていません）")
        with tarfile.open(archive, "r:gz") as package:
            member = package.getmember(f"linux-{arch}/helm")
            if not member.isfile():
                raise ValueError("Helmアーカイブのバイナリが通常ファイルではありません")
            with package.extractfile(member) as source:
                binary = directory / "helm"
                binary.write_bytes(source.read())
        privilege = [] if os.geteuid() == 0 else ["sudo"]
        subprocess.run([*privilege, "install", "-d", str(DESTINATION.parent)], check=True)
        subprocess.run([*privilege, "install", "-m", "0755", str(binary), str(DESTINATION)], check=True)
    platform_plugins.require_helm()
    print("Helm " + VERSION + " を " + str(DESTINATION) + " へインストールしました。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print("エラー: " + str(exc), file=sys.stderr)
        sys.exit(1)
