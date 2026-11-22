"""Antigravity IDE multi-account switching via the shared macOS Keychain item.

Approach B: close the IDE, snapshot the live `gemini`/`antigravity` token,
write another saved token, then reopen. Only one account can be live at a time.
"""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
import uuid

from accounts import AccountError, atomic, claims, clean_alias, display_name, profile_alias, read_private
from antigravity_quota import QuotaFetcher, updated_blob


DEFAULT_DIRECTORY = Path(__file__).resolve().parent / '.relay-accounts' / 'antigravity'
APP = '/Applications/Antigravity.app'
KEYCHAIN_SERVICE = 'gemini'
KEYCHAIN_ACCOUNT = 'antigravity'
KEYRING_PREFIX = 'go-keyring-base64:'
SECURITY = '/usr/bin/security'
KEYCHAIN_TRUSTED = (APP, SECURITY)
FILE_RELATIVE = (
    Path('.gemini') / 'antigravity-cli' / 'antigravity-oauth-token',
    Path('.gemini') / 'jetski-standalone-oauth-token',
)
CLOUD_MARKERS = (
    '/library/mobile documents/',
    '/library/cloudstorage/',
    '/mobile documents/',
    'icloud drive',
)
BACKUP_XATTRS = (
    'com.apple.metadata:com_apple_backup_excludeItem',
    'com.apple.fileprovider.ignore#P',
)


def cloud_sync_path(path):
    text = str(Path(path).absolute()).replace('\\', '/').lower()
    return any(marker in text for marker in CLOUD_MARKERS)


