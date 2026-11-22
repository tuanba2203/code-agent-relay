import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from accounts import AccountError
from antigravity_provider import (
    APP, KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, KEYRING_PREFIX, MacOSLiveStore,
    MemoryLiveStore, AntigravityDesktop, AntigravityProfiles, identify_antigravity,
    protect_private_directory, unwrap_keychain_secret, wrap_keychain_secret,
)


class MemoryQuota:
    def __init__(self, payload=None):
        self.lock = threading.Lock()
        self.inflight = set()
        self.payload = payload or {
            'status': 'ok', 'plan': 'Pro',
            'short': {'used': 25, 'resets_at': 1800000000, 'group': 'Gemini Models', 'minutes': 300},
            'weekly': {'used': 40, 'resets_at': 1800600000, 'group': 'Gemini Models', 'minutes': 10080},
            'observed_at': 100, 'stale': False, 'error': None, 'limit_group': 'Gemini Models',
        }

    def peek(self, identity):
        return dict(self.payload)

    def cached_or_loading(self, identity):
        return dict(self.payload)

    def drop(self, identity):
        pass

    def store(self, identity, quota):
        self.payload = quota

    def fetch(self, blob):
        return dict(self.payload), None


def token(subject, refresh=None, access=None, email=None):
    refresh = refresh or ('refresh-' + subject)
    access = access or ('access-' + subject)
    email = email or (subject + '@example.com')
    payload = base64.urlsafe_b64encode(json.dumps({
        'sub': subject, 'email': email}).encode()).decode().rstrip('=')
    return json.dumps({
        'token': {
            'access_token': access,
            'token_type': 'Bearer',
            'refresh_token': refresh,
            'expiry': '2099-01-01T00:00:00Z',
        },
        'auth_method': 'consumer',
        'id_token': 'header.' + payload + '.sig',
        'id_token_email': email,
    }).encode()


