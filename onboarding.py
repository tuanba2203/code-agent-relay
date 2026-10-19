"""User-completed OAuth onboarding in an isolated Codex home."""
from pathlib import Path
from contextlib import ExitStack
import tempfile
import threading
import time
from urllib.parse import urlsplit
from accounts import AccountError, atomic, clean_alias, identify, read_private
from live_quota import RPC


class LoginFlow:
    def __init__(self,manager,on_complete=lambda:None,rpc_factory=RPC,timeout=300):
        self.manager,self.on_complete,self.rpc_factory=manager,on_complete,rpc_factory
        self.timeout=timeout
        self.lock=threading.Lock()
        self.cancel_event=threading.Event()
        self.state={'status':'idle'}
        self.thread=None

    def status(self):
        with self.lock:
            return dict(self.state)

    def set_state(self,**values):
        with self.lock:
            self.state=values

    def start(self,alias=None):
        alias=clean_alias(alias)
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise AccountError('A login is already in progress; complete or cancel it first')
            self.cancel_event.clear()
            self.state={'status':'starting'}
            self.thread=threading.Thread(target=self._run,args=(alias,),daemon=True)
            self.thread.start()
        return self.status()

    def cancel(self):
        self.cancel_event.set()
        return {'status':'cancelling'}

    def _run(self,alias):
        rpc=None
        try:
            with ExitStack() as stack:
                temporary=stack.enter_context(tempfile.TemporaryDirectory(prefix='login-',dir=self.manager.directory))
                home=Path(temporary)
                atomic(home/'config.toml',b'cli_auth_credentials_store="file"\n')
                rpc=self.rpc_factory(home)
                stack.callback(rpc.close)
                result=rpc.call('account/login/start',{'type':'chatgpt'})
                auth_url=result.get('authUrl')
                url=urlsplit(auth_url or '')
                if (url.scheme!='https' or url.hostname not in ('auth.openai.com','chatgpt.com','auth0.openai.com')
                        or url.username or url.password):
                    raise AccountError('Codex returned an unrecognized sign-in URL')
                self.set_state(status='waiting',auth_url=auth_url)
                deadline=time.monotonic()+self.timeout
                while time.monotonic()<deadline:
                    if self.cancel_event.wait(0.4):
                        self.set_state(status='cancelled')
                        return
                    if not (home/'auth.json').is_file():
                        continue
                    local=identify(read_private(home/'auth.json'))
                    account=rpc.call('account/read',{'refreshToken':False}).get('account') or {}
                    if account.get('type')!='chatgpt' or (local.get('email') and account.get('email')!=local['email']):
                        raise AccountError('Completed login identity could not be verified')
                    saved=self.manager.capture(alias,home/'auth.json')
                    self.on_complete()
                    self.set_state(status='complete',profile_id=saved['id'])
                    return
                self.set_state(status='expired')
        except Exception:
            self.set_state(status='error',message='Không hoàn tất đăng nhập. Thử lại hoặc dùng accounts.py login trong Terminal.')
