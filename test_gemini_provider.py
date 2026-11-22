import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import Mock, patch

from accounts import AccountError
from gemini_provider import AUTH_ENV, GeminiProfiles


class GeminiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manager = GeminiProfiles(self.root / 'vault' / 'gemini')

    def create(self, alias='Google Work'):
        return self.manager.create(alias)['profiles'][-1]

    def test_status_does_not_create_state_or_start_cli(self):
        with patch('gemini_provider.subprocess.run') as run:
            self.assertEqual(self.manager.status()['profiles'], [])
        run.assert_not_called()
        self.assertFalse(self.manager.directory.exists())

    def test_profiles_are_separate_private_native_homes_and_metadata_only(self):
        first, second = self.create(), self.create('Personal')
        self.assertNotEqual(first['home'], second['home'])
        home = Path(first['home'])
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        settings = home / '.gemini' / 'settings.json'
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(settings.read_text())['security']['auth']['selectedType'], 'oauth-personal')
        # Relay must never inspect the OAuth cache, even if it is invalid JSON.
        (home / '.gemini' / 'oauth_creds.json').write_text('secret-credential')
        status = self.manager.status()
        self.assertNotIn('secret-credential', json.dumps(status))
        self.assertEqual(status['profiles'][0]['quota']['status'], 'unknown')
        self.assertFalse(status['proxy_supported'])
        restored = GeminiProfiles(self.manager.directory).status()
        self.assertEqual(restored['profiles'], status['profiles'])

    def test_alias_update_and_archive_preserve_native_state(self):
        profile = self.create()
        credentials = Path(profile['home']) / '.gemini' / 'oauth_creds.json'
        credentials.write_text('keep-this')
        self.manager.update(profile['id'], alias='Renamed', archived=True)
        with patch('gemini_provider.subprocess.run') as run:
            with self.assertRaises(AccountError):
                self.manager.launch(profile['id'])
            run.assert_not_called()
        restored = self.manager.update(profile['id'], archived=False)['profiles'][0]
        self.assertEqual(restored['alias'], 'Renamed')
        self.assertEqual(credentials.read_text(), 'keep-this')

    def test_native_launch_isolated_environment_and_no_replay(self):
        profile = self.create()
        project = self.root / 'project with spaces'
        project.mkdir()
        fake_env = {key: 'must-not-inherit' for key in AUTH_ENV}
        fake_env.update(GEMINI_CLI_HOME='/other-profile', GOOGLE_CLOUD_PROJECT='work-project')
        with patch.dict(os.environ, fake_env), patch('gemini_provider.shutil.which', return_value='/official/gemini'), patch('gemini_provider.subprocess.run', return_value=Mock(returncode=53)) as run:
            self.assertEqual(self.manager.launch(profile['id'], project), 53)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ['/official/gemini'])
        self.assertEqual(run.call_args.kwargs['cwd'], project)
        env = run.call_args.kwargs['env']
        self.assertEqual(env['GEMINI_CLI_HOME'], profile['home'])
        self.assertEqual(env['GOOGLE_CLOUD_PROJECT'], 'work-project')
        self.assertFalse(set(AUTH_ENV) & set(env))
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertNotIn('capture_output', run.call_args.kwargs)

    def test_login_uses_profile_home_not_project(self):
        profile = self.create()
        with patch('gemini_provider.shutil.which', return_value='/official/gemini'), patch('gemini_provider.subprocess.run', return_value=Mock(returncode=0)) as run:
            self.manager.launch(profile['id'])
        self.assertEqual(run.call_args.kwargs['cwd'], Path(profile['home']))

    def test_fake_native_executable_uses_each_profile_without_touching_default_home(self):
        first, second = self.create(), self.create('Personal')
        project = self.root / 'project'
        project.mkdir()
        default = self.root / 'default-gemini-state'
        default.write_text('unchanged')
        proof = self.root / 'proof.json'
        executable = self.root / 'fake-gemini'
        executable.write_text('#!/usr/bin/env python3\nimport json,os\nfrom pathlib import Path\n'
            + 'Path('+repr(str(proof))+').write_text(json.dumps({"home":os.environ["GEMINI_CLI_HOME"],"cwd":os.getcwd()}))\n')
        executable.chmod(0o700)
        for profile in (first, second):
            with patch('gemini_provider.shutil.which', return_value=str(executable)):
                self.assertEqual(self.manager.launch(profile['id'], project), 0)
            self.assertEqual(json.loads(proof.read_text()), {'home': profile['home'], 'cwd': str(project)})
        self.assertEqual(default.read_text(), 'unchanged')

    def test_concurrent_native_client_for_same_profile_is_blocked(self):
        profile = self.create()
        def started(*args, **kwargs):
            with self.assertRaisesRegex(AccountError, 'already has a running client'):
                self.manager.launch(profile['id'])
            return Mock(returncode=0)
        with patch('gemini_provider.shutil.which', return_value='/official/gemini'), patch('gemini_provider.subprocess.run', side_effect=started) as run:
            self.manager.launch(profile['id'])
        run.assert_called_once()

    def test_missing_cli_or_project_never_launches(self):
        profile = self.create()
        with patch('gemini_provider.shutil.which', return_value=None), patch('gemini_provider.subprocess.run') as run:
            with self.assertRaises(AccountError):
                self.manager.launch(profile['id'])
            run.assert_not_called()
        with patch('gemini_provider.shutil.which', return_value='/official/gemini'), patch('gemini_provider.subprocess.run') as run:
            with self.assertRaises(AccountError):
                self.manager.launch(profile['id'], self.root / 'missing')
            run.assert_not_called()

    def test_commands_quote_paths_without_exposing_alias_or_tokens(self):
        manager = GeminiProfiles(self.root / 'spaces and $shell' / 'gemini')
        profile = manager.create('$(do-not-execute)')['profiles'][0]
        command = shlex.split(profile['login_command'])
        self.assertEqual(command[-2:], ['login', profile['id']])
        self.assertEqual(command[command.index('--directory') + 1], str(manager.directory))
        self.assertNotIn('do-not-execute', profile['login_command'])

    def test_reject_symlinks_and_unknown_profile_ids(self):
        self.root.joinpath('redirect').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(AccountError):
            GeminiProfiles(self.root / 'redirect' / 'gemini').create('No')
        profile = self.create()
        for identity in ('../outside', None, 'unknown'):
            with self.assertRaises(AccountError):
                self.manager.update(identity, alias='No')
        native = Path(profile['home']) / '.gemini'
        settings = native / 'settings.json'
        # A redirected metadata file is never read or overwritten.
        metadata = self.manager.directory / 'profiles.json'
        metadata.unlink()
        metadata.symlink_to(settings)
        with self.assertRaises(AccountError):
            self.manager.status()

    def test_invalid_alias_and_corrupt_metadata_rejected(self):
        for alias in ('', 'a' * 81, 'bad\nname', 123):
            with self.assertRaises(AccountError):
                self.manager.create(alias)
        self.create()
        metadata = self.manager.directory / 'profiles.json'
        metadata.write_text('[{"id":"../../outside","alias":"X","archived":false}]')
        with self.assertRaises(AccountError):
            self.manager.status()

    def test_start_login_creates_and_opens_terminal_on_macos(self):
        with patch('gemini_provider.sys.platform', 'darwin'), \
             patch('gemini_provider.shutil.which', return_value='/opt/homebrew/bin/gemini'), \
             patch('gemini_provider.subprocess.run', return_value=Mock(returncode=0)) as run:
            result = self.manager.start_login('')
        self.assertTrue(result['created'])
        self.assertTrue(result['launched'])
        self.assertEqual(result['profiles'][0]['alias'], 'Gemini 1')
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], '/usr/bin/osascript')
        self.assertIn(result['profiles'][0]['id'], run.call_args.args[0][-1])

    def test_start_login_without_cli_does_not_create_orphan_profile(self):
        with patch('gemini_provider.sys.platform', 'darwin'), \
             patch('gemini_provider.shutil.which', return_value=None), \
             patch('gemini_provider.subprocess.run') as run:
            with self.assertRaises(AccountError):
                self.manager.start_login('Work')
            run.assert_not_called()
        self.assertEqual(self.manager.status()['profiles'], [])


if __name__ == '__main__':
    unittest.main()
