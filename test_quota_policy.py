import unittest
from quota_policy import priority,entry_ceiling,reset_wait
from dashboard import demo_snapshot,build_view


def window(remaining,minutes,until):
    return {'used':100-remaining,'minutes':minutes,'resets_at':10000+until}


class QuotaPriorityTests(unittest.TestCase):
    def test_weekly_near_reset_is_preferred_at_equal_capacity(self):
        short=window(80,300,10000)
        self.assertGreater(priority(short,window(80,10080,3600),10000)[0],
                           priority(short,window(80,10080,6*86400),10000)[0])

    def test_short_near_reset_breaks_equal_weekly_capacity(self):
        weekly=window(80,10080,86400)
        self.assertGreater(priority(window(80,300,600),weekly,10000)[0],
                           priority(window(80,300,17000),weekly,10000)[0])

    def test_remaining_quota_increases_priority_and_waste_bonus(self):
        short=window(80,300,600)
        high=priority(short,window(95,10080,3600),10000)
        low=priority(short,window(45,10080,3600),10000)
        self.assertGreater(high[0],low[0]);self.assertGreater(high[1],low[1])

    def test_past_reset_does_not_imply_refilled_quota(self):
        score,bonus=priority(window(80,300,-1),window(80,10080,-1),10000)
        self.assertEqual(bonus,0)
        self.assertEqual(score,60)

    def test_short_quota_floor_blocks_attractive_weekly_reset(self):
        packet=demo_snapshot(10000)
        candidate=packet['result']['accounts'][1]
        candidate['usage']['primary']['used_percent']=71
        candidate['usage']['secondary']['used_percent']=0
        candidate['usage']['secondary']['resets_at']=10060
        view=build_view(packet,now=10000)
        row=next(a for a in view['accounts'] if a['id']==candidate['account_key'])
        self.assertFalse(row['eligible'])
        self.assertEqual(view['entry_threshold'],70)
        self.assertEqual(entry_ceiling({'reserve':40,'margin':20}),40)


class ResetWaitTests(unittest.TestCase):
    def test_all_limiting_windows_must_reset_soon(self):
        self.assertEqual(reset_wait([window(10,300,240),window(80,10080,86400)],85,10000),10241)
        self.assertIsNone(reset_wait([window(10,300,240),window(10,10080,86400)],85,10000))
        self.assertEqual(reset_wait([window(10,300,120),window(10,10080,300)],85,10000),10301)

    def test_boundaries_and_unknown_reset(self):
        for reset in (None,9999,10000,10301,float('nan'),True):
            self.assertIsNone(reset_wait([{'used':90,'resets_at':reset}],85,10000))
        self.assertIsNone(reset_wait([window(80,300,120)],85,10000))

    def test_view_waits_then_requires_observed_recovery(self):
        packet=demo_snapshot(10000)
        active=packet['result']['accounts'][0]['usage']
        active['primary']['resets_at']=10240
        self.assertEqual(build_view(packet,now=10000)['action'],'wait_reset')
        self.assertIsNone(build_view(packet,now=10000)['target'])
        active['secondary']['used_percent']=90
        self.assertEqual(build_view(packet,now=10000)['action'],'handoff')
        active['secondary']['used_percent']=10
        active['primary']['used_percent']=10
        self.assertEqual(build_view(packet,now=10000)['action'],'keep')
