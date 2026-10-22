"""Check the public go-httpbin response contract without a cluster."""
import importlib.util
import json
from pathlib import Path
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

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


class FourRouteCommand(unittest.TestCase):
    def test_all_four_hosts_and_both_zero_recoveries(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch
        root = Path(__file__).resolve().parents[1]
        calls = []
        pod_polls = {}

        class Forward:
            stdout = io.StringIO('Forwarding from 127.0.0.1:18080 -> 80\n')
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

        class Connection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, method, path, headers):
                self.host = headers['Host']
                calls.append((path, self.host))

            def getresponse(self):
                body = json.dumps({'headers': {'Host': [self.host]},
                                   'url': 'http://' + self.host + '/get'}).encode()
                return SimpleNamespace(status=200, getheaders=lambda: [], read=lambda n: body)

            def close(self):
                pass

        def get_pods(command, **kwargs):
            selector = command[command.index('-l') + 1]
            pod_polls[selector] = pod_polls.get(selector, 0) + 1
            pods = [] if pod_polls[selector] == 1 else [
                {'metadata': {'name': 'recovered'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}]
            return SimpleNamespace(stdout=json.dumps({'items': pods}))

        output = io.StringIO()
        with patch.object(routes.sys, 'argv', ['check-routes', '--config', str(root / 'configs/environment/example.json')]), \
                patch.object(routes.subprocess, 'Popen', return_value=Forward()), \
                patch.object(routes.subprocess, 'run', side_effect=get_pods), \
                patch.object(routes.http.client, 'HTTPConnection', Connection), redirect_stdout(output):
            self.assertEqual(routes.main(), 0)
        self.assertIn('6/6 成功', output.getvalue())
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(set(host for path, host in calls)), 4)
        self.assertTrue(all(path == '/get' for path, host in calls))
        self.assertEqual(pod_polls, {'serving.knative.dev/service=hello': 2,
                                     'serving.knative.dev/service=hello-nobuffer': 2})
