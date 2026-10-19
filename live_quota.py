"""Real read-only quota collection using official Codex app-server stdio.

No model turns, frontend control, global auth writes or undocumented HTTP APIs.
"""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from accounts import AccountError, atomic, identify, read_private
from quota_schedule import fingerprint, interval


class QuotaError(ValueError):
    pass


class RPC:
    def __init__(self, home, binary=None):
        binary = binary or shutil.which('codex')
        if not binary:
            # Terminal launched outside the desktop app may not inherit its PATH.
            for candidate in (
                '/Applications/Codex.app/Contents/Resources/codex',
                '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
            ):
                if Path(candidate).is_file() and os.access(candidate,os.X_OK):
                    binary=candidate;break
        if not binary:
            raise QuotaError('Official Codex CLI is not installed')
        env = dict(os.environ, CODEX_HOME=str(home))
        for key in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','OPENAI_BASE_URL','CODEX_CLI_PATH'):
            env.pop(key, None)
        self.process = subprocess.Popen([binary,'-c','cli_auth_credentials_store="file"','app-server'],
                                        cwd=home,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL,text=True,bufsize=1)
        self.messages = queue.Queue()
        self.counter = 0
        self.reader = threading.Thread(target=self._read,daemon=True)
        self.reader.start()
        try:
            self.call('initialize',{'clientInfo':{'name':'coding_provider_relay','version':'0.3.0'}})
            self.send({'method':'initialized','params':{}})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (OSError,ValueError):
            pass
        finally:
            self.messages.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message)+'\n')
        self.process.stdin.flush()

    def call(self, method, params=None, timeout=20):
        self.counter += 1
        request = self.counter
        try:
            self.send({'id':request,'method':method,'params':params or {}})
            deadline = time.monotonic()+timeout
            while True:
                left = deadline-time.monotonic()
                if left <= 0:
                    raise queue.Empty()
                message = self.messages.get(timeout=left)
                if message is None:
                    raise QuotaError('Codex quota process disconnected')
                if 'method' in message:
                    if 'id' in message:
                        self.send({'id':message['id'],'error':{'code':-32601,'message':'Read-only collector'}})
                    else:
                        self.notification(message)
                    continue
                if message.get('id') != request:
                    continue
                if message.get('error'):
                    raise QuotaError('Codex did not return '+method)
                return message.get('result',{})
        except (queue.Empty,OSError):
            raise QuotaError('Codex quota request timed out or failed') from None

    def notification(self, message):
        pass

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdin.close(); self.process.stdout.close()


def quota_snapshot(response):
    buckets = response.get('rateLimitsByLimitId')
    snapshot = buckets.get('codex') if buckets else response.get('rateLimits')
    if not isinstance(snapshot,dict) or snapshot.get('limitId') not in (None,'codex'):
        raise QuotaError('Codex quota bucket unavailable')
    result = {}
    for field in ('primary','secondary'):
        window = snapshot.get(field)
        result[field] = ({'used_percent':window.get('usedPercent'),
                          'window_minutes':window.get('windowDurationMins'),
                          'resets_at':window.get('resetsAt')} if isinstance(window,dict) else None)
    blocked = (response.get('ordinaryUsageAllowed') is False or snapshot.get('rateLimitReachedType')
               or snapshot.get('spendControlReached') is True)
    result['source'] = 'api'
    result['refresh'] = {'requested':True,'method':'api','status':'error' if blocked else 'ok'}
    return result