def _mkdir_private(path):
    if path.exists():
        return
    if path.parent == path:
        raise AccountError('Cannot create Antigravity credential directory')
    _mkdir_private(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def protect_private_directory(path):
    path = Path(path).absolute()
    if cloud_sync_path(path):
        raise AccountError('Không lưu token Antigravity trên iCloud Drive hoặc thư mục đồng bộ đám mây')
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise AccountError('Use real directories, not symlinked credential paths')
    _mkdir_private(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AccountError('Profile directory is not owned by you')
    os.chmod(path, 0o700)
    parent = path.parent
    if parent.name == '.relay-accounts' and parent.stat().st_uid == os.getuid() and parent.is_dir():
        os.chmod(parent, 0o700)
    marker = path / '.nosync'
    if marker.is_symlink():
        raise AccountError('Unsafe Antigravity cloud-exclusion marker')
    if not marker.exists():
        fd = os.open(marker, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    _exclude_from_cloud(path)
    return path


def _exclude_from_cloud(path):
    setter = getattr(os, 'setxattr', None)
    if setter is not None:
        for name in BACKUP_XATTRS:
            try:
                setter(path, name, b'1')
            except OSError:
                pass
        return
    if sys.platform != 'darwin':
        return
    for name in BACKUP_XATTRS:
        try:
            subprocess.run(['/usr/bin/xattr', '-w', name, '1', str(path)],
                           capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def identify_antigravity(data):
    """Parse LOCAL Antigravity OAuth JSON. This is not signature validation."""
    try:
        doc = json.loads(data)
        if not isinstance(doc, dict):
            raise ValueError()
        token = doc.get('token') if isinstance(doc.get('token'), dict) else doc
        if not isinstance(token, dict):
            raise ValueError()
        refresh = token.get('refresh_token')
        access = token.get('access_token')
        if not (isinstance(refresh, str) and refresh) and not (isinstance(access, str) and access):
            raise ValueError()
        id_token = doc.get('id_token')
        if not isinstance(id_token, str) or not id_token:
            id_token = token.get('id_token') if isinstance(token.get('id_token'), str) else None
        email = doc.get('id_token_email') if isinstance(doc.get('id_token_email'), str) else None
        subject = None
        if id_token:
            payload = claims(id_token)
            if isinstance(payload.get('sub'), str) and payload['sub']:
                subject = payload['sub']
            if isinstance(payload.get('email'), str) and payload['email']:
                email = payload['email']
        secret = refresh if isinstance(refresh, str) and refresh else access
        identity = hashlib.sha256(((subject or '') + '\0' + secret).encode()).hexdigest()
        method = doc.get('auth_method') if isinstance(doc.get('auth_method'), str) else None
        return {'id': identity, 'email': email, 'auth_method': method}
    except (ValueError, TypeError, AttributeError, AccountError):
        raise AccountError('Expected an Antigravity OAuth snapshot in Keychain or the token file') from None


def unwrap_keychain_secret(text):
    if not isinstance(text, str) or not text.strip():
        raise AccountError('Keychain token is empty')
    value = text.strip()
    if value.startswith(KEYRING_PREFIX):
        try:
            decoded = base64.b64decode(value[len(KEYRING_PREFIX):], validate=True)
        except (ValueError, TypeError):
            raise AccountError('Keychain token encoding is invalid') from None
        if not decoded:
            raise AccountError('Keychain token is empty')
        return decoded
    return value.encode()


def wrap_keychain_secret(blob):
    return KEYRING_PREFIX + base64.b64encode(blob).decode('ascii')


def credential_digest(blob):
    info = identify_antigravity(blob)
    doc = json.loads(blob)
    token = doc.get('token') if isinstance(doc.get('token'), dict) else doc
    refresh = token.get('refresh_token') if isinstance(token, dict) else None
    secret = refresh if isinstance(refresh, str) and refresh else info['id']
    return hashlib.sha256(secret.encode()).hexdigest()


class MemoryLiveStore:
    """In-memory Keychain/file stand-in for tests."""

    def __init__(self, blob=None):
        self.blob = blob
        self.writes = []

    def read(self):
        if not self.blob:
            raise AccountError('Không đọc được token Antigravity trong Keychain')
        return self.blob

    def write(self, blob):
        identify_antigravity(blob)
        self.blob = blob
        self.writes.append(blob)


class MacOSLiveStore:
    def __init__(self, home=None, run=None):
        self.home = Path(home or Path.home()).absolute()
        self.run = subprocess.run if run is None else run

    def _files(self):
        return [self.home / relative for relative in FILE_RELATIVE]

    def _security(self, args, stdin=None):
        try:
            return self.run([SECURITY, *args], input=stdin, capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            raise AccountError('Không truy cập được macOS Keychain') from None

    def _read_keychain(self):
        result = self._security(['find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', KEYCHAIN_ACCOUNT, '-w'])
        if result.returncode:
            return None
        try:
            return unwrap_keychain_secret(result.stdout)
        except AccountError:
            return None

    def _read_files(self):
        for path in self._files():
            if path.is_symlink() or not path.is_file():
                continue
            try:
                blob = read_private(path)
                identify_antigravity(blob)
                return blob
            except AccountError:
                continue
        return None

    def read(self):
        blob = self._read_keychain()
        if blob is not None:
            identify_antigravity(blob)
            return blob
        blob = self._read_files()
        if blob is not None:
            return blob
        raise AccountError('Không đọc được token Antigravity trong Keychain (gemini / antigravity)')

    def write(self, blob):
        identify_antigravity(blob)
        encoded = wrap_keychain_secret(blob)
        trusted = ''.join(' -T %s' % shlex.quote(app) for app in KEYCHAIN_TRUSTED)
        command = 'add-generic-password -U -s %s -a %s%s -w %s\n' % (
            shlex.quote(KEYCHAIN_SERVICE), shlex.quote(KEYCHAIN_ACCOUNT), trusted, shlex.quote(encoded))
        result = self._security(['-i'], stdin=command)
        if result.returncode:
            raise AccountError('Không ghi được token Antigravity vào Keychain')
        live = self._read_keychain()
        if live is None or credential_digest(live) != credential_digest(blob):
            raise AccountError('Keychain không khớp token vừa ghi; không mở lại IDE')
        for path in self._files():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            atomic(path, blob)
            os.chmod(path, 0o600)


class AntigravityDesktop:
    def __init__(self, app=APP, run=None):
        self.app = app
        self.run = subprocess.run if run is None else run

    def processes(self):
        if sys.platform != 'darwin':
            raise AccountError('Đổi tài khoản Antigravity hiện chỉ hỗ trợ macOS')
        try:
            result = self.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            raise AccountError('Không kiểm tra được process Antigravity') from None
        rows = []
        prefix = self.app + '/Contents/'
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit() and parts[1].startswith(prefix):
                rows.append({'app': self.app, 'pid': int(parts[0])})
        return rows

    def running(self):
        return bool(self.processes())

    def close(self, timeout=30):
        if self.app != APP:
            raise AccountError('Unsupported Antigravity application')
        if not self.running():
            return
        try:
            self.run(['/usr/bin/osascript', '-e', 'tell application "' + self.app + '" to quit'],
                     capture_output=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError):
            raise AccountError('Antigravity IDE không nhận lệnh thoát; hãy đóng thủ công. Không force kill.') from None
        deadline = time.monotonic() + timeout
        while self.running():
            if time.monotonic() >= deadline:
                raise AccountError('Antigravity IDE vẫn chạy sau khi quit; hãy đóng thủ công. Không force kill.')
            time.sleep(0.5)

    def reopen(self):
        if self.app != APP:
            raise AccountError('Unsupported Antigravity application')
        try:
            self.run(['/usr/bin/open', '-a', self.app], capture_output=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError):
            raise AccountError('Không mở lại được Antigravity IDE; hãy mở thủ công') from None


class AntigravityProfiles:
    def __init__(self, directory=DEFAULT_DIRECTORY, live=None, desktop=None, home=None, quota=None):
        self.directory = Path(directory).absolute()
        self.live = live or MacOSLiveStore(home=home)
        self.desktop = desktop or AntigravityDesktop()
        self.quota = quota or QuotaFetcher()
        self.lock = threading.RLock()
        self.registry = self.directory / 'profiles.json'
        self.journal = self.directory / 'switch-pending.json'
        self.removed = self.directory / 'removed-profiles.json'

    def _private_directory(self):
        protect_private_directory(self.directory)

    @contextmanager
    def _locked(self):
        with self.lock:
            self._private_directory()
            fd = os.open(self.directory / 'profiles.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise AccountError('Unsafe Antigravity profile lock')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise AccountError('Another Antigravity account operation is running') from None
                yield
            finally:
                os.close(fd)

    def _load(self):
        if not self.registry.exists():
            return {}
        try:
            result = json.loads(read_private(self.registry))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except ValueError:
            raise AccountError('Antigravity profile registry is invalid; inspect backups') from None

    def _live_info(self):
        try:
            blob = self.live.read()
            return identify_antigravity(blob), blob
        except AccountError:
            return None, None

    def _public(self, extra=None):
        current, _blob = self._live_info()
        current_id = current['id'] if current else None
        registry = self._load() if self.registry.exists() else {}
        profiles = []
        for key, profile in registry.items():
            if not isinstance(profile, dict):
                continue
            row = {
                'id': key,
                'name': display_name(profile, key),
                'display_name': display_name(profile, key),
                'alias': profile_alias(profile),
                'email': profile.get('email') if isinstance(profile.get('email'), str) else None,
                'saved_at': profile.get('saved_at'),
                'archived': bool(profile.get('archived')),
                'selected': key == current_id,
                'quota': None,
            }
            if not row['archived']:
                row['quota'] = self.quota.cached_or_loading(key)
                self._schedule_quota(key)
            profiles.append(row)
        result = {
            'enabled': True,
            'provider': 'antigravity-ide',
            'auth': 'macos-keychain',
            'mode': 'keychain_swap',
            'platform': sys.platform,
            'app': APP,
            'app_installed': Path(APP).is_dir() and not Path(APP).is_symlink(),
            'pending_recovery': self.journal.exists(),
            'live': {
                'readable': current is not None,
                'email': current.get('email') if current else None,
                'id': current_id,
            },
            'profiles': profiles,
            'proxy_supported': False,
            'scope': 'one live Keychain login; Antigravity 2.6+ stores the token in gemini/antigravity',
            'security': {
                'tokens_are_login': True,
                'directory_mode': '0700',
                'icloud_sync': False,
                'keychain_acl': list(KEYCHAIN_TRUSTED),
            },
        }
        if extra:
            result.update(extra)
        return result

    def status(self):
        with self.lock:
            if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
                raise AccountError('Unsafe Antigravity profile directory')
            if not self.directory.exists():
                current, _blob = self._live_info()
                return {
                    'enabled': True, 'provider': 'antigravity-ide', 'auth': 'macos-keychain',
                    'mode': 'keychain_swap', 'platform': sys.platform, 'app': APP,
                    'app_installed': Path(APP).is_dir() and not Path(APP).is_symlink(),
                    'pending_recovery': False,
                    'live': {
                        'readable': current is not None,
                        'email': current.get('email') if current else None,
                        'id': current['id'] if current else None,
                    },
                    'profiles': [], 'proxy_supported': False,
                    'scope': 'one live Keychain login; Antigravity 2.6+ stores the token in gemini/antigravity',
                    'security': {
                        'tokens_are_login': True,
                        'directory_mode': '0700',
                        'icloud_sync': False,
                        'keychain_acl': list(KEYCHAIN_TRUSTED),
                    },
                }
            return self._public()

    def dashboard_summary(self):
        """Public inventory for the accounts table. Never includes token fields."""
        try:
            status = self.status()
        except (AccountError, OSError):
            return {'enabled': True, 'readable': False, 'profiles': []}
        profiles = []
        for profile in status.get('profiles') or []:
            if not isinstance(profile, dict):
                continue
            identity = profile.get('id')
            row = {
                'id': identity,
                'name': profile.get('name') or profile.get('display_name'),
                'alias': profile.get('alias') if isinstance(profile.get('alias'), str) else None,
                'email': profile.get('email') if isinstance(profile.get('email'), str) else None,
                'archived': bool(profile.get('archived')),
                'selected': bool(profile.get('selected')),
                'saved_at': profile.get('saved_at') if isinstance(profile.get('saved_at'), (int, float)) else None,
                'quota': None,
            }
            if isinstance(identity, str) and not row['archived']:
                row['quota'] = self.quota.cached_or_loading(identity)
                self._schedule_quota(identity)
            profiles.append(row)
        live = status.get('live') if isinstance(status.get('live'), dict) else {}
        return {
            'enabled': True,
            'readable': True,
            'pending_recovery': bool(status.get('pending_recovery')),
            'live': {
                'readable': bool(live.get('readable')),
                'email': live.get('email') if isinstance(live.get('email'), str) else None,
                'id': live.get('id') if isinstance(live.get('id'), str) else None,
            },
            'profiles': profiles,
        }

    def _auth_path(self, identity):
        return self.directory / (identity + '.auth.json')

    def _schedule_quota(self, identity, force=False):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
            return
        cached = self.quota.peek(identity)
        if cached and not force and not cached.get('stale') and cached.get('status') == 'ok':
            return
        with self.quota.lock:
            if identity in self.quota.inflight:
                return
            self.quota.inflight.add(identity)
        worker = threading.Thread(target=self._refresh_one_quota, args=(identity,), daemon=True)
        worker.start()

    def _refresh_one_quota(self, identity):
        try:
            path = self._auth_path(identity)
            if path.is_symlink() or not path.is_file():
                self.quota.store(identity, {
                    'status': 'error', 'plan': None, 'short': None, 'weekly': None, 'groups': [],
                    'observed_at': time.time(), 'stale': False,
                    'error': 'Không đọc được snapshot Antigravity', 'limit_group': None,
                })
                return
            blob = read_private(path)
            result, refreshed = self.quota.fetch(blob)
            self.quota.store(identity, result)
            if refreshed is not None:
                try:
                    if identify_antigravity(updated_blob(refreshed))['id'] == identity:
                        atomic(path, updated_blob(refreshed))
                        os.chmod(path, 0o600)
                except (AccountError, OSError):
                    pass
        except AccountError as error:
            previous = self.quota.peek(identity)
            payload = {
                'status': 'error', 'plan': None, 'short': None, 'weekly': None, 'groups': [],
                'observed_at': time.time(), 'stale': False,
                'error': str(error), 'limit_group': None,
            }
            if previous and (previous.get('short') or previous.get('weekly')):
                payload = dict(previous)
                payload['stale'] = True
                payload['error'] = str(error)
            self.quota.store(identity, payload)
        except OSError:
            self.quota.store(identity, {
                'status': 'error', 'plan': None, 'short': None, 'weekly': None, 'groups': [],
                'observed_at': time.time(), 'stale': False,
                'error': 'Không đọc được snapshot Antigravity', 'limit_group': None,
            })
        finally:
            with self.quota.lock:
                self.quota.inflight.discard(identity)

    def refresh_quota(self, identity=None, wait=True):
        if identity is not None and (not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity)):
            raise AccountError('Unknown saved Antigravity profile')
        status = self.status()
        targets = []
        for profile in status.get('profiles') or []:
            if profile.get('archived') or not isinstance(profile.get('id'), str):
                continue
            if identity is None or profile['id'] == identity:
                targets.append(profile['id'])
        if identity is not None and identity not in targets:
            raise AccountError('Unknown saved Antigravity profile')
        for key in targets:
            self.quota.drop(key)
            if wait:
                self._refresh_one_quota(key)
            else:
                self._schedule_quota(key, force=True)
        return self.dashboard_summary()

    def capture(self, name=None):
        alias = clean_alias(name)
        with self._locked():
            if self.journal.exists():
                raise AccountError('Khôi phục lần chuyển Antigravity bị gián đoạn trước')
            blob = self.live.read()
            info = identify_antigravity(blob)
            registry = self._load()
            alias = alias or profile_alias(registry.get(info['id'], {}))
            info.update(alias=alias, saved_at=time.time(), archived=False)
            info['name'] = display_name(info, info['id'])
            atomic(self.directory / (info['id'] + '.auth.json'), blob)
            registry[info['id']] = info
            atomic(self.registry, registry)
            if self.removed.exists():
                removed = json.loads(read_private(self.removed))
                if info['id'] in removed:
                    atomic(self.removed, [key for key in removed if key != info['id']])
        return self.status()

    def update(self, identity, alias=None, archived=None):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
            raise AccountError('Unknown saved Antigravity profile')
        with self._locked():
            if self.journal.exists():
                raise AccountError('Khôi phục lần chuyển Antigravity bị gián đoạn trước')
            registry = self._load()
            if identity not in registry:
                raise AccountError('Unknown saved Antigravity profile')
            if alias is not None:
                registry[identity]['alias'] = clean_alias(alias)
                registry[identity]['name'] = display_name(registry[identity], identity)
            if archived is not None:
                if type(archived) is not bool:
                    raise AccountError('archived must be a boolean')
                registry[identity]['archived'] = archived
            atomic(self.registry, registry)
        return self.status()

    def remove(self, identity):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
            raise AccountError('Unknown saved Antigravity profile')
        with self._locked():
            if self.journal.exists():
                raise AccountError('Khôi phục lần chuyển Antigravity bị gián đoạn trước')
            registry = self._load()
            if identity not in registry:
                raise AccountError('Unknown saved Antigravity profile')
            removed = json.loads(read_private(self.removed)) if self.removed.exists() else []
            if identity not in removed:
                removed.append(identity)
            atomic(self.removed, removed)
            registry.pop(identity)
            atomic(self.registry, registry)
            path = self.directory / (identity + '.auth.json')
            if path.exists() or path.is_symlink():
                path.unlink()
        result = self.status()
        result['removed'] = identity
        result['active_login_unchanged'] = True
        return result

    def _snapshot_outgoing(self, registry, live_blob):
        info = identify_antigravity(live_blob)
        removed = json.loads(read_private(self.removed)) if self.removed.exists() else []
        if info['id'] in removed:
            return info
        existing = registry.get(info['id'], {})
        info.update(alias=profile_alias(existing), saved_at=time.time(),
                    archived=bool(existing.get('archived')))
        info['name'] = display_name(info, info['id'])
        registry[info['id']] = info
        atomic(self.directory / (info['id'] + '.auth.json'), live_blob)
        atomic(self.registry, registry)
        return info

    def switch(self, identity, relaunch=False):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
            raise AccountError('Unknown saved Antigravity profile')
        if type(relaunch) is not bool:
            raise AccountError('relaunch must be a boolean')
        closed = False
        with self._locked():
            if self.journal.exists():
                raise AccountError('A prior Antigravity switch needs recovery')
            registry = self._load()
            if identity not in registry:
                raise AccountError('Unknown saved Antigravity profile')
            if registry[identity].get('archived'):
                raise AccountError('Khôi phục profile Antigravity trước khi dùng')
            target = read_private(self.directory / (identity + '.auth.json'))
            if identify_antigravity(target)['id'] != identity:
                raise AccountError('Saved Antigravity credentials do not match profile identity')
            live = self.live.read()
            live_info = identify_antigravity(live)
            if live_info['id'] == identity:
                return self._public({'changed': False, 'restart_required': True, 'selected_profile': identity})
            running = self.desktop.running()
            if running and not relaunch:
                raise AccountError('Đóng Antigravity IDE trước khi đổi tài khoản, hoặc bật tự đóng/mở lại')
            if running:
                self.desktop.close()
                closed = True
                live = self.live.read()
                live_info = identify_antigravity(live)
                if live_info['id'] == identity:
                    extra = {'changed': False, 'restart_required': True, 'selected_profile': identity}
                    if relaunch:
                        self.desktop.reopen()
                        extra['relaunched'] = True
                    return self._public(extra)
            outgoing = self._snapshot_outgoing(registry, live)
            backup = 'backup-' + uuid.uuid4().hex + '.auth.json'
            atomic(self.directory / backup, live)
            atomic(self.journal, {'backup': backup, 'before': outgoing['id'], 'target': identity,
                                  'target_digest': credential_digest(target), 'closed': closed})
            try:
                if self.desktop.running():
                    raise AccountError('Antigravity IDE opened again during switch')
                self.live.write(target)
                written = self.live.read()
                if credential_digest(written) != credential_digest(target):
                    raise AccountError('Auth replacement verification failed')
                atomic(self.directory / 'last-switch.json',
                       {'before': outgoing['id'], 'target': identity, 'backup': backup, 'at': time.time()})
                self.journal.unlink()
            except Exception:
                try:
                    self.live.write(live)
                except AccountError:
                    pass
                raise AccountError('Switch stopped; backup retained. Restore before retrying') from None
            extra = {'changed': True, 'restart_required': True, 'selected_profile': identity,
                     'message': 'Đã ghi token vào Keychain. Mở lại Antigravity IDE và kiểm tra tài khoản hiển thị.'}
            if relaunch:
                self.desktop.reopen()
                extra['relaunched'] = True
            return self._public(extra)

    def restore(self):
        with self._locked():
            if self.desktop.running():
                raise AccountError('Đóng Antigravity IDE trước khi khôi phục lần chuyển bị gián đoạn')
            if not self.journal.exists():
                raise AccountError('No interrupted Antigravity switch to restore')
            info = json.loads(read_private(self.journal))
            backup = info.get('backup', '')
            if not re.fullmatch(r'backup-[0-9a-f]{32}\.auth\.json', backup):
                raise AccountError('Invalid recovery journal')
            old = read_private(self.directory / backup)
            current = self.live.read()
            if (credential_digest(current) != credential_digest(old)
                    and credential_digest(current) != info.get('target_digest')):
                raise AccountError('Live Keychain changed independently; restore blocked')
            self.live.write(old)
            self.journal.unlink()
            return self._public({'restored': True, 'restart_required': True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT_DIRECTORY)
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('capture')
    capture.add_argument('--name', default=None)
    update = sub.add_parser('alias')
    update.add_argument('identity')
    update.add_argument('--name', default='')
    remove = sub.add_parser('remove')
    remove.add_argument('identity')
    sub.add_parser('list')
    switch = sub.add_parser('switch')
    switch.add_argument('identity')
    switch.add_argument('--relaunch', action='store_true')
    sub.add_parser('restore')
    args = parser.parse_args()
    manager = AntigravityProfiles(args.directory.resolve())
    if args.command == 'capture':
        result = manager.capture(args.name)
    elif args.command == 'list':
        result = manager.status()
    elif args.command == 'switch':
        result = manager.switch(args.identity, relaunch=args.relaunch)
    elif args.command == 'alias':
        result = manager.update(args.identity, args.name)
    elif args.command == 'remove':
        result = manager.remove(args.identity)
    else:
        result = manager.restore()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except AccountError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except OSError:
        print('Antigravity account file operation failed; inspect permissions and recovery journal.', file=sys.stderr)
        raise SystemExit(1)
