"""Independent apply/wait phases share the full installer without unrelated work."""
import json
import re
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import infra
import sso
import tls


class SetupPhases(unittest.TestCase):
    def test_reset_remnants_and_unchanged_host_config_allow_reinstallation(self):
        root = Path(self.tmp.name) / 'host'
        (root / 'var/lib/k0s/empty').mkdir(parents=True)
        (root / 'etc/k0s').mkdir(parents=True)
        (root / 'etc/k0s/k0s.yaml').write_bytes((ROOT / 'configs/kube/k0s.yaml').read_bytes())
        self.assertEqual(infra.existing_k0s_paths(root), [])

    def test_real_cluster_data_and_unknown_config_still_block_reinstallation(self):
        root = Path(self.tmp.name) / 'host'
        (root / 'var/lib/k0s/etcd').mkdir(parents=True)
        (root / 'var/lib/k0s/etcd/data').write_text('cluster-data')
        (root / 'etc/k0s').mkdir(parents=True)
        (root / 'etc/k0s/k0s.yaml').write_text('custom-config')
        self.assertEqual(infra.existing_k0s_paths(root), [root / 'var/lib/k0s', root / 'etc/k0s'])
        self.assertEqual((root / 'var/lib/k0s/etcd/data').read_text(), 'cluster-data')

    def test_reset_remnant_symlinks_are_not_treated_as_empty(self):
        root = Path(self.tmp.name) / 'host'
        (root / 'etc').mkdir(parents=True)
        (root / 'etc/k0s').symlink_to(root / 'missing', target_is_directory=True)
        self.assertEqual(infra.existing_k0s_paths(root), [root / 'etc/k0s'])

    def test_make_separates_privileged_host_operations_from_user_deployment(self):
        for target in ('up', 'apply-secret-google-sso', 'restart-deployment-oauth2-proxy', 'wait-deployment-oauth2-proxy', 'install-helm'):
            result = subprocess.run(['make', '-n', target], cwd=ROOT, check=True, capture_output=True, text=True)
            self.assertNotIn('sudo', result.stdout)
            self.assertNotIn('k0s kubectl', result.stdout)
        for target, action in (('install-k0s', 'install'), ('start-k0s', 'start'), ('stop-k0s', 'stop'), ('reset-k0s', 'reset')):
            result = subprocess.run(['make', '-n', target], cwd=ROOT, check=True, capture_output=True, text=True)
            self.assertIn('sudo ./bin/infra host ' + action, result.stdout)

    def test_failed_k0s_reset_keeps_binary_and_installation_record(self):
        with patch.object(infra.os, 'geteuid', return_value=0), \
             patch.object(infra.platform, 'system', return_value='Linux'), \
             patch.object(infra.Path, 'is_file', return_value=True), \
             patch.object(infra.Path, 'unlink') as unlink, \
             patch.object(infra, 'run', side_effect=[None, subprocess.CalledProcessError(1, ['k0s', 'reset'])]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                infra.host_action('reset')
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ['stop', 'reset'])
        unlink.assert_not_called()

    def test_host_install_does_not_load_environment_or_deploy(self):
        with patch.object(sys, 'argv', ['infra', 'host', 'install']), \
             patch.object(infra, 'host_action') as host, patch.object(infra, 'load') as load, \
             patch.object(infra, 'deploy') as deploy:
            infra.main()
        host.assert_called_once_with('install')
        load.assert_not_called()
        deploy.assert_not_called()

    def test_new_host_up_uses_user_kubectl_without_installing_host(self):
        with patch.object(sys, 'argv', ['infra', 'up', '--config', str(self.path)]), \
             patch.object(infra, 'install_host', side_effect=AssertionError('root host installation')), \
             patch.object(infra, 'Cluster', return_value=self.cluster) as client, \
             patch.object(infra, 'wait_cluster') as wait, patch.object(infra, 'render'), \
             patch.object(infra, 'bootstrap') as bootstrap, patch.object(infra, 'deploy') as deploy, \
             patch.object(sso, 'preflight'), patch.object(tls, 'preflight'):
            infra.main()
        client.assert_called_once_with('kubectl')
        wait.assert_called_once_with(self.cluster)
        bootstrap.assert_called_once()
        deploy.assert_called_once()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'rendered'
        raw = json.loads((ROOT / 'configs/environment/example.json').read_text())
        raw['tls'].update(enabled=True, email='admin@example.test', cloudflare_zone='example.test')
        raw['sso'] = {'enabled': True, 'email_domain': 'example.test'}
        self.path = Path(self.tmp.name) / 'config.json'
        self.path.write_text(json.dumps(raw))
        self.config = infra.load(self.path)
        self.cluster = Mock()
        self.cluster.call.return_value = SimpleNamespace(stdout='')
        def get(name, *args):
            if name == 'configmap/session-auth-code':
                return {'data': {p.name: p.read_text() for p in (ROOT / 'plugins/sso/kong/plugins/session-auth').glob('*.lua')}}
            service = name.removeprefix('ksvc/')
            route = 'knative_nobuffer_domain' if service == 'hello-nobuffer' else 'knative_domain'
            return {'status': {'url': 'http://' + service + '-test-ksvc.' + self.config[route],
                               'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        self.cluster.get.side_effect = get

    def test_all_wait_commands_are_read_only_and_need_no_secret_files(self):
        for action in (a for a in infra.PHASE_ACTIONS if a.startswith('wait-')):
            with self.subTest(action=action), patch.object(infra, 'render', side_effect=AssertionError('render')), \
                 patch.object(sso, 'preflight', side_effect=AssertionError('SSO credentials')), \
                 patch.object(tls, 'read_token_file', side_effect=AssertionError('Cloudflare credentials')):
                self.cluster.reset_mock()
                infra.run_phase(action, self.cluster, self.config, self.output)
                self.assertTrue(self.cluster.call.called)
                for call in self.cluster.call.call_args_list:
                    self.assertIn(call.args[0], ('wait', 'rollout', 'get'))
                    if call.args[0] == 'rollout':
                        self.assertEqual(call.args[1], 'status')
                self.assertFalse(self.output.exists())

    def test_certificate_apply_does_not_install_read_tokens_or_wait(self):
        with patch.object(tls, 'install_manager', side_effect=AssertionError('install')), \
             patch.object(tls, 'ensure_token', side_effect=AssertionError('secret')), \
             patch.object(tls, 'wait_certificates', side_effect=AssertionError('wait')), \
             patch.object(tls, 'migrate_acme_account'), \
             patch.object(tls, 'tls_manager_migration', return_value=False), \
             patch.object(infra, 'apply_admission_config') as apply:
            infra.run_phase('apply-certificates', self.cluster, self.config, self.output)
        self.assertEqual(apply.call_args.args[1], self.output / 'tls.json')
        self.assertFalse(any(c.args[0] in ('wait', 'rollout') for c in self.cluster.call.call_args_list))

    def test_apps_apply_does_not_wait_or_install_other_components(self):
        with patch.object(infra, 'wait_apps', side_effect=AssertionError('wait')), \
             patch.object(tls, 'prepare', side_effect=AssertionError('certificates')), \
             patch.object(sso, 'install', side_effect=AssertionError('SSO')):
            infra.run_phase('apply-apps', self.cluster, self.config, self.output)
        applies = [c.args for c in self.cluster.call.call_args_list if c.args[0] == 'apply']
        self.assertEqual(applies, [('apply', '-f', self.output / 'apps.yaml')])
        self.assertFalse(any(c.args[0] in ('wait', 'rollout') for c in self.cluster.call.call_args_list))

    def test_sso_apply_disables_readiness_wait(self):
        with patch.object(sso, 'preflight'), patch.object(sso, 'install') as install:
            infra.run_phase('apply-sso', self.cluster, self.config, self.output)
        self.assertEqual(install.call_args.kwargs, {'wait': False})

    def test_sso_installer_can_apply_and_restart_without_waiting(self):
        infra.render(self.config, self.output)
        with patch.object(sso, 'wait_ready', side_effect=AssertionError('wait')):
            sso.install(self.cluster, self.config, self.output, wait=False)
        self.assertIn(('rollout', 'restart', 'deployment/oauth2-proxy', '-n', 'test'),
                      [c.args for c in self.cluster.call.call_args_list])
        self.assertFalse(any(c.args[:2] == ('rollout', 'status') for c in self.cluster.call.call_args_list))

    def test_plugin_installs_only_the_selected_component(self):
        for plugin in ('knative', 'cert-manager', 'kong'):
            with self.subTest(plugin=plugin), patch.object(infra, 'install_knative') as knative, \
                 patch.object(tls, 'install_manager') as certs, \
                 patch.object(infra, 'install_kic_prerequisites') as kong:
                infra.run_phase('plugin', self.cluster, self.config, self.output, plugin)
                self.assertEqual([knative.called, certs.called, kong.called],
                                 [plugin == name for name in ('knative', 'cert-manager', 'kong')])

    def test_full_knative_bootstrap_keeps_install_apply_wait_order(self):
        calls = []
        with patch.object(infra, 'install_knative', side_effect=lambda *_: calls.append('install')), \
             patch.object(infra, 'apply_config_knative', side_effect=lambda *_: calls.append('apply')), \
             patch.object(infra, 'wait_knative', side_effect=lambda *_: calls.append('wait')):
            infra.bootstrap(self.cluster, self.output, self.config)
        self.assertEqual(calls, ['install', 'apply', 'wait'])

    def test_full_tls_prepare_keeps_secret_install_apply_wait_order(self):
        calls = []
        with patch.object(tls, 'check_port'), \
             patch.object(tls, 'ensure_token', side_effect=lambda *_: calls.append('secret')), \
             patch.object(tls, 'install_manager', side_effect=lambda *_: calls.append('install')), \
             patch.object(tls, 'apply_certificates', side_effect=lambda *_: calls.append('apply')), \
             patch.object(tls, 'wait_certificates', side_effect=lambda *_: calls.append('wait')):
            tls.prepare(self.cluster, self.config, self.output, Mock(), Mock())
        self.assertEqual(calls, ['secret', 'install', 'apply', 'wait'])

    def test_existing_knative_install_and_config_are_not_overwritten(self):
        self.config['mode'] = 'existing'
        for action, plugin in [('plugin', 'knative'), ('apply-config-knative', None)]:
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'existing'):
                infra.run_phase(action, self.cluster, self.config, self.output, plugin)
        self.cluster.call.assert_not_called()

    def test_individual_kong_phase_cannot_toggle_sso_or_cut_over_legacy(self):
        for action in ('apply-kong', 'apply-routes'):
            with self.subTest(action=action), patch.object(infra, 'find_legacy_gateway', return_value={}):
                self.cluster.call.side_effect = lambda *a, **k: SimpleNamespace(stdout=json.dumps(
                    {'metadata': {'labels': infra.OWNER}} if a[:2] == ('get', 'namespace') else
                    {'spec': {'template': {}}}))
                with self.assertRaisesRegex(ValueError, 'SSO'):
                    infra.run_phase(action, self.cluster, self.config, self.output)

    def test_first_kong_apply_is_possible_but_routes_require_a_deployment(self):
        with patch.object(infra, 'find_legacy_gateway', return_value={}):
            infra.run_phase('apply-kong', self.cluster, self.config, self.output)
            applies = [c.args for c in self.cluster.call.call_args_list if c.args[0] == 'apply']
            self.assertEqual(applies, [('apply', '-f', self.output / 'kong.yaml')])
            self.cluster.reset_mock()
            with self.assertRaisesRegex(ValueError, 'apply-kong'):
                infra.run_phase('apply-routes', self.cluster, self.config, self.output)
            self.assertFalse(any(c.args[0] == 'apply' for c in self.cluster.call.call_args_list))
            with patch.object(infra, 'find_legacy_gateway', return_value={'metadata': {'name': 'kong'}}):
                for action in ('apply-kong', 'apply-routes'):
                    with self.assertRaisesRegex(ValueError, '旧Kong'):
                        infra.run_phase(action, self.cluster, self.config, self.output)

    def test_cli_dispatches_individual_commands_without_global_preflight(self):
        for args in (['wait-certificates'], ['apply-apps'], ['plugin', 'install', 'knative']):
            with self.subTest(args=args), patch.object(sys, 'argv', ['infra', *args, '--config', str(self.path)]), \
                 patch.object(infra, 'run_phase') as phase, \
                 patch.object(sso, 'preflight', side_effect=AssertionError('SSO preflight')), \
                 patch.object(tls, 'preflight', side_effect=AssertionError('TLS preflight')):
                infra.main()
                self.assertEqual(phase.call_args.args[0], args[0])

    def test_sso_routes_wait_for_the_exact_kong_rollout(self):
        infra.render(self.config, self.output)
        code = re.search(r'sso-code: "([a-f0-9]+)"', (self.output / 'kong.yaml').read_text()).group(1)
        current = {'metadata': {'generation': 7}, 'spec': {'replicas': 1, 'template': {
            'metadata': {'annotations': {'infra.sasano8.dev/sso-code': code}}}},
            'status': {'observedGeneration': 6, 'replicas': 1, 'updatedReplicas': 1, 'availableReplicas': 1}}
        def call(*args, **kwargs):
            resource = current if args[:2] == ('get', 'deployment/kong') else {'metadata': {'labels': infra.OWNER}}
            return SimpleNamespace(stdout=json.dumps(resource))
        self.cluster.call.side_effect = call
        with patch.object(infra, 'find_legacy_gateway', return_value={}):
            with self.assertRaisesRegex(ValueError, 'rollout'):
                infra.run_phase('apply-routes', self.cluster, self.config, self.output)
            current['status'].update(observedGeneration=7, replicas=2)
            with self.assertRaisesRegex(ValueError, 'rollout'):
                infra.run_phase('apply-routes', self.cluster, self.config, self.output)
            current['status']['replicas'] = 1
            current['spec']['template']['metadata']['annotations']['infra.sasano8.dev/sso-code'] = 'old-code'
            with self.assertRaisesRegex(ValueError, 'apply-kong'):
                infra.run_phase('apply-routes', self.cluster, self.config, self.output)
            current['spec']['template']['metadata']['annotations']['infra.sasano8.dev/sso-code'] = code
            infra.run_phase('apply-routes', self.cluster, self.config, self.output)
        self.assertEqual([c.args for c in self.cluster.call.call_args_list if c.args[0] == 'apply'],
                         [('apply', '-f', self.output / 'routes.yaml')])

    def test_inventory_is_recorded_only_on_explicit_request(self):
        with patch.object(infra, 'render', side_effect=AssertionError('render')), \
             patch.object(infra.inventory, 'record') as record:
            infra.run_phase('record-inventory', self.cluster, self.config, self.output)
        record.assert_called_once_with(self.cluster, self.config)
        self.cluster.call.assert_not_called()

    def test_kong_requires_current_sso_code_before_applying(self):
        self.cluster.get.return_value = {'data': {'handler.lua': 'stale-code'}}
        self.cluster.get.side_effect = None
        with patch.object(infra, 'find_legacy_gateway', return_value={}):
            with self.assertRaisesRegex(ValueError, 'apply-sso'):
                infra.run_phase('apply-kong', self.cluster, self.config, self.output)
        self.assertFalse(any(c.args[0] == 'apply' for c in self.cluster.call.call_args_list))
