#!/usr/bin/env python3
"""Local Codex dashboard with explicit account, rotation and opt-in proxy controls."""
import argparse
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit, parse_qs
from reference_bridge import SnapshotError, decode_snapshot, finite_number
from accounts import AccountError, Accounts
from live_quota import Collector, QuotaError
from onboarding import LoginFlow
from rotation import RotationService, RotationError, ExecutionLease
from quota_policy import priority, entry_ceiling, reset_wait
from smart_proxy import SmartProxy,ProxyError

STATIC = Path(__file__).parent / "ui"
SECRET_FIELDS = {"access_token", "refresh_token", "id_token", "openai_api_key",
                 "authorization", "password", "client_secret", "tokens", "auth.json"}


def check_metadata(value, depth=0):
    if depth > 24:
        raise SnapshotError("Snapshot nesting too deep")
    if isinstance(value, dict):
        if any(str(k).lower() in SECRET_FIELDS for k in value):
            raise SnapshotError("Only quota metadata accepted; credential fields detected")
        for child in value.values():
            check_metadata(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            check_metadata(child, depth + 1)


def demo_snapshot(now):
    profiles = [("Studio", 93, 38, 62), ("Build", 22, 31, 210),
                ("Research", 48, 67, 95), ("Reserve", 12, 18, 285)]
    accounts = []
    for i, (name, short, weekly, reset) in enumerate(profiles):
        accounts.append({"account_key": "demo-%d" % (i + 1), "alias": name,
                         "email": "account%d@example.com" % (i + 1), "plan": "business",
                         "auth_mode": "chatgpt", "active": i == 0,
                         "usage": {"source": "api", "updated_at": now,
                                   "primary": {"used_percent": short, "window_minutes": 300,
                                               "resets_at": now + reset * 60},
                                   "secondary": {"used_percent": weekly, "window_minutes": 10080,
                                                 "resets_at": now + (3 + i) * 86400},
                                   "refresh": {"requested": True, "method": "api", "status": "ok"}}})
    return {"received_at": now, "result": {"schema_version": 1, "command": "list",
                                            "active_account_key": "demo-1", "accounts": accounts}}


def settings(values):
    reserve = values.get("reserve", 15)
    margin = values.get("margin", 10)
    strategy = values.get("strategy", "reset_first")
    disabled = values.get("disabled", [])
    if (not finite_number(reserve) or not 1 <= reserve <= 40
            or not finite_number(margin) or not 0 <= margin <= 20
            or strategy not in ("balanced", "reset_first")
            or not isinstance(disabled, list) or len(disabled) > 100
            or any(not isinstance(v, str) for v in disabled)):
        raise SnapshotError("Invalid rotation settings")
    return {"reserve": reserve, "margin": margin, "strategy": strategy, "disabled": disabled}


def build_view(envelope, options=None, now=None, demo=False, max_age=60):
    now = time.time() if now is None else now
    opts = settings(options or {})
    check_metadata(envelope)
    # Accept old snapshots for DISPLAY only. Eligibility is evaluated separately.
    candidates = decode_snapshot(envelope, now, max_age=10**12, allow_unknown_active=True)
    receipt = envelope["received_at"]
    raw_accounts = {a["account_key"]: a for a in envelope["result"]["accounts"]}
    threshold = 100 - opts["reserve"]
    entry_threshold = entry_ceiling(opts)
    cards = []
    for candidate in candidates:
        raw = raw_accounts[candidate.profile.identity]
        windows = []
        for field in ("primary", "secondary"):
            value = (raw.get("usage") or {}).get(field)
            if isinstance(value, dict) and finite_number(value.get("used_percent")):
                windows.append({"used": value["used_percent"], "minutes": value.get("window_minutes"),
                                "resets_at": value.get("resets_at")})
        short = next((w for w in windows if w["minutes"] == 300), None)
        weekly = next((w for w in windows if w["minutes"] == 10080), None)
        excluded = candidate.profile.identity in opts["disabled"]
        polling=raw.get('polling') if max_age!=60 else None
        age_limit=max_age
        if isinstance(polling,dict):
            seconds=polling.get('interval_seconds')
            if finite_number(seconds) and 30<=seconds<=1800:
                age_limit=seconds+60
        eligible = not excluded and candidate.usage.eligible(now, entry_threshold, max_age=age_limit)
        bottleneck = 100 - max(candidate.usage.windows) if candidate.usage.windows else None
        score,bonus=priority(short,weekly,now,opts['strategy']) if eligible else (None,0)
        if eligible and score is None:
            eligible=False
        reason = ("Đã tạm loại khỏi vòng rotate" if excluded else
                  "Dữ liệu đã cũ; cần đọc quota mới" if now - candidate.usage.observed_at > age_limit else
                  "Quota chưa được xác nhận" if candidate.reason else
                  "Chưa đủ dư địa cho một phiên mới" if not eligible else
                  "Đủ quota ở tất cả cửa sổ được báo cáo")
        cards.append({"id": candidate.profile.identity, "name": candidate.profile.name,
                      "active": candidate.active, "short": short, "weekly": weekly,
                      "reset_credits": (raw.get("usage") or {}).get("reset_credits"),
                      "windows": windows, "eligible": eligible, "excluded": excluded,
                      "bottleneck_remaining": bottleneck, "score": score, "reset_bonus": bonus,
                      "reason": reason, "can_keep": candidate.usage.eligible(now, threshold, max_age=age_limit) and not excluded})
    active = next((c for c in cards if c["active"]),None)
    ranked = sorted((c for c in cards if not c["active"] and c["eligible"]),
                    key=lambda c: (-c["score"], c["id"]))
    wait_until=None
    if active and not active['can_keep'] and not active['excluded']:
        raw=raw_accounts[active['id']].get('usage') or {}
        observed=raw.get('observed_at',receipt)
        if ((raw.get('refresh') or {}).get('status')=='ok' and now-observed<=max_age):
            wait_until=reset_wait(active['windows'],threshold,now)
    action = "pause" if active is None else "keep" if active["can_keep"] else "wait_reset" if wait_until else "handoff" if ranked else "pause"
    target = ranked[0]["id"] if action == "handoff" else None
    return {"demo": demo, "received_at": receipt, "now": now, "stale": now - receipt > max_age,
            "accounts": cards, "active": active["id"] if active else None, "target": target,
            "ranking": [c["id"] for c in ranked], "action": action,"reset_wait_until":wait_until, "settings": opts,
            "threshold": threshold, "entry_threshold": entry_threshold,
            "execution_enabled": False,
            "integration": {"desktop": "unverified", "vscode": "unverified"}}


class Store:
    def __init__(self, snapshot=None, accounts=None, collector=None, demo=False):
        self.path = snapshot
        self.imported = None
        self.options = settings({})
        self.lock = threading.Lock()
        self.operation_lock = ExecutionLease(accounts.directory/'execution.lock') if accounts else threading.Lock()
        self.csrf = secrets.token_urlsafe(32)
        self.accounts = accounts
        self.collector = collector
        self.demo = demo
        self.proxy = None
        self.live_packet = None
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.live_error = None
        self.generation = 0
        self.force_refresh = False
        self.pending_refresh = set()
        self.checking = set()
        self.login = LoginFlow(accounts,self.invalidate_live) if accounts else None
        self.rotation = RotationService(accounts,collector,lambda:dict(self.options),self.invalidate_live,
                                        self.operation_lock) if accounts and collector else None

    def collect_once(self):
        with self.operation_lock:
            self._collect_once()
        if self.rotation:
            try:self.rotation.sync_names()
            except (AccountError,RotationError,OSError,QuotaError):pass
            if not self.proxy:self.rotation.maybe_auto(self.view())

    def _collect_once(self):
        with self.lock:
            generation=self.generation
            disabled=tuple(self.options['disabled'])
            force=self.force_refresh
            self.force_refresh=False
            force_keys=set(self.pending_refresh)
            self.pending_refresh.clear()
            self.checking=force_keys
        try:
            packet = self.collector.snapshot(disabled=disabled, force=force, force_keys=force_keys)
            with self.lock:
                if generation==self.generation:
                    self.live_packet = packet
                    self.live_error = None
        except Exception:
            with self.lock:
                self.live_error = 'Không đọc được quota live; giữ số đo cũ và không suy đoán quota mới.'
        finally:
            with self.lock:
                self.checking.clear()

    def start(self):
        if self.collector is None:
            return
        def run():
            while not self.stop_event.is_set():
                self.refresh_event.clear()
                self.collect_once()
                self.refresh_event.wait(self.poll_interval())
        self.worker = threading.Thread(target=run,daemon=True)
        self.worker.start()

    def poll_interval(self):
        # Wake locally at least every 30 seconds to notice a changed login;
        # the collector only makes network calls for profiles whose rule is due.
        return max(1,min(30,getattr(self.collector,'next_due',time.time()+30)-time.time()))

    def close(self):
        if self.rotation:
            self.rotation.cancel()
        if self.proxy:self.proxy.close()
        if self.login:
            self.login.cancel()
        self.stop_event.set()
        self.refresh_event.set()

    def invalidate_live(self):
        with self.lock:
            self.generation += 1
            self.force_refresh = True
            self.live_packet = None
        self.refresh_event.set()

    def view(self):
        with self.lock:
            live = self.collector is not None
            if live:
                packet = self.live_packet or {'received_at':time.time(),'result':{
                    'schema_version':1,'command':'list','active_account_key':None,'accounts':[]}}
            elif self.imported is not None:
                packet = self.imported
            elif self.path:
                if self.path.stat().st_size > 4 * 1024 * 1024:
                    raise SnapshotError("Snapshot too large")
                packet = json.loads(self.path.read_text())
            elif self.demo:
                packet = demo_snapshot(time.time())
            else:
                packet = {'received_at':time.time(),'result':{'schema_version':1,'command':'list',
                          'active_account_key':None,'accounts':[]}}
            view = build_view(packet, self.options, demo=self.demo and self.imported is None and self.path is None, max_age=180 if live else 60)
            view["csrf"] = self.csrf
            view["account_management"] = self.accounts is not None
            view['live'] = live
            view['loading'] = live and self.live_packet is None
            view['live_error'] = self.live_error
            if live:
                row_map={a['account_key']:a for a in packet['result']['accounts']}
                for card in view['accounts']:
                    raw=row_map[card['id']]
                    card['plan']=raw.get('plan')
                    card['token_summary']=raw.get('token_summary')
                    card['connection_error']=raw.get('connection_error')
                    card['email']=raw.get('email')
                    card['alias']=raw.get('alias')
                    card['polling']=raw.get('polling')
                    card['refreshing']=card['id'] in self.pending_refresh or card['id'] in self.checking
                active_row=next((r for r in row_map.values() if r.get('active')),None)
                if active_row and active_row.get('polling'):
                    view['received_at']=active_row['polling']['last_checked_at']
            return view


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No account metadata or request paths in access logs.

        def allowed(self, mutation=False):
            host = self.headers.get("Host")
            expected = "127.0.0.1:%d" % self.server.server_port
            if host != expected:
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + expected:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            if mutation and not secrets.compare_digest(self.headers.get("X-Relay-CSRF", ""), store.csrf):
                return False
            return True

        def respond(self, status, body, kind="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                self.respond(403, b'{"error":"forbidden"}')
                return
            path = urlsplit(self.path).path
            if path in ('/api/rotation','/api/rotation/sessions','/api/rotation/handoff'):
                if not store.rotation:
                    self.respond(409,b'{"error":"Live rotation is not available"}');return
                try:
                    if path.endswith('/sessions'):
                        result={'sessions':store.rotation.sessions()}
                    elif path.endswith('/handoff'):
                        name=parse_qs(urlsplit(self.path).query).get('name',[''])[0]
                        self.respond(200,store.rotation.download(name),'text/markdown; charset=utf-8');return
                    else:result=store.rotation.status()
                    self.respond(200,json.dumps(result,ensure_ascii=False).encode())
                except (RotationError,AccountError,OSError):
                    self.respond(409,b'{"error":"Cannot read rotation state"}')
                return
            if path == '/api/accounts/login':
                self.respond(200,json.dumps(store.login.status() if store.login else {'status':'disabled'}).encode())
                return
            if path == '/api/accounts':
                try:
                    result = store.accounts.list() if store.accounts else {'profiles':[], 'enabled':False}
                    self.respond(200, json.dumps(result, ensure_ascii=False).encode())
                except (AccountError, OSError):
                    self.respond(409, b'{"error":"Cannot read saved profiles safely."}')
                return
            if path == '/api/proxy':
                try:result=store.proxy.status() if store.proxy else {'enabled':False}
                except (ProxyError,AccountError,OSError):
                    self.respond(409,b'{"error":"Cannot read proxy state"}');return
                self.respond(200,json.dumps(result,ensure_ascii=False).encode());return
            if path == "/api/state":
                try:
                    self.respond(200, json.dumps(store.view(), ensure_ascii=False).encode())
                except (OSError, ValueError, TypeError, KeyError):
                    self.respond(422, b'{"error":"Snapshot invalid or unavailable. No changes made."}')
                return
            routes = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in routes:
                self.respond(404, b'{"error":"not found"}')
                return
            filename, content_type = routes[path]
            self.respond(200, (STATIC / filename).read_bytes(), content_type)

        def do_POST(self):
            if not self.allowed(mutation=True):
                self.respond(403, b'{"error":"forbidden"}')
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4 * 1024 * 1024:
                    raise SnapshotError("Invalid size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise SnapshotError("Object required")
                if self.path == '/api/quota/reset':
                    if not store.collector or not store.accounts:
                        self.respond(409,b'{"error":"Live quota required"}');return
                    try:
                        with store.operation_lock:
                            result=store.collector.consume_reset(data.get('identity'),data.get('operation'),data.get('expires_at'))
                            store.collector.cache.pop(data.get('identity'),None)
                            with store.lock:
                                store.force_refresh=True
                            store._collect_once()
                        payload=store.view()
                        payload['reset_outcome']=result['reset_outcome']
                        self.respond(200,json.dumps(payload,ensure_ascii=False).encode())
                    except (QuotaError,AccountError,OSError,ValueError) as error:
                        store.invalidate_live()
                        self.respond(409,json.dumps({'error':str(error) if isinstance(error,QuotaError) else 'Không xác nhận được reset; kiểm tra quota trước khi thử lại.'},ensure_ascii=False).encode())
                    return
                if self.path in ('/api/proxy/checkpoint','/api/proxy/alias'):
                    if not store.proxy:
                        self.respond(409,b'{"error":"Proxy is not enabled"}');return
                    try:
                        result=(store.proxy.rename(data.get('session'),data.get('alias')) if self.path.endswith('/alias')
                                else store.proxy.start_checkpoint(data.get('session'),data.get('target')))
                    except (ProxyError,AccountError,OSError):
                        self.respond(409,b'{"error":"Cannot start proxy checkpoint; inspect the session and target"}');return
                    self.respond(200,json.dumps(result,ensure_ascii=False).encode());return
                if self.path in ('/api/rotation/settings','/api/rotation/start','/api/rotation/cancel','/api/rotation/recovery','/api/rotation/workflow','/api/rotation/names','/api/rotation/automatic'):
                    if not store.rotation:
                        self.respond(409,b'{"error":"Live rotation is not available"}');return
                    try:
                        if store.proxy and (self.path.endswith('/start') or
                                self.path.endswith(('/settings','/automatic')) and data.get('enabled')):
                            raise RotationError('Cold rotation is unavailable while proxy mode is enabled; use proxy handoff routing')
                        if self.path.endswith('/settings'):result=store.rotation.configure(data)
                        elif self.path.endswith('/workflow'):result=store.rotation.update_workflow(data)
                        elif self.path.endswith('/names'):result=store.rotation.sync_names(force=True)
                        elif self.path.endswith('/start'):result=store.rotation.start(data.get('mode','rotate'),data.get('target'),workflow_id=data.get('workflow_id'),restart_desktop=data.get('restart_desktop',False))
                        elif self.path.endswith('/automatic'):result=store.rotation.set_automatic(data.get('enabled'))
                        elif self.path.endswith('/cancel'):result=store.rotation.cancel()
                        else:result=store.rotation.acknowledge_recovery()
                        self.respond(200,json.dumps(result,ensure_ascii=False).encode())
                    except (RotationError,AccountError) as error:
                        self.respond(409,json.dumps({'error':str(error)},ensure_ascii=False).encode())
                    return
                if self.path in ('/api/accounts/capture', '/api/accounts/switch', '/api/accounts/restore',
                                 '/api/accounts/update','/api/accounts/remove',
                                 '/api/accounts/login/start','/api/accounts/login/cancel',
                                 '/api/accounts/export','/api/accounts/import'):
                    if store.accounts is None:
                        self.respond(403, b'{"error":"Account management is disabled. Start server with --manage-accounts."}')
                        return
                    if store.rotation and store.rotation.status()['busy']:
                        self.respond(409,b'{"error":"Account changes are locked while rotation is running"}')
                        return
                    try:
                        if self.path.endswith('/export'):
                            result=store.accounts.export_bundle()
                        elif self.path.endswith('/import'):
                            result=store.accounts.import_bundle(data.get('bundle'))
                        elif self.path.endswith('/login/start'):
                            result=store.login.start(data.get('alias'))
                        elif self.path.endswith('/login/cancel'):
                            result=store.login.cancel()
                        elif self.path.endswith('/update'):
                            result=store.accounts.update(data.get('identity'),data.get('alias'))
                        elif self.path.endswith('/remove'):
                            result=store.accounts.remove(data.get('identity'))
                        elif self.path.endswith('/capture'):
                            result = store.accounts.capture(data.get('name'))
                        elif self.path.endswith('/switch'):
                            result = store.accounts.switch(data.get('identity'))
                        else:
                            result = store.accounts.restore()
                        if not self.path.endswith('/export'):
                            store.invalidate_live()
                        self.respond(200, json.dumps(result, ensure_ascii=False).encode())
                    except AccountError as error:
                        self.respond(409, json.dumps({'error':str(error)}, ensure_ascii=False).encode())
                    return
                if self.path == "/api/settings":
                    option = settings(data)
                    with store.lock:
                        store.options = option
                    store.refresh_event.set()
                elif self.path == "/api/import":
                    if store.collector is not None:
                        raise SnapshotError('Snapshot import is disabled in live mode')
                    build_view(data)  # Verify before replacing a working snapshot.
                    with store.lock:
                        store.imported = data
                elif self.path == "/api/demo":
                    if store.collector is not None:
                        raise SnapshotError('Demo is disabled in live mode')
                    with store.lock:
                        store.imported, store.path = None, None
                        store.demo = True
                elif self.path == '/api/refresh':
                    identity=data.get('identity')
                    keys={p['id'] for p in store.accounts.list()['profiles']} if store.accounts else set()
                    if identity is not None and identity not in keys:
                        raise ValueError('Unknown profile')
                    with store.lock:
                        if identity is None:
                            store.force_refresh=True
                            store.pending_refresh.update(keys)
                        else:
                            store.pending_refresh.add(identity)
                    store.refresh_event.set()
                else:
                    self.respond(404, b'{"error":"not found"}')
                    return
                self.respond(200, json.dumps(store.view(), ensure_ascii=False).encode())
            except (OSError, ValueError, TypeError, KeyError):
                self.respond(422, b'{"error":"Invalid quota metadata or settings; credentials are not accepted."}')
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument('--proxy-port',type=int,default=None,help='Opt in to experimental subscription proxy on this loopback port')
    parser.add_argument('--demo', action='store_true', help='Explicitly use sample data instead of connecting accounts')
    parser.add_argument('--manage-accounts', action='store_true', help='Enable explicit file-auth capture and offline switching')
    parser.add_argument('--accounts-dir', type=Path, default=Path(__file__).resolve().parent/'.relay-accounts')
    parser.add_argument('--codex-home', type=Path, default=None)
    args = parser.parse_args()
    manager = None
    if args.manage_accounts or (not args.demo and args.snapshot is None):
        import os
        home = args.codex_home or Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))
        manager = Accounts(args.accounts_dir.resolve(), home.resolve())
        if not manager.journal.exists():
            try:
                manager.auto_capture()
            except AccountError:
                print('Current local login unavailable; add a profile with accounts.py login.',flush=True)
    collector=Collector(manager) if manager and not args.demo and args.snapshot is None else None
    store=Store(args.snapshot,manager,collector,demo=args.demo)
    if args.proxy_port is not None:
        if not manager or not collector:parser.error('--proxy-port requires live account mode')
        store.proxy=SmartProxy(manager,collector,lambda:dict(store.options),store.operation_lock,port=args.proxy_port)
        store.proxy.start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(store))
    server.timeout = 10
    print("Codex Relay: http://127.0.0.1:%d" % server.server_port, flush=True)
    print('LIVE Codex quota' if collector else 'DEMO metadata only' if args.demo else 'Reading metadata snapshot',flush=True)
    store.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
        server.server_close()


if __name__ == "__main__":
    main()
