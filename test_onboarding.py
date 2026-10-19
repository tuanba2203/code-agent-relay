from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from accounts import Accounts,atomic,AccountError
from onboarding import LoginFlow
from test_accounts import auth


class FakeLoginRPC:
    instances=[]
    def __init__(self,home):
        self.home=home;self.closed=False;self.__class__.instances.append(self)
    def call(self,method,params):
        if method=='account/login/start':
            atomic(self.home/'auth.json',auth('added'))
            return {'authUrl':'https://auth.openai.com/oauth/authorize?state=test'}
        if method=='account/read':return {'account':{'type':'chatgpt','email':'added@example.com'}}
        raise AssertionError(method)
    def close(self):
        self.closed=True
        self.home_existed_on_close=self.home.is_dir()


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name).resolve();self.home=root/'home';self.home.mkdir()
        (self.home/'auth.json').write_bytes(auth('current'))
        self.manager=Accounts(root/'vault',self.home)

    def test_login_adds_profile_with_alias_without_switching(self):
        before=(self.home/'auth.json').read_bytes()
        completed=[]
        job=LoginFlow(self.manager,lambda:completed.append(True),FakeLoginRPC)
        job.start('Work');job.thread.join(3)
        self.assertEqual(job.status()['status'],'complete')
        profile=self.manager.list()['profiles'][0]
        self.assertEqual(profile['alias'],'Work')
        self.assertEqual((self.home/'auth.json').read_bytes(),before)
        self.assertEqual(completed,[True])
        self.assertTrue(FakeLoginRPC.instances[-1].home_existed_on_close)
        self.assertFalse(FakeLoginRPC.instances[-1].home.exists())

    def test_login_without_alias_displays_email(self):
        job=LoginFlow(self.manager,rpc_factory=FakeLoginRPC)
        job.start();job.thread.join(3)
        self.assertEqual(self.manager.list()['profiles'][0]['display_name'],'added_unknown')

    def test_untrusted_login_url_rejected(self):
        class BadRPC(FakeLoginRPC):
            def call(self,method,params):return {'authUrl':'https://attacker.example/login'}
        job=LoginFlow(self.manager,rpc_factory=BadRPC)
        job.start();job.thread.join(3)
        self.assertEqual(job.status()['status'],'error')
        self.assertEqual(self.manager.list()['profiles'],[])

    def test_cancel_and_duplicate_login(self):
        class WaitingRPC(FakeLoginRPC):
            def call(self,method,params):return {'authUrl':'https://auth.openai.com/oauth/authorize'}
        job=LoginFlow(self.manager,rpc_factory=WaitingRPC)
        job.start()
        with self.assertRaises(AccountError):job.start()
        job.cancel();job.thread.join(3)
        self.assertEqual(job.status()['status'],'cancelled')
        self.assertEqual(self.manager.list()['profiles'],[])
