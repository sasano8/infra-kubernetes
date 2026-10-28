"""Real Kong/Envoy routing and TLS regression checks; Docker and PyYAML required.

Envoy's virtual hosts mirror net-kourier v1.20's domainsForRule and prefix routes.
This exercises both proxies, but does not replace a live KIC/Knative reconciliation test.
"""
import http.client
import json
import os
from pathlib import Path
import socket
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra


def main():
    env = dict(os.environ)
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(key, None)
    docker_cmd = ["docker", "--host=unix:///var/run/docker.sock"]

    def docker(*args):
        result = subprocess.run(docker_cmd + list(args), env=env, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError("docker " + args[0] + ": " + result.stderr[-4000:])
        return (result.stdout + (result.stderr if args[0] == "logs" else "")).strip()

    name = "infra-route-errors-" + uuid.uuid4().hex[:10]
    containers = []
    created_network = False
    with tempfile.TemporaryDirectory(prefix="infra-route-errors-") as td:
        path = Path(td)
        path.chmod(0o755)
        raw = json.loads((ROOT / "configs/environment/example.json").read_text())
        raw["tls"].update(enabled=True, email="test@example.test", cloudflare_zone="example.test")
        raw["sso"] = {"enabled": True, "email_domain": "example.test"}
        (path / "config.json").write_text(json.dumps(raw))
        config = infra.load(path / "config.json")
        infra.render(config, path / "rendered")
        apps = list(yaml.safe_load_all((path / "rendered/apps.yaml").read_text()))
        code = next(i for i in apps if i["kind"] == "ConfigMap" and i["metadata"]["name"] == "sso-portal")
        generated_routes = list(yaml.safe_load_all((path / "rendered/routes.yaml").read_text()))
        reason_plugin = next(i for i in generated_routes if i["kind"] == "KongPlugin" and i["metadata"]["name"] == "routing-error-reason")
        strip_plugin = next(i for i in generated_routes if i["kind"] == "KongPlugin" and i["metadata"]["name"] == "strip-error-reason")
        oauth_items = json.loads((path / "rendered/sso.json").read_text())["items"]
        oauth_plugin = next(i for i in oauth_items if i["kind"] == "KongPlugin" and i["metadata"]["name"] == "sso-oauth-endpoints")
        oauth_ingress = next(i for i in oauth_items if i["kind"] == "Ingress")
        auth_paths = [p["path"] for p in oauth_ingress["spec"]["rules"][0]["http"]["paths"]]
        shutil.copytree(ROOT / "plugins/sso/kong/plugins/session-auth", path / "session-auth")
        (path / "session-auth").chmod(0o755)
        for fixture in (path / "session-auth").iterdir():
            fixture.chmod(0o644)
        for filename, content in code["data"].items():
            (path / filename).write_text(content)
        (path / "app.py").write_text('''from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class App(BaseHTTPRequestHandler):
    def do_GET(self):
        status = int(self.path.rsplit("/", 1)[1]) if self.path.startswith("/status/") else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/vnd.test-app+json")
        self.end_headers()
        self.wfile.write((self.headers.get("X-Infra-Error", "absent").encode() if self.path == "/echo-error-header" else b'{"application":"own response"}'))
    def log_message(self, *args): pass
ThreadingHTTPServer(("0.0.0.0", 8080), App).serve_forever()
''')
        public_domain = config["routes"]["knative_public"]["domain"]
        standard_host = "hello-test-ssvc." + config["routes"]["standard_domain"]["domain"]
        private_host = "hello-test-ksvc." + config["routes"]["knative_domain"]["domain"]
        public_host = "sso-test-ksvc." + public_domain
        unknown_app = "does-not-exist-test-ksvc." + public_domain
        invalid_domain = "sso-test-ksvc.knative-public-typo.example.test"
        auth_host = config["sso"]["auth_host"]
        wildcard = "*." + public_domain
        ingress = next(i for i in apps if i["apiVersion"] == "networking.internal.knative.dev/v1alpha1")
        fallback_hosts = ingress["spec"]["rules"][0]["hosts"]
        assert wildcard in fallback_hosts
        fallback_hash = "f" * 64

        def virtual_host(host, cluster):
            # Same domain expansion as net-kourier's domainsForRule.
            routes = [{"match": {"prefix": "/"}, "route": {"cluster": cluster}}]
            if cluster == "fallback":
                routes[0]["request_headers_to_add"] = [{"header": {"key": "X-Infra-Error", "value": "unknown_host"},
                                                       "append_action": "OVERWRITE_IF_EXISTS_OR_ADD"}]
            if cluster == "fallback":
                # InsertProbe plus headersToAdd in net-kourier/networking.
                routes.insert(0, {"match": {"prefix": "/", "headers": [{"name": "K-Network-Hash",
                    "string_match": {"exact": "override"}}]}, "route": {"cluster": cluster},
                    "request_headers_to_add": [{"header": {"key": "X-Infra-Error", "value": "unknown_host"},
                                                "append_action": "OVERWRITE_IF_EXISTS_OR_ADD"},
                                               {"header": {"key": "K-Network-Hash", "value": fallback_hash},
                                                "append_action": "OVERWRITE_IF_EXISTS_OR_ADD"}]})
            domains = [domain for name in fallback_hosts for domain in (name, name + ":*")] if cluster == "fallback" else [host, host + ":*"]
            return {"name": host, "domains": domains, "routes": routes}

        clusters = []
        for cluster, host, port in (("app", "app", 8080), ("fallback", "route-errors", 8080)):
            clusters.append({"name": cluster, "type": "STRICT_DNS", "connect_timeout": "2s",
                             "load_assignment": {"cluster_name": cluster, "endpoints": [{"lb_endpoints": [
                                 {"endpoint": {"address": {"socket_address": {"address": host, "port_value": port}}}}]}]}})
        envoy = {"static_resources": {"listeners": [{"name": "http", "address": {
            "socket_address": {"address": "0.0.0.0", "port_value": 8080}}, "filter_chains": [{"filters": [{
                "name": "envoy.filters.network.http_connection_manager", "typed_config": {
                    "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
                    "stat_prefix": "test", "route_config": {"name": "external", "virtual_hosts": [
                        virtual_host(public_host, "app"), virtual_host(wildcard, "fallback")]},
                    "http_filters": [{"name": "envoy.filters.http.router", "typed_config": {
                        "@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"}}]}}]}]}],
                                      "clusters": clusters}}
        (path / "envoy.json").write_text(json.dumps(envoy))
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(path / "tls.key"), "-out", str(path / "tls.crt"),
                        "-subj", "/CN=" + wildcard, "-addext", "subjectAltName=DNS:" + wildcard + ",DNS:" + auth_host],
                       capture_output=True, check=True)
        (path / "tls.key").chmod(0o644)  # Ephemeral fixture key, readable by the Kong container user.
        kong = {"_format_version": "3.0", "services": [
            {"name": "app", "url": "http://app:8080", "plugins": [{"name": "request-transformer", "config": strip_plugin["config"]}], "routes": [
                {"name": "standard", "hosts": [standard_host], "paths": ["/"], "strip_path": False, "preserve_host": True}]},
            {"name": "kourier", "url": "http://kourier:8080", "plugins": [{"name": "request-transformer", "config": strip_plugin["config"]}], "routes": [
                {"name": "public", "hosts": [wildcard], "paths": ["/"], "strip_path": False, "preserve_host": True},
                {"name": "private-existing", "hosts": [private_host], "paths": ["/"], "strip_path": False, "preserve_host": True}]},
            {"name": "oauth", "url": "http://app:8080", "plugins": [{"name": "session-auth", "config": oauth_plugin["config"]}], "routes": [
                {"name": "oauth", "hosts": [auth_host], "paths": auth_paths, "strip_path": False, "preserve_host": True}]},
            {"name": "fallback", "url": "http://route-errors:8080", "plugins": [{"name": reason_plugin["plugin"], "config": reason_plugin["config"]}], "routes": [
                {"name": "fallback", "paths": ["/"], "strip_path": False, "preserve_host": True}]}],
                "certificates": [{"cert": (path / "tls.crt").read_text(), "key": (path / "tls.key").read_text(),
                                  "snis": [{"name": wildcard}, {"name": auth_host}]}]}
        (path / "kong.yaml").write_text(yaml.safe_dump(kong))
        for fixture in path.iterdir():
            if fixture.is_file():
                fixture.chmod(0o644)  # Fixtures contain only generated test credentials/configuration.

        def start(alias, image, args=(), flags=()):
            container = name + "-" + alias
            docker("run", "-d", "--name", container, "--network", name, "--network-alias", alias,
                   "--mount", f"type=bind,src={path},dst=/fixtures,readonly", *flags, image, *args)
            containers.append(container)
            return container

        try:
            docker("network", "create", name)
            created_network = True
            start("route-errors", "python:3.13-alpine", ("python3", "/fixtures/server.py"),
                  ("--mount", f"type=bind,src={path},dst=/site,readonly"))
            start("app", "python:3.13-alpine", ("python3", "/fixtures/app.py"))
            start("kourier", "envoyproxy/envoy:v1.34-latest", ("-c", "/fixtures/envoy.json", "--concurrency", "1"))
            gateway = start("kong", "kong:3.9.1", flags=(
                "-p", "127.0.0.1::8000", "-p", "127.0.0.1::8443",
                "-e", "KONG_DATABASE=off", "-e", "KONG_DECLARATIVE_CONFIG=/fixtures/kong.yaml",
                "-e", "KONG_PROXY_LISTEN=0.0.0.0:8000,0.0.0.0:8443 ssl", "-e", "KONG_ADMIN_LISTEN=off",
                "-e", "KONG_NGINX_WORKER_PROCESSES=1", "-e", "KONG_PLUGINS=bundled,session-auth",
                "-e", "KONG_LUA_PACKAGE_PATH=/opt/?.lua;;",
                "--mount", f"type=bind,src={path / 'session-auth'},dst=/opt/kong/plugins/session-auth,readonly"))
            http_port = int(docker("port", gateway, "8000/tcp").rsplit(":", 1)[1])
            tls_port = int(docker("port", gateway, "8443/tcp").rsplit(":", 1)[1])

            def request(host, uri="/", context=None, sni=None, headers=None):
                port = tls_port if context is not None else http_port
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                if context is not None:
                    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                    try:
                        conn.sock = context.wrap_socket(sock, server_hostname=sni or host)
                    except Exception:
                        sock.close()
                        raise
                try:
                    conn.request("GET", uri, headers={"Host": host, **(headers or {})})
                    response = conn.getresponse()
                    return response.status, response.read(), dict(response.getheaders())
                finally:
                    conn.close()

            deadline = time.monotonic() + 60
            while True:
                states = docker("inspect", "--format", "{{.State.Status}}", *containers).splitlines()
                if any(state != "running" for state in states):
                    raise RuntimeError("Test proxy exited before becoming ready: " + repr(states))
                try:
                    if request(public_host)[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Kong/Envoy did not become ready")
                time.sleep(0.25)
            cases = [(standard_host, "/", 200), (public_host, "/", 200),
                     (standard_host, "/status/404", 404), (public_host, "/status/404", 404),
                     (unknown_app, "/", 421), (unknown_app, "/status/404", 421),
                     (invalid_domain, "/", 421), (private_host, "/", 421)]
            cases += [(host, "/status/" + str(status), status)
                      for host in (standard_host, public_host) for status in (421, 422, 500)]
            for host, uri, expected in cases:
                status, body, headers = request(host, uri)
                assert status == expected, (host, uri, status, expected)
                if host in (standard_host, public_host) and uri.startswith("/status/"):
                    assert body == b'{"application":"own response"}', "Application error body was replaced"
                    assert headers["Content-Type"] == "application/vnd.test-app+json", "Application Content-Type was replaced"
                elif expected == 421:
                    problem = json.loads(body)
                    assert problem["code"] == "unknown_host" and problem["status"] == 421
                    assert problem["type"] == "about:blank"
                    assert any(k.lower() == "content-type" and v == "application/problem+json" for k, v in headers.items())
                    assert "Location" not in headers and "Set-Cookie" not in headers
            verified = ssl.create_default_context(cafile=str(path / "tls.crt"))
            for uri in ("/", "/missing", "/oauth2/auth"):
                status, body, _ = request(auth_host, uri, context=verified)
                assert status == 404 and json.loads(body) == {"error": "path_not_found"}, (uri, status, body)
            for uri in ("/oauth2/start", "/oauth2/callback", "/oauth2/sign_in", "/oauth2/sign_out"):
                assert request(auth_host, uri, context=verified)[0] == 200, uri
            assert request(unknown_app, context=verified)[0] == 421
            try:
                request(invalid_domain, context=verified)
            except ssl.SSLCertVerificationError:
                pass
            else:
                raise AssertionError("Unknown domain must fail certificate verification before HTTP")
            # Negative TLS diagnostic only; normal verification above stays enabled.
            diagnostic = ssl._create_unverified_context()
            assert request(invalid_domain, context=diagnostic)[0] == 421
            assert request(invalid_domain, context=verified, sni=public_host)[0] == 421
            status, _, headers = request(unknown_app, headers={"K-Network-Probe": "probe", "K-Network-Hash": "override"})
            returned_hash = next((value for key, value in headers.items() if key.lower() == "k-network-hash"), None)
            assert (status, returned_hash) == (200, fallback_hash), (status, returned_hash)
            status, body, _ = request(public_host, "/echo-error-header", headers={"X-Infra-Error": "unknown_host"})
            assert status == 200 and body == b"absent", "Client error hint reached an application"
            status, _, _ = request(invalid_domain, headers={"X-Infra-Error": "path_not_found", "K-Network-Probe": "probe", "K-Network-Hash": "a" * 64})
            assert status == 421, "Client headers changed Kong fallback classification"
            # An unavailable renderer must not intercept responses from existing apps.
            docker("stop", name + "-route-errors")
            for host in (standard_host, public_host):
                for code in (200, 404):
                    uri = "/" if code == 200 else "/status/404"
                    status, body, headers = request(host, uri)
                    assert status == code and body == b'{"application":"own response"}'
                    assert headers["Content-Type"] == "application/vnd.test-app+json"
            print("Kong/Envoy routing and TLS checks: 32 passed")
        except Exception:
            for container in containers:
                print(docker("logs", "--tail", "40", container), file=sys.stderr)
            raise
        finally:
            for container in reversed(containers):
                docker("rm", "-f", container)
            if created_network:
                docker("network", "rm", name)


if __name__ == "__main__":
    main()
