import json
from pathlib import Path
import tempfile
import unittest
from accounts import Accounts
from live_quota import Collector, QuotaError, quota_snapshot
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
