"""Google OAuth profiles launched through the official Gemini CLI only.

No Google tokens are read, copied, exported, refreshed or sent by Relay.
Importing this module and reading dashboard status never starts the CLI.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import uuid

from accounts import AccountError, atomic, clean_alias, read_private


DEFAULT_DIRECTORY = Path(__file__).resolve().parent / '.relay-accounts' / 'gemini'
INSTALL_COMMAND = 'npm install -g @google/gemini-cli'
AUTH_ENV = (
    'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS',
    'GOOGLE_GENAI_USE_VERTEXAI', 'GOOGLE_GENAI_USE_GCA',
    'GOOGLE_GEMINI_BASE_URL', 'GOOGLE_VERTEX_BASE_URL',
    'GEMINI_CLI_TRUST_WORKSPACE',
)


def _find_gemini_binary():
    extra = ['/opt/homebrew/bin', '/usr/local/bin', str(Path.home() / '.npm-global' / 'bin'), str(Path.home() / '.local' / 'bin')]
    current = os.environ.get('PATH', '')
    search_path = os.pathsep.join([p for p in [current, *extra] if p])
    return shutil.which('gemini', path=search_path)


class GeminiProfiles:
    def __init__(self, directory=DEFAULT_DIRECTORY):
        self.directory = Path(directory).absolute()
        self.lock = threading.RLock()

    @staticmethod
    def _private_directory(path):
        # Check parents too: never follow a redirect into another profile/home.
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise AccountError('Gemini profile directories must not contain symlinks')
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise AccountError('Gemini directory must be owned by you')
        os.chmod(path, 0o700)

    @contextmanager
    def _locked(self):
        with self.lock:
            self._private_directory(self.directory)
            fd = os.open(self.directory / 'profiles.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise AccountError('Unsafe Gemini profile lock')
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _read(self):
        path = self.directory / 'profiles.json'
        if not path.exists() and not path.is_symlink():
            return []
        try:
            rows = json.loads(read_private(path))
            if not isinstance(rows, list):
                raise ValueError()
            seen = set()
            for row in rows:
                if (not isinstance(row, dict) or set(row) != {'id', 'alias', 'archived'}
                        or not isinstance(row['id'], str) or not re.fullmatch(r'[a-f0-9]{32}', row['id'])
                        or row['id'] in seen or type(row['archived']) is not bool
                        or not clean_alias(row['alias'])):
                    raise ValueError()
                seen.add(row['id'])
            return rows
        except (ValueError, TypeError, KeyError):
            raise AccountError('Invalid Gemini profile metadata') from None

    def _profile(self, rows, identity):
        if not isinstance(identity, str):
            raise AccountError('Unknown Gemini profile')
        row = next((row for row in rows if row['id'] == identity), None)
        if row is None:
            raise AccountError('Unknown Gemini profile')
        return row

    def _home(self, identity):
        home = self.directory / identity
        if any(path.is_symlink() for path in (home, home / '.gemini')):
            raise AccountError('Unsafe Gemini profile home')
        return home

    def status(self):
        # No directory creation, credential inspection or subprocess on GET.
        with self.lock:
            if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
                raise AccountError('Unsafe Gemini profile directory')
            rows = self._read()
            script = str(Path(__file__).resolve())
            profiles = []
            for row in rows:
                home = self._home(row['id'])
                base = ['python3', script, '--directory', str(self.directory)]
                creds = home / '.gemini' / 'oauth_creds.json'
                signed_in = (not creds.is_symlink()) and creds.is_file()
                profiles.append(dict(row, home=str(home), auth_status='signed_in' if signed_in else 'managed_by_cli',
                    signed_in=signed_in,
                    login_command=shlex.join(base + ['login', row['id']]),
                    run_command=shlex.join(base + ['run', row['id'], '--project', '/path/to/project']),
                    quota={'status': 'unknown', 'source': 'Gemini CLI /stats model'}))
            return {'enabled': True, 'provider': 'gemini-cli', 'auth': 'google-oauth',
                    'cli_installed': _find_gemini_binary() is not None,
                    'install_command': INSTALL_COMMAND,
                    'proxy_supported': False, 'profiles': profiles}

    def create(self, alias):
        alias = clean_alias(alias)
        if not alias:
            raise AccountError('A Gemini profile alias is required')
        with self._locked():
            rows = self._read()
            if len(rows) >= 100:
                raise AccountError('Gemini profile limit reached')
            identity = uuid.uuid4().hex
            home = self._home(identity)
            self._private_directory(home)
            self._private_directory(home / '.gemini')
            # Native CLI owns authentication. No shared ~/.gemini is modified.
            atomic(home / '.gemini' / 'settings.json', {
                'security': {'auth': {'selectedType': 'oauth-personal'}}})
            rows.append({'id': identity, 'alias': alias, 'archived': False})
            atomic(self.directory / 'profiles.json', rows)
        return self.status()

    def update(self, identity, alias=None, archived=None):
        with self._locked():
            rows = self._read()
            row = self._profile(rows, identity)
            if alias is not None:
                alias = clean_alias(alias)
                if not alias:
                    raise AccountError('A Gemini profile alias is required')
                row['alias'] = alias
            if archived is not None:
                if type(archived) is not bool:
                    raise AccountError('archived must be a boolean')
                row['archived'] = archived
            atomic(self.directory / 'profiles.json', rows)
        return self.status()

    def open_login_terminal(self, identity):
        if sys.platform != 'darwin':
            raise AccountError('Mở Terminal tự động hiện chỉ hỗ trợ macOS; hãy chạy lệnh đăng nhập trong Terminal')
        with self._locked():
            row = self._profile(self._read(), identity)
            if row['archived']:
                raise AccountError('Khôi phục profile Gemini trước khi đăng nhập')
            home = self._home(identity)
            if not (home / '.gemini').is_dir():
                raise AccountError('Thư mục profile Gemini không tồn tại')
            if not _find_gemini_binary():
                raise AccountError(f'Chưa cài Gemini CLI. Chạy trong Terminal: {INSTALL_COMMAND}')
            script_path = str(Path(__file__).resolve())
            command = shlex.join(['python3', script_path, '--directory', str(self.directory), 'login', row['id']])
        applescript = (
            'on run argv\n'
            '  tell application "Terminal"\n'
            '    activate\n'
            '    do script (item 1 of argv)\n'
            '  end tell\n'
            'end run'
        )
        try:
            subprocess.run(['/usr/bin/osascript', '-e', applescript, '--', command],
                           capture_output=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError):
            raise AccountError('Không mở được Terminal tự động. Hãy copy lệnh đăng nhập và chạy trong Terminal.') from None
        result = self.status()
        result.update(launched=True, launched_profile=identity)
        return result

    def start_login(self, alias=None):
        cleaned = clean_alias(alias)
        if not _find_gemini_binary():
            raise AccountError(f'Chưa cài Gemini CLI. Chạy trong Terminal: {INSTALL_COMMAND}')
        with self._locked():
            rows = self._read()
            if not cleaned:
                used = {row['alias'] for row in rows}
                index = len(rows) + 1
                while f'Gemini {index}' in used:
                    index += 1
                cleaned = f'Gemini {index}'
        created = self.create(cleaned)
        identity = created['profiles'][-1]['id']
        result = self.open_login_terminal(identity)
        result['created'] = True
        return result

    def launch(self, identity, project=None):
        with self._locked():
            row = self._profile(self._read(), identity)
            if row['archived']:
                raise AccountError('Restore the archived Gemini profile before use')
            home = self._home(identity)
            if not (home / '.gemini').is_dir():
                raise AccountError('Gemini profile home is missing')
            binary = _find_gemini_binary()
            if not binary:
                raise AccountError('Install the official Gemini CLI before signing in or running')
            cwd = Path(project).expanduser().resolve() if project is not None else home
            if not cwd.is_dir():
                raise AccountError('Project directory does not exist')
            lease = os.open(home / 'client.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(lease)
                raise AccountError('This Gemini profile already has a running client') from None
        try:
            env = {key: value for key, value in os.environ.items() if key not in AUTH_ENV}
            env['GEMINI_CLI_HOME'] = str(home)
            # Interactive native CLI owns consent, OAuth refresh and model calls.
            # Never capture its credential/login output or retry a failed session.
            return subprocess.run([binary], cwd=cwd, env=env, check=False).returncode
        finally:
            os.close(lease)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT_DIRECTORY)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    create = commands.add_parser('create')
    create.add_argument('alias')
    for name in ('login', 'run'):
        command = commands.add_parser(name)
        command.add_argument('identity')
        if name == 'run':
            command.add_argument('--project', type=Path, required=True)
    args = parser.parse_args()
    manager = GeminiProfiles(args.directory)
    try:
        if args.command in ('login', 'run'):
            return manager.launch(args.identity, getattr(args, 'project', None))
        result = manager.create(args.alias) if args.command == 'create' else manager.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AccountError, OSError):
        # Do not include subprocess/credential payloads in diagnostics.
        print('Cannot complete Gemini operation. Check CLI installation, profile and directory permissions.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
