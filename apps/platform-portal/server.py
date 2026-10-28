"""Single-port portal and RFC 9457 error renderer; no proxying or body inspection."""
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import re

ERROR_HEADER = 'X-Infra-Error'
PROBLEMS = {
    'unknown_host': (421, 'No published service matches the requested host.'),
    'path_not_found': (404, 'No resource is published at the requested path.'),
}


def problem(status, code, detail):
    return {'type': 'about:blank', 'title': HTTPStatus(status).phrase,
            'status': status, 'detail': detail, 'code': code}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def respond(self):
        reasons = self.headers.get_all(ERROR_HEADER, [])
        path = self.path.partition('?')[0]
        network_hash = self.headers.get('K-Network-Hash', '')
        # Kourier stamps the reason, then overwrites the probe hash with its
        # current Ingress hash. Kong's fallback clears client probe headers.
        probe = (reasons == ['unknown_host'] and self.headers.get('K-Network-Probe') == 'probe'
                 and re.fullmatch(r'[0-9a-f]{64}', network_hash))
        extra = {}
        if probe:
            status, body, content_type = 200, b'', 'text/plain'
            extra['K-Network-Hash'] = network_hash
        elif reasons:
            if reasons != ['unknown_host']:
                status = 400
                data = problem(status, 'invalid_error_reason', 'A supported error reason is required.')
            else:
                code = reasons[0]
                status, detail = PROBLEMS[code]
                data = problem(status, code, detail)
            body = json.dumps(data, separators=(',', ':')).encode() + b'\n'
            content_type = 'application/problem+json'
        elif path == '/' and self.command in ('GET', 'HEAD'):
            status, body, content_type = 200, self.server.index, 'text/html; charset=utf-8'
        else:
            status = 405 if path == '/' else 404
            code = 'method_not_allowed' if status == 405 else 'path_not_found'
            detail = 'Only GET and HEAD are supported here.' if status == 405 else PROBLEMS[code][1]
            body = json.dumps(problem(status, code, detail), separators=(',', ':')).encode() + b'\n'
            content_type = 'application/problem+json'
            if status == 405:
                extra['Allow'] = 'GET, HEAD'
        self.send_response_only(status)
        self.send_header('Date', self.date_time_string())
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        if content_type.startswith('text/html'):
            self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)
        self.close_connection = True

    def __getattr__(self, name):
        if name.startswith('do_'):
            return self.respond
        raise AttributeError(name)

    def log_message(self, *_):
        pass


def make_server(address, index=b'<!doctype html><title>Platform portal</title>'):
    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    server.index = index
    return server


if __name__ == '__main__':
    server = make_server(('0.0.0.0', 8080))
    index = Path('/site/index.html')
    if index.is_file():
        server.index = index.read_bytes()
    server.serve_forever()
