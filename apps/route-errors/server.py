"""Terminal routing errors only; never proxy requests or inspect their bodies."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def respond(self):
        host = self.headers.get("Host", "").lower().split(":", 1)[0]
        network_hash = self.headers.get("K-Network-Hash", "")
        # Echo the actual fallback Ingress hash so probes for a newly created
        # application keep retrying until its more specific route is installed.
        probe = (self.server.layer == "kourier"
                 and self.headers.get("K-Network-Probe") == "probe"
                 and re.fullmatch(r"[0-9a-f]{64}", network_hash))
        if probe:
            status, code = 200, "network_probe"
        elif self.server.layer == "kong" and host in self.server.known_hosts:
            status, code = 404, "path_not_found"
        else:
            status, code = 421, "unknown_host"
        body = json.dumps({"error": code}, separators=(",", ":")).encode() + b"\n"
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if probe:
            self.send_header("K-Network-Hash", network_hash)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.respond
        raise AttributeError(name)

    def log_message(self, *_):
        pass


def make_server(address, layer, config):
    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    server.layer = layer
    server.known_hosts = set(config["known_hosts"])
    return server


if __name__ == "__main__":
    config = json.loads(Path("/code/config.json").read_text())
    kong = make_server(("0.0.0.0", 8081), "kong", config)
    threading.Thread(target=kong.serve_forever, daemon=True).start()
    make_server(("0.0.0.0", 8080), "kourier", config).serve_forever()
