import json
import io
import unittest
import time
from types import SimpleNamespace
from dashboard import Store, build_view, check_metadata, demo_snapshot, handler_for
from reference_bridge import SnapshotError


class ViewTests(unittest.TestCase):
    def test_fifteen_percent_reserve_boundary(self):
        packet = demo_snapshot(100)
        window = packet['result']['accounts'][0]['usage']['primary']
        window['used_percent'] = 84
        self.assertEqual(build_view(packet, now=100)['action'], 'keep')
        window['used_percent'] = 85
        view = build_view(packet, now=100)
        self.assertEqual(view['action'], 'handoff')
        self.assertEqual(view['settings']['reserve'], 15)
        self.assertEqual(build_view(packet, {'reserve':5}, now=100)['threshold'], 95)

    def test_demo_is_explicit_and_execution_disabled(self):
        view = build_view(demo_snapshot(100), now=100, demo=True)
        self.assertTrue(view['demo'])
        self.assertFalse(view['execution_enabled'])
        self.assertEqual(view['action'], 'handoff')
        self.assertEqual(view['target'], 'demo-4')

    def test_keep_current_even_if_another_account_has_more_quota(self):
        packet = demo_snapshot(100)
        packet['result']['accounts'][0]['usage']['primary']['used_percent'] = 50
        self.assertEqual(build_view(packet, now=100)['action'], 'keep')

    def test_reset_priority_never_triggers_early_rotation(self):
        packet=demo_snapshot(100)
        active=packet['result']['accounts'][0]
        active['usage']['primary']['used_percent']=84
        active['usage']['secondary']['used_percent']=20
        for row in packet['result']['accounts'][1:]:
            row['usage']['primary'].update(used_percent=0,resets_at=101)
            row['usage']['secondary'].update(used_percent=0,resets_at=101)
        view=build_view(packet,now=100)
        self.assertEqual(view['action'],'keep')
        self.assertIsNone(view['target'])
        active['usage']['secondary']['used_percent']=85
        self.assertEqual(build_view(packet,now=100)['action'],'handoff')

    def test_hysteresis_excludes_account_near_switch_threshold(self):
        packet = demo_snapshot(100)
        for a in packet['result']['accounts'][1:]:
            a['usage']['primary']['used_percent'] = 85
        self.assertEqual(build_view(packet, now=100)['action'], 'pause')

    def test_toggle_excludes_candidate(self):
        view = build_view(demo_snapshot(100), {'disabled':['demo-4']}, now=100)
        self.assertEqual(view['target'], 'demo-2')

    def test_stale_data_visible_but_not_selectable(self):
        view = build_view(demo_snapshot(100), now=170)
        self.assertTrue(view['stale'])
        self.assertEqual(len(view['accounts']), 4)
        self.assertEqual(view['action'], 'pause')

    def test_reset_bonus_scales_with_remaining_quota(self):
        packet = demo_snapshot(100)
        a = packet['result']['accounts'][1]
        a['usage']['primary']['resets_at'] = 1000
        view = build_view(packet, {'strategy':'reset_first'}, now=100)
        candidate = next(c for c in view['accounts'] if c['id']=='demo-2')
        self.assertGreater(candidate['reset_bonus'], 0)
        self.assertLessEqual(candidate['reset_bonus'], 25)
        a['usage']['secondary']['used_percent'] = 65
        view = build_view(packet, {'strategy':'reset_first'}, now=100)
        self.assertLess(next(c for c in view['accounts'] if c['id']=='demo-2')['reset_bonus'], candidate['reset_bonus'])

    def test_unknown_token_counts_not_fabricated(self):
        self.assertNotIn('tokens', build_view(demo_snapshot(100), now=100)['accounts'][0])

    def test_credentials_rejected_recursively(self):
        for key in ['access_token', 'OPENAI_API_KEY', 'Authorization', 'tokens']:
            with self.assertRaises(SnapshotError):
                check_metadata({'nested': [{key:'secret'}]})


