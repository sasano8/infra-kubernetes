"""Real Kong/Envoy routing and TLS regression checks; Docker and PyYAML required.

Envoy's virtual hosts mirror net-kourier v1.20's domainsForRule and prefix routes.
This exercises both proxies, but does not replace a live KIC/Knative reconciliation test.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
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
from threading import Event
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
        raw["upload_limits"].update(client_body_idle_timeout_seconds=3,
                                    upstream_read_idle_timeout_seconds=2)
        (path / "config.json").write_text(json.dumps(raw))
        config = infra.load(path / "config.json")
        infra.render(config, path / "rendered")
        kong_docs = list(yaml.safe_load_all((path / "rendered/kong.yaml").read_text()))
        limits_map = next(i for i in kong_docs if i["kind"] == "ConfigMap")
        (path / "upload-limits.conf").write_text(limits_map["data"]["upload-limits.conf"])
        (path / "kong.lua").write_text(limits_map["data"]["kong.lua"])
        deployment = next(i for i in kong_docs if i["kind"] == "Deployment")
        kong_env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        limits_flags = tuple(arg for env_var in kong_env
                             if env_var["name"].startswith("KONG_NGINX_PROXY_") or env_var["name"] == "KONG_NGINX_HTTP_INCLUDE"
                             for arg in ("-e", f'{env_var["name"]}={env_var["value"]}'))
        lua_path = next(env_var["value"] for env_var in kong_env if env_var["name"] == "KONG_LUA_PACKAGE_PATH")
        apps = list(yaml.safe_load_all((path / "rendered/apps.yaml").read_text()))
        code = next(i for i in apps if i["kind"] == "ConfigMap" and i["metadata"]["name"] == "sso-portal")
        generated_routes = list(yaml.safe_load_all((path / "rendered/routes.yaml").read_text()))
        reason_plugin = next(i for i in generated_routes if i["kind"] == "KongPlugin" and i["metadata"]["name"] == "routing-error-reason")
        strip_plugin = next(i for i in generated_routes if i["kind"] == "KongPlugin" and i["metadata"]["name"] == "strip-error-reason")
        oauth_items = json.loads((path / "rendered/sso.json").read_text())["items"]
        oauth_plugin = next(i for i in oauth_items if i["kind"] == "KongPlugin" and i["metadata"]["name"] == "sso-oauth-endpoints")
        oauth_ingress = next(i for i in oauth_items if i["kind"] == "Ingress")
        auth_paths = [p["path"] for p in oauth_ingress["spec"]["rules"][0]["http"]["paths"]]
        route_annotations = {i["metadata"]["name"]: i["metadata"]["annotations"]
                             for i in generated_routes if i["kind"] == "Ingress"}

        def buffering(annotations):
            return {key.replace("-", "_"): annotations["konghq.com/" + key] == "true"
                    for key in ("request-buffering", "response-buffering")}
        shutil.copytree(ROOT / "plugins/sso/kong/plugins/session-auth", path / "session-auth")
        (path / "session-auth").chmod(0o755)
        for fixture in (path / "session-auth").iterdir():
            fixture.chmod(0o644)
        for filename, content in code["data"].items():
            (path / filename).write_text(content)
        (path / "app.py").write_text('''import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event

uploads = set()
responses = {}

class App(BaseHTTPRequestHandler):
    def reply(self, status, body=b''):
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        remaining = int(self.headers.get('Content-Length', '0'))
        expected = remaining
        if remaining:
            if not self.rfile.read(1):
                return
            uploads.add(self.headers.get('X-Test-Upload'))
            remaining -= 1
        while remaining:
            data = self.rfile.read(min(remaining, 65536))
            if not data:
                return
            remaining -= len(data)
        self.reply(200, str(expected).encode())

    do_PUT = do_POST

    def do_GET(self):
        if self.path.startswith('/observed/upload/'):
            return self.reply(204 if self.path.rsplit('/', 1)[-1] in uploads else 404)
        if self.path.startswith('/observed/response/'):
            return self.reply(204 if self.path.rsplit('/', 1)[-1] in responses else 404)
        if self.path.startswith('/release/'):
            responses[self.path.rsplit('/', 1)[-1]].set()
            return self.reply(204)
        if self.path.startswith('/stream/'):
            _, _, mode, token = self.path.split('/')
            self.send_response(200)
            self.send_header('X-Accel-Buffering', mode)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'x')
            self.wfile.flush()
            gate = responses[token] = Event()
            if gate.wait(timeout=8):
                self.wfile.write(b'y')
            return
        if self.path == '/upstream-idle':
            time.sleep(5)
            return
        status = int(self.path.rsplit("/", 1)[1]) if self.path.startswith("/status/") else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/vnd.test-app+json")
        self.end_headers()
        self.wfile.write(json.dumps(dict(self.headers)).encode() if self.path in ("/echo-headers", "/oauth2/callback") else
                        self.headers.get("X-Infra-Error", "absent").encode() if self.path == "/echo-error-header" else b'{"application":"own response"}')
    def log_message(self, *args): pass
ThreadingHTTPServer(("0.0.0.0", 8080), App).serve_forever()
''')
        (path / "auth.py").write_text('''from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Auth(BaseHTTPRequestHandler):
    def do_GET(self):
        valid = self.path == '/oauth2/auth' and self.headers.get('Cookie', '').strip() == '__Secure-infra_sso=fixture-valid'
        self.send_response(202 if valid else 401)
        if valid:
            self.send_header('X-Auth-Request-User', 'fixture-user')
            self.send_header('X-Auth-Request-Email', 'fixture-user@example.test')
        self.end_headers()
    def log_message(self, *args): pass
ThreadingHTTPServer(('0.0.0.0', 4180), Auth).serve_forever()
''')
        protected_domains = [route["domain"] for route in config["routes"].values() if route["auth"] != "public"]
        public_domain = config["routes"]["knative_public"]["domain"]
        standard_host = "hello-test-ssvc." + config["routes"]["standard_domain"]["domain"]
        private_host = "hello-test-ksvc." + config["routes"]["knative_domain"]["domain"]
        stream_host = "audit-stream." + config["routes"]["knative_nobuffer_domain"]["domain"]
        s3_host = "storage.example.test"
        public_host = "sso-test-ksvc." + public_domain
        public_sso_host = "audit-test-ksvc." + public_domain
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
                        virtual_host(public_host, "app"), virtual_host(public_sso_host, "app"),
                        virtual_host(stream_host, "app"), virtual_host(wildcard, "fallback")]},
                    "http_filters": [{"name": "envoy.filters.http.router", "typed_config": {
                        "@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"}}]}}]}]}],
                                      "clusters": clusters}}
        (path / "envoy.json").write_text(json.dumps(envoy))
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(path / "tls.key"), "-out", str(path / "tls.crt"),
                        "-subj", "/CN=" + wildcard, "-addext", "subjectAltName=DNS:" + wildcard + ",DNS:" + auth_host + "".join(",DNS:*." + domain for domain in protected_domains)],
                       capture_output=True, check=True)
        (path / "tls.key").chmod(0o644)  # Ephemeral fixture key, readable by the Kong container user.
        kong = {"_format_version": "3.0", "services": [
            {"name": "app", "url": "http://app:8080", "plugins": [{"name": "request-transformer", "config": strip_plugin["config"]}], "routes": [
                {"name": "standard", "hosts": [standard_host, s3_host], "paths": ["/"], "strip_path": False,
                 "preserve_host": True, **buffering(route_annotations["standard"])}]},
            {"name": "kourier", "url": "http://kourier:8080", "plugins": [{"name": "request-transformer", "config": strip_plugin["config"]}], "routes": [
                {"name": "public", "hosts": [wildcard], "paths": ["/"], "strip_path": False,
                 "preserve_host": True, **buffering(route_annotations["knative-public"])},
                {"name": "stream", "hosts": [stream_host], "paths": ["/"], "strip_path": False,
                 "preserve_host": True, **buffering(route_annotations["hello-nobuffer"])},
                {"name": "private-existing", "hosts": [private_host], "paths": ["/"], "strip_path": False, "preserve_host": True}]},
            {"name": "public-sso", "url": "http://kourier:8080", "plugins": [
                {"name": "request-transformer", "config": strip_plugin["config"]},
                {"name": "session-auth", "config": next(i for i in oauth_items if i["kind"] == "KongPlugin" and i["metadata"]["name"] == "sso-knative-public")["config"]}], "routes": [
                {"name": "public-sso", "hosts": [public_sso_host], "paths": ["/"], "strip_path": False, "preserve_host": True}]},
            {"name": "oauth", "url": "http://app:8080", "plugins": [
                {"name": "request-transformer", "config": strip_plugin["config"]},
                {"name": "session-auth", "config": oauth_plugin["config"]}], "routes": [
                {"name": "oauth", "hosts": [auth_host], "paths": auth_paths, "strip_path": False,
                 "preserve_host": True, **buffering(oauth_ingress["metadata"]["annotations"])}]},
            {"name": "fallback", "url": "http://route-errors:8080", "plugins": [{"name": reason_plugin["plugin"], "config": reason_plugin["config"]}], "routes": [
                {"name": "fallback", "paths": ["/"], "strip_path": False, "preserve_host": True}]}],
                "certificates": [{"cert": (path / "tls.crt").read_text(), "key": (path / "tls.key").read_text(),
                                  "snis": [{"name": wildcard}, {"name": auth_host}] + [{"name": "*." + domain} for domain in protected_domains]}]}
        generated_services = {i["metadata"]["name"]: i for i in apps + generated_routes
                              if i["kind"] == "Service" and i["apiVersion"] == "v1"}
        for service, name_ in ((kong["services"][0], "hello-ssvc"), (kong["services"][1], "kourier-proxy")):
            annotations = generated_services[name_]["metadata"]["annotations"]
            for key in ("connect-timeout", "write-timeout", "read-timeout"):
                service[key.replace("-", "_")] = int(annotations["konghq.com/" + key])
            service["retries"] = int(annotations["konghq.com/retries"])
        # Derive the protected fallbacks from generated Ingresses, rather than
        # hand-writing a different set of routes than the installer deploys.
        for ingress in generated_routes:
            if ingress["kind"] != "Ingress" or not ingress["metadata"]["name"].startswith("route-errors-"):
                continue
            plugin_names = ingress["metadata"]["annotations"]["konghq.com/plugins"].split(",")
            auth_config = next(i["config"] for i in oauth_items if i["kind"] == "KongPlugin"
                               and i["metadata"]["name"] in plugin_names)
            name_suffix = ingress["metadata"]["name"]
            kong["services"].append({"name": name_suffix, "url": "http://route-errors:8080", "plugins": [
                {"name": reason_plugin["plugin"], "config": reason_plugin["config"]},
                {"name": "session-auth", "config": auth_config}], "routes": [
                    {"name": name_suffix + str(index), "hosts": [rule["host"]], "paths": ["/"],
                     "strip_path": False, "preserve_host": True}
                    for index, rule in enumerate(ingress["spec"]["rules"])]})
            # A precise application route must win over the protected wildcard.
            domain = ingress["spec"]["rules"][0]["host"]
            kong["services"].append({"name": "app-" + name_suffix, "url": "http://app:8080", "plugins": [
                {"name": "request-transformer", "config": strip_plugin["config"]},
                {"name": "session-auth", "config": auth_config}], "routes": [
                    {"name": "app-" + name_suffix, "hosts": ["audit-existing." + domain], "paths": ["/"],
                     "strip_path": False, "preserve_host": True}]})
        # KIC groups routes with the same Kubernetes Service+port into one
        # Kong Service; Ingress plugin annotations are attached to Routes.
        merged_errors = {"name": "fallback", "url": "http://route-errors:8080", "routes": []}
        remaining = []
        for service in kong["services"]:
            if service["url"] == merged_errors["url"]:
                for route in service["routes"]:
                    route["plugins"] = service.get("plugins", [])
                    merged_errors["routes"].append(route)
            else:
                remaining.append(service)
        kong["services"] = remaining + [merged_errors]
        (path / "kong.yaml").write_text(yaml.safe_dump(kong))
        before = copy.deepcopy(kong)
        before["services"] = [service for service in before["services"]
                              if not service["name"].startswith("app-route-errors-")]
        for service in before["services"]:
            if service["name"] == "fallback":
                service["routes"] = [route for route in service["routes"]
                                     if not route["name"].startswith("route-errors-")]
        (path / "kong-before.yaml").write_text(yaml.safe_dump(before))
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
            start("auth", "python:3.13-alpine", ("python3", "/fixtures/auth.py"),
                  ("--network-alias", "oauth2-proxy.test.svc.cluster.local"))
            start("kourier", "envoyproxy/envoy:v1.34-latest", ("-c", "/fixtures/envoy.json", "--concurrency", "1"))
            gateway = start("kong", "kong:3.9.1", flags=(
                "-p", "127.0.0.1::8000", "-p", "127.0.0.1::8443",
                "-e", "KONG_DATABASE=off", "-e", "KONG_KIC=on", "-e", "KONG_DECLARATIVE_CONFIG=/fixtures/kong-before.yaml",
                "-e", "KONG_PROXY_LISTEN=0.0.0.0:8000,0.0.0.0:8443 ssl", "-e", "KONG_ADMIN_LISTEN=127.0.0.1:8001",
                "-e", "KONG_NGINX_WORKER_PROCESSES=1", "-e", "KONG_PLUGINS=bundled,session-auth",
                "-e", "KONG_LUA_PACKAGE_PATH=" + lua_path,
                *limits_flags,
                "--mount", f"type=bind,src={path / 'upload-limits.conf'},dst=/etc/kong/infra/upload-limits.conf,readonly",
                "--mount", f"type=bind,src={path / 'kong.lua'},dst=/etc/kong/infra/kong.lua,readonly",
                "--mount", f"type=bind,src={path / 'session-auth'},dst=/opt/kong/plugins/session-auth,readonly"))
            http_port = int(docker("port", gateway, "8000/tcp").rsplit(":", 1)[1])
            tls_port = int(docker("port", gateway, "8443/tcp").rsplit(":", 1)[1])

            def request(host, uri="/", context=None, sni=None, headers=None, method="GET", body=None):
                port = tls_port if context is not None else http_port
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                if context is not None:
                    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                    try:
                        conn.sock = context.wrap_socket(sock, server_hostname=(host if sni is None else sni or None))
                    except Exception:
                        sock.close()
                        raise
                try:
                    conn.request(method, uri, body=body, headers={"Host": host, **(headers or {})})
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
            # Reproduce installing new protected fallbacks into an already
            # running gateway, as KIC does via the DB-less Admin API.
            docker("exec", gateway, "resty", "-e", """
