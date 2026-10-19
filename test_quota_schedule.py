import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from accounts import Accounts
from live_quota import Collector
from quota_schedule import interval
from test_accounts import auth


def usage(used):
    return {'primary':{'used_percent':used,'resets_at':99999,'window_minutes':300},
            'secondary':{'used_percent':0,'resets_at':999999,'window_minutes':10080},
            'observed_at':10000,'source':'api','refresh':{'requested':True,'method':'api','status':'ok'}}


class RuleTests(unittest.TestCase):
    def test_boundaries(self):
        for used,seconds in ((49,300),(50,120),(70,120),(71,30)):
            self.assertEqual(interval(True,False,usage(used),10000,10001,30)[0],seconds)
        self.assertEqual(interval(True,False,usage(80),10000,10001,1800)[0],1800)

    def test_inactivity_override_and_disabled_precedence(self):
        self.assertEqual(interval(True,False,usage(80),10000,11800,30)[0],30)
        for used in (10,50,80,100):
            self.assertEqual(interval(True,False,usage(used),10000,11801,30),(1800,'inactive'))
        self.assertEqual(interval(True,True,usage(80),10000,11801,30),(1800,'disabled'))
        self.assertEqual(interval(False,False,usage(80),10000,11801,30),(1800,'standby'))


class CollectorScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name).resolve();home=root/'home';home.mkdir()
        (home/'auth.json').write_bytes(auth('one'))
        self.manager=Accounts(root/'vault',home,guard=lambda:None)
        self.key=self.manager.capture('One')['id']
        self.clock=patch('live_quota.time.time',return_value=10000)
        self.now=self.clock.start();self.addCleanup(self.clock.stop)
        self.collector=Collector(self.manager)
        self.reader=patch.object(self.collector,'_read_profile',return_value={'usage':usage(10)})
        self.read=self.reader.start();self.addCleanup(self.reader.stop)

    def row(self,**kwargs):
        return self.collector.snapshot(**kwargs)['result']['accounts'][0]

    def test_reset_interrupts_idle_polling_without_assuming_refill(self):
        data=usage(95);data['primary']['resets_at']=12120
        self.read.return_value={'usage':data}
        self.row();self.now.return_value=12000
        row=self.row()
        self.assertEqual(row['polling']['interval_seconds'],1800)
        self.assertEqual(row['polling']['next_check_at'],12121)
        calls=self.read.call_count
        self.now.return_value=12121
        row=self.row()
        self.assertEqual(self.read.call_count,calls+1)
        self.assertEqual(row['usage']['primary']['used_percent'],95)

    def test_cache_due_time_force_and_inactive(self):
        self.assertEqual(self.row()['polling']['next_check_at'],10300)
        self.now.return_value=10299;self.row();self.assertEqual(self.read.call_count,1)
        self.now.return_value=10300;self.row();self.assertEqual(self.read.call_count,2)
        self.now.return_value=10301;self.row(force=True);self.assertEqual(self.read.call_count,3)
        self.now.return_value=11801
        row=self.row()
        self.assertEqual(row['polling']['last_changed_at'],10000)
        self.assertEqual(row['polling']['interval_seconds'],1800)
        self.assertEqual(row['polling']['reason'],'inactive')

    def test_change_resumes_normal_schedule_and_survives_restart(self):
        self.row();self.now.return_value=12000
        self.read.return_value={'usage':usage(55)}
        row=self.row()
        self.assertEqual(row['polling']['last_changed_at'],12000)
        self.assertEqual(row['polling']['interval_seconds'],120)
        fresh=Collector(self.manager)
        self.assertEqual(fresh.history[self.key]['last_changed'],12000)
        self.assertEqual(fresh.history_path.stat().st_mode & 0o777,0o600)

    def test_disabled_account_only_checks_every_30_minutes(self):
        self.row(disabled=[self.key]);self.now.return_value=11799
        self.row(disabled=[self.key]);self.assertEqual(self.read.call_count,1)
        self.now.return_value=11800;self.row(disabled=[self.key]);self.assertEqual(self.read.call_count,2)
        self.now.return_value=11801;self.row();self.assertEqual(self.read.call_count,3)

    def test_standby_and_activation(self):
        self.row()
        (self.manager.home/'auth.json').write_bytes(auth('other'))
        self.now.return_value=10300
        row=self.row();self.assertEqual(row['polling']['interval_seconds'],1800)
        self.assertEqual(self.read.call_count,1)
        (self.manager.home/'auth.json').write_bytes(auth('one'))
        self.now.return_value=10301;self.row();self.assertEqual(self.read.call_count,2)

    def test_low_quota_checks_every_30_seconds_then_idle_every_30_minutes(self):
        self.read.return_value={'usage':usage(80)}
        row=self.row()
        self.assertEqual(row['polling']['interval_seconds'],30)
        self.now.return_value=10029;self.row();self.assertEqual(self.read.call_count,1)
        self.now.return_value=10030;self.row();self.assertEqual(self.read.call_count,2)
        self.now.return_value=11801
        row=self.row()
        self.assertEqual(row['polling']['interval_seconds'],1800)
        self.assertEqual(row['polling']['next_check_at'],11830)
        self.assertEqual(self.read.call_count,2)

    def test_read_error_is_not_relabelled_as_success(self):
        from live_quota import QuotaError
        self.read.side_effect=QuotaError('failed')
        self.assertIn('connection_error',self.row())
        self.now.return_value=10030
        row=self.row()
        self.assertIn('connection_error',row)
        self.assertEqual(self.read.call_count,1)
        self.assertIsNone(row['usage']['primary'])

    def test_manual_refresh_only_forces_selected_profile(self):
        source=self.manager.directory/'second.auth'
        source.write_bytes(auth('second'))
        second=self.manager.capture('Second',source)['id']
        self.collector.snapshot()
        self.read.reset_mock()
        self.now.return_value=10010
        packet=self.collector.snapshot(force_keys={second})
        self.assertEqual(self.read.call_count,1)
        self.assertEqual(self.read.call_args[0][0]['id'],second)
        rows={r['account_key']:r for r in packet['result']['accounts']}
        self.assertEqual(rows[second]['polling']['last_checked_at'],10010)
        self.assertEqual(rows[self.key]['polling']['last_checked_at'],10000)