class HttpTests(unittest.TestCase):
    """Exercise the real HTTP parser/handlers with a memory-backed transport."""
    def setUp(self):
        self.store = Store(demo=True)

    def http(self, path='/', method='GET', data=None, headers=None, authorized=True):
        body = b'' if data is None else json.dumps(data).encode()
        fields = {'Host':'127.0.0.1:8765', 'Content-Length':str(len(body))}
        if method == 'POST' and authorized:
            fields['X-Relay-CSRF'] = self.store.csrf
        fields.update(headers or {})
        wire = (method+' '+path+' HTTP/1.0\r\n'+''.join(k+': '+v+'\r\n' for k,v in fields.items())+'\r\n').encode()+body

        class MemoryConnection:
            def __init__(self):
                self.output = bytearray()
            def makefile(self, *args):
                return io.BytesIO(wire)
            def sendall(self, value):
                self.output.extend(value)
        connection = MemoryConnection()
        handler_for(self.store)(connection, ('127.0.0.1', 9999), SimpleNamespace(server_port=8765))
        head, response = bytes(connection.output).split(b'\r\n\r\n', 1)
        status = int(head.split(b' ')[1])
        return status, head.decode(), response

    def test_ui_and_state_load(self):
        status, headers, body = self.http()
        self.assertEqual(status, 200)
        self.assertIn(b'Codex Relay', body)
        self.assertIn("frame-ancestors 'none'", headers)
        self.assertTrue(json.loads(self.http('/api/state')[2])['demo'])

    def test_proxy_disabled_by_default_and_controls_require_csrf(self):
        self.assertEqual(json.loads(self.http('/api/proxy')[2]),{'enabled':False})
        self.assertEqual(self.http('/api/proxy/checkpoint','POST',{'session':'x'},authorized=False)[0],403)
        self.assertEqual(self.http('/api/proxy/checkpoint','POST',{'session':'x'})[0],409)

    def test_proxy_dashboard_controls_and_cold_rotation_exclusion(self):
        from unittest.mock import Mock
        proxy=Mock()
        proxy.status.return_value={'enabled':True,'sessions':[]}
        proxy.start_checkpoint.return_value={'enabled':True}
        self.store.proxy=proxy
        self.assertTrue(json.loads(self.http('/api/proxy')[2])['enabled'])
        self.assertEqual(self.http('/api/proxy/checkpoint','POST',{'session':'thread','target':'target'})[0],200)
        proxy.start_checkpoint.assert_called_once_with('thread','target')
        self.store.rotation=Mock()
        self.assertEqual(self.http('/api/rotation/start','POST',{'mode':'rotate'})[0],409)
        self.store.rotation.start.assert_not_called()
        self.assertEqual(self.http('/api/rotation/automatic','POST',{'enabled':True})[0],409)
        self.store.rotation.set_automatic.assert_not_called()

    def test_reset_redemption_requires_csrf_and_live_accounts(self):
        data={'identity':'sample-account','operation':'sample-operation','expires_at':2000000000}
        self.assertEqual(self.http('/api/quota/reset','POST',data,authorized=False)[0],403)
        self.assertEqual(self.http('/api/quota/reset','POST',data)[0],409)

    def test_settings_require_csrf(self):
        self.assertEqual(self.http('/api/settings', 'POST', {}, authorized=False)[0], 403)

    def test_cross_origin_denied(self):
        self.assertEqual(self.http('/api/state', headers={'Origin':'https://untrusted.example'})[0], 403)

    def test_host_rebinding_denied(self):
        self.assertEqual(self.http('/api/state', headers={'Host':'untrusted.example'})[0], 403)

    def test_valid_settings_update(self):
        status, _, body = self.http('/api/settings', 'POST', {'reserve':25})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['threshold'], 75)

    def test_credential_import_rejected_and_not_stored(self):
        self.assertEqual(self.http('/api/import', 'POST', {'tokens':{'access_token':'private'}})[0], 422)
        self.assertIsNone(self.store.imported)

    def test_no_account_switch_endpoint(self):
        self.assertEqual(self.http('/api/switch', 'POST', {})[0], 404)

    def test_account_management_is_opt_in(self):
        self.assertEqual(self.http('/api/accounts/switch', 'POST', {'identity':'a'*64})[0], 403)


