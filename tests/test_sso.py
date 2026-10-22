"""SSO configuration, generated resources, and fail-closed lifecycle checks."""
import base64
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import infra
import sso
import inventory


class SSOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.c = json.loads((ROOT / 'configs/environment/example.json').read_text())
        self.c['tls'].update(enabled=True, email='admin@example.test', cloudflare_zone='example.test')
        self.c['sso'] = {'enabled': True, 'email_domain': 'example.test'}

    def config(self):
        p = self.path / 'config.json'; p.write_text(json.dumps(self.c))
        return infra.load(p)

    def fixture_secret(self):
        p = self.path / 'credentials.json'
        data = dict(zip(sso.KEYS, ('test-client', 'test-secret', base64.b64encode(b'x'*32).decode())))
        p.write_text(json.dumps(data)); p.chmod(0o600)
        self.c['sso']['secret_file'] = str(p)
        return p

    def test_enabled_requires_https_and_allow_domain(self):
        self.c['tls']['enabled'] = False
        with self.assertRaisesRegex(ValueError, 'tls.enabled'): self.config()
        self.c['tls']['enabled'] = True
        self.c['sso']['email_domain'] = '*'
        with self.assertRaisesRegex(ValueError, 'email_domain'): self.config()

    def test_policy_validation(self):
        for key, value in [('enabled', 'true'), ('routes', {'missing':'public'}), ('routes', {'knative_domain':'skip'}),
                           ('api_paths',['/api/']), ('secret_file',None)]:
            with self.subTest(key=key,value=value):
                self.c['sso'] = {'enabled':True,'email_domain':'example.test',key:value}
                with self.assertRaises(ValueError): self.config()

    def test_render_has_auth_for_every_explicit_and_default_route(self):
        c = self.config(); infra.render(c,self.path)
        routes = (self.path/'routes.yaml').read_text()
        self.assertEqual(routes.count('konghq.com/plugins: "sso-'),6)
        kong = (self.path/'kong.yaml').read_text()
        self.assertIn('bundled,session-auth',kong)
        self.assertIn('/opt/kong/plugins/session-auth',kong)
        self.assertIn('$uri $status',kong)
        self.assertNotIn('$request_uri',kong)
        manifest = json.loads((self.path/'sso.json').read_text())
        proxy = next(x for x in manifest['items'] if x['kind']=='Deployment')
        container=proxy['spec']['template']['spec']['containers'][0]
        self.assertIn('--upstream=static://202',container['args'])
        self.assertIn('--redirect-url=/oauth2/callback',container['args'])
        self.assertFalse(any('cookie-domain' in a for a in container['args']))
        self.assertEqual(len(container['env']),3)
        self.assertTrue(all('secretKeyRef' in e['valueFrom'] for e in container['env']))
        callback = next(x for x in manifest['items'] if x['kind']=='Ingress')
        self.assertEqual(len(callback['spec']['rules']),6)
        self.assertTrue(any(x['kind']=='NetworkPolicy' for x in manifest['items']))

    def test_off_rerender_clears_auth_and_does_not_need_secret(self):
        c=self.config();infra.render(c,self.path)
        self.c['sso']={'enabled':False};c=self.config();infra.render(c,self.path)
        self.assertEqual(json.loads((self.path/'sso.json').read_text())['items'],[])
        self.assertEqual((self.path/'routes.yaml').read_text().count('konghq.com/plugins: ""'),6)
        self.assertNotIn('session-auth',(self.path/'kong.yaml').read_text())
        sso.preflight(c,None)

    def test_public_api_policies_do_not_change_buffering(self):
        self.c['sso']['routes']={'standard_domain':'public','knative_nobuffer_domain':'api'}
        c=self.config();infra.render(c,self.path)
        plugins=[x for x in json.loads((self.path/'sso.json').read_text())['items'] if x['kind']=='KongPlugin']
        self.assertEqual(next(p['config']['mode'] for p in plugins if p['metadata']['name']=='sso-standard-domain'),'public')
        self.assertEqual(next(p['config']['mode'] for p in plugins if p['metadata']['name']=='sso-knative-nobuffer-domain'),'api')
        self.assertEqual((self.path/'routes.yaml').read_text().count('konghq.com/request-buffering: "false"'),2)

    def test_secret_file_is_private_valid_and_never_rendered(self):
        p=self.fixture_secret();c=self.config();sso.preflight(c,None);infra.render(c,self.path)
        for artifact in ('sso.json','kong.yaml','routes.yaml'):
            self.assertNotIn('test-secret',(self.path/artifact).read_text())
            self.assertNotIn(base64.b64encode(b'x'*32).decode(),(self.path/artifact).read_text())
        p.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'600'):sso.preflight(c,None)
        p.chmod(0o600);p.write_text('{"invalid":"sensitive-value"}')
        with self.assertRaises(ValueError) as ctx:sso.preflight(c,None)
        self.assertNotIn('sensitive-value',str(ctx.exception))

    def test_preflight_checks_secret_keys_without_printing_values(self):
        class Cluster:
            def call(self,*args,**kwargs):
                self.args=args
                return SimpleNamespace(stdout='\n'.join(sso.KEYS))
        cluster=Cluster();sso.preflight(self.config(),cluster)
        self.assertIn('go-template=', ' '.join(cluster.args))
        self.assertNotIn('json',cluster.args)
        with self.assertRaises(ValueError):sso.preflight(self.config(),None)

    def test_omitting_sso_cannot_silently_disable_installed_sso(self):
        del self.c['sso']
        cluster=SimpleNamespace(call=lambda *a,**k:SimpleNamespace(stdout='ingress/oauth2-proxy'))
        with self.assertRaisesRegex(ValueError,'明示'):sso.preflight(self.config(),cluster)

    def test_external_plan_is_port_aware_and_contains_no_credentials(self):
        self.fixture_secret();self.c['tls']['host_https_port']=9443
        self.c['sso']['routes']={'standard_domain':'public','knative_domain':'api'}
        c=self.config();plan=sso.test_plan(c,'shell')
        self.assertIn(':9443/get',plan);self.assertIn('"$status" = 401',plan)
        self.assertIn('"$status" = 302',plan);self.assertIn('"$status" = 200',plan)
        self.assertNotIn('test-secret',plan);self.assertNotIn('--insecure',plan)
        subprocess.run(['bash','-n'],input=plan,text=True,check=True)

    def test_inventory_reads_sso_identities_without_execution(self):
        identities=inventory.python_identities((ROOT/'scripts/sso.py').read_text(),'scripts/sso.py')
        self.assertTrue(any(x['kind']=='KongPlugin' and x['name']=='sso-oauth-endpoints' for x in identities))
        self.assertTrue(any(x['kind']=='NetworkPolicy' for x in identities))

    @unittest.skipUnless(shutil.which('texlua'),'Lua runtime unavailable')
    def test_actual_lua_handler(self):
        result=subprocess.run(['texlua','tests/session_auth.lua'],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertIn('20 passed',result.stdout)

    def test_deploy_orders_plugin_loading_and_unloading(self):
        for enabled in (False, True):
            self.c['sso']={'enabled':enabled,'email_domain':'example.test'}
            c=self.config(); calls=[]
            class Cluster:
                def get(self,name,*args):
                    if name=='configmap/config-network': return {'data':{'domain-template':infra.TEMPLATE}}
                    name=name.removeprefix('ksvc/')
                    domain=c['knative_nobuffer_domain'] if name=='hello-nobuffer' else c[c['default_routes']['knative']] if name=='hello-default' else c['knative_domain']
                    return {'status':{'url':'http://'+name+'-test-ksvc.'+domain,'conditions':[{'type':'Ready','status':'True'}]}}
                def call(self,*args,**kwargs):
                    calls.append(args)
                    if args[:2]==('get','namespace'):return SimpleNamespace(stdout=json.dumps({'metadata':{'labels':infra.OWNER}}))
                    return SimpleNamespace(stdout='')
            with patch.object(sso,'preflight'),patch.object(sso,'install'),patch.object(infra,'install_kic_prerequisites'), \
                 patch.object(infra,'find_legacy_gateway',return_value={}),patch.object(infra.tls,'prepare'),patch.object(inventory,'record'):
                infra.deploy(Cluster(),c,self.path)
            routes=calls.index(('apply','-f',self.path/'routes.yaml'))
            gateway=calls.index(('apply','-f',self.path/'kong.yaml'))
            self.assertEqual(routes > gateway, enabled)


    def test_internal_knative_diagnostic_forwards_real_service_not_externalname(self):
        cfg=self.path/'config.json';self.config()
        spec=importlib.util.spec_from_file_location('sso_route_check',ROOT/'scripts/check-routes.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module.sys,'argv',['check','--config',str(cfg),'--internal','knative']), \
             patch.object(module.subprocess,'Popen',side_effect=RuntimeError('stop before network')) as start:
            with self.assertRaisesRegex(RuntimeError,'stop before network'):module.main()
        command=start.call_args.args[0]
        self.assertIn('service/kourier',command)
        self.assertIn('kourier-system',command)
        self.assertNotIn('service/kourier-proxy',command)
