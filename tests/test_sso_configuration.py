"""Interactive SSO file updates preserve sessions and apply only owned Secrets."""
import base64
import getpass
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
import sso


class SSOConfiguration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.secret = self.directory / 'secrets' / 'google-sso.json'
        raw = json.loads((ROOT / 'configs/environment/example.json').read_text())
        raw['tls'].update(enabled=True, email='admin@example.test', cloudflare_zone='example.test')
        raw['sso'] = {'enabled': True, 'email_domain': 'example.test', 'secret_file': str(self.secret)}
        self.path = self.directory / 'config.json'
        self.path.write_text(json.dumps(raw))
        self.config = infra.load(self.path)
        self.original = dict(zip(sso.KEYS, ['existing-client', 'existing-secret', base64.b64encode(b'x'*32).decode()]))

    def write_original(self):
        self.secret.parent.mkdir(parents=True, exist_ok=True)
        self.secret.write_text(json.dumps(self.original))
        self.secret.chmod(0o600)

    def configure(self, answers, rotate=False):
        output = io.StringIO()
        with patch('getpass.getpass', side_effect=answers), redirect_stdout(output):
            sso.configure_credentials(self.config, rotate_cookie=rotate)
        return output.getvalue()

    def test_new_file_is_private_and_generates_a_valid_cookie_key(self):
        output = self.configure(['new-client', 'new-secret'])
        result = json.loads(self.secret.read_text())
        self.assertEqual(result['GOOGLE_CLIENT_ID'], 'new-client')
        self.assertEqual(result['GOOGLE_CLIENT_SECRET'], 'new-secret')
        self.assertEqual(len(base64.b64decode(result['OAUTH2_PROXY_COOKIE_SECRET'])), 32)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('new-secret', output)
        self.assertNotIn(result['OAUTH2_PROXY_COOKIE_SECRET'], output)
        sso.credentials(self.config)

    def test_blank_inputs_keep_current_values_and_cookie_key(self):
        self.write_original()
        self.configure(['', ''])
        self.assertEqual(json.loads(self.secret.read_text()), self.original)
        self.configure(['changed-client', 'changed-secret'])
        result = json.loads(self.secret.read_text())
        self.assertEqual(result['GOOGLE_CLIENT_SECRET'], 'changed-secret')
        self.assertEqual(result['OAUTH2_PROXY_COOKIE_SECRET'], self.original['OAUTH2_PROXY_COOKIE_SECRET'])

    def test_cookie_rotation_requires_an_explicit_option(self):
        self.write_original()
        self.configure(['', ''], rotate=True)
        result = json.loads(self.secret.read_text())
        self.assertNotEqual(result['OAUTH2_PROXY_COOKIE_SECRET'], self.original['OAUTH2_PROXY_COOKIE_SECRET'])
        self.assertEqual(result['GOOGLE_CLIENT_ID'], self.original['GOOGLE_CLIENT_ID'])

    def test_canceled_or_empty_input_does_not_change_file(self):
        self.write_original()
        with patch('getpass.getpass', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            sso.configure_credentials(self.config)
        self.assertEqual(json.loads(self.secret.read_text()), self.original)
        self.secret.unlink()
        with self.assertRaises(ValueError):
            self.configure(['', ''])
        self.assertFalse(self.secret.exists())

    def test_failed_atomic_replace_preserves_existing_file_and_removes_temp(self):
        self.write_original()
        with patch('os.replace', side_effect=OSError('fixture failure')), self.assertRaises(OSError):
            self.configure(['new-client', 'new-secret'])
        self.assertEqual(json.loads(self.secret.read_text()), self.original)
        self.assertEqual(list(self.secret.parent.iterdir()), [self.secret])

    def test_external_secret_configuration_is_not_replaced_by_local_file(self):
        self.config['sso']['secret_file'] = ''
        with self.assertRaisesRegex(ValueError, 'secret_file'):
            self.configure([])

    def test_echoing_input_fallback_is_rejected_without_changing_the_file(self):
        self.write_original()
        with patch('getpass.getpass', side_effect=getpass.GetPassWarning), self.assertRaisesRegex(ValueError, '対話端末'):
            sso.configure_credentials(self.config)
        self.assertEqual(json.loads(self.secret.read_text()), self.original)

    def test_empty_owner_label_on_existing_secret_cannot_be_overwritten(self):
        self.write_original()
        output = self.directory / 'rendered'
        output.mkdir()
        (output / 'sso.json').write_text('{"items": []}')
        cluster = Mock()
        def call(*args, **kwargs):
            if args[:2] == ('get', 'secret/google-sso'):
                # Kubernetes permits an explicitly empty label value. The old
                # template emitted an empty string for that existing Secret.
                return SimpleNamespace(stdout='present:' if 'present:' in args[-1] else '')
            return SimpleNamespace(stdout='')
        cluster.call.side_effect = call
        with self.assertRaisesRegex(ValueError, '外部管理'):
            sso.install(cluster, self.config, output)
        self.assertFalse(any(c.args[0] == 'apply' for c in cluster.call.call_args_list))

    def test_apply_only_updates_secret_without_restart_or_unrelated_preflight(self):
        self.write_original()
        cluster = Mock()
        cluster.call.return_value = SimpleNamespace(stdout='present:infra-kubernetes')
        with patch.object(sys, 'argv', ['infra', 'apply-secret-google-sso', '--config', str(self.path)]), \
             patch.object(infra, 'Cluster', return_value=cluster), \
             patch.object(infra.tls, 'preflight', side_effect=AssertionError('unrelated Cloudflare')), \
             patch.object(infra, 'deploy', side_effect=AssertionError('unrelated deploy')):
            infra.main()
        self.assertEqual([c.args[0] for c in cluster.call.call_args_list], ['get', 'apply'])
        resource = json.loads(cluster.call.call_args_list[-1].kwargs['input'])
        self.assertEqual(resource['metadata']['name'], 'google-sso')
        self.assertEqual(base64.b64decode(resource['data']['GOOGLE_CLIENT_SECRET']), b'existing-secret')

    def test_configure_cli_is_local_only(self):
        with patch.object(sys, 'argv', ['infra', 'configure-secret-google-sso', '--config', str(self.path)]), \
             patch.object(infra, 'Cluster', side_effect=AssertionError('must not access cluster')), \
             patch('getpass.getpass', side_effect=['new-client', 'new-secret']):
            infra.main()
        self.assertEqual(json.loads(self.secret.read_text())['GOOGLE_CLIENT_ID'], 'new-client')

    def test_foreign_secret_and_api_errors_do_not_expose_credentials(self):
        self.write_original()
        for owner in ('present:', 'present:external-manager'):
            with self.subTest(owner=owner):
                cluster = Mock()
                cluster.call.return_value = SimpleNamespace(stdout=owner)
                with self.assertRaisesRegex(ValueError, '外部管理'):
                    sso.apply_credentials(cluster, self.config, require_file=True)
                self.assertEqual(cluster.call.call_count, 1)
        cluster = Mock()
        cluster.call.side_effect = [SimpleNamespace(stdout='present:infra-kubernetes'),
                                   subprocess.CalledProcessError(1, ['kubectl'], stderr='existing-secret')]
        with self.assertRaises(ValueError) as error:
            sso.apply_credentials(cluster, self.config, require_file=True)
        self.assertNotIn('existing-secret', str(error.exception))

    def test_make_targets_keep_apply_restart_and_wait_separate(self):
        for target, expected, absent in (
            ('configure-secret-google-sso', 'configure-secret-google-sso', 'sudo'),
            ('apply-secret-google-sso', 'apply-secret-google-sso', 'rollout'),
            ('restart-deployment-oauth2-proxy', 'rollout restart', 'rollout status'),
            ('wait-deployment-oauth2-proxy', 'rollout status', 'rollout restart'),
        ):
            with self.subTest(target=target):
                result = subprocess.run(['make', '--no-print-directory', '-n', target], cwd=ROOT,
                                        capture_output=True, text=True, check=True)
                self.assertIn(expected, result.stdout)
                self.assertNotIn(absent, result.stdout)
