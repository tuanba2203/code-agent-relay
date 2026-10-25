import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import accounts
from accounts import Accounts, AccountError, identify


def auth(subject, access='test-access'):
    payload = base64.urlsafe_b64encode(json.dumps({'sub':subject,'email':subject+'@example.com'}).encode()).decode().rstrip('=')
    return json.dumps({'auth_mode':'chatgpt','tokens':{'account_id':'workspace-test',
                       'access_token':access,'id_token':'header.'+payload+'.signature',
                       'refresh_token':'test-refresh'}}).encode()


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root/'home'; self.home.mkdir()
        (self.home/'config.toml').write_text('cli_auth_credentials_store = "file"\n')
        self.old, self.new = auth('old'), auth('new')
        (self.home/'auth.json').write_bytes(self.old)
        self.store = Accounts(self.root/'vault', self.home, guard=lambda:None)
        source = self.root/'new-auth.json'; source.write_bytes(self.new)
        self.target = self.store.capture('Work',source)['id']
        self.env = patch.dict(os.environ, {'CODEX_ACCESS_TOKEN':'','OPENAI_API_KEY':'','CODEX_API_KEY':''})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_switch_changes_only_auth_and_preserves_backup(self):
        result = self.store.switch(self.target)
        self.assertTrue(result['changed'])
        self.assertEqual((self.home/'auth.json').read_bytes(),self.new)
        self.assertEqual((self.home/'auth.json').stat().st_mode & 0o777,0o600)
        self.assertEqual((self.home/'config.toml').read_text(),'cli_auth_credentials_store = "file"\n')
        self.assertEqual(next(self.store.directory.glob('backup-*.auth.json')).read_bytes(),self.old)
        self.assertFalse(self.store.journal.exists())

    def test_live_client_blocks_all_mutation(self):
        def blocked(): raise AccountError('client running')
        self.store.guard = blocked
        with self.assertRaises(AccountError): self.store.switch(self.target)
        self.assertEqual((self.home/'auth.json').read_bytes(),self.old)
        self.assertFalse(self.store.journal.exists())

    def test_keyring_or_implicit_mode_rejected(self):
        for config in ('cli_auth_credentials_store="keyring"','', '[profile]\ncli_auth_credentials_store="file"'):
            (self.home/'config.toml').write_text(config)
            with self.assertRaises(AccountError): self.store.switch(self.target)

    def test_unknown_or_traversal_identity_rejected(self):
        for key in ('../auth', 'a'*64, None):
            with self.assertRaises(AccountError): self.store.switch(key)

    def test_registry_output_contains_no_tokens(self):
        listing = json.dumps(self.store.list())
        self.assertNotIn('access_token',listing)
        self.assertNotIn('refresh_token',listing)
        self.assertNotIn('test-access',listing)

    def test_export_import_preserves_alias_and_active_login(self):
        bundle=self.store.export_bundle()
        other=Accounts(self.root/'other-vault',self.home,guard=lambda:None)
        self.assertEqual(other.import_bundle(bundle)['imported'],1)
        self.assertEqual(other.list()['profiles'][0]['alias'],'Work')
        self.assertEqual((self.home/'auth.json').read_bytes(),self.old)
        saved=other.directory/(self.target+'.auth.json')
        self.assertEqual(saved.stat().st_mode & 0o777,0o600)
        other.update(self.target,'Local alias')
        self.assertEqual(other.import_bundle(bundle)['skipped'],1)
        self.assertEqual(other.list()['profiles'][0]['alias'],'Local alias')

    def test_invalid_import_validates_before_writing(self):
        bundle=self.store.export_bundle()
        bundle['profiles'].append({'alias':'Bad','auth':{}})
        other=Accounts(self.root/'other-vault',self.home,guard=lambda:None)
        with self.assertRaises(AccountError):other.import_bundle(bundle)
        self.assertEqual(other.list()['profiles'],[])

    def test_duplicate_import_is_rejected(self):
        bundle=self.store.export_bundle()
        bundle['profiles']*=2
        with self.assertRaises(AccountError):self.store.import_bundle(bundle)

    def test_backup_blocked_during_recovery(self):
        bundle=self.store.export_bundle()
        self.store.journal.write_text('{}')
        with self.assertRaises(AccountError):self.store.export_bundle()
        with self.assertRaises(AccountError):self.store.import_bundle(bundle)

    def test_tampered_snapshot_identity_rejected(self):
        (self.store.directory/(self.target+'.auth.json')).write_bytes(self.old)
        with self.assertRaises(AccountError): self.store.switch(self.target)
        self.assertEqual((self.home/'auth.json').read_bytes(),self.old)

    def test_newer_outgoing_refresh_is_saved(self):
        fresh = auth('old','fresh-access')
        (self.home/'auth.json').write_bytes(fresh)
        self.store.switch(self.target)
        self.assertEqual((self.store.directory/(identify(fresh)['id']+'.auth.json')).read_bytes(),fresh)

    def test_interrupted_switch_can_restore(self):
        original = accounts.atomic
        def failing(path,data):
            if path.name=='last-switch.json': raise OSError('disk full')
            original(path,data)
        with patch.object(accounts,'atomic',side_effect=failing):
            with self.assertRaises(AccountError): self.store.switch(self.target)
        self.assertTrue(self.store.journal.exists())
        self.assertEqual((self.home/'auth.json').read_bytes(),self.new)
        with self.assertRaises(AccountError): self.store.switch(self.target)
        self.store.restore()
        self.assertEqual((self.home/'auth.json').read_bytes(),self.old)
        self.assertFalse(self.store.journal.exists())

    def test_restore_does_not_overwrite_independent_login(self):
        self.test_interrupted_switch_can_restore()
        self.store.journal.write_text(json.dumps({'backup':next(self.store.directory.glob('backup-*.auth.json')).name,
                                                 'target_digest':'invalid'}))
        independent = auth('independent')
        (self.home/'auth.json').write_bytes(independent)
        with self.assertRaises(AccountError): self.store.restore()
        self.assertEqual((self.home/'auth.json').read_bytes(),independent)

    def test_symlink_auth_rejected(self):
        live=self.home/'auth.json';live.unlink();live.symlink_to(self.root/'new-auth.json')
        with self.assertRaises(AccountError): self.store.switch(self.target)
        self.assertEqual((self.root/'new-auth.json').read_bytes(),self.new)

    def test_duplicate_capture_updates_same_profile(self):
        self.store.capture('Renamed',self.root/'new-auth.json')
        self.assertEqual(len(self.store.list()['profiles']),1)
        self.assertEqual(self.store.list()['profiles'][0]['name'],'Renamed')

    def test_non_file_api_key_mode_not_imported(self):
        with self.assertRaises(AccountError): identify(b'{"auth_mode":"apikey","OPENAI_API_KEY":"test"}')

    def test_process_guard_fails_closed_and_detects_vscode(self):
        with patch('subprocess.run', return_value=type('Result',(),{'stdout':'/Applications/Visual Studio Code.app/Contents/MacOS/Electron\n'})()):
            with self.assertRaises(AccountError): accounts.clients_stopped()
        with patch('subprocess.run',side_effect=OSError()):
            with self.assertRaises(AccountError): accounts.clients_stopped()

    def test_process_guard_ignores_only_exact_vscode_crash_reporter(self):
        reporter='/Applications/Visual Studio Code.app/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler'
        with patch('subprocess.run',return_value=type('Result',(),{'stdout':'  '+reporter+'\n/usr/bin/python3\n'})()):
            accounts.clients_stopped()
        for blocker in (
            '/Applications/Visual Studio Code.app/Contents/MacOS/Electron',
            '/Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper.app/Contents/MacOS/Code Helper',
            '/Applications/Codex.app/Contents/MacOS/Codex',
            reporter.replace('Visual Studio Code.app','Codex.app'),
            reporter.replace('chrome_crashpad_handler','another_helper'),
        ):
            with self.subTest(blocker=blocker):
                with patch('subprocess.run',return_value=type('Result',(),{'stdout':reporter+'\n'+blocker+'\n'})()):
                    with self.assertRaises(AccountError) as caught:
                        accounts.clients_stopped()
                self.assertIn(blocker,str(caught.exception))
                self.assertNotIn(reporter,str(caught.exception))

    def test_process_guard_names_the_actual_blocker(self):
        command='/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'
        with patch('subprocess.run',return_value=type('Result',(),{'stdout':command+'\n/usr/bin/python3\n'})()):
            with self.assertRaisesRegex(AccountError,'ChatGPT.app/Contents/MacOS/ChatGPT'):
                accounts.clients_stopped()
        with patch('subprocess.run',return_value=type('Result',(),{'stdout':'/usr/bin/python3\n/bin/zsh\n'})()):
            accounts.clients_stopped()

    def test_auth_override_env_blocked(self):
        with patch.dict(os.environ,{'CODEX_ACCESS_TOKEN':'test'}):
            with self.assertRaises(AccountError): self.store.switch(self.target)

    def test_alias_fallback_and_clear(self):
        self.store.update(self.target,'')
        profile=self.store.list()['profiles'][0]
        self.assertEqual(profile['alias'],'new_unknown')
        self.assertEqual(profile['display_name'],'new_unknown')
        self.store.update(self.target,'  Công việc  ')
        self.assertEqual(self.store.list()['profiles'][0]['display_name'],'Công việc')

    def test_automatic_alias_plan_and_manual_override(self):
        for plan,expected in [('plus','person_plus'),('business','person_team'),('team','person_team'),('pro','person_pro'),(None,'person_unknown')]:
            with self.subTest(plan=plan):
                payload={'sub':'person','email':'person@example.com','https://api.openai.com/auth':{'chatgpt_plan_type':plan}}
                token=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
                doc=json.loads(auth('person'));doc['tokens']['id_token']='header.'+token+'.signature'
                source=self.root/'auto.json';source.write_text(json.dumps(doc))
                result=self.store.capture(source=source)
                self.assertEqual(result['name'],expected)
        self.store.update(result['id'],'Custom')
        self.assertEqual(self.store.capture(source=source)['name'],'Custom')

    def test_alias_persists_on_recapture(self):
        self.store.update(self.target,'My work')
        self.store.capture(source=self.root/'new-auth.json')
        self.assertEqual(self.store.list()['profiles'][0]['alias'],'My work')

    def test_legacy_auto_name_migrates_to_email(self):
        registry=self.store.load();registry[self.target].pop('alias',None)
        registry[self.target]['name']='Codex local hiện tại'
        accounts.atomic(self.store.registry,registry)
        self.assertEqual(self.store.list()['profiles'][0]['display_name'],'new_unknown')

    def test_remove_keeps_live_auth_untouched(self):
        before=(self.home/'auth.json').read_bytes()
        self.store.remove(self.target)
        self.assertEqual(self.store.list()['profiles'],[])
        self.assertFalse((self.store.directory/(self.target+'.auth.json')).exists())
        self.assertEqual((self.home/'auth.json').read_bytes(),before)

    def test_removed_current_not_resurrected_by_auto_capture(self):
        info=self.store.capture()
        self.store.remove(info['id'])
        self.assertIsNone(self.store.auto_capture())
        self.assertNotIn(info['id'],self.store.load())
        self.store.capture()
        self.assertIn(info['id'],self.store.load())

    def test_switch_does_not_resurrect_forgotten_outgoing_profile(self):
        info=self.store.capture();self.store.remove(info['id'])
        self.store.switch(self.target)
        self.assertNotIn(info['id'],self.store.load())

    def test_pending_switch_blocks_crud(self):
        accounts.atomic(self.store.journal,{})
        with self.assertRaises(AccountError):self.store.remove(self.target)
        with self.assertRaises(AccountError):self.store.update(self.target,'Changed')

    def test_invalid_alias_and_unknown_id(self):
        for alias in ('x'*81,'line\nbreak',123):
            with self.assertRaises(AccountError):self.store.update(self.target,alias)
        with self.assertRaises(AccountError):self.store.update('missing','Work')
        with self.assertRaises(AccountError):self.store.remove('../auth.json')


if __name__=='__main__': unittest.main()