local http = require 'resty.http'
local file = assert(io.open('/fixtures/kong.yaml'))
local config = file:read('*a'); file:close()
local res, err = http.new():request_uri('http://127.0.0.1:8001/config', {
  method = 'POST', body = config, headers = {['Content-Type'] = 'application/yaml'}
})
assert(res, err)
assert(res.status == 201, res.body)
""")
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
            for domain in protected_domains:
                for prefix in ("audit-existing", "audit-missing"):
                    host = prefix + "." + domain
                    status, _, headers = request(host, context=verified)
                    assert status == 302, (host, "anonymous", status, headers)
                    assert headers["Location"].startswith("https://" + auth_host + "/oauth2/start?rd=")
                    status, body, _ = request(host, context=verified, headers={"Cookie": "__Secure-infra_sso=fixture-valid"})
                    expected = 200 if prefix == "audit-existing" else 421
                    assert status == expected, (host, "authenticated", status, expected, body)
                    if expected == 421:
                        assert json.loads(body)["code"] == "unknown_host"
                for uri in ("/api/resource", "/upload"):
                    assert request("audit-missing." + domain, uri, context=verified)[0] == 401
            for uri in ("/", "/missing", "/oauth2/auth"):
                status, body, _ = request(auth_host, uri, context=verified)
                assert status == 404 and json.loads(body) == {"error": "path_not_found"}, (uri, status, body)
            for uri in ("/oauth2/start", "/oauth2/callback", "/oauth2/sign_in", "/oauth2/sign_out"):
                assert request(auth_host, uri, context=verified)[0] == 200, uri
            assert request(unknown_app, context=verified)[0] == 421
            # A handshake must fail even when the client skips verification, so a
            # mismatched or default certificate cannot mask a missing SNI guard.
            diagnostic = ssl._create_unverified_context()
            for sni in (invalid_domain, "bad_label." + public_domain, "nested.app." + public_domain, ""):
                try:
                    request(public_host, context=diagnostic, sni=sni)
                except ssl.SSLError as error:
                    assert error.reason == "TLSV1_ALERT_INTERNAL_ERROR", (sni, error)
                else:
                    raise AssertionError(f"Expected ClientHello rejection for SNI {sni!r}")
            assert request(invalid_domain, context=verified, sni=public_host)[0] == 421
            # External requests must not select Knative's internal probe routes.
            spoofed = {"K-Original-Host": "attacker.invalid", "K-Proxy-Request": "activator",
                       "K-Network-Probe": "probe", "K-Network-Hash": "override",
                       "K-Kubelet-Probe": "queue", "Knative-Serving-Revision": "private-revision",
                       "Knative-Serving-Namespace": "private-namespace", "X-Infra-Error": "unknown_host"}
            for host in (unknown_app, invalid_domain):
                status, _, headers = request(host, headers=spoofed)
                assert status == 421 and not any(k.lower() == "k-network-hash" for k in headers), headers
            # SSO off/on public routes, standard apps, and OAuth all share the boundary.
            for host, uri, tls in ((standard_host, "/echo-headers", None), (public_host, "/echo-headers", None),
                                   (public_sso_host, "/echo-headers", verified), (auth_host, "/oauth2/callback", verified)):
                status, body, _ = request(host, uri, context=tls, headers=spoofed)
                echoed = {k.lower(): v for k, v in json.loads(body).items()}
                assert status == 200 and echoed.get("host") == host, (host, status, echoed)
                assert not set(k.lower() for k in spoofed).intersection(echoed), echoed
            # Real controller probes go directly to Kourier, bypassing external Kong.
            # The returned fallback hash must not impersonate a new app's hash.
            probe = """import http.client,json
