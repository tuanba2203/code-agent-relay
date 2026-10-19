from collections import deque
import json
import os
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from accounts import Accounts, AccountError, identify
from rotation import RotationService, RotationError, SessionRPC, catalog, backup_database
from test_accounts import auth


class FakeSessionRPC:
    instances=[]
    failure=None
    created=0
    def __init__(self,home):
        self.home=home;self.events=deque();self.calls=[];self.closed=False
        self.instances.append(self)
    def call(self,method,params=None,**kwargs):
        self.calls.append((method,params))
        if method==self.failure:raise RotationError('Injected failure')
        if method=='account/read':return {'account':{'type':'chatgpt','email':identify((self.home/'auth.json').read_bytes())['email']}}
        if method=='account/rateLimits/read':return {'rateLimits':{'limitId':'codex','primary':{'usedPercent':20},'secondary':{'usedPercent':10}}}
        if method=='turn/start':return {'turn':{'id':'turn-summary'}}
        if method=='thread/read':return {'thread':{'id':params['threadId'],'cwd':str(self.home.parent),'status':{'type':'idle'}}}
        if method=='thread/start':
            FakeSessionRPC.created+=1
            return {'thread':{'id':'imported-thread' if FakeSessionRPC.created==1 else 'imported-thread-'+str(FakeSessionRPC.created)}}
        return {}
    def wait_turn(self,thread_id,turn_id=None,compact=False,**kwargs):
        if self.failure=='compact' and compact:raise RotationError('Compaction failed')
        return '# Objective\nFinish project.\n# Next steps\nRun tests; do not repeat completed edits.'
    def close(self):self.closed=True


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();home=self.root/'home';home.mkdir()
        (home/'auth.json').write_bytes(auth('source'))
        (home/'config.toml').write_text('cli_auth_credentials_store="file"')
        self.manager=Accounts(self.root/'vault',home,guard=lambda:None)
        self.source=self.manager.capture('Source')['id']
        target=self.root/'target.json';target.write_bytes(auth('target'))
        self.target=self.manager.capture('Target',target)['id']
        self.rollout=home/'sessions'/'original.jsonl';self.rollout.parent.mkdir()
        self.rollout.write_text('original conversation')
        self.inventory=[{'id':'source-thread','title':'Task','cwd':str(self.root),
                         'frontend':'vscode','updated_at':1,'path':str(self.rollout)}]
        self.options={'reserve':8,'margin':10,'disabled':[]}
        self.collected=[]
        class Collector:
            def _read_profile(inner,profile):
                self.collected.append(profile['id'])
                return {'usage':{'primary':{'used_percent':20},'secondary':{'used_percent':10},'refresh':{'status':'ok'}}}
        self.collector=Collector()
        self.invalidated=[]
        FakeSessionRPC.instances=[];FakeSessionRPC.failure=None;FakeSessionRPC.created=0
        self.env=patch.dict(os.environ,{'CODEX_ACCESS_TOKEN':'','OPENAI_API_KEY':'','CODEX_API_KEY':''})
        self.env.start();self.addCleanup(self.env.stop)
        self.service=self.make()
        self.service.configure({'threads':['source-thread'],'enabled':False,'continue_work':False})

    def make(self):
        return RotationService(self.manager,self.collector,lambda:self.options,
                               lambda:self.invalidated.append(True),threading.Lock(),
                               rpc_factory=FakeSessionRPC,catalog_fn=lambda home:self.inventory)
    def run_job(self,mode='rotate',target=None):
        self.service.start(mode,target)
        self.service.worker.join(5)
        self.assertFalse(self.service.worker.is_alive())
        return self.service.status()['job']

    def test_automatic_rechecks_source_before_any_compaction(self):
        import time
        for used in (20,95):
            with self.subTest(used=used):
                with patch.object(self.collector,'_read_profile',return_value={'usage':{
                    'primary':{'used_percent':used,'resets_at':time.time()+120},
                    'secondary':{'used_percent':10,'resets_at':time.time()+86400},
                    'refresh':{'status':'ok'}}}):
                    self.service.start(automatic=True)
                    self.service.worker.join(5)
                    self.assertFalse(self.service.worker.is_alive())
                    self.assertEqual(self.service.status()['job']['phase'],'waiting_reset')
                    self.assertEqual(identify((self.manager.home/'auth.json').read_bytes())['id'],self.source)
                    self.assertFalse(FakeSessionRPC.instances)

    def test_real_file_switch_compact_export_import_and_persistent_journal(self):
        job=self.run_job()
        self.assertEqual(job['phase'],'complete',job)
        self.assertEqual(identify((self.manager.home/'auth.json').read_bytes())['id'],self.target)
        self.assertEqual(job['imports'][0]['id'],'imported-thread')
        self.assertTrue(job['imports'][0]['imported'])
        folder=Path(job['directory'])
        self.assertEqual((folder/'source-00.rollout').read_text(),'original conversation')
        self.assertIn(b'Finish project',self.service.download(job['handoffs'][0]['file']))
        self.assertEqual((folder/job['handoffs'][0]['file']).stat().st_mode & 0o777,0o600)
        self.assertEqual(json.loads((folder/'transaction.json').read_text())['phase'],'complete')
        methods=[m for rpc in FakeSessionRPC.instances for m,_ in rpc.calls]
        self.assertEqual(methods,['thread/name/set','thread/resume','thread/read','thread/compact/start','turn/start','account/read','account/rateLimits/read','thread/start','thread/name/set','thread/inject_items'])
        self.assertTrue(all(rpc.closed for rpc in FakeSessionRPC.instances))
        self.assertEqual(self.service.config['threads'],['imported-thread'])
        self.assertEqual(len(self.collected),2)  # target quota checked before compaction and before switch

    def test_checkpoint_does_not_change_auth_or_import(self):
        before=(self.manager.home/'auth.json').read_bytes()
        job=self.run_job('checkpoint')
        self.assertEqual(job['phase'],'checkpoint_complete')
        self.assertEqual((self.manager.home/'auth.json').read_bytes(),before)
        self.assertEqual(job['imports'],[])
        self.assertTrue(job['handoffs'])

    def test_failed_compact_preserves_original_account_and_backup(self):
        FakeSessionRPC.failure='compact'
        job=self.run_job()
        self.assertEqual(job['phase'],'failed')
        self.assertEqual(job['failed_phase'],'compacting')
        self.assertEqual(identify((self.manager.home/'auth.json').read_bytes())['id'],self.source)
        self.assertEqual(job['imports'],[])
        with self.assertRaises(RotationError):self.service.start()

    def test_partial_import_is_journalled_and_account_rolled_back(self):
        FakeSessionRPC.failure='thread/inject_items'
        job=self.run_job()
        self.assertEqual(job['phase'],'failed',job)
        self.assertTrue(job['rolled_back'])
        self.assertFalse(job['imports'][0]['imported'])
        self.assertEqual(identify((self.manager.home/'auth.json').read_bytes())['id'],self.source)
        self.assertTrue(self.service.download(job['handoffs'][0]['file']))

    def test_open_clients_block_execution_and_cancel_does_not_switch(self):
        def blocked():raise AccountError('client open')
        self.manager.guard=blocked
        self.service.start()
        self.assertEqual(self.service.status()['job']['phase'],'waiting_clients_closed')
        self.service.cancel();self.service.worker.join(5)
        self.assertEqual(self.service.status()['job']['phase'],'cancelled')
        self.assertEqual(FakeSessionRPC.instances,[])
        self.assertEqual(identify((self.manager.home/'auth.json').read_bytes())['id'],self.source)

    def test_wait_diagnostics_are_persisted_in_job_and_logs(self):
        details={'turn_id':'summary-turn','elapsed_seconds':35,'idle_seconds':35,
                 'events':0,'output_chars':0,'last_event':'none','error_code':None}
        self.service._save('exporting','Waiting for summary')
        self.service._wait_progress(details)
        record=json.loads(self.service.journal.read_text())
        self.assertEqual(record['phase'],'exporting')
        self.assertEqual(record['current_turn'],'summary-turn')
        self.assertEqual(record['wait'],details)
        self.assertIn('35s',self.service.logs[-1]['message'])
        self.assertEqual(FakeSessionRPC.instances,[])

    def test_recovery_before_preflight_does_not_require_closing_clients(self):
        from accounts import atomic
        atomic(self.service.journal,{'phase':'waiting_clients_closed','handoffs':[],'imports':[]})
        fresh=self.make()
        self.assertEqual(fresh.status()['job']['phase'],'cancelled')
        self.assertTrue(fresh.status()['job']['auto_recovered'])
        with patch.object(self.manager,'guard',side_effect=AccountError('Clients open')):
            self.assertEqual(fresh.acknowledge_recovery()['job']['phase'],'idle')
        self.assertEqual(identify((self.manager.home/'auth.json').read_bytes())['id'],self.source)

    def test_waiting_recovery_does_not_clear_evidence_of_actions(self):
        from accounts import atomic
        for evidence in ({'source':self.source},{'target':self.target},{'handoffs':[{}]},{'imports':[{}]}):
            with self.subTest(evidence=evidence):
                atomic(self.service.journal,dict(phase='waiting_clients_closed',**evidence))
                self.assertEqual(self.make().status()['job']['phase'],'recovery_required')

    def test_legacy_waiting_recovery_is_automatically_cancelled_and_persisted(self):
        from accounts import atomic
        atomic(self.service.journal,{'phase':'recovery_required','interrupted_phase':'waiting_clients_closed'})
        self.assertEqual(self.make().status()['job']['phase'],'cancelled')
        self.assertEqual(self.make().status()['job']['phase'],'cancelled')

    def test_legacy_client_wait_timeout_recovers_automatically(self):
        from accounts import atomic
        atomic(self.service.journal,{'phase':'failed','failed_phase':'waiting_clients_closed',
                                    'handoffs':[],'imports':[]})
        fresh=self.make()
        self.assertEqual(fresh.status()['job']['phase'],'cancelled')
        self.assertTrue(fresh.status()['job']['auto_recovered'])

    def test_restart_requires_recovery_and_never_auto_rearms(self):
        self.service._save('switching','interrupted')
        self.service.config['enabled']=True
        from accounts import atomic
        atomic(self.service.config_path,self.service.config)
        fresh=self.make()
        self.assertEqual(fresh.status()['job']['phase'],'recovery_required')
        self.assertFalse(fresh.config['enabled'])
        with self.assertRaises(RotationError):fresh.start()
        self.assertEqual(fresh.acknowledge_recovery()['job']['phase'],'idle')

    def test_download_rejects_other_files(self):
        self.run_job('checkpoint')
        with self.assertRaises(RotationError):self.service.download('../auth.json')

    def test_auto_trigger_uses_plan_only_when_enabled(self):
        with patch.object(self.service,'start') as start:
            self.service.maybe_auto({'action':'handoff','target':self.target})
            start.assert_not_called()
            self.service.config['enabled']=True
            self.service.maybe_auto({'action':'keep','target':None});start.assert_not_called()
            self.service.maybe_auto({'action':'handoff','target':self.target})
            start.assert_called_once_with(target=self.target,automatic=True)

    def test_optional_continuation_runs_in_relay_with_restricted_policy(self):
        self.service.config['continue_work']=True
        job=self.run_job()
        self.assertEqual(job['phase'],'complete')
        params=[p for rpc in FakeSessionRPC.instances for m,p in rpc.calls if m=='turn/start'][-1]
        self.assertEqual(params['sandboxPolicy']['type'],'workspaceWrite')
        self.assertFalse(params['sandboxPolicy']['networkAccess'])
        self.assertEqual(params['approvalPolicy'],'on-request')
        self.assertTrue((Path(job['directory'])/'continuation-00.md').exists())

    def test_all_imports_finish_before_continuation_and_reopened_client_defers_work(self):
        self.inventory.append(dict(self.inventory[0],id='source-two',title='Second task'))
        self.service.configure({'threads':['source-thread','source-two'],'enabled':False,'continue_work':True})
        original=FakeSessionRPC.call
        def call(rpc,method,params=None,**kwargs):
            if method=='turn/start' and (params.get('sandboxPolicy') or {}).get('type')=='workspaceWrite':
                self.assertEqual(len(self.service.record['imports']),2)
                self.assertTrue(all(row['imported'] for row in self.service.record['imports']))
                def blocked():raise AccountError('Client reopened')
                self.manager.guard=blocked
            return original(rpc,method,params,**kwargs)
        with patch.object(FakeSessionRPC,'call',call):
            job=self.run_job()
        self.assertEqual(job['phase'],'complete',job)
        self.assertTrue(job['continuation_deferred'])
        self.assertEqual(self.service._identity(),self.target)
        self.assertTrue(all(row['imported'] for row in job['imports']))

    def test_event_history_survives_restart_and_redacts_credentials(self):
        self.run_job()
        self.service._log_event('test','Bearer secret-value sk-secret123 eyJabc.def.ghi')
        before=self.service.status()['logs']
        restored=self.make().status()['logs']
        self.assertEqual(restored[:-1],before)
        self.assertEqual(restored[-1]['phase'],'server_start')
        encoded=json.dumps(restored)
        for secret in ('secret-value','sk-secret123','eyJabc.def.ghi'):
            self.assertNotIn(secret,encoded)
        self.assertIn('switching',[row['phase'] for row in restored])

    def test_log_snapshots_account_and_workflow_identity(self):
        self.run_job()
        logs=self.service.status()['logs']
        source=next(row for row in logs if row['phase']=='compacting')
        target=next(row for row in logs if row['phase']=='imported')
        self.assertEqual(source['context']['account_name'],'Source')
        self.assertEqual(source['context']['thread_id'],'source-thread')
        self.assertEqual(target['context']['account_name'],'Target')
        self.assertEqual(target['context']['thread_id'],'imported-thread')
        self.assertEqual(source['context']['alias'],target['context']['alias'])
        self.assertEqual(target['source_account']['id'],self.source)
        self.assertEqual(target['target_account']['id'],self.target)
        key=source['context']['workflow_id']
        self.service.managed.rename(key,'Renamed later')
        self.assertEqual(next(row for row in self.make().status()['logs'] if row['phase']=='imported')['context']['alias'],target['context']['alias'])

    def test_recovery_status_separates_old_error_and_live_check(self):
        self.service.record.update(phase='recovery_required',message='Old client error')
        self.assertTrue(self.service.status()['client_check']['clear'])
        def blocked():raise AccountError('Client still open now')
        self.manager.guard=blocked
        result=self.service.status()
        self.assertFalse(result['client_check']['clear'])
        self.assertEqual(result['job']['message'],'Old client error')
        self.assertEqual(result['client_check']['message'],'Client still open now')

    def test_old_journal_history_is_loaded_once(self):
        folder=self.service.directory/'legacy-job'
        folder.mkdir()
        (folder/'transaction.json').write_text(json.dumps({'id':'legacy-job','phase':'failed',
            'updated_at':1,'handoffs':[{}],'imports':[{'imported':True}]}))
        first=self.make()
        entries=[row for row in first.status()['logs'] if row['job_id']=='legacy-job']
        self.assertEqual(len(entries),1)
        self.assertIn('1 handoff',entries[0]['message'])
        self.assertEqual(len([row for row in self.make().status()['logs'] if row['job_id']=='legacy-job']),1)

    def test_handoff_filename_contains_account_and_workflow_alias(self):
        key=self.service.config['workflow_ids'][0]
        self.service.managed.rename(key,'Project / Hot Fix')
        job=self.run_job()
        filename=job['handoffs'][0]['file']
        self.assertTrue(filename.startswith('handoff--Source--Project-Hot-Fix--'))
        self.assertEqual(Path(filename).name,filename)
        self.assertTrue(self.service.download(filename))

    def test_repair_missing_workflow_uses_saved_handoff_without_compact_or_switch(self):
        checkpoint=self.run_job(mode='checkpoint')
        key=checkpoint['workflow_ids'][0]
        self.manager.switch(self.target)
        FakeSessionRPC.instances=[]
        self.service.start('repair',workflow_id=key)
        self.service.worker.join(5)
        job=self.service.status()['job']
        self.assertEqual(job['phase'],'complete',job)
        self.assertEqual(self.service._identity(),self.target)
        self.assertEqual(len(job['imports']),1)
        calls=[method for rpc in FakeSessionRPC.instances for method,_ in rpc.calls]
        self.assertNotIn('thread/compact/start',calls)
        self.assertNotIn('turn/start',calls)
        self.assertIn('thread/inject_items',calls)
        with self.assertRaises(RotationError):self.service.start('repair',workflow_id=key)

    def test_missing_source_binding_fails_before_any_compaction(self):
        self.manager.switch(self.target)
        before=self.service.status()['job']
        directories=set(self.service.directory.iterdir())
        for mode in ('rotate','checkpoint'):
            with self.subTest(mode=mode),self.assertRaisesRegex(RotationError,'Nạp handoff còn thiếu'):
                self.service.start(mode,target=self.source)
        self.assertEqual(self.service.status()['job'],before)
        self.assertEqual(set(self.service.directory.iterdir()),directories)
        self.assertFalse(FakeSessionRPC.instances)

    def test_prepared_source_binding_is_rejected_before_job_creation(self):
        key=self.service.config['workflow_ids'][0]
        self.service.managed.bind(key,self.source,'source-thread',state='prepared')
        with self.assertRaisesRegex(RotationError,'chưa xác nhận import'):
            self.service.start()
        self.assertEqual(self.service.status()['job']['phase'],'idle')
        self.assertIsNone(self.service.worker)

    def test_disabled_or_depleted_target_prevents_compaction(self):
        self.options['disabled']=[self.target]
        job=self.run_job()
        self.assertEqual(job['phase'],'failed')
        self.assertEqual(job['failed_phase'],'preflight')
        self.assertFalse(FakeSessionRPC.instances)

    def test_target_verified_again_before_auth_switch(self):
        original=self.collector._read_profile
        count=[0]
        def changed(profile):
            result=original(profile);count[0]+=1
            if count[0]>1:result['usage']['primary']['used_percent']=95
            return result
        self.collector._read_profile=changed
        job=self.run_job()
        self.assertEqual(job['phase'],'failed')
        self.assertTrue(job['handoffs'])
        self.assertEqual(identify((self.manager.home/'auth.json').read_bytes())['id'],self.source)
        self.assertFalse(job['imports'])

    def test_target_auth_failure_rolls_back_before_import(self):
        FakeSessionRPC.failure='account/read'
        job=self.run_job()
        self.assertEqual(job['phase'],'failed')
        self.assertTrue(job['rolled_back'])
        self.assertFalse(job['imports'])

    def test_implicit_credential_mode_rejected_before_any_model_turn(self):
        (self.manager.home/'config.toml').write_text('')
        with self.assertRaises(AccountError):self.service.start()
        self.assertFalse(FakeSessionRPC.instances)

    def test_other_process_service_cannot_start_over_existing_job(self):
        def blocked():raise AccountError('client running')
        self.manager.guard=blocked
        self.service.start()
        second=self.make()
        # Its startup sees an interrupted journal and requires explicit recovery.
        self.assertEqual(second.status()['job']['phase'],'recovery_required')
        with self.assertRaises(RotationError):second.start()
        self.service.cancel();self.service.worker.join(5)

    def add_imported_inventory(self,job):
        for row in job['imports']:
            if not any(item['id']==row['id'] for item in self.inventory):
                self.inventory.append({'id':row['id'],'title':'Imported task','cwd':str(self.root),
                    'frontend':'appServer','updated_at':2,'path':str(self.rollout)})

    def test_a_b_a_b_reuses_both_account_threads(self):
        key=self.service.config['workflow_ids'][0]
        first=self.run_job(target=self.target);self.add_imported_inventory(first)
        self.assertFalse(first['imports'][0]['reused'])
        second=self.run_job(target=self.source);self.add_imported_inventory(second)
        self.assertEqual(second['phase'],'complete',second)
        self.assertEqual(second['imports'][0]['id'],'source-thread')
        self.assertTrue(second['imports'][0]['reused'])
        third=self.run_job(target=self.target)
        self.assertEqual(third['phase'],'complete',third)
        self.assertTrue(third['imports'][0]['reused'])
        self.assertEqual(FakeSessionRPC.created,1)
        flow=self.service.managed.get(key)
        self.assertEqual(flow['bindings'][self.source]['thread_id'],'source-thread')
        self.assertEqual(flow['bindings'][self.target]['thread_id'],'imported-thread')
        self.assertEqual([event['status'] for event in flow['history']],['complete']*3)
        fresh=self.make()
        self.assertEqual(fresh.managed.get(key),flow)

    def test_a_b_c_a_creates_only_missing_account_thread(self):
        path=self.root/'third.json';path.write_bytes(auth('third'))
        third=self.manager.capture('Third',path)['id']
        key=self.service.config['workflow_ids'][0]
        for target in (self.target,third,self.source):
            job=self.run_job(target=target)
            self.assertEqual(job['phase'],'complete',job)
            self.add_imported_inventory(job)
        self.assertEqual(FakeSessionRPC.created,2)
        self.assertEqual(set(self.service.managed.get(key)['bindings']),{self.source,self.target,third})

    def test_two_logical_workflows_do_not_mix_bindings(self):
        second=dict(self.inventory[0],id='source-two',title='Second task')
        self.inventory.append(second)
        self.service.configure({'threads':['source-thread','source-two'],'enabled':False,'continue_work':False})
        keys=list(self.service.config['workflow_ids'])
        job=self.run_job(target=self.target)
        self.assertEqual(job['phase'],'complete',job)
        self.assertEqual(len(job['imports']),2)
        mappings=[self.service.managed.get(key)['bindings'][self.target]['thread_id'] for key in keys]
        self.assertEqual(len(set(mappings)),2)

    def test_known_target_missing_never_falls_back_to_new_thread(self):
        first=self.run_job(target=self.target);self.add_imported_inventory(first)
        original=FakeSessionRPC.call
        def call(rpc,method,params=None,**kwargs):
            if method=='thread/read' and params['threadId']=='source-thread':
                raise RotationError('Tracked destination missing')
            return original(rpc,method,params,**kwargs)
        with patch.object(FakeSessionRPC,'call',call):job=self.run_job(target=self.source)
        self.assertEqual(job['phase'],'failed')
        self.assertTrue(job['rolled_back'])
        self.assertEqual(FakeSessionRPC.created,1)

    def test_failed_injection_keeps_prepared_binding_for_recovery(self):
        FakeSessionRPC.failure='thread/inject_items'
        job=self.run_job(target=self.target)
        flow=self.service.managed.get(self.service.config['workflow_ids'][0])
        self.assertEqual(flow['bindings'][self.target]['state'],'prepared')
        self.assertEqual(flow['history'][-1]['status'],'failed')
        self.assertTrue(flow['history'][-1]['rolled_back'])

    def test_label_and_manual_existing_binding_are_persistent(self):
        key=self.service.config['workflow_ids'][0]
        self.inventory.append(dict(self.inventory[0],id='existing-target-thread'))
        state=self.service.update_workflow({'workflow_id':key,'label':'Thread XXX',
            'account_id':self.target,'thread_id':'existing-target-thread'})
        self.assertEqual(state['managed_threads'][0]['label'],'Thread XXX')
        job=self.run_job(target=self.target)
        self.assertEqual(job['phase'],'complete',job)
        self.assertTrue(job['imports'][0]['reused'])
        self.assertEqual(job['imports'][0]['id'],'existing-target-thread')
        self.assertEqual(FakeSessionRPC.created,0)

    def test_pair_cannot_be_assigned_to_two_workflows(self):
        self.inventory.append(dict(self.inventory[0],id='source-two'))
        self.service.configure({'threads':['source-thread','source-two']})
        first,second=self.service.config['workflow_ids']
        self.service.managed.bind(first,self.target,'target-one')
        with self.assertRaises(AccountError):self.service.managed.bind(second,self.target,'target-one')

    def test_corrupt_registry_is_not_recreated(self):
        self.service.managed.path.write_text('{broken')
        with self.assertRaises(AccountError):self.service.managed.listing()

    def test_opt_in_desktop_closes_before_rpc_and_reopens_after_complete(self):
        order=[]
        with patch('rotation.DesktopLifecycle') as cls:
            controller=cls.return_value
            controller.plan.return_value=[{'app':'/Applications/ChatGPT.app','pids':[99]}]
            controller.close.side_effect=lambda *args:order.append('close')
            controller.reopen.side_effect=lambda *args:order.append('reopen')
            original=self.service.rpc_factory
            def rpc(home):
                order.append('rpc')
                return original(home)
            self.service.rpc_factory=rpc
            self.service.start('rotate',target=self.target,restart_desktop=True)
            self.service.worker.join(5)
            self.assertEqual(self.service.record['phase'],'complete')
            self.assertEqual(order[0],'close')
            self.assertEqual(order[-1],'reopen')
            self.assertTrue(self.service.record['desktop_reopened'])

    def test_desktop_quit_failure_stops_before_compaction_or_switch(self):
        before=(self.manager.home/'auth.json').read_bytes()
        with patch('rotation.DesktopLifecycle') as cls:
            controller=cls.return_value
            controller.plan.return_value=[{'app':'/Applications/ChatGPT.app','pids':[99]}]
            controller.close.side_effect=AccountError('quit timed out')
            self.service.start('rotate',target=self.target,restart_desktop=True)
            self.service.worker.join(5)
            self.assertEqual(self.service.record['phase'],'failed')
            self.assertFalse(FakeSessionRPC.instances)
            self.assertEqual((self.manager.home/'auth.json').read_bytes(),before)
            controller.reopen.assert_called_once()

    def test_desktop_reopen_is_deferred_when_account_recovery_is_needed(self):
        def fail_import(*args):
            def blocked():raise AccountError('client reopened')
            self.manager.guard=blocked
            raise RotationError('import failed')
        with patch('rotation.DesktopLifecycle') as cls,patch.object(self.service,'_import_handoffs',side_effect=fail_import):
            controller=cls.return_value
            controller.plan.return_value=[{'app':'/Applications/ChatGPT.app','pids':[99]}]
            self.service.start('rotate',target=self.target,restart_desktop=True)
            self.service.worker.join(5)
            self.assertEqual(self.service.record['phase'],'recovery_required')
            self.assertTrue(self.service.record['desktop_reopen_deferred'])
            controller.reopen.assert_not_called()

    def test_automatic_toggle_does_not_cancel_or_start_job(self):
        self.service.set_automatic(True)
        self.assertTrue(self.service.config['enabled'])
        self.assertIsNone(self.service.worker)
        self.service.set_automatic(False)
        self.assertFalse(self.service.config['enabled'])
        self.assertFalse(self.service.cancel_event.is_set())
        with self.assertRaises(RotationError):self.service.set_automatic('true')

    def test_active_source_is_not_compacted(self):
        original=FakeSessionRPC.call
        def call(rpc,method,params=None,**kwargs):
            if method=='thread/read':return {'thread':{'id':params['threadId'],'status':{'type':'active'}}}
            return original(rpc,method,params,**kwargs)
        with patch.object(FakeSessionRPC,'call',call):job=self.run_job()
        self.assertEqual(job['phase'],'failed')
        self.assertFalse(any(m=='thread/compact/start' for rpc in FakeSessionRPC.instances for m,p in rpc.calls))
        self.assertFalse(job['handoffs'])

    def test_automatic_cannot_quit_desktop(self):
        with self.assertRaises(RotationError):
            self.service.start('rotate',automatic=True,restart_desktop=True)

    def test_shared_alias_sets_native_name_before_injecting_new_context(self):
        key=self.service.config['workflow_ids'][0]
        self.service.update_workflow({'workflow_id':key,'label':'Project Focus'})
        job=self.run_job(target=self.target)
        self.assertEqual(job['phase'],'complete',job)
        named=[p for rpc in FakeSessionRPC.instances for method,p in rpc.calls if method=='thread/name/set']
        self.assertEqual(named,[{'threadId':'source-thread','name':'Project Focus · Source'},
                                {'threadId':'imported-thread','name':'Project Focus · Target'}])
        target_rpc=FakeSessionRPC.instances[-1]
        methods=[method for method,_ in target_rpc.calls]
        self.assertLess(methods.index('thread/name/set'),methods.index('thread/inject_items'))
        self.assertEqual(self.service.managed.get(key)['bindings'][self.target]['synced_name'],'Project Focus · Target')

    def test_alias_change_synchronizes_all_account_threads_without_model_turns(self):
        key=self.service.config['workflow_ids'][0]
        self.run_job(target=self.target)
        self.service.update_workflow({'workflow_id':key,'label':'XXX — shared alias'})
        self.assertTrue(all(b['name_status']=='pending' for b in self.service.managed.get(key)['bindings'].values()))
        before=len(FakeSessionRPC.instances)
        auth_before=(self.manager.home/'auth.json').read_bytes()
        self.service.sync_names()
        calls=[(m,p) for rpc in FakeSessionRPC.instances[before:] for m,p in rpc.calls]
        self.assertEqual(len(calls),2)
        self.assertTrue(all(m=='thread/name/set' for m,p in calls))
        self.assertEqual({p['name'] for m,p in calls},{'XXX — shared alias · Source','XXX — shared alias · Target'})
        self.assertEqual((self.manager.home/'auth.json').read_bytes(),auth_before)
        self.assertTrue(all(b['name_status']=='synced' for b in self.make().managed.get(key)['bindings'].values()))

    def test_account_alias_change_resyncs_native_postfix(self):
        key=self.service.config['workflow_ids'][0]
        self.service.sync_names()
        self.manager.update(self.source,'Source renamed')
        before=len(FakeSessionRPC.instances)
        self.service.sync_names()
        calls=[p for rpc in FakeSessionRPC.instances[before:] for m,p in rpc.calls if m=='thread/name/set']
        self.assertEqual(calls,[{'threadId':'source-thread','name':'Task · Source renamed'}])
        self.assertEqual(self.service.managed.get(key)['label'],'Task')
        self.assertEqual(self.service.managed.get(key)['bindings'][self.source]['synced_name'],'Task · Source renamed')
        before=len(FakeSessionRPC.instances)
        self.service.sync_names()
        self.assertEqual(len(FakeSessionRPC.instances),before)

    def test_open_clients_leave_name_sync_pending_until_closed(self):
        key=self.service.config['workflow_ids'][0]
        self.service.update_workflow({'workflow_id':key,'label':'Shared'})
        def blocked():raise AccountError('client open')
        self.manager.guard=blocked
        self.service.sync_names()
        self.assertFalse(FakeSessionRPC.instances)
        self.assertEqual(self.service.managed.get(key)['bindings'][self.source]['name_status'],'pending')
        self.manager.guard=lambda:None
        self.service.sync_names()
        self.assertEqual(self.service.managed.get(key)['bindings'][self.source]['name_status'],'synced')

    def test_stale_name_ack_does_not_mark_new_alias_as_synced(self):
        key=self.service.config['workflow_ids'][0]
        self.service.managed.rename(key,'New alias')
        self.service.managed.name_result(key,self.source,'Old alias')
        self.assertEqual(self.service.managed.get(key)['bindings'][self.source]['name_status'],'pending')

    def test_failed_name_sync_is_visible_and_can_be_retried(self):
        key=self.service.config['workflow_ids'][0]
        FakeSessionRPC.failure='thread/name/set'
        self.service.sync_names()
        self.assertEqual(self.service.managed.get(key)['bindings'][self.source]['name_status'],'error')
        FakeSessionRPC.failure=None
        self.service.sync_names(force=True)
        self.assertEqual(self.service.managed.get(key)['bindings'][self.source]['name_status'],'synced')

    def test_executor_uses_weekly_reset_priority_on_fresh_quota(self):
        path=self.root/'third.json';path.write_bytes(auth('third'))
        third=self.manager.capture('Third',path)['id']
        now=__import__('time').time()
        def quota(profile):
            weekly,short,reset=(5,60,3600) if profile['id']==self.target else (25,10,6*86400)
            return {'usage':{'refresh':{'status':'ok'},
                    'primary':{'used_percent':short,'window_minutes':300,'resets_at':now+10000},
                    'secondary':{'used_percent':weekly,'window_minutes':10080,'resets_at':now+reset}}}
        self.collector._read_profile=quota
        chosen=self.service._pick_target(self.source)
        self.assertEqual(chosen['id'],self.target)


