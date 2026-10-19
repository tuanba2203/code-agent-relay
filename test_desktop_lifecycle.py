import subprocess
import threading
import unittest
from unittest.mock import patch,Mock
from accounts import AccountError
from desktop_lifecycle import DesktopLifecycle


class DesktopLifecycleTests(unittest.TestCase):
    def test_plan_only_matches_allowlisted_app_paths_and_records_pids(self):
        result=Mock(stdout='10 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n11 /Applications/ChatGPT.app/Contents/Helpers/helper\n12 /Applications/Visual Studio Code.app/Contents/MacOS/Electron\n13 /tmp/ChatGPT.app/Contents/MacOS/ChatGPT\n')
        with patch('desktop_lifecycle.sys.platform','darwin'),patch('desktop_lifecycle.subprocess.run',return_value=result):
            self.assertEqual(DesktopLifecycle().plan(),[{'app':'/Applications/ChatGPT.app','pids':[10,11]}])

    def test_quit_waits_for_helpers_and_never_kills(self):
        service=DesktopLifecycle();cancel=Mock();cancel.is_set.return_value=False
        plan=[{'app':'/Applications/ChatGPT.app','pids':[10]}]
        with patch.object(service,'processes',side_effect=[[{'app':'/Applications/ChatGPT.app','pid':11}],[]]),patch('desktop_lifecycle.subprocess.run') as run:
            service.close(plan,cancel)
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0][0],'/usr/bin/osascript')
            cancel.wait.assert_called_once()

    def test_timeout_does_not_kill_or_change_auth(self):
        service=DesktopLifecycle()
        with patch.object(service,'processes',return_value=[{'app':'/Applications/Codex.app','pid':10}]),patch('desktop_lifecycle.subprocess.run') as run:
            with self.assertRaises(AccountError):service.close([{'app':'/Applications/Codex.app'}],threading.Event(),timeout=0)
            self.assertEqual(run.call_count,1)

    def test_quit_failure_and_invalid_app_are_blocked(self):
        service=DesktopLifecycle()
        with patch('desktop_lifecycle.subprocess.run',side_effect=subprocess.TimeoutExpired('quit',10)):
            with self.assertRaises(AccountError):service.close([{'app':'/Applications/Codex.app'}],threading.Event())
        with patch('desktop_lifecycle.subprocess.run') as run:
            with self.assertRaises(AccountError):service.reopen([{'app':'/tmp/evil.app'}])
            run.assert_not_called()

    def test_reopen_uses_exact_app(self):
        with patch('desktop_lifecycle.subprocess.run') as run:
            DesktopLifecycle().reopen([{'app':'/Applications/ChatGPT.app'}])
            self.assertEqual(run.call_args.args[0],['/usr/bin/open','-a','/Applications/ChatGPT.app'])
