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
import re
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
    if label in ('5h', '5-hour', 'five_hour', 'five-hour', 'five hour'):
        return '5h'
    if label in ('weekly', 'week'):
        return 'weekly'
    ident = (bucket_id or '').lower()
    if 'weekly' in ident:
        return 'weekly'
    if '5h' in ident or 'five' in ident:
        return '5h'
    return ''


GENERIC_BUCKET_LABELS = {
    'weekly limit remaining',
    'five hour limit remaining',
    '5 hour limit remaining',
}
MODEL_VARIANT_SUFFIX = re.compile(r'-(?:low|medium|high|xhigh|sonar|tiered(?:-.*)?)$', re.I)
THINKING_DISPLAY = re.compile(r'\s*\((?:Low|Medium|High|XHigh|Sonar)\)\s*$', re.I)


def window_label(kind, display_name=None):
    if isinstance(display_name, str) and display_name.strip() and display_name.strip().lower() not in GENERIC_BUCKET_LABELS:
        return display_name.strip()
    if kind == '5h':
        return '5 giờ'
    if kind == 'weekly':
        return 'tuần'
    return kind or 'quota'


def window_kind(info, bucket_id=None, now=None):
    kind = bucket_window(info.get('window') if isinstance(info, dict) else None, bucket_id)
    if kind:
        return kind
    reset = unix_time(info.get('resetTime') if isinstance(info, dict) else None)
    if reset is not None:
        delta = reset - (time.time() if now is None else now)
        if 0 < delta <= 6 * 3600:
            return '5h'
        if delta > 6 * 3600:
            return 'weekly'
    label = ''
    if isinstance(info, dict) and isinstance(info.get('window'), str):
        label = info['window'].strip()
    if not label and isinstance(bucket_id, str):
        label = bucket_id.strip()
    return label or 'quota'


def parse_window(info, group_name, bucket_id=None, now=None):
    if not isinstance(info, dict):
        return None
    remaining = remaining_percent(info.get('remainingFraction'))
    if remaining is None:
        return None
    disabled = info.get('disabled') is True
    if disabled:
        remaining = 0
    kind = window_kind(info, bucket_id, now=now)
    return {
        'kind': kind,
        'remaining': remaining,
        'resets_at': unix_time(info.get('resetTime')),
        'group': group_name,
        'label': window_label(kind, info.get('displayName')),
        'disabled': disabled,
    }


def group_name_from(group, fallback='Models'):
    if not isinstance(group, dict):
        return fallback
    for key in ('displayName', 'name', 'title', 'groupId', 'id'):
        value = group.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def upsert_window(row, candidate):
    if not candidate:
        return
    kind = candidate['kind']
    windows = row.setdefault('windows', [])
    found = None
    for item in windows:
        if item.get('kind') == kind:
            found = item
            break
    merged = lower_window(found, candidate)
    if found is None:
        windows.append(merged)
    else:
        found.update(merged)
    if kind == '5h':
        row['short'] = lower_window(row.get('short'), candidate)
    elif kind == 'weekly':
        row['weekly'] = lower_window(row.get('weekly'), candidate)


def empty_group(name, kind='pool', tag=None):
    return {'name': name, 'kind': kind, 'tag': tag, 'short': None, 'weekly': None, 'windows': []}


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
        'kind': window.get('kind'),
        'label': window.get('label') or window_label(window.get('kind')),
        'disabled': bool(window.get('disabled')),
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
        name = group_name_from(group, '')
        if not name:
            continue
        row = empty_group(name, 'pool')
        for bucket in group.get('buckets') or []:
            if not isinstance(bucket, dict):
                continue
            upsert_window(row, parse_window(bucket, name, bucket.get('bucketId')))
        if not row['windows']:
            continue
        groups.append(row)
    return groups


def merge_groups(current, extra):
    by_name = {}
    order = []
    for row in list(current or []) + list(extra or []):
        name = row.get('name')
        if not name:
            continue
        previous = by_name.get(name)
        if previous is None:
            copied = empty_group(name, row.get('kind') or 'pool', row.get('tag'))
            for window in row.get('windows') or ():
                upsert_window(copied, window)
            upsert_window(copied, row.get('short'))
            upsert_window(copied, row.get('weekly'))
            by_name[name] = copied
            order.append(name)
            continue
        if previous.get('kind') != 'pool' and row.get('kind') == 'pool':
            previous['kind'] = 'pool'
        if not previous.get('tag') and row.get('tag'):
            previous['tag'] = row['tag']
        for window in row.get('windows') or ():
            upsert_window(previous, window)
        upsert_window(previous, row.get('short'))
        upsert_window(previous, row.get('weekly'))
    return [by_name[name] for name in order]


def public_groups(groups):
    rows = []
    for row in groups or []:
        windows = [public_window(item) for item in row.get('windows') or ()]
        windows = [item for item in windows if item]
        if not windows:
            if row.get('short'):
                windows.append(public_window(row['short']))
            if row.get('weekly'):
                windows.append(public_window(row['weekly']))
            windows = [item for item in windows if item]
        rank = {'5h': 0, 'weekly': 1}
        windows.sort(key=lambda item: (rank.get(item.get('kind'), 2), item.get('label') or ''))
        rows.append({
            'name': row['name'],
            'kind': row.get('kind') or 'pool',
            'tag': row.get('tag'),
            'short': public_window(row.get('short')),
            'weekly': public_window(row.get('weekly')),
            'windows': windows,
        })
    return rows