class NotificationTests(unittest.TestCase):
    def rpc(self,messages):
        rpc=object.__new__(SessionRPC);rpc.events=deque(messages);rpc.messages=queue.Queue()
        return rpc
    def test_compact_requires_both_item_and_completed_turn(self):
        rpc=self.rpc([{'method':'item/completed','params':{'threadId':'a','item':{'type':'contextCompaction'}}},
                      {'method':'turn/completed','params':{'threadId':'a','turn':{'id':'t','status':'completed'}}}])
        self.assertEqual(rpc.wait_turn('a',compact=True),'')
        rpc=self.rpc([{'method':'turn/completed','params':{'threadId':'a','turn':{'id':'t','status':'completed'}}}])
        with self.assertRaises(RotationError):rpc.wait_turn('a',compact=True)
    def test_summary_ignores_other_threads_and_failed_turns(self):
        rpc=self.rpc([{'method':'item/completed','params':{'threadId':'b','item':{'type':'agentMessage','text':'wrong'}}},
                      {'method':'item/completed','params':{'threadId':'a','turnId':'t','item':{'type':'agentMessage','text':'summary'}}},
                      {'method':'turn/completed','params':{'threadId':'a','turn':{'id':'t','status':'completed'}}}])
        self.assertEqual(rpc.wait_turn('a','t'),'summary')

    def test_silent_wait_reports_heartbeat_without_waiting_in_real_time(self):
        rpc=self.rpc([])
        now=[0]
        reports=[]
        def receive(**kwargs):
            if now[0]==0:
                now[0]=35
                raise queue.Empty()
            return {'method':'turn/completed','params':{'threadId':'a','turn':{'id':'t','status':'completed'}}}
        with patch('rotation.time.monotonic',side_effect=lambda:now[0]),patch.object(rpc.messages,'get',side_effect=receive):
            self.assertEqual(rpc.wait_turn('a','t',progress=reports.append),'')
        self.assertEqual([r['elapsed_seconds'] for r in reports],[0,35])
        self.assertEqual(reports[-1]['idle_seconds'],35)
        self.assertEqual(reports[-1]['turn_id'],'t')

    def test_retry_notification_is_reported_without_replaying_turn(self):
        rpc=self.rpc([
            {'method':'error','params':{'threadId':'a','turnId':'t','willRetry':True,
                'error':{'message':'secret raw error','codexErrorInfo':{'responseStreamDisconnected':{}}}}},
            {'method':'item/completed','params':{'threadId':'a','turnId':'t','item':{'type':'agentMessage','text':'summary'}}},
            {'method':'turn/completed','params':{'threadId':'a','turn':{'id':'t','status':'completed'}}}])
        reports=[]
        self.assertEqual(rpc.wait_turn('a','t',progress=reports.append),'summary')
        self.assertEqual(reports[-1]['error_code'],'responseStreamDisconnected')
        self.assertNotIn('secret raw error',json.dumps(reports))

    def test_non_retryable_error_stops_and_does_not_expose_server_text(self):
        rpc=self.rpc([{'method':'error','params':{'threadId':'a','turnId':'t','willRetry':False,
            'error':{'message':'credential contents','codexErrorInfo':'credential contents'}}}])
        with patch.object(rpc,'call',return_value={}) as call:
            with self.assertRaisesRegex(RotationError,'non-retryable error \\(unknown\\)'):
                rpc.wait_turn('a','t')
        self.assertEqual(call.call_args.args[0],'turn/interrupt')

    def test_timeout_survives_failed_interrupt(self):
        from live_quota import QuotaError
        rpc=self.rpc([])
        with patch.object(rpc,'call',side_effect=QuotaError('interrupt unavailable')):
            with self.assertRaisesRegex(RotationError,'Timed out after 0s.*events=0'):
                rpc.wait_turn('a','t',timeout=0)

    def test_cancel_survives_failed_interrupt(self):
        from live_quota import QuotaError
        rpc=self.rpc([])
        cancel=threading.Event();cancel.set()
        with patch.object(rpc,'call',side_effect=QuotaError('interrupt unavailable')):
            with self.assertRaisesRegex(RotationError,'Cancelled while waiting'):
                rpc.wait_turn('a','t',cancel=cancel)

    def test_other_turn_completion_does_not_finish_current_turn(self):
        rpc=self.rpc([
            {'method':'turn/completed','params':{'threadId':'a','turn':{'id':'old','status':'failed'}}},
            {'method':'turn/completed','params':{'threadId':'a','turn':{'id':'t','status':'failed',
                'error':{'message':'do not expose','codexErrorInfo':'usageLimitExceeded'}}}}])
        with self.assertRaisesRegex(RotationError,'successfully \\(usageLimitExceeded\\)'):
            rpc.wait_turn('a','t')


class CatalogTests(unittest.TestCase):
    def test_readonly_catalog_filters_archived_and_outside_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();sessions=root/'sessions';sessions.mkdir()
            rollout=sessions/'test.jsonl';rollout.write_text('private history')
            db=sqlite3.connect(str(root/'state_5.sqlite'))
            db.execute('CREATE TABLE threads(id,title,cwd,source,updated_at,rollout_path,archived)')
            for key,path,archived in [('valid',str(rollout),0),('outside','/etc/passwd',0),('archived',str(rollout),1)]:
                db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)',(key,'Title',str(root),'vscode',1,path,archived))
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)',('guardian','Review',str(root),'{"subagent":{"other":"guardian"}}',1,str(rollout),0))
            db.commit();db.close()
            backup_database(root,root/'snapshot.sqlite')
            copied=sqlite3.connect((root/'snapshot.sqlite').as_uri()+'?mode=ro',uri=True)
            self.assertEqual(copied.execute('SELECT count(*) FROM threads').fetchone()[0],4)
            copied.close()
            self.assertEqual((root/'snapshot.sqlite').stat().st_mode & 0o777,0o600)
            self.assertEqual([r['id'] for r in catalog(root)],['valid'])