class FakeDesktop:
    def __init__(self):
        self.alive = False
        self.closed = 0
        self.opened = 0

    def running(self):
        return self.alive

    def close(self, timeout=30):
        if self.alive:
            self.closed += 1
            self.alive = False

    def reopen(self):
        self.opened += 1
        self.alive = True


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.old, self.new = token('old'), token('new')
        self.live = MemoryLiveStore(self.old)
        self.desktop = FakeDesktop()
        self.quota = MemoryQuota()
        self.manager = AntigravityProfiles(self.root / 'vault', live=self.live, desktop=self.desktop, quota=self.quota)

    def capture_both(self):
        first = self.manager.capture('Work')
        old_id = first['profiles'][0]['id']
        self.live.blob = self.new
        second = self.manager.capture('Personal')
        new_id = next(p['id'] for p in second['profiles'] if p['alias'] == 'Personal')
        return old_id, new_id

    def test_status_does_not_create_state(self):
        status = self.manager.status()
        self.assertEqual(status['profiles'], [])
        self.assertEqual(status['mode'], 'keychain_swap')
        self.assertFalse(self.manager.directory.exists())
        self.assertEqual(status['live']['email'], 'old@example.com')

    def test_capture_and_list_hide_tokens(self):
        status = self.manager.capture('Work')
        listing = json.dumps(status)
        self.assertNotIn('refresh-old', listing)
        self.assertNotIn('access-old', listing)
        self.assertNotIn('id_token', listing)
        self.assertNotIn('refresh_token', listing)
        profile = status['profiles'][0]
        self.assertEqual(profile['alias'], 'Work')
        self.assertTrue(profile['selected'])
        self.assertEqual(profile['email'], 'old@example.com')
        saved = self.manager.directory / (profile['id'] + '.auth.json')
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.manager.directory / '.nosync').is_file())

    def test_dashboard_summary_lists_profiles_without_tokens(self):
        self.manager.capture('Work')
        summary = self.manager.dashboard_summary()
        payload = json.dumps(summary)
        self.assertNotIn('refresh-old', payload)
        self.assertNotIn('access-old', payload)
        self.assertNotIn('id_token', payload)
        self.assertNotIn('auth.json', payload)
        self.assertTrue(summary['enabled'])
        self.assertEqual(summary['profiles'][0]['alias'], 'Work')
        self.assertEqual(summary['profiles'][0]['email'], 'old@example.com')
        self.assertTrue(summary['profiles'][0]['selected'])
        self.assertEqual(set(summary['profiles'][0]), {'id', 'name', 'alias', 'email', 'archived', 'selected', 'saved_at', 'quota'})
        self.assertEqual(summary['profiles'][0]['quota']['plan'], 'Pro')
        self.assertEqual(summary['profiles'][0]['quota']['short']['used'], 25)
        self.assertEqual(summary['profiles'][0]['quota']['weekly']['used'], 40)

    def test_switch_writes_target_and_snapshots_outgoing(self):
        old_id, new_id = self.capture_both()
        self.live.blob = self.old
        result = self.manager.switch(new_id)
        self.assertTrue(result['changed'])
        self.assertEqual(self.live.blob, self.new)
        self.assertTrue(any(p['id'] == new_id and p['selected'] for p in result['profiles']))
        self.assertEqual(read := (self.manager.directory / (old_id + '.auth.json')).read_bytes(), self.old)
        self.assertFalse(self.manager.journal.exists())
        self.assertEqual(self.desktop.closed, 0)
        self.assertEqual(self.desktop.opened, 0)

    def test_running_ide_blocks_switch_without_relaunch(self):
        _old_id, new_id = self.capture_both()
        self.live.blob = self.old
        self.desktop.alive = True
        with self.assertRaisesRegex(AccountError, 'Đóng Antigravity IDE'):
            self.manager.switch(new_id)
        self.assertEqual(self.live.blob, self.old)
        self.assertFalse(self.manager.journal.exists())

    def test_relaunch_closes_writes_and_reopens(self):
        _old_id, new_id = self.capture_both()
        self.live.blob = self.old
        self.desktop.alive = True
        result = self.manager.switch(new_id, relaunch=True)
        self.assertTrue(result['changed'])
        self.assertTrue(result['relaunched'])
        self.assertEqual(self.live.blob, self.new)
        self.assertEqual(self.desktop.closed, 1)
        self.assertEqual(self.desktop.opened, 1)
        self.assertTrue(self.desktop.alive)

    def test_same_account_does_not_rewrite(self):
        profile = self.manager.capture('Work')['profiles'][0]
        writes = list(self.live.writes)
        result = self.manager.switch(profile['id'])
        self.assertFalse(result['changed'])
        self.assertEqual(self.live.writes, writes)

    def test_failed_write_keeps_journal_and_restores(self):
        _old_id, new_id = self.capture_both()
        self.live.blob = self.old

        class Broken(MemoryLiveStore):
            def write(inner, blob):
                if blob == self.new:
                    raise AccountError('keychain write failed')
                super().write(blob)

        self.manager.live = Broken(self.old)
        with self.assertRaisesRegex(AccountError, 'Switch stopped'):
            self.manager.switch(new_id)
        self.assertTrue(self.manager.journal.exists())
        self.assertEqual(self.manager.live.blob, self.old)
        restored = self.manager.restore()
        self.assertTrue(restored['restored'])
        self.assertFalse(self.manager.journal.exists())
        self.assertEqual(self.manager.live.blob, self.old)

    def test_restore_blocked_when_ide_running(self):
        _old_id, new_id = self.capture_both()
        self.live.blob = self.old

        class Broken(MemoryLiveStore):
            def write(inner, blob):
                raise AccountError('keychain write failed')

        self.manager.live = Broken(self.old)
        with self.assertRaises(AccountError):
            self.manager.switch(new_id)
        self.desktop.alive = True
        with self.assertRaisesRegex(AccountError, 'Đóng Antigravity IDE'):
            self.manager.restore()

    def test_archive_and_remove(self):
        profile = self.manager.capture('Work')['profiles'][0]
        self.manager.update(profile['id'], alias='Renamed', archived=True)
        with self.assertRaisesRegex(AccountError, 'Khôi phục profile'):
            self.manager.switch(profile['id'])
        self.manager.update(profile['id'], archived=False)
        self.manager.remove(profile['id'])
        self.assertEqual(self.manager.status()['profiles'], [])
        self.assertEqual(self.live.blob, self.old)

    def test_refuse_icloud_and_cloudstorage_paths(self):
        cloud = self.root / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs' / 'vault'
        with self.assertRaisesRegex(AccountError, 'iCloud'):
            AntigravityProfiles(cloud, live=self.live, desktop=self.desktop).capture('No')
        with self.assertRaisesRegex(AccountError, 'iCloud'):
            protect_private_directory(self.root / 'Library' / 'CloudStorage' / 'iCloudDrive' / 'vault')

    def test_reject_unknown_and_symlink_paths(self):
        self.root.joinpath('redirect').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(AccountError):
            AntigravityProfiles(self.root / 'redirect' / 'vault', live=self.live, desktop=self.desktop).capture('No')
        profile = self.manager.capture('Work')['profiles'][0]
        for identity in ('../outside', None, 'unknown', 'a' * 64):
            with self.assertRaises(AccountError):
                self.manager.switch(identity)
        self.manager.remove(profile['id'])

    def test_identity_stable_when_access_token_rotates(self):
        first = identify_antigravity(token('user', access='a1'))
        second = identify_antigravity(token('user', access='a2'))
        self.assertEqual(first['id'], second['id'])
        other = identify_antigravity(token('other'))
        self.assertNotEqual(first['id'], other['id'])


class KeychainStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.blob = token('work')
        self.encoded = wrap_keychain_secret(self.blob)

    def test_unwrap_go_keyring_prefix_and_plain_json(self):
        self.assertEqual(unwrap_keychain_secret(self.encoded), self.blob)
        self.assertEqual(unwrap_keychain_secret(self.blob.decode()), self.blob)
        self.assertTrue(self.encoded.startswith(KEYRING_PREFIX))

    def test_write_updates_keychain_atomically_and_mirrors_files(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs.get('input')))
            result = Mock()
            result.returncode = 0
            if command[-1] == '-w' or (len(command) > 1 and command[1] == 'find-generic-password'):
                result.stdout = self.encoded + '\n'
            else:
                result.stdout = ''
            result.stderr = ''
            return result

        store = MacOSLiveStore(home=self.home, run=run)
        store.write(self.blob)
        write = next(item for item in calls if item[0][1:] == ['-i'])
        self.assertEqual(write[0][0], '/usr/bin/security')
        self.assertIn('add-generic-password -U', write[1])
        self.assertIn('-s ' + KEYCHAIN_SERVICE, write[1])
        self.assertIn('-a ' + KEYCHAIN_ACCOUNT, write[1])
        self.assertIn('-T ' + APP, write[1])
        self.assertNotIn(' -A ', ' ' + write[1] + ' ')
        self.assertNotIn('delete-generic-password', write[1])
        self.assertIn(self.encoded, write[1])
        self.assertNotIn(b'refresh-work'.decode() if False else 'refresh-work', json.dumps(write[1]))
        oauth = self.home / '.gemini' / 'oauth_creds.json'
        self.assertFalse(oauth.exists())
        mirrored = self.home / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token'
        standalone = self.home / '.gemini' / 'jetski-standalone-oauth-token'
        self.assertEqual(mirrored.read_bytes(), self.blob)
        self.assertEqual(standalone.read_bytes(), self.blob)
        self.assertEqual(mirrored.stat().st_mode & 0o777, 0o600)

    def test_read_prefers_keychain_over_file(self):
        file_blob = token('file')
        path = self.home / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token'
        path.parent.mkdir(parents=True)
        path.write_bytes(file_blob)

        def run(command, **kwargs):
            result = Mock(returncode=0, stdout=wrap_keychain_secret(self.blob) + '\n', stderr='')
            return result

        store = MacOSLiveStore(home=self.home, run=run)
        self.assertEqual(identify_antigravity(store.read())['email'], 'work@example.com')

    def test_file_fallback_when_keychain_missing(self):
        path = self.home / '.gemini' / 'jetski-standalone-oauth-token'
        path.parent.mkdir(parents=True)
        path.write_bytes(self.blob)

        def run(command, **kwargs):
            return Mock(returncode=44, stdout='', stderr='not found')

        store = MacOSLiveStore(home=self.home, run=run)
        self.assertEqual(store.read(), self.blob)


class DesktopTests(unittest.TestCase):
    def test_plan_only_matches_antigravity_app(self):
        result = Mock(stdout='10 /Applications/Antigravity.app/Contents/MacOS/Antigravity\n'
                      '11 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n'
                      '12 /tmp/Antigravity.app/Contents/MacOS/Antigravity\n')
        with patch('antigravity_provider.sys.platform', 'darwin'), \
             patch('antigravity_provider.subprocess.run', return_value=result) as run:
            rows = AntigravityDesktop().processes()
        self.assertEqual(rows, [{'app': APP, 'pid': 10}])
        self.assertEqual(run.call_args.args[0][0], '/bin/ps')

    def test_quit_never_kills(self):
        run = Mock(returncode=0, stdout='', stderr='')
        desktop = AntigravityDesktop(run=run)
        with patch.object(desktop, 'processes', side_effect=[[{'app': APP, 'pid': 1}], []]):
            desktop.close()
        self.assertEqual(run.call_args.args[0][0], '/usr/bin/osascript')
        self.assertNotIn('kill', json.dumps(run.call_args.args[0]))

    def test_reopen_uses_allowlisted_app(self):
        with patch('antigravity_provider.subprocess.run') as run:
            AntigravityDesktop().reopen()
        self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-a', APP])


if __name__ == '__main__':
    unittest.main()
