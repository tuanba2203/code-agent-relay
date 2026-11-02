import json
from pathlib import Path
import tempfile
import unittest
import time
import uuid
from accounts import Accounts
from live_quota import Collector, QuotaError, quota_snapshot, earliest_reset
from dashboard import Store
from test_accounts import auth


LIMITS={'rateLimitsByLimitId':{'codex':{'limitId':'codex',
        'primary':{'usedPercent':15,'windowDurationMins':300,'resetsAt':9999999999},
        'secondary':{'usedPercent':2,'windowDurationMins':10080,'resetsAt':9999999999}}}}


class FakeRPC:
    calls=[]
    home=None
    def __init__(self,home):
        self.home=home
        self.calls=[]
        FakeRPC.instance=self
    def call(self,method,params=None,**kwargs):
        self.calls.append(method)
        if method=='account/read':return {'account':{'type':'chatgpt','email':'real@example.com','planType':'team'}}
        if method=='account/rateLimits/read':return LIMITS
        if method=='account/usage/read':return {'summary':{'lifetimeTokens':123456,'peakDailyTokens':1000,'unknown':'ignored'}}
        raise AssertionError('Unexpected operation: '+method)
    def close(self):pass


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();home=self.root/'home';home.mkdir()
        (home/'auth.json').write_bytes(auth('real'))
        self.manager=Accounts(self.root/'vault',home,guard=lambda:None)
        self.manager.capture('Real login')

    def test_collects_actual_protocol_fields_without_global_auth_write(self):
        live=self.manager.home/'auth.json';before=live.read_bytes()
        packet=Collector(self.manager,FakeRPC).snapshot()
        row=packet['result']['accounts'][0]
        self.assertEqual(row['usage']['primary']['used_percent'],15)
        self.assertEqual(row['usage']['secondary']['used_percent'],2)
        self.assertEqual(row['token_summary']['lifetimeTokens'],123456)
        self.assertEqual(live.read_bytes(),before)
        self.assertFalse(FakeRPC.instance.home.exists())
        self.assertEqual(FakeRPC.instance.calls,['account/read','account/rateLimits/read','account/usage/read'])
        self.assertNotIn('access_token',json.dumps(packet))

    def test_reset_credits_count_expiry_and_privacy(self):
        response=json.loads(json.dumps(LIMITS))
        response['rateLimitResetCredits']={'availableCount':3,'credits':[
            {'id':'sample-credit-1','status':'available','title':'Full reset','expiresAt':2000000200},
            {'id':'sample-credit-2','status':'available','title':'Full reset','expiresAt':2000000100},
            {'id':'sample-used','status':'redeemed','title':'Used','expiresAt':2000000000}]}
        value=quota_snapshot(response)['reset_credits']
        self.assertEqual(value['available_count'],3)  # Count is authoritative; rows may be capped.
        self.assertEqual([c['expires_at'] for c in value['credits']],[2000000100,2000000200])
        self.assertNotIn('sample-credit',json.dumps(value))
        packet=Collector(self.manager,FakeRPC).snapshot()
        packet['result']['accounts'][0]['usage']['reset_credits']=value
        store=Store(collector=Collector(self.manager,FakeRPC));store.live_packet=packet
        self.assertEqual(store.view()['accounts'][0]['reset_credits'],value)

    def test_reset_credits_unknown_zero_and_missing_details_are_distinct(self):
        for raw,expected in [(None,None),({},None),({'availableCount':True},None),
                             ({'availableCount':-1},None),({'availableCount':1.5},None),
                             ({'availableCount':0,'credits':[]},{'available_count':0,'credits':[]}),
                             ({'availableCount':2,'credits':None},{'available_count':2,'credits':None})]:
            with self.subTest(raw=raw):
                self.assertEqual(quota_snapshot(dict(LIMITS,rateLimitResetCredits=raw))['reset_credits'],expected)
        value=quota_snapshot(dict(LIMITS,rateLimitResetCredits={'availableCount':1,'credits':[
            {'status':'available','expiresAt':float('nan'),'title':None}]}))['reset_credits']
        self.assertEqual(value['credits'],[{'title':'Lượt reset','expires_at':None}])

    def test_consume_earliest_reset_once_and_refresh(self):
        expiry=int(time.time())+3600
        calls=[]
        class ResetRPC(FakeRPC):
            def call(self,method,params=None,**kwargs):
                calls.append((method,params))
                if method=='account/rateLimits/read':
                    return dict(LIMITS,rateLimitResetCredits={'availableCount':2,'credits':[
                        {'id':'sample-later','status':'available','expiresAt':expiry+3600},
                        {'id':'sample-earliest','status':'available','expiresAt':expiry}]})
                if method=='account/rateLimitResetCredit/consume':return {'outcome':'reset'}
                return super().call(method,params,**kwargs)
        collector=Collector(self.manager,ResetRPC)
        identity=self.manager.list()['profiles'][0]['id'];operation=str(uuid.uuid4())
        before=(self.manager.home/'auth.json').read_bytes()
        result=collector.consume_reset(identity,operation,expiry)
        self.assertEqual(result['reset_outcome'],'reset')
        consume=[p for m,p in calls if m=='account/rateLimitResetCredit/consume']
        self.assertEqual(consume,[{'idempotencyKey':operation,'creditId':'sample-earliest'}])
        self.assertEqual([m for m,p in calls].count('account/rateLimits/read'),2)
        self.assertEqual((self.manager.home/'auth.json').read_bytes(),before)
        with self.assertRaises(QuotaError):collector.consume_reset(identity,operation,expiry)
        self.assertEqual(len([m for m,p in calls if m=='account/rateLimitResetCredit/consume']),1)

    def test_uncertain_reset_blocks_another_after_restart(self):
        expiry=int(time.time())+3600
        class FailingRPC(FakeRPC):
            def call(self,method,params=None,**kwargs):
                if method=='account/rateLimits/read':return dict(LIMITS,rateLimitResetCredits={
                    'availableCount':1,'credits':[{'id':'sample','status':'available','expiresAt':expiry}]})
                if method=='account/rateLimitResetCredit/consume':raise QuotaError('Disconnected')
                return super().call(method,params,**kwargs)
        identity=self.manager.list()['profiles'][0]['id']
        with self.assertRaises(QuotaError):Collector(self.manager,FailingRPC).consume_reset(identity,str(uuid.uuid4()),expiry)
        with self.assertRaisesRegex(QuotaError,'chưa rõ'):
            Collector(self.manager,FailingRPC).consume_reset(identity,str(uuid.uuid4()),expiry)

    def test_incomplete_unknown_expired_reset_details_are_rejected(self):
        for value in [None,{'availableCount':2,'credits':[{'status':'available','id':'sample','expiresAt':2000}]},
                      {'availableCount':1,'credits':[{'status':'available','id':'sample','expiresAt':None}]},
                      {'availableCount':1,'credits':[{'status':'available','id':'sample','expiresAt':1}]}]:
            with self.subTest(value=value):
                with self.assertRaises(QuotaError):earliest_reset({'rateLimitResetCredits':value},1000)

    def test_changed_reset_expiry_rejected_without_redemption(self):
        expiry=int(time.time())+3600
        class ChangedRPC(FakeRPC):
            def call(self,method,params=None,**kwargs):
                if method=='account/rateLimits/read':return dict(LIMITS,rateLimitResetCredits={
                    'availableCount':1,'credits':[{'id':'sample','status':'available','expiresAt':expiry}]})
                return super().call(method,params,**kwargs)
        identity=self.manager.list()['profiles'][0]['id']
        with self.assertRaisesRegex(QuotaError,'thay đổi'):
            Collector(self.manager,ChangedRPC).consume_reset(identity,str(uuid.uuid4()),expiry+1)
        self.assertFalse((self.manager.directory/'reset-operations.json').exists())

    def test_unknown_bucket_not_misreported_as_codex(self):
        with self.assertRaises(QuotaError):quota_snapshot({'rateLimitsByLimitId':{'other':{}}})

    def test_other_buckets_do_not_replace_codex_quota(self):
        response=json.loads(json.dumps(LIMITS))
        response['rateLimitsByLimitId']['chatgpt']={'limitId':'chatgpt',
            'primary':{'usedPercent':99,'windowDurationMins':300,'resetsAt':123}}
        self.assertEqual(quota_snapshot(response)['primary']['used_percent'],15)

    def test_server_block_overrides_low_percentages(self):
        result=quota_snapshot(dict(LIMITS,ordinaryUsageAllowed=False))
        self.assertEqual(result['refresh']['status'],'error')

    def test_failed_read_displays_unknown_not_demo_or_zero(self):
        def failure(home):raise QuotaError('network unavailable')
        packet=Collector(self.manager,failure).snapshot()
        self.assertEqual(packet['result']['accounts'][0]['usage']['source'],'none')
        self.assertIsNone(packet['result']['accounts'][0]['usage']['primary'])

    def test_store_default_has_no_demo(self):
        view=Store().view()
        self.assertFalse(view['demo']);self.assertEqual(view['accounts'],[])

    def test_live_store_uses_collected_data_not_import(self):
        store=Store(accounts=self.manager,collector=Collector(self.manager,FakeRPC))
        self.assertTrue(store.view()['loading'])
        store.collect_once();view=store.view()
        self.assertFalse(view['demo']);self.assertTrue(view['live'])
        self.assertEqual(len(view['accounts']),1)
        self.assertEqual(view['accounts'][0]['short']['used'],15)
        self.assertNotIn('chatgpt_quota',view['accounts'][0])
        self.assertFalse(view['loading'])

    def test_frontend_identity_not_guessed_without_current_auth(self):
        (self.manager.home/'auth.json').unlink()
        packet=Collector(self.manager,FakeRPC).snapshot()
        store=Store(collector=Collector(self.manager,FakeRPC))
        store.live_packet=packet
        self.assertIsNone(store.view()['active'])
        self.assertEqual(store.view()['action'],'pause')


if __name__=='__main__':unittest.main()
