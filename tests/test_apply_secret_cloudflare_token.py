"""Token rotation changes only the owned Secret and does not expose its value."""
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import infra
import tls


class CloudflareTokenApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.token = self.path / 'token'
        self.token.write_text('fixture-sensitive-token\n')
        self.token.chmod(0o600)
        raw = json.loads((ROOT / 'configs/environment/example.json').read_text())
        raw['tls'].update(enabled=True, email='admin@example.test', cloudflare_zone='example.test',
                          cloudflare_token_file=str(self.token))
        raw['sso'] = {'enabled': True, 'email_domain': 'example.test'}
        self.config = self.path / 'environment.json'
        self.config.write_text(json.dumps(raw))
        self.cluster = Mock()
        self.cluster.call.return_value = SimpleNamespace(stdout='present:infra-kubernetes:api-token,')

    def execute(self):
        output = io.StringIO()
        argv = ['infra', 'apply-secret-cloudflare-token', '--config', str(self.config), '--kubectl', 'k0s kubectl']
        with patch.object(sys, 'argv', argv), patch.object(infra, 'Cluster', return_value=self.cluster), \
             patch.object(infra.sso, 'preflight', side_effect=AssertionError('SSO must not be queried')), \
             patch.object(infra, 'deploy', side_effect=AssertionError('must not deploy')), redirect_stdout(output):
            infra.main()
        return output.getvalue()

    def test_cli_updates_only_the_owned_secret_and_hides_value(self):
        output = self.execute()
        self.assertNotIn('fixture-sensitive-token', output)
        calls = self.cluster.call.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[:2], ('get', 'secret/cloudflare-api-token'))
        self.assertEqual(calls[1].args[:2], ('apply', '--server-side'))
        manifest = json.loads(calls[1].kwargs['input'])
        self.assertEqual(manifest['metadata']['name'], 'cloudflare-api-token')
        self.assertEqual(manifest['metadata']['namespace'], 'test')
        self.assertEqual(base64.b64decode(manifest['data']['api-token']), b'fixture-sensitive-token')
        self.assertTrue(calls[1].kwargs['capture_output'])

    def test_missing_or_invalid_file_stops_before_cluster_access(self):
        for contents, mode in (('', 0o600), ('bad token', 0o600), ('fixture', 0o644), (None, 0o600)):
            with self.subTest(contents=contents, mode=mode):
                self.cluster.reset_mock()
                if contents is None:
                    self.token.unlink()
                else:
                    self.token.write_text(contents)
                    self.token.chmod(mode)
                with self.assertRaises(ValueError):
                    self.execute()
                self.cluster.call.assert_not_called()

    def test_foreign_secret_is_not_overwritten(self):
        self.cluster.call.return_value = SimpleNamespace(stdout='present:another-owner:api-token,')
        with self.assertRaisesRegex(ValueError, '所有ラベル'):
            self.execute()
        self.assertEqual(self.cluster.call.call_count, 1)

    def test_apply_error_does_not_expose_secret_or_api_response(self):
        self.cluster.call.side_effect = [SimpleNamespace(stdout='present:infra-kubernetes:api-token,'),
                                        subprocess.CalledProcessError(1, ['kubectl', 'apply'],
                                                                      stderr='fixture-sensitive-token')]
        with self.assertRaises(ValueError) as error:
            self.execute()
        self.assertNotIn('fixture-sensitive-token', str(error.exception))