def model_family_id(model_id, families):
    if model_id in families:
        return model_id
    for family in sorted(families, key=len, reverse=True):
        if model_id == family or model_id.startswith(family + '-'):
            return family
    stripped = MODEL_VARIANT_SUFFIX.sub('', model_id)
    return stripped or model_id


def model_family_name(family_id, families, display_name):
    info = families.get(family_id)
    if isinstance(info, dict) and isinstance(info.get('displayName'), str) and info['displayName'].strip():
        return info['displayName'].strip()
    cleaned = THINKING_DISPLAY.sub('', display_name or '').strip()
    return cleaned or display_name or family_id


def _model_order(payload, models):
    order = []
    sorts = payload.get('agentModelSorts')
    if isinstance(sorts, list):
        for sort in sorts:
            if not isinstance(sort, dict):
                continue
            for group in sort.get('groups') or []:
                if not isinstance(group, dict):
                    continue
                for model_id in group.get('modelIds') or []:
                    if isinstance(model_id, str) and model_id not in order:
                        order.append(model_id)
    for model_id in models:
        if model_id not in order:
            order.append(model_id)
    return order


def groups_from_models(payload, now=None):
    groups = []
    if not isinstance(payload, dict):
        return groups
    models = payload.get('models')
    if not isinstance(models, dict):
        return groups
    families = payload.get('groupedModels') if isinstance(payload.get('groupedModels'), dict) else {}
    buckets = {}
    order = []

    def ensure(family_id, name, tag=None):
        row = buckets.get(family_id)
        if row is None:
            row = empty_group(name, 'model', tag)
            buckets[family_id] = row
            order.append(family_id)
        elif tag and not row.get('tag'):
            row['tag'] = tag
        return row

    for family_id, info in families.items():
        name = info.get('displayName') if isinstance(info, dict) else None
        ensure(family_id, name.strip() if isinstance(name, str) and name.strip() else family_id)
    for model_id in _model_order(payload, models):
        info = models.get(model_id)
        if not isinstance(info, dict):
            continue
        display = info.get('displayName')
        if not isinstance(display, str) or not display.strip():
            continue
        family_id = model_family_id(model_id, families)
        tag = info.get('tagTitle') if isinstance(info.get('tagTitle'), str) and info['tagTitle'].strip() else None
        row = ensure(family_id, model_family_name(family_id, families, display.strip()), tag)
        quota = info.get('quotaInfo') if isinstance(info.get('quotaInfo'), dict) else info
        upsert_window(row, parse_window(quota, row['name'], model_id, now=now))
    for family_id in order:
        row = buckets[family_id]
        if row['windows']:
            groups.append(row)
    return groups


def windows_from_models(quota, now=None):
    short = weekly = None
    for row in groups_from_models(quota, now=now):
        short = lower_window(short, row.get('short'))
        weekly = lower_window(weekly, row.get('weekly'))
        for window in row.get('windows') or ():
            if window.get('kind') == '5h':
                short = lower_window(short, window)
            elif window.get('kind') == 'weekly':
                weekly = lower_window(weekly, window)
    if short is not None or weekly is not None:
        return short, weekly
    if not isinstance(quota, dict):
        return short, weekly
    rows = []
    models = quota.get('models')
    if isinstance(models, dict):
        for name, info in models.items():
            if not isinstance(info, dict):
                continue
            q = info.get('quotaInfo') if isinstance(info.get('quotaInfo'), dict) else info
            rows.append((name, q))
    for bucket in quota.get('buckets') or []:
        if isinstance(bucket, dict):
            rows.append((bucket.get('modelId') or bucket.get('model'), bucket))
    clock = time.time() if now is None else now
    for name, info in rows:
        candidate = parse_window(info if isinstance(info, dict) else {}, str(name or '') or 'Models', name, now=clock)
        if not candidate:
            continue
        if candidate['kind'] == 'weekly':
            weekly = lower_window(weekly, candidate)
        else:
            short = lower_window(short, candidate)
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
            status, models, creds = self._post_auth(
                creds, endpoint + ':fetchAvailableModels', {})
            if 200 <= status <= 299:
                extra_short, extra_weekly = windows_from_models(models)
                short = lower_window(short, extra_short)
                weekly = lower_window(weekly, extra_weekly)
                groups = merge_groups(groups, groups_from_models(models))
            if groups or short is not None or weekly is not None:
                break
            status, quota, creds = self._post_auth(
                creds, endpoint + ':retrieveUserQuota', summary_body)
            if 200 <= status <= 299:
                extra_short, extra_weekly = windows_from_models(quota)
                short = lower_window(short, extra_short)
                weekly = lower_window(weekly, extra_weekly)
                groups = merge_groups(groups, groups_from_models(quota))
            if short is not None or weekly is not None or groups:
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