class ManagedAccountHttpTests(HttpTests):
    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path
        from accounts import Accounts
        from test_accounts import auth
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name).resolve()
        home=root/'home';home.mkdir()
        (home/'auth.json').write_bytes(auth('current'))
        (home/'config.toml').write_text('cli_auth_credentials_store="file"')
        self.store.accounts=Accounts(root/'vault',home,guard=lambda:None)

    def test_account_management_is_opt_in(self):
        # Explicitly enabled; invalid identity must be rejected rather than guessed.
        self.assertEqual(self.http('/api/accounts/switch','POST',{'identity':'a'*64})[0],409)

    def test_capture_and_list_never_return_credentials(self):
        status,_,body=self.http('/api/accounts/capture','POST',{'name':'Current'})
        self.assertEqual(status,200)
        status,_,body=self.http('/api/accounts')
        self.assertEqual(status,200)
        result=json.loads(body)
        self.assertEqual(len(result['profiles']),1)
        self.assertTrue(result['profiles'][0]['selected_on_disk'])
        self.assertNotIn(b'access_token',body)

    def test_account_mutation_requires_csrf(self):
        self.assertEqual(self.http('/api/accounts/capture','POST',{'name':'Current'},authorized=False)[0],403)

    def test_alias_crud_and_email_fallback(self):
        self.assertEqual(self.http('/api/accounts/capture','POST',{})[0],200)
        profile=json.loads(self.http('/api/accounts')[2])['profiles'][0]
        key=profile['id']
        self.assertEqual(profile['display_name'],profile['email'].split('@')[0]+'_unknown')
        for alias in ('Business chính',''):
            self.assertEqual(self.http('/api/accounts/update','POST',{'identity':key,'alias':alias})[0],200)
            row=json.loads(self.http('/api/accounts')[2])['profiles'][0]
            self.assertEqual(row['display_name'],alias or row['email'].split('@')[0]+'_unknown')
        live=(self.store.accounts.home/'auth.json').read_bytes()
        self.assertEqual(self.http('/api/accounts/remove','POST',{'identity':key})[0],200)
        self.assertEqual(json.loads(self.http('/api/accounts')[2])['profiles'],[])
        self.assertEqual((self.store.accounts.home/'auth.json').read_bytes(),live)

    def test_crud_endpoints_require_csrf(self):
        for path in ('update','remove','login/start','login/cancel','export','import'):
            self.assertEqual(self.http('/api/accounts/'+path,'POST',{},authorized=False)[0],403)

    def test_backup_http_roundtrip_and_no_get_export(self):
        self.http('/api/accounts/capture','POST',{'name':'Work'})
        status,headers,body=self.http('/api/accounts/export','POST',{})
        self.assertEqual(status,200)
        self.assertIn('no-store',headers)
        bundle=json.loads(body)
        status,_,body=self.http('/api/accounts/import','POST',{'bundle':bundle})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['skipped'],1)
        self.assertEqual(self.http('/api/accounts/export')[0],404)

    def test_scheduler_wakes_locally_without_forcing_network(self):
        self.store.collector=SimpleNamespace(next_due=time.time()+300)
        self.assertLessEqual(self.store.poll_interval(),30)
        self.store.collector.next_due=time.time()-1
        self.assertEqual(self.store.poll_interval(),1)

    def test_targeted_refresh_validates_profile_and_csrf(self):
        self.http('/api/accounts/capture','POST',{})
        self.store.force_refresh=False
        key=self.store.accounts.list()['profiles'][0]['id']
        self.assertEqual(self.http('/api/refresh','POST',{'identity':key},authorized=False)[0],403)
        self.assertEqual(self.http('/api/refresh','POST',{'identity':'unknown'})[0],422)
        self.assertEqual(self.http('/api/refresh','POST',{'identity':key})[0],200)
        self.assertEqual(self.store.pending_refresh,{key})
        self.assertFalse(self.store.force_refresh)

    def test_rotation_routes_require_csrf_and_live_service(self):
        for path in ('settings','start','cancel','recovery','workflow','names'):
            self.assertEqual(self.http('/api/rotation/'+path,'POST',{},authorized=False)[0],403)
            self.assertEqual(self.http('/api/rotation/'+path,'POST',{})[0],409)

    def test_account_mutation_blocked_while_rotating(self):
        self.store.rotation=SimpleNamespace(status=lambda:{'busy':True})
        self.assertEqual(self.http('/api/accounts/capture','POST',{})[0],409)


if __name__ == '__main__':
    unittest.main()