c=http.client.HTTPConnection('kourier',8080,timeout=5)
c.request('GET','/',headers={'Host':%r,'K-Network-Probe':'probe','K-Network-Hash':'override'})
r=c.getresponse()
print(json.dumps([r.status,r.getheader('K-Network-Hash')]))
r.read();c.close()
""" % unknown_app
            internal = json.loads(docker("exec", name + "-app", "python3", "-c", probe))
            assert internal == [200, fallback_hash], internal
            status, body, _ = request(public_host, "/echo-error-header", headers={"X-Infra-Error": "unknown_host"})
            assert status == 200 and body == b"absent", "Client error hint reached an application"
            status, _, _ = request(invalid_domain, headers={"X-Infra-Error": "path_not_found", "K-Network-Probe": "probe", "K-Network-Hash": "a" * 64})
            assert status == 421, "Client headers changed Kong fallback classification"

            def wait_observed(kind, token):
                deadline = time.monotonic() + 2
                while request(standard_host, f"/observed/{kind}/{token}")[0] != 204:
                    assert time.monotonic() < deadline, ("upstream did not receive the first byte", kind, token)
                    time.sleep(0.025)

            def partial_upload(host, uri, token, method="POST", length=2):
                conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=6)
                try:
                    conn.putrequest(method, uri, skip_host=True)
                    conn.putheader("Host", host)
                    conn.putheader("Content-Length", str(length))
                    conn.putheader("X-Test-Upload", token)
                    conn.endheaders(b"x")
                except BaseException:
                    conn.close()
                    raise
                return conn

            for host in (standard_host, stream_host):
                token = uuid.uuid4().hex
                with closing(partial_upload(host, "/upload", token)) as conn:
                    wait_observed("upload", token)
                    conn.send(b"y")
                    response = conn.getresponse()
                    assert (response.status, response.read()) == (200, b"2"), host
                for mode in ("yes", "no"):
                    token = uuid.uuid4().hex
                    received = Event()

                    def read_stream():
                        with closing(http.client.HTTPConnection("127.0.0.1", http_port, timeout=6)) as conn:
                            conn.request("GET", f"/stream/{mode}/{token}", headers={"Host": host})
                            response = conn.getresponse()
                            first = response.read(1)
                            if first:
                                received.set()
                            return response.status, first + response.read()

                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(read_stream)
                        wait_observed("response", token)
                        try:
                            early = received.wait(0.35 if mode == "yes" else 1.25)
                            assert early == (mode == "no"), (host, mode, early)
                        finally:
                            assert request(standard_host, f"/release/{token}")[0] == 204
                        assert future.result(timeout=6) == (200, b"xy"), (host, mode)

            limits = config["upload_limits"]
            with closing(partial_upload(standard_host, "/upload", "oversize",
                                        length=limits["max_body_bytes"] + 1)) as conn:
                assert conn.getresponse().status == 413
            with closing(http.client.HTTPConnection("127.0.0.1", http_port, timeout=6)) as conn:
                conn.putrequest("POST", "/upload", skip_host=True)
                conn.putheader("Host", standard_host)
                conn.putheader("Transfer-Encoding", "chunked")
                conn.endheaders(f'{limits["max_body_bytes"] + 1:x}\r\nx'.encode())
                assert conn.getresponse().status == 413
            with ExitStack() as uploads:
                for index in range(limits["max_concurrent_upload_requests"]):
                    # Arbitrary paths and S3 hosts share the same body-request limit.
                    host, uri, method = (s3_host, "/bucket/key?partNumber=1&uploadId=fixture", "PUT") if index == 0 else (
                        stream_host, "/arbitrary/file", "POST")
                    token = f"concurrent-{index}"
                    uploads.enter_context(closing(partial_upload(host, uri, token, method=method)))
                    wait_observed("upload", token)
                assert request(standard_host, "/upload", method="POST", body=b"x")[0] == 429
                assert request(s3_host, "/bucket/key", method="PUT", body=b"x")[0] == 429
                assert request(standard_host, "/api/update", method="POST", body=b'{"value":1}',
                               headers={"Content-Type": "application/json"})[0] == 429
                assert request(public_host, "/any-path", method="POST", body=b"x")[0] == 429
                with closing(http.client.HTTPConnection("127.0.0.1", http_port, timeout=6)) as conn:
                    conn.request("POST", "/another-path", body=b"1\r\nx\r\n0\r\n\r\n",
                                 headers={"Host": standard_host, "Transfer-Encoding": "chunked"})
                    assert conn.getresponse().status == 429
                assert request(standard_host, "/api/update", method="POST", body=b"")[0] == 200
                assert request(standard_host, "/api/update", method="POST", headers={"Content-Length": "000"})[0] == 200
                assert request(auth_host, "/oauth2/callback", context=verified)[0] == 200
            deadline = time.monotonic() + 2
            while True:
                status, body, _ = request(standard_host, "/upload", method="POST", body=b"x")
                if status == 200:
                    assert body == b"1"
                    break
                assert status == 429, (status, body)
                assert time.monotonic() < deadline, "client disconnect did not release the upload slots"
                time.sleep(0.025)

            started = time.monotonic()
            with closing(partial_upload(standard_host, "/upload", "idle")) as conn:
                wait_observed("upload", "idle")
                try:
                    assert conn.getresponse().status == 408
                except http.client.RemoteDisconnected:
                    # NGINX may close a stalled body stream without sending a response.
                    pass
            assert limits["client_body_idle_timeout_seconds"] - 0.5 <= time.monotonic() - started < 6
            assert request(standard_host, "/upstream-idle")[0] == 504

            # An unavailable renderer must not intercept responses from existing apps.
            docker("stop", name + "-route-errors")
            for host in (standard_host, public_host):
                for code in (200, 404):
                    uri = "/" if code == 200 else "/status/404"
                    status, body, headers = request(host, uri)
                    assert status == code and body == b'{"application":"own response"}'
                    assert headers["Content-Type"] == "application/vnd.test-app+json"
            print("Kong/Envoy routing, auth, TLS, streaming and upload limits: passed")
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
