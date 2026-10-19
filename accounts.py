"""Local file-auth profile manager. Never restarts or controls running clients."""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid


class AccountError(ValueError):
    pass


def clean_alias(value):
    if value is None:
        return None
    if not isinstance(value,str) or len(value.strip())>80 or any(ord(c)<32 or ord(c)==127 for c in value):
        raise AccountError('Alias must be at most 80 printable characters')
    return value.strip() or None


def profile_alias(profile):
    if 'alias' in profile:
        return clean_alias(profile['alias'])
    legacy=profile.get('name')
    return None if legacy in (None,'Codex local hiện tại','Previous account','Current account') else clean_alias(legacy)


def automatic_alias(profile):
    email=profile.get('email')
    if not isinstance(email,str) or not email.split('@',1)[0]:return None
    kind=profile.get('account_type') or 'unknown'
    kind={'business':'team'}.get(kind,kind)
    if kind not in ('free','plus','pro','team','enterprise','edu'):kind='unknown'
    return clean_alias(email.split('@',1)[0][:80-len(kind)-1]+'_'+kind)


def display_name(profile, identity=''):
    return profile_alias(profile) or automatic_alias(profile) or profile.get('email') or identity[:12] or 'Tài khoản chưa rõ' 


def read_private(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise AccountError('Credential file must be a regular file owned by you')
            value = handle.read(1024 * 1024 + 1)
            if len(value) > 1024 * 1024:
                raise AccountError('Credential file is too large')
            return value
    except OSError:
        raise AccountError('Cannot read credential file; symlinks are not supported') from None


def atomic(path, data):
    if path.is_symlink():
        raise AccountError('Refusing symlink destination')
    data = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, indent=2).encode()
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.relay-')
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def claims(token):
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError()
        result = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise AccountError('Unsupported local token metadata') from None


def identify(data):
    """Parse LOCAL metadata only. This is not signature/entitlement validation."""
    try:
        doc = json.loads(data)
        tokens = doc.get('tokens')
        if doc.get('auth_mode') not in (None, 'chatgpt') or not isinstance(tokens, dict):
            raise ValueError()
        if not all(isinstance(tokens.get(k), str) and tokens[k] for k in ('access_token', 'id_token', 'account_id')):
            raise ValueError()
        payload = claims(tokens['id_token'])
        subject = payload.get('sub')
        if not isinstance(subject, str) or not subject:
            raise ValueError()
        identity = hashlib.sha256((subject+'\0'+tokens['account_id']).encode()).hexdigest()
        metadata=payload.get('https://api.openai.com/auth')
        plan=metadata.get('chatgpt_plan_type') if isinstance(metadata,dict) else None
        if not plan:
            try:access_metadata=claims(tokens['access_token']).get('https://api.openai.com/auth') if tokens['access_token'].count('.')==2 else None
            except AccountError:access_metadata=None
            plan=access_metadata.get('chatgpt_plan_type') if isinstance(access_metadata,dict) else None
        return {'id':identity, 'email':payload.get('email') if isinstance(payload.get('email'), str) else None,
                'account_type':plan if isinstance(plan,str) else None}
    except (ValueError, TypeError, AttributeError):
        raise AccountError('Expected a ChatGPT file login; API keys/service credentials are not supported here') from None


def clients_stopped():
    """Conservative process guard. No command lines or credentials are read."""
    try:
        output = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True,
                                text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        raise AccountError('Cannot verify client processes; switch blocked') from None
    blockers=[]
    for command in output.splitlines():
        name = Path(command.strip()).name.lower()
        if (name in ('codex', 'codext', 'chatgpt', 'code', 'code-insiders')
                or 'code helper' in name or 'codex helper' in name or 'chatgpt helper' in name
                or any(marker in command.lower() for marker in ('visual studio code.app/', 'chatgpt.app/', 'codex.app/'))):
            blockers.append(command.strip())
    if blockers:
        raise AccountError('Client còn chạy: '+ '; '.join(dict.fromkeys(blockers))[:1500])


