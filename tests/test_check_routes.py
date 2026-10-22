"""Check the public go-httpbin response contract without a cluster."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "check_routes", Path(__file__).resolve().parents[1] / "scripts/check-routes.py")
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)


class ResponseContract(unittest.TestCase):
    host = "hello-test-ksvc.knative.example.com"

    def body(self, host=None, url=None):
        return json.dumps({"headers": {"Host": [host or self.host]},
                           "url": url or "http://" + self.host + "/get"}).encode()

    def test_preserved_host(self):
        routes.validate_response(200, self.body(), self.host)

    def test_rewritten_host_or_url_is_rejected(self):
        for body in (self.body(host="internal.svc"), self.body(url="http://internal.svc/get"),
                     self.body(url="http://" + self.host + "/")):
            with self.subTest(body=body), self.assertRaises(RuntimeError):
                routes.validate_response(200, body, self.host)

    def test_echo_html_and_malformed_json_are_rejected(self):
        for body in (b"standard-service", b"<html>login</html>", b"[]", b"null",
                     b'{"headers": []}', b'{"headers": {}, "url": null}'):
            with self.subTest(body=body), self.assertRaises((RuntimeError, ValueError)):
                routes.validate_response(200, body, self.host)

    def test_errors_and_oversized_responses_are_rejected(self):
        for status, body in ((302, self.body()), (503, self.body()), (200, b" " * 65537)):
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                routes.validate_response(status, body, self.host)
