#!/usr/bin/env python3
"""Verify both sample application HTTP routes and Knative recovery after scaling to zero."""
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
import infra
from urllib.parse import urlsplit



def validate_response(status, body, host):
    """Reject stale echo apps, error pages, and rewritten Hosts."""
    if status != 200 or len(body) > 65536:
        raise RuntimeError("expected HTTP 200 and bounded go-httpbin JSON")
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("headers"), dict):
        raise RuntimeError("expected go-httpbin JSON object with headers")
    headers = {key.lower(): value for key, value in payload["headers"].items()}
    url = payload.get("url")
    if not isinstance(url, str):
        raise RuntimeError("expected go-httpbin URL")
    parsed = urlsplit(url)
    if headers.get("host") != [host] or parsed.netloc != host or parsed.path != "/get":
        raise RuntimeError("go-httpbin JSON does not preserve the requested Host and path")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "configs/environment/local.json")
    parser.add_argument("--kubectl", default="k0s kubectl")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout in seconds")
    parser.add_argument("--scale-zero-timeout", type=int, default=600,
                        help="Maximum wait for zero Pods, in seconds (default: 600)")
    parser.add_argument("--poll-interval", type=int, default=5,
                        help="Pod polling interval in seconds (default: 5)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or min(args.timeout, args.scale_zero_timeout, args.poll_interval) <= 0:
        parser.error("port must be 1..65535 and all timeouts/intervals must be positive")
    config = infra.load(args.config)
    cases = [
        ("standard-buffer", "hello-test-ssvc." + config["standard_domain"], None),
        ("standard-nobuffer", "hello-test-ssvc." + config["standard_nobuffer_domain"], None),
        ("knative-buffer", "hello-test-ksvc." + config["knative_domain"], "hello"),
        ("knative-nobuffer", "hello-nobuffer-test-ksvc." + config["knative_nobuffer_domain"], "hello-nobuffer"),
    ]
    cases.extend([
        ("standard-default", "hello-default-test-ssvc." + config[infra.resolve_route(config, "standard")], None),
        ("knative-default", "hello-default-test-ksvc." + config[infra.resolve_route(config, "knative")], "hello-default"),
    ])
    command = shlex.split(args.kubectl)
    if not command:
        parser.error("kubectl command must not be empty")
    kubectl = command.copy()
    command += ["-n", "kong-system", "port-forward", "--address=127.0.0.1",
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

    def request(name, host):
        connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=args.timeout)
        print(f"\n[{name}] GET /get Host: {host}", flush=True)
        try:
            if process.poll() is not None:
                raise RuntimeError("port-forward has stopped")
            connection.request("GET", "/get", headers={"Host": host})
            response = connection.getresponse()
            body = response.read(65537)
            print(f"HTTP {response.status}")
            for key, value in response.getheaders():
                print(f"{key}: {value}")
            print("\n" + body.decode("utf-8", errors="replace"))
            validate_response(response.status, body, host)
        finally:
            connection.close()

    def wait_for_pods(service, zero, timeout):
        started = time.monotonic()
        deadline = started + timeout
        while True:
            if process.poll() is not None:
                raise RuntimeError("port-forward has stopped")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Pod がゼロになりませんでした" if zero else "復帰後の Ready Pod を確認できませんでした")
            result = subprocess.run(
                kubectl + ["--request-timeout=10s", "-n", "test", "get", "pods",
                           "-l", "serving.knative.dev/service=" + service, "-o", "json"],
                stdout=subprocess.PIPE, text=True, check=True, timeout=min(15, remaining),
            )
            pods = json.loads(result.stdout)["items"]
            ready_pods = [p for p in pods if not p["metadata"].get("deletionTimestamp")
                          and any(c.get("type") == "Ready" and c.get("status") == "True"
                                  for c in p.get("status", {}).get("conditions", []))]
            print(f"[scale-zero] 経過 {int(time.monotonic() - started)} 秒: "
                  f"Pod {len(pods)} 件 / Ready {len(ready_pods)} 件", flush=True)
            if zero and not pods:
                print("[scale-zero] Pod 0 件を確認しました。", flush=True)
                return
            if not zero and ready_pods:
                print("[scale-zero] 復帰後の Ready Pod: "
                      + ", ".join(p["metadata"]["name"] for p in ready_pods), flush=True)
                return
            time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))

    try:
        deadline = time.monotonic() + 30
        while not ready.wait(0.1):
            if process.poll() is not None:
                raise RuntimeError(f"port-forward exited: {process.returncode}")
            if time.monotonic() >= deadline:
                raise RuntimeError("port-forward startup timed out after 30 seconds")
        passed = 0
        knative_ok = []
        for name, host, service in cases:
            try:
                request(name, host)
                passed += 1
                if service:
                    knative_ok.append((name, host, service))
                print(f"PASS: {name}", flush=True)
            except (OSError, ValueError, http.client.HTTPException, RuntimeError) as exc:
                print(f"FAIL: {name}: {exc}", flush=True)
        for name, host, service in knative_ok:
            try:
                print(f"\n[scale-zero] {name}: テスト内容: Knative の Pod が0件になった後、同じ Kong 入口へ"
                      "再アクセスし、HTTP 200・go-httpbin JSON の Host・Ready Pod の再生成を確認します。",
                      flush=True)
                print("\n[scale-zero] Pod がゼロ件になるまで待機します（目安: 最後のアクセスから1〜2分程度、"
                      f"最大 {args.scale_zero_timeout} 秒）。"
                      "Knative への別のアクセスも停止してください。", flush=True)
                wait_for_pods(service, zero=True, timeout=args.scale_zero_timeout)
                # No HTTP requests are sent during the zero-Pod wait above.
                request(name + "-cold-start", host)
                wait_for_pods(service, zero=False, timeout=args.timeout)
                passed += 1
                print(f"PASS: {name}-scale-zero-recovery", flush=True)
            except (OSError, ValueError, KeyError, http.client.HTTPException,
                    RuntimeError, subprocess.SubprocessError) as exc:
                print(f"FAIL: {name}-scale-zero-recovery: {exc}", flush=True)
        if len(knative_ok) < 3:
            print("SKIP: 初回リクエストが失敗した Knative の復帰確認", flush=True)
        print(f"\n結果: {passed}/9 成功（明示4経路・既定2経路 / Knative 3サービスのスケールゼロ復帰）。")
        return 0 if passed == 9 else 1
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
