"""Read-only Antigravity quota from Google Code Assist.

Uses saved account snapshots. Never logs tokens. Refresh uses the public
Antigravity client ID from the official app. The matching client secret is
discovered from the installed Antigravity binary (same approach as agy-auth)
and is not shipped in Relay source.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from accounts import AccountError, claims


CONSUMER_CLIENT_ID = '1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com'
GCP_CLIENT_ID = '884354919052-36trc1jjb3tguiac32ov6cod268c5blh.apps.googleusercontent.com'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
ENDPOINTS = (
    'https://daily-cloudcode-pa.googleapis.com/v1internal',
    'https://cloudcode-pa.googleapis.com/v1internal',
)
USER_AGENT = 'antigravity/cli/1.4.5 (darwin; os_type=darwin; arch=arm64; auth_method=consumer)'
TTL_SECONDS = 60
MAX_BODY = 1 << 20
CLIENT_SECRET_HASHES = (
    '1d2f041093fd95aa8995a038c711d50a7960da09a505381c09a745d6ad0ecc60',
    '23d89fc5fbce73ed5b9e0cf0ca786bd0eb1bcdff8f65d03dc022d299ad2234b6',
)
SECRET_PREFIX = b'GOCSPX-'
_secret_lock = threading.Lock()
_discovered_secret = None


def _secret_char(value):
    return (48 <= value <= 57) or (65 <= value <= 90) or (97 <= value <= 122) or value in (45, 95)


def scan_client_secret(path, hashes=CLIENT_SECRET_HASHES):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return None
    wanted = set(hashes)
    carry = b''
    try:
        with open(path, 'rb') as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                data = carry + chunk
                offset = 0
                while True:
                    index = data.find(SECRET_PREFIX, offset)
                    if index < 0:
                        break
                    end = index + len(SECRET_PREFIX)
                    while end < len(data) and _secret_char(data[end]):
                        end += 1
                    run = data[index:end]
                    for length in range(20, min(len(run), 48) + 1):
                        candidate = run[:length]
                        if hashlib.sha256(candidate).hexdigest() in wanted:
                            return candidate.decode('ascii')
                    offset = index + len(SECRET_PREFIX)
                carry = data[-80:]
    except OSError:
        return None
    return None


def client_secret_paths():
    paths = [
        Path('/Applications/Antigravity.app/Contents/Resources/bin/language_server'),
        Path('/Applications/Antigravity.app/Contents/Resources/app.asar'),
    ]
    found = shutil.which('agy')
    if found:
        paths.append(Path(found))
    return paths


def discover_client_secret(paths=None, hashes=CLIENT_SECRET_HASHES):
    env = os.environ.get('ANTIGRAVITY_CLIENT_SECRET') or os.environ.get('AGY_AUTH_CLIENT_SECRET')
    if isinstance(env, str) and env.startswith('GOCSPX-'):
        return env
    for path in (paths if paths is not None else client_secret_paths()):
        found = scan_client_secret(path, hashes=hashes)
        if found:
            return found
    return None


def cached_client_secret():
    global _discovered_secret
    with _secret_lock:
        if _discovered_secret is None:
            _discovered_secret = discover_client_secret() or ''
        return _discovered_secret or None


TIER_LABELS = {
    'free-tier': 'Free',
    'standard-tier': 'Standard',
    'g1-pro-tier': 'Pro',
    'g1-ultra-tier': 'Ultra',
}


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def unix_time(value):
    parsed = value if isinstance(value, datetime) else parse_time(value)
    if parsed is None:
        return None
    return parsed.timestamp()


def remaining_percent(fraction):
    try:
        value = float(fraction)
    except (TypeError, ValueError):
        return None
    if value < 0:
        value = 0
    if value > 1:
        value = 1
    return int(round(value * 100))


def bucket_window(window, bucket_id):
    label = (window or '').strip().lower()
    if label in ('5h', '5-hour', 'five_hour', 'five-hour'):
        return '5h'
    if label in ('weekly', 'week'):
        return 'weekly'
    ident = (bucket_id or '').lower()
    if 'weekly' in ident:
        return 'weekly'
    if '5h' in ident or 'five' in ident:
        return '5h'
    return ''


def plan_label(assist, auth_method=None):
    if not isinstance(assist, dict):
        assist = {}
    for key in ('paidTier', 'currentTier'):
        tier = assist.get(key)
        if not isinstance(tier, dict):
            continue
        ident = tier.get('id') if isinstance(tier.get('id'), str) else ''
        if ident in TIER_LABELS:
            return TIER_LABELS[ident]
        name = tier.get('name') if isinstance(tier.get('name'), str) else ''
        if name.strip():
            return name.strip()
        if ident.strip():
            return ident.strip()
    if isinstance(auth_method, str) and auth_method.lower() == 'gcp':
        return 'PayGo'
    return None


def lower_window(current, candidate):
    if not candidate:
        return current
    if current is None:
        return candidate
    if candidate['remaining'] < current['remaining']:
        return candidate
    if candidate['remaining'] == current['remaining']:
        left, right = candidate.get('resets_at'), current.get('resets_at')
        if left is not None and (right is None or left < right):
            return candidate
    return current


def limit_group(short, weekly):
    if short is None and weekly is None:
        return None
    if short is None:
        return weekly.get('group')
    if weekly is None:
        return short.get('group')
    if weekly['remaining'] < short['remaining']:
        return weekly.get('group')
    return short.get('group')


def public_window(window):
    if not window:
        return None
    return {
        'used': 100 - window['remaining'],
        'resets_at': window.get('resets_at'),
        'group': window.get('group'),
        'minutes': 300 if window.get('kind') == '5h' else 10080 if window.get('kind') == 'weekly' else None,
    }


def apply_summary(summary):
    short = weekly = None
    for row in apply_groups(summary):
        short = lower_window(short, row.get('short'))
        weekly = lower_window(weekly, row.get('weekly'))
    return short, weekly


def apply_groups(summary):
    groups = []
    if not isinstance(summary, dict):
        return groups
    for group in summary.get('groups') or []:
        if not isinstance(group, dict):
            continue
        name = group.get('displayName') if isinstance(group.get('displayName'), str) else None
        if not name:
            continue
        short = weekly = None
        for bucket in group.get('buckets') or []:
            if not isinstance(bucket, dict):
                continue
            remaining = remaining_percent(bucket.get('remainingFraction'))
            if remaining is None:
                continue
            kind = bucket_window(bucket.get('window'), bucket.get('bucketId'))
            if not kind:
                continue
            candidate = {
                'kind': kind,
                'remaining': remaining,
                'resets_at': unix_time(bucket.get('resetTime')),
                'group': name,
            }
            if kind == '5h':
                short = lower_window(short, candidate)
            else:
                weekly = lower_window(weekly, candidate)
        if short is None and weekly is None:
            continue
        groups.append({'name': name, 'short': short, 'weekly': weekly})
    return groups


def merge_groups(current, extra):
    by_name = {}
    for row in list(current or []) + list(extra or []):
        name = row.get('name')
        if not name:
            continue
        previous = by_name.get(name)
        if previous is None:
            by_name[name] = {'name': name, 'short': row.get('short'), 'weekly': row.get('weekly')}
            continue
        previous['short'] = lower_window(previous.get('short'), row.get('short'))
        previous['weekly'] = lower_window(previous.get('weekly'), row.get('weekly'))
    return list(by_name.values())


def public_groups(groups):
    rows = []
    for row in groups or []:
        rows.append({
            'name': row['name'],
            'short': public_window(row.get('short')),
            'weekly': public_window(row.get('weekly')),
        })
    return rows


def windows_from_models(quota):
    short = weekly = None
    if not isinstance(quota, dict):
        return short, weekly
    models = quota.get('models')
    rows = []
    if isinstance(models, dict):
        for name, info in models.items():
            if not isinstance(info, dict):
                continue
            q = info.get('quotaInfo') if isinstance(info.get('quotaInfo'), dict) else info
            rows.append((name, q))
    for bucket in quota.get('buckets') or []:
        if isinstance(bucket, dict):
            rows.append((bucket.get('modelId') or bucket.get('model'), bucket))
    now = time.time()
    for name, info in rows:
        if not isinstance(info, dict):
            continue
        remaining = remaining_percent(info.get('remainingFraction'))
        if remaining is None:
            continue
        reset = unix_time(info.get('resetTime'))
        kind = None
        if reset is not None:
            delta = reset - now
            if 0 < delta <= 6 * 3600:
                kind = '5h'
            elif delta > 6 * 3600:
                kind = 'weekly'
        ident = str(name or '')
        if kind is None:
            kind = bucket_window('', ident) or '5h'
        candidate = {'kind': kind, 'remaining': remaining, 'resets_at': reset, 'group': ident or None}
        if kind == '5h':
            short = lower_window(short, candidate)
        else:
            weekly = lower_window(weekly, candidate)
    return short, weekly


def credential_from_blob(blob):
    from antigravity_provider import identify_antigravity
    identify_antigravity(blob)
    doc = json.loads(blob)
    token = doc.get('token') if isinstance(doc.get('token'), dict) else doc
    if not isinstance(token, dict):
        raise AccountError('Expected an Antigravity OAuth snapshot')
    access = token.get('access_token') if isinstance(token.get('access_token'), str) else None
    refresh = token.get('refresh_token') if isinstance(token.get('refresh_token'), str) else None
    if not access and not refresh:
        raise AccountError('Expected an Antigravity OAuth snapshot')
    expiry = parse_time(token.get('expiry'))
    auth_method = doc.get('auth_method') if isinstance(doc.get('auth_method'), str) else 'consumer'
    client_id = None
    id_token = doc.get('id_token') if isinstance(doc.get('id_token'), str) else token.get('id_token')
    if isinstance(id_token, str) and id_token:
        try:
            payload = claims(id_token)
        except AccountError:
            payload = {}
        for key in ('azp', 'aud'):
            value = payload.get(key)
            if isinstance(value, str) and value.endswith('.apps.googleusercontent.com'):
                client_id = value
                break
    if client_id is None:
        client_id = GCP_CLIENT_ID if auth_method.lower() == 'gcp' else CONSUMER_CLIENT_ID
    return {
        'access': access,
        'refresh': refresh,
        'expiry': expiry,
        'auth_method': auth_method,
        'client_id': client_id,
        'project': doc.get('project_id') if isinstance(doc.get('project_id'), str) else None,
        'doc': doc,
        'token': token,
    }


class UrlLibTransport:
    def __init__(self, opener=None, timeout=12):
        self.opener = opener
        self.timeout = timeout
        self.context = ssl.create_default_context()

    def _open(self, request):
        if self.opener is not None:
            return self.opener(request, timeout=self.timeout)
        return urlopen(request, timeout=self.timeout, context=self.context)

    def request(self, url, data, headers):
        request = Request(url, data=data, headers=headers, method='POST')
        try:
            with self._open(request) as response:
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    raise AccountError('Quota response is too large')
                return response.status, body
        except HTTPError as error:
            body = b''
            try:
                body = error.read(MAX_BODY)
            except OSError:
                pass
            return error.code, body
        except (URLError, TimeoutError, ssl.SSLError, OSError):
            raise AccountError('Không đọc được quota Antigravity từ Google') from None


class QuotaFetcher:
    def __init__(self, transport=None, now=None, ttl=TTL_SECONDS, secret=None):
        self.transport = transport or UrlLibTransport()
        self.now = now or time.time
        self.ttl = ttl
        self.secret = secret
        self.lock = threading.Lock()
        self.cache = {}
        self.inflight = set()

    def _headers(self, access=None, form=False):
        headers = {
            'Accept': 'application/json',
            'User-Agent': USER_AGENT,
            'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
        }
        if access:
            headers['Authorization'] = 'Bearer ' + access
        return headers

    def _decode(self, body):
        if not body:
            return {}
        try:
            payload = json.loads(body.decode())
        except (UnicodeDecodeError, ValueError, TypeError):
            raise AccountError('Quota response is invalid') from None
        return payload if isinstance(payload, dict) else {}

    def _post_json(self, url, payload, access):
        status, body = self.transport.request(
            url, json.dumps(payload).encode(), self._headers(access))
        return status, self._decode(body)

    def _client_secret(self):
        if self.secret is not None:
            return self.secret or None
        return cached_client_secret()

    def refresh_access(self, creds):
        refresh = creds.get('refresh')
        if not refresh:
            raise AccountError('Phiên Antigravity hết hạn; mở IDE rồi lưu lại snapshot')
        fields = {
            'client_id': creds['client_id'],
            'refresh_token': refresh,
            'grant_type': 'refresh_token',
        }
        secret = self._client_secret()
        if secret:
            fields['client_secret'] = secret
        form = urlencode(fields).encode()
        status, body = self.transport.request(TOKEN_URL, form, self._headers(form=True))
        payload = self._decode(body)
        if status != 200 or not isinstance(payload.get('access_token'), str) or not payload['access_token']:
            raise AccountError('Không làm mới được phiên Antigravity; mở IDE rồi lưu lại snapshot')
        expires_in = payload.get('expires_in')
        try:
            seconds = int(expires_in)
        except (TypeError, ValueError):
            seconds = 3600
        if seconds <= 0:
            seconds = 3600
        expiry = datetime.fromtimestamp(self.now() + seconds, tz=timezone.utc)
        creds['access'] = payload['access_token']
        creds['expiry'] = expiry
        if isinstance(payload.get('id_token'), str) and payload['id_token']:
            creds['doc']['id_token'] = payload['id_token']
        creds['token']['access_token'] = payload['access_token']
        creds['token']['expiry'] = expiry.isoformat().replace('+00:00', 'Z')
        creds['updated'] = True
        return creds

    def ensure_access(self, creds):
        access = creds.get('access')
        expiry = creds.get('expiry')
        if access and (expiry is None or expiry.timestamp() > self.now() + 30):
            return creds
        return self.refresh_access(creds)

    def _post_auth(self, creds, url, payload):
        status, body = self._post_json(url, payload, creds['access'])
        if status == 401:
            creds = self.refresh_access(creds)
            status, body = self._post_json(url, payload, creds['access'])
        return status, body, creds

    def _assist_and_summary(self, creds):
        metadata = {'ideType': 'ANTIGRAVITY', 'platform': 'PLATFORM_UNSPECIFIED', 'pluginType': 'GEMINI'}
        assist_body = {'metadata': metadata}
        if creds.get('project'):
            assist_body['cloudaicompanionProject'] = creds['project']
        plan = None
        short = weekly = None
        groups = []
        last_error = None
        for endpoint in ENDPOINTS:
            status, assist, creds = self._post_auth(creds, endpoint + ':loadCodeAssist', assist_body)
            if status in (401, 403):
                last_error = 'forbidden' if status == 403 else 'expired'
                continue
            project = creds.get('project')
            if 200 <= status <= 299:
                if plan is None:
                    plan = plan_label(assist, creds.get('auth_method'))
                if isinstance(assist.get('cloudaicompanionProject'), str):
                    project = assist['cloudaicompanionProject']
            else:
                last_error = last_error or 'unavailable'
            summary_body = {'project': project} if project else {}
            status, summary, creds = self._post_auth(
                creds, endpoint + ':retrieveUserQuotaSummary', summary_body)
            if 200 <= status <= 299:
                extra_short, extra_weekly = apply_summary(summary)
                short = lower_window(short, extra_short)
                weekly = lower_window(weekly, extra_weekly)
                groups = merge_groups(groups, apply_groups(summary))
            elif status in (401, 403):
                last_error = 'forbidden' if status == 403 else 'expired'
            else:
                last_error = last_error or 'unavailable'
            if groups:
                break
            status, models, creds = self._post_auth(
                creds, endpoint + ':fetchAvailableModels', {})
            if 200 <= status <= 299:
                extra_short, extra_weekly = windows_from_models(models)
                short = lower_window(short, extra_short)
                weekly = lower_window(weekly, extra_weekly)
            if short is None and weekly is None:
                status, quota, creds = self._post_auth(
                    creds, endpoint + ':retrieveUserQuota', summary_body)
                if 200 <= status <= 299:
                    extra_short, extra_weekly = windows_from_models(quota)
                    short = lower_window(short, extra_short)
                    weekly = lower_window(weekly, extra_weekly)
            if short is not None or weekly is not None:
                break
        if short is not None or weekly is not None:
            return {
                'status': 'ok',
                'plan': plan,
                'short': public_window(short),
                'weekly': public_window(weekly),
                'groups': public_groups(groups),
                'observed_at': self.now(),
                'stale': False,
                'error': None,
                'limit_group': limit_group(short, weekly),
            }, creds
        expired = last_error == 'expired'
        return {
            'status': last_error or 'error',
            'plan': plan, 'short': None, 'weekly': None, 'groups': public_groups(groups),
            'observed_at': self.now(), 'stale': False,
            'error': 'Phiên hết hạn; mở IDE rồi lưu lại snapshot' if expired
            else 'Không đọc được hạn mức Antigravity',
            'limit_group': None,
        }, creds

    def fetch(self, blob):
        creds = credential_from_blob(blob)
        creds = self.ensure_access(creds)
        result, creds = self._assist_and_summary(creds)
        return result, creds if creds.get('updated') else None

    def peek(self, identity):
        with self.lock:
            row = self.cache.get(identity)
            if not row:
                return None
            copy = dict(row['quota'])
            if self.now() - row['fetched_at'] > self.ttl:
                copy['stale'] = True
            return copy

    def store(self, identity, quota):
        with self.lock:
            self.cache[identity] = {'quota': quota, 'fetched_at': self.now()}

    def drop(self, identity):
        with self.lock:
            self.cache.pop(identity, None)
            self.inflight.discard(identity)

    def cached_or_loading(self, identity):
        found = self.peek(identity)
        if found:
            return found
        return {'status': 'loading', 'plan': None, 'short': None, 'weekly': None, 'groups': [],
                'observed_at': None, 'stale': False, 'error': None, 'limit_group': None}


def updated_blob(creds):
    doc = creds['doc']
    token = creds['token']
    if isinstance(doc.get('token'), dict):
        doc['token'] = token
    else:
        doc.update(token)
    return json.dumps(doc, ensure_ascii=False).encode()