class Accounts:
    def __init__(self, directory, codex_home, guard=clients_stopped):
        self.directory = Path(directory).absolute()
        self.home = Path(codex_home).absolute()
        self.guard = guard
        for path in (self.directory, self.home):
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                # macOS /tmp alias should be resolved by callers before use.
                raise AccountError('Use real directories, not symlinked credential paths')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.stat().st_uid != os.getuid():
            raise AccountError('Profile directory is not owned by you')
        os.chmod(self.directory, 0o700)
        self.registry = self.directory / 'profiles.json'
        self.journal = self.directory / 'switch-pending.json'
        self.removed = self.directory / 'removed-profiles.json'

    @contextmanager
    def lock(self):
        fd = os.open(self.directory/'profiles.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AccountError('Another account operation is running') from None
            yield
        finally:
            os.close(fd)

    def load(self):
        if not self.registry.exists():
            return {}
        try:
            result = json.loads(read_private(self.registry))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except ValueError:
            raise AccountError('Profile registry is invalid; inspect backups') from None

    def file_mode(self):
        try:
            text = (self.home/'config.toml').read_text()
        except OSError:
            text = ''
        # Only a top-level explicit file setting is accepted; no TOML rewrite.
        top = re.split(r'^\s*\[', text, maxsplit=1, flags=re.M)[0]
        if not re.search(r'^\s*cli_auth_credentials_store\s*=\s*[\x22\x27]file[\x22\x27]\s*(?:#.*)?$', top, re.M):
            raise AccountError('Set top-level cli_auth_credentials_store = "file" in your Codex config and sign in again first')

    def capture(self, name=None, source=None):
        alias=clean_alias(name)
        with self.lock():
            if self.journal.exists():
                raise AccountError('Resolve the pending switch with accounts.py restore first')
            data = read_private(Path(source) if source else self.home/'auth.json')
            info = identify(data)
            registry = self.load()
            alias=alias or profile_alias(registry.get(info['id'],{}))
            info.update(alias=alias, saved_at=time.time())
            info['name']=display_name(info,info['id'])
            atomic(self.directory/(info['id']+'.auth.json'), data)
            registry[info['id']] = info
            atomic(self.registry, registry)
            if self.removed.exists():
                removed=json.loads(read_private(self.removed))
                if info['id'] in removed:
                    atomic(self.removed,[key for key in removed if key!=info['id']])
            return info

    def auto_capture(self):
        """Do not resurrect an intentionally removed profile on server restart."""
        info=identify(read_private(self.home/'auth.json'))
        with self.lock():
            removed=json.loads(read_private(self.removed)) if self.removed.exists() else []
            if info['id'] in removed:
                return None
        return self.capture()

    def update(self,key,alias):
        alias=clean_alias(alias)
        with self.lock():
            if self.journal.exists():
                raise AccountError('Resolve the pending switch first')
            registry=self.load()
            if key not in registry:
                raise AccountError('Unknown saved profile')
            registry[key]['alias']=alias
            registry[key]['name']=display_name(registry[key],key)
            atomic(self.registry,registry)
            return {'id':key,'alias':alias,'display_name':registry[key]['name']}

    def remove(self,key):
        if not isinstance(key,str) or not re.fullmatch('[0-9a-f]{64}',key):
            raise AccountError('Unknown saved profile')
        with self.lock():
            if self.journal.exists():
                raise AccountError('Resolve the pending switch first')
            registry=self.load()
            if key not in registry:
                raise AccountError('Unknown saved profile')
            removed=json.loads(read_private(self.removed)) if self.removed.exists() else []
            if key not in removed:
                removed.append(key)
            atomic(self.removed,removed)
            registry.pop(key)
            atomic(self.registry,registry)
            path=self.directory/(key+'.auth.json')
            if path.exists() or path.is_symlink():
                path.unlink()
            return {'removed':key,'active_login_unchanged':True}

    def export_bundle(self):
        with self.lock():
            if self.journal.exists():
                raise AccountError('Resolve the pending switch first')
            return {'format':'relay-accounts-v1','profiles':[
                {'alias':profile_alias(p),'auth':json.loads(read_private(self.directory/(key+'.auth.json')))}
                for key,p in self.load().items()]}

    def import_bundle(self,bundle):
        if not isinstance(bundle,dict) or bundle.get('format')!='relay-accounts-v1':
            raise AccountError('Unsupported account backup')
        rows=bundle.get('profiles')
        if not isinstance(rows,list) or not 0<len(rows)<=100:
            raise AccountError('Backup must contain 1–100 profiles')
        prepared={}
        for row in rows:
            if not isinstance(row,dict):
                raise AccountError('Invalid profile')
            data=json.dumps(row.get('auth')).encode()
            info=identify(data)
            alias=clean_alias(row.get('alias'))
            info.update(alias=alias,saved_at=time.time())
            info['name']=display_name(info,info['id'])
            if info['id'] in prepared:
                raise AccountError('Duplicate profile in backup')
            prepared[info['id']]=(info,data)
        with self.lock():
            if self.journal.exists():
                raise AccountError('Resolve the pending switch first')
            registry=self.load()
            # Existing profiles retain their newer local credentials and aliases.
            added=0
            for key,(info,data) in prepared.items():
                if key in registry:
                    continue
                atomic(self.directory/(key+'.auth.json'),data)
                registry[key]=info
                added+=1
            atomic(self.registry,registry)
            if self.removed.exists():
                removed=json.loads(read_private(self.removed))
                atomic(self.removed,[key for key in removed if key not in prepared])
            return {'imported':added,'skipped':len(prepared)-added,'active_login_unchanged':True}

    def list(self):
        with self.lock():
            current = None
            try:
                current = identify(read_private(self.home/'auth.json'))['id']
            except AccountError:
                pass
            registry=self.load()
            # Older registries lack plan metadata. Enrich display without rewriting credentials.
            for key,profile in registry.items():
                if isinstance(profile,dict) and not profile.get('account_type'):
                    try:profile['account_type']=identify(read_private(self.directory/(key+'.auth.json'))).get('account_type')
                    except AccountError:pass
            return {'profiles':[{'id':k, 'name':display_name(p,k), 'display_name':display_name(p,k),
                                 'alias':profile_alias(p) or automatic_alias(p), 'email':p.get('email'),
                                 'saved_at':p.get('saved_at'), 'selected_on_disk':k==current}
                                for k,p in registry.items() if isinstance(p, dict)],
                    'pending_recovery':self.journal.exists(), 'mode':'restart_required',
                    'scope':'shared file auth only; running frontend identity not verified'}

    def switch(self, key):
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key):
            raise AccountError('Unknown profile identity')
        with self.lock():
            if self.journal.exists():
                raise AccountError('A prior switch needs recovery; run accounts.py restore')
            self.guard()
            if any(os.environ.get(k) for k in ('CODEX_ACCESS_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY')):
                raise AccountError('Unset token/API-key authentication environment variables before file-auth switching')
            self.file_mode()
            registry = self.load()
            if key not in registry:
                raise AccountError('Unknown saved profile')
            target = read_private(self.directory/(key+'.auth.json'))
            if identify(target)['id'] != key:
                raise AccountError('Saved credentials do not match profile identity')
            live = self.home/'auth.json'
            old = read_private(live)  # Fail closed; require an existing login to back up.
            old_info = identify(old)
            if old_info['id'] == key:
                return {'changed':False, 'restart_required':True}
            # Preserve token refreshes from the outgoing account before switching.
            old_info.update(alias=profile_alias(registry.get(old_info['id'],{})),saved_at=time.time())
            old_info['name']=display_name(old_info,old_info['id'])
            removed=json.loads(read_private(self.removed)) if self.removed.exists() else []
            if old_info['id'] not in removed:
                registry[old_info['id']] = old_info
                atomic(self.directory/(old_info['id']+'.auth.json'), old)
            atomic(self.registry, registry)
            backup = 'backup-'+uuid.uuid4().hex+'.auth.json'
            atomic(self.directory/backup, old)
            atomic(self.journal, {'backup':backup, 'before':old_info['id'], 'target':key,
                                  'target_digest':hashlib.sha256(target).hexdigest()})
            try:
                self.guard()
                if read_private(live) != old:
                    raise AccountError('Auth changed during switch; retry after closing all clients')
                atomic(live, target)
                if read_private(live) != target:
                    raise AccountError('Auth replacement verification failed')
                atomic(self.directory/'last-switch.json', {'before':old_info['id'], 'target':key,
                                                          'backup':backup, 'at':time.time()})
                self.journal.unlink()
            except Exception:
                # Keep journal; do not overwrite an independently changed login.
                raise AccountError('Switch stopped; backup retained. Run accounts.py restore before retrying') from None
            return {'changed':True, 'restart_required':True, 'selected_profile':key,
                    'message':'Auth file replaced. Reopen both clients; verify their displayed account.'}

    def restore(self):
        with self.lock():
            self.guard()
            if not self.journal.exists():
                raise AccountError('No interrupted switch to restore')
            info = json.loads(read_private(self.journal))
            backup = info.get('backup', '')
            if not re.fullmatch(r'backup-[0-9a-f]{32}\.auth\.json', backup):
                raise AccountError('Invalid recovery journal')
            old = read_private(self.directory/backup)
            current = read_private(self.home/'auth.json')
            if current != old and hashlib.sha256(current).hexdigest() != info.get('target_digest'):
                raise AccountError('Live auth changed independently; restore blocked to avoid overwriting it')
            atomic(self.home/'auth.json', old)
            self.journal.unlink()
            return {'restored':True, 'restart_required':True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parent/'.relay-accounts')
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home()/'.codex')))
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('capture'); capture.add_argument('--name', default=None, help='Optional alias; defaults to email')
    login = sub.add_parser('login'); login.add_argument('--name', default=None, help='Optional alias; defaults to email')
    update=sub.add_parser('alias');update.add_argument('identity');update.add_argument('--name',default='')
    remove=sub.add_parser('remove');remove.add_argument('identity')
    sub.add_parser('list')
    switch = sub.add_parser('switch'); switch.add_argument('identity')
    sub.add_parser('restore')
    args = parser.parse_args()
    manager = Accounts(args.directory.resolve(), args.codex_home.resolve())
    if args.command == 'login':
        binary = shutil.which('codex')
        if not binary:
            raise AccountError('Official codex CLI must be installed')
        with tempfile.TemporaryDirectory(prefix='login-', dir=manager.directory) as temporary:
            login_home = Path(temporary)
            atomic(login_home/'config.toml', b'cli_auth_credentials_store = "file"\n')
            env = dict(os.environ, CODEX_HOME=str(login_home))
            for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN'):
                env.pop(key, None)
            completed = subprocess.run([binary, '-c', 'cli_auth_credentials_store="file"', 'login'], env=env)
            if completed.returncode:
                raise AccountError('Login did not complete')
            result = manager.capture(args.name, login_home/'auth.json')
    elif args.command == 'capture': result = manager.capture(args.name)
    elif args.command == 'list': result = manager.list()
    elif args.command == 'switch': result = manager.switch(args.identity)
    elif args.command == 'alias': result = manager.update(args.identity,args.name)
    elif args.command == 'remove': result = manager.remove(args.identity)
    else: result = manager.restore()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except AccountError as error:
        import sys
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except OSError:
        import sys
        print('Account file operation failed; inspect permissions and recovery journal.', file=sys.stderr)
        sys.exit(1)