class Collector:
    def __init__(self, manager, rpc_factory=RPC):
        self.manager = manager
        self.rpc_factory = rpc_factory
        self.cache = {}
        self.history_path = manager.directory/'quota-history.json'
        try:
            history=json.loads(read_private(self.history_path))
            self.history=history if isinstance(history,dict) else {}
        except (AccountError,ValueError):
            self.history={}
        self.next_due=time.time()

    def snapshot(self, disabled=(), force=False, low_interval=30, force_keys=()):
        listing = self.manager.list()
        profiles = listing['profiles']
        current = next((p['id'] for p in profiles if p['selected_on_disk']),None)
        rows = []
        due=[]
        for profile in profiles:
            key=profile['id']
            now=time.time()
            info = {'account_key':profile['id'],'alias':profile.get('alias'),'email':profile.get('email'),
                    'auth_mode':'chatgpt','active':profile['id']==current}
            cached=self.cache.get(key)
            history=self.history.get(key,{})
            last_changed=history.get('last_changed',now)
            if not isinstance(last_changed,(int,float)) or not 0<=last_changed<=now:
                last_changed=now
            seconds,reason=interval(info['active'],key in disabled,(cached or {}).get('usage',{}),last_changed,now,low_interval)
            deadline=(cached or {}).get('checked_at',0)+seconds
            # React immediately to an account becoming active or being enabled.
            became_active=cached and info['active'] and not cached.get('was_active')
            enabled=cached and cached.get('was_disabled') and key not in disabled
            reset_due=info['active'] and cached and any(
                isinstance((cached.get('usage',{}).get(name) or {}).get('resets_at'),(int,float))
                and cached['checked_at']<=(cached['usage'].get(name) or {})['resets_at']
                and now>=(cached['usage'].get(name) or {})['resets_at']+1 for name in ('primary','secondary'))
            try:
                if force or key in force_keys or not cached or now>=deadline or became_active or enabled or reset_due:
                    result=self._read_profile(profile)
                    now=time.time()
                    measured=fingerprint(result['usage'])
                    if history.get('fingerprint')!=measured:
                        last_changed=now
                    self.history[key]={'fingerprint':measured,'last_changed':last_changed}
                    cached=dict(result,checked_at=now)
                    self.cache[key]=cached
                info.update({k:v for k,v in cached.items() if k not in ('checked_at','was_active','was_disabled')})
                seconds,reason=interval(info['active'],key in disabled,info['usage'],last_changed,now,low_interval)
                deadline=cached['checked_at']+seconds
            except (QuotaError,AccountError,OSError,ValueError) as error:
                info['usage'] = {'source':'none','refresh':{'requested':True,'method':'api','status':'error'},
                                 'primary':None,'secondary':None}
                messages={
                    'Official Codex CLI is not installed':'Không tìm thấy Codex CLI trong PATH hoặc ứng dụng đã cài.',
                    'Codex quota request timed out or failed':'Codex không trả lời kịp; kiểm tra mạng rồi Refresh.',
                    'Codex quota process disconnected':'Tiến trình Codex bị ngắt khi đọc quota.',
                    'Codex did not return account/rateLimits/read':'Codex từ chối đọc quota; kiểm tra đăng nhập và kết nối.',
                    'Codex did not return account/read':'Codex không xác nhận được đăng nhập của profile.',
                    'Account switch recovery is pending':'Cần phục hồi thao tác đổi account trước khi đọc quota.',
                }
                info['connection_error'] = messages.get(str(error),'Không đọc được quota; cần kiểm tra hoặc đăng nhập lại profile.')
                # Retain old success privately for retry timing, never display it as a fresh success.
                seconds=1800 if key in disabled or not info['active'] else 120
                reason='read_error'
                cached={'usage':info['usage'],'connection_error':info['connection_error'],'checked_at':time.time()}
                self.cache[key]=cached
                deadline=cached['checked_at']+seconds
            if info['active'] and key not in disabled:
                upcoming=[(info.get('usage',{}).get(name) or {}).get('resets_at') for name in ('primary','secondary')]
                upcoming=[v+1 for v in upcoming if isinstance(v,(int,float)) and v>=time.time()]
                if upcoming:deadline=min(deadline,min(upcoming))
            cached['was_active']=info['active']
            cached['was_disabled']=key in disabled
            info['polling']={'interval_seconds':seconds,'next_check_at':deadline,
                             'last_checked_at':cached['checked_at'],'last_changed_at':last_changed,'reason':reason}
            due.append(deadline)
            rows.append(info)
        keys={p['id'] for p in profiles}
        self.cache={k:v for k,v in self.cache.items() if k in keys}
        self.history={k:v for k,v in self.history.items() if k in keys}
        atomic(self.history_path,self.history)
        self.next_due=min(due) if due else time.time()+1800
        return {'received_at':time.time(),'result':{'schema_version':1,'command':'list',
                                                  'active_account_key':current,'accounts':rows}}

    def _read_profile(self, profile):
        with self.manager.lock():
            if self.manager.journal.exists():
                raise QuotaError('Account switch recovery is pending')
            path = self.manager.directory/(profile['id']+'.auth.json')
            saved = read_private(path)
            original = saved
            try:
                live = read_private(self.manager.home/'auth.json')
                if identify(live)['id']==profile['id']:
                    saved = live
            except AccountError:
                pass
            if identify(saved)['id'] != profile['id']:
                raise QuotaError('Stored profile identity mismatch')
        # Network reads must not block CRUD or hold the profile mutation lock.
        with tempfile.TemporaryDirectory(prefix='quota-',dir=self.manager.directory) as temp:
            home=Path(temp)
            atomic(home/'auth.json',saved)
            atomic(home/'config.toml',b'cli_auth_credentials_store="file"\n')
            rpc = self.rpc_factory(home)
            try:
                account = rpc.call('account/read',{'refreshToken':False}).get('account') or {}
                if account.get('type') != 'chatgpt':
                    raise QuotaError('Profile is not a ChatGPT account')
                if profile.get('email') and account.get('email') != profile['email']:
                    raise QuotaError('Authenticated account identity mismatch')
                limits = rpc.call('account/rateLimits/read')
                measured=quota_snapshot(limits)
                measured['observed_at']=time.time()
                result = {'usage':measured,'plan':account.get('planType')}
                try:
                    activity=rpc.call('account/usage/read',timeout=10)
                    summary=activity.get('summary') or {}
                    result['token_summary']={k:v for k,v in summary.items()
                                             if k in ('lifetimeTokens','peakDailyTokens')
                                             and isinstance(v,(int,float)) and not isinstance(v,bool)}
                except QuotaError:
                    pass
            finally:
                rpc.close()
            refreshed=read_private(home/'auth.json')
            if identify(refreshed)['id'] != profile['id']:
                raise QuotaError('Refreshed credentials changed identity')
            with self.manager.lock():
                if (profile['id'] in self.manager.load() and path.exists()
                        and read_private(path)==original):
                    atomic(path,refreshed)
            return result
