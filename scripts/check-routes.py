#!/usr/bin/env python3
"""Start a temporary Kong port-forward and verify both SAS-47 HTTP routes."""
import argparse
import http.client
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "configs/environment/local.json")
    parser.add_argument("--kubectl", default="k0s kubectl")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout in seconds")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.timeout <= 0:
        parser.error("port must be 1..65535 and timeout must be positive")
    config = json.loads(args.config.read_text())
    cases = [
        ("standard", config["standard_host"], "standard-service"),
        ("knative", "sas47-apps--serverless." + config["domain"], "knative-service"),
    ]
    command = shlex.split(args.kubectl)
    if not command:
        parser.error("kubectl command must not be empty")
    command += ["-n", "sas47-kong", "port-forward", "--address=127.0.0.1",
                "service/kong-proxy", f"{args.port}:80"]
    print("+ " + shlex.join(command), flush=True)
    ready = threading.Event()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)

    def log_output():
        for line in process.stdout:
            print("[port-forward] " + line.rstrip(), flush=True)
            if re.search(rf"Forwarding from 127\.0\.0\.1:{args.port} ->", line):
                ready.set()

    reader = threading.Thread(target=log_output, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 30
        while not ready.wait(0.1):
            if process.poll() is not None:
                raise RuntimeError(f"port-forward exited: {process.returncode}")
            if time.monotonic() >= deadline:
                raise RuntimeError("port-forward startup timed out after 30 seconds")
        failures = 0
        for name, host, expected in cases:
            connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=args.timeout)
            print(f"\n[{name}] GET / Host: {host}", flush=True)
            try:
                if process.poll() is not None:
                    raise RuntimeError("port-forward has stopped")
                connection.request("GET", "/", headers={"Host": host})
                response = connection.getresponse()
                body = response.read(65537)
                print(f"HTTP {response.status}")
                for key, value in response.getheaders():
                    print(f"{key}: {value}")
                print("\n" + body.decode("utf-8", errors="replace"))
                if response.status != 200 or body.strip() != expected.encode():
                    raise RuntimeError(f"expected HTTP 200 and body {expected!r}")
                print(f"PASS: {name}", flush=True)
            except (OSError, http.client.HTTPException, RuntimeError) as exc:
                failures += 1
                print(f"FAIL: {name}: {exc}", flush=True)
            finally:
                connection.close()
        print(f"\n結果: {2 - failures}/2 成功。スケールゼロ復帰はこのスクリプトの対象外です。")
        return 1 if failures else 0
    finally:
        # Stop only the process this script started, even on failure or Ctrl-C.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=5)
        if not reader.is_alive():
            process.stdout.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        sys.exit(1)
