import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qsl

from antigravity_quota import (
    QuotaFetcher, apply_groups, apply_summary, discover_client_secret, plan_label,
    remaining_percent, scan_client_secret,
)
from test_antigravity_provider import token


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, data, headers):
        self.calls.append({
            'url': url,
            'has_auth': 'Authorization' in headers,
            'content_type': headers.get('Content-Type'),
        })
        if 'Authorization' in headers:
            value = headers['Authorization']
            if value.startswith('Bearer '):
                self.calls[-1]['bearer'] = value.split(' ', 1)[1]
        if headers.get('Content-Type') == 'application/x-www-form-urlencoded' and data:
            self.calls[-1]['form_keys'] = [key for key, _value in parse_qsl(data.decode(), keep_blank_values=True)]
        if not self.responses:
            return 500, b'{}'
        status, body = self.responses.pop(0)
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        return status, body


SUMMARY = {
    'groups': [
        {'displayName': 'Claude and GPT models', 'buckets': [
            {'bucketId': '3p-5h', 'window': '5h', 'remainingFraction': 0.8, 'resetTime': '2026-10-22T18:11:00Z'},
            {'bucketId': '3p-weekly', 'window': 'weekly', 'remainingFraction': 0.6, 'resetTime': '2026-10-29T13:11:00Z'},
        ]},
        {'displayName': 'Gemini Models', 'buckets': [
            {'bucketId': 'gemini-5h', 'window': '5h', 'remainingFraction': 0.2, 'resetTime': '2026-10-22T19:11:00Z'},
            {'bucketId': 'gemini-weekly', 'window': 'weekly', 'remainingFraction': 0.4, 'resetTime': '2026-10-29T14:11:00Z'},
        ]},
    ]
}


class QuotaParseTests(unittest.TestCase):
    def test_summary_uses_most_constrained_windows(self):
        short, weekly = apply_summary(SUMMARY)
        self.assertEqual(short['remaining'], 20)
        self.assertEqual(short['group'], 'Gemini Models')
        self.assertEqual(weekly['remaining'], 40)
        self.assertEqual(datetime.fromtimestamp(weekly['resets_at'], tz=timezone.utc).day, 29)
        names = [row['name'] for row in apply_groups(SUMMARY)]
        self.assertEqual(names, ['Claude and GPT models', 'Gemini Models'])
        gemini = apply_groups(SUMMARY)[1]
        self.assertEqual(gemini['short']['remaining'], 20)
        self.assertEqual(gemini['weekly']['remaining'], 40)

    def test_plan_labels(self):
        self.assertEqual(plan_label({'paidTier': {'id': 'g1-pro-tier'}}), 'Pro')
        self.assertEqual(plan_label({'currentTier': {'id': 'free-tier'}}), 'Free')
        self.assertEqual(remaining_percent(0.755), 76)


class QuotaFetchTests(unittest.TestCase):
    def test_fetch_plan_and_windows_without_exposing_tokens_in_result(self):
        transport = ScriptedTransport([
            (200, {'paidTier': {'id': 'g1-pro-tier'}, 'cloudaicompanionProject': 'proj'}),
            (200, SUMMARY),
        ])
        fetcher = QuotaFetcher(transport=transport, now=lambda: 1_700_000_000)
        result, refreshed = fetcher.fetch(token('work'))
        self.assertIsNone(refreshed)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['plan'], 'Pro')
        self.assertEqual(result['short']['used'], 80)
        self.assertEqual(result['weekly']['used'], 60)
        self.assertEqual([row['name'] for row in result['groups']], [
            'Claude and GPT models', 'Gemini Models'])
        self.assertEqual(result['groups'][1]['short']['used'], 80)
        self.assertEqual(result['groups'][0]['weekly']['used'], 40)
        encoded = json.dumps(result)
        self.assertNotIn('refresh-work', encoded)
        self.assertNotIn('access-work', encoded)
        self.assertEqual(transport.calls[0]['bearer'], 'access-work')
        self.assertTrue(transport.calls[0]['url'].endswith(':loadCodeAssist'))
        self.assertTrue(transport.calls[1]['url'].endswith(':retrieveUserQuotaSummary'))

    def test_expired_token_refreshes_then_reads_quota(self):
        blob = token('work')
        doc = json.loads(blob)
        doc['token']['expiry'] = '2020-01-01T00:00:00Z'
        transport = ScriptedTransport([
            (200, {'access_token': 'fresh-access', 'expires_in': 3600}),
            (200, {'paidTier': {'id': 'g1-ultra-tier'}}),
            (200, SUMMARY),
        ])
        fetcher = QuotaFetcher(transport=transport, now=lambda: 1_700_000_000, secret='GOCSPX-unit-test')
        result, refreshed = fetcher.fetch(json.dumps(doc).encode())
        self.assertEqual(result['plan'], 'Ultra')
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed['access'], 'fresh-access')
        self.assertEqual(transport.calls[0]['url'], 'https://oauth2.googleapis.com/token')
        self.assertFalse(transport.calls[0]['has_auth'])
        self.assertEqual(transport.calls[0]['form_keys'], [
            'client_id', 'refresh_token', 'grant_type', 'client_secret',
        ])
        self.assertEqual(transport.calls[1]['bearer'], 'fresh-access')
        self.assertNotIn('GOCSPX-unit-test', json.dumps(result))
        self.assertNotIn('GOCSPX-unit-test', json.dumps(transport.calls))

    def test_models_fallback_when_summary_empty(self):
        transport = ScriptedTransport([
            (200, {'currentTier': {'id': 'free-tier'}}),
            (200, {'groups': []}),
            (200, {'models': {
                'gemini-3-pro': {'quotaInfo': {'remainingFraction': 0.5, 'resetTime': '2099-01-01T00:00:00Z'}},
                'claude-sonnet': {'quotaInfo': {'remainingFraction': 0.1, 'resetTime': '2099-01-01T01:00:00Z'}},
            }}),
        ])
        fetcher = QuotaFetcher(transport=transport, now=lambda: 1_700_000_000)
        result, _refreshed = fetcher.fetch(token('work'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['plan'], 'Free')
        self.assertEqual(result['weekly']['used'], 90)

    def test_cache_ttl(self):
        fetcher = QuotaFetcher(now=lambda: self.now)
        self.now = 100
        fetcher.store('abc', {'status': 'ok', 'short': {'used': 10}})
        self.assertEqual(fetcher.peek('abc')['short']['used'], 10)
        self.now = 200
        self.assertTrue(fetcher.peek('abc')['stale'])

    def test_summary_when_assist_is_invalid(self):
        transport = ScriptedTransport([
            (400, {'error': {'code': 400, 'status': 'INVALID_ARGUMENT'}}),
            (200, SUMMARY),
        ])
        fetcher = QuotaFetcher(transport=transport, now=lambda: 1_700_000_000)
        result, _refreshed = fetcher.fetch(token('work'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['short']['used'], 80)
        self.assertEqual(result['weekly']['used'], 60)
        self.assertTrue(transport.calls[0]['url'].endswith(':loadCodeAssist'))
        self.assertTrue(transport.calls[1]['url'].endswith(':retrieveUserQuotaSummary'))

    def test_keeps_every_summary_group(self):
        transport = ScriptedTransport([
            (200, {'paidTier': {'id': 'g1-pro-tier'}}),
            (200, {'groups': [
                {'displayName': 'Gemini Models', 'buckets': [
                    {'bucketId': 'gemini-5h', 'window': '5h', 'remainingFraction': 0.34,
                     'resetTime': '2026-10-22T18:11:00Z'},
                    {'bucketId': 'gemini-weekly', 'window': 'weekly', 'remainingFraction': 0.41,
                     'resetTime': '2026-10-29T13:11:00Z'},
                ]},
                {'displayName': 'Claude and GPT models', 'buckets': [
                    {'bucketId': '3p-5h', 'window': '5h', 'remainingFraction': 1.0,
                     'resetTime': '2026-10-22T18:11:00Z'},
                    {'bucketId': '3p-weekly', 'window': 'weekly', 'remainingFraction': 1.0,
                     'resetTime': '2026-10-29T13:11:00Z'},
                ]},
                {'displayName': 'Gemini 4 Argon', 'buckets': [
                    {'bucketId': 'gemini-argon-wtu-weekly', 'window': 'weekly',
                     'remainingFraction': 0.0, 'resetTime': '2026-10-28T16:06:31Z'},
                ]},
            ]}),
        ])
        fetcher = QuotaFetcher(transport=transport, now=lambda: 1_700_000_000)
        result, _refreshed = fetcher.fetch(token('work'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['plan'], 'Pro')
        self.assertEqual(result['short']['used'], 66)
        self.assertEqual(result['weekly']['used'], 100)
        self.assertEqual(result['weekly']['group'], 'Gemini 4 Argon')
        names = [row['name'] for row in result['groups']]
        self.assertEqual(names, ['Gemini Models', 'Claude and GPT models', 'Gemini 4 Argon'])
        self.assertEqual(result['groups'][0]['weekly']['used'], 59)
        self.assertEqual(result['groups'][1]['short']['used'], 0)
        self.assertIsNone(result['groups'][2]['short'])
        self.assertEqual(len(transport.calls), 2)

    def test_empty_windows_are_error(self):
        transport = ScriptedTransport([
            (200, {'paidTier': {'id': 'g1-pro-tier'}}),
            (200, {'groups': []}),
            (200, {'models': {}}),
            (200, {}),
        ])
        fetcher = QuotaFetcher(transport=transport, now=lambda: 1_700_000_000)
        result, _refreshed = fetcher.fetch(token('work'))
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['plan'], 'Pro')
        self.assertIsNone(result['short'])
        self.assertIsNone(result['weekly'])
        self.assertEqual(result['error'], 'Không đọc được hạn mức Antigravity')


class SecretDiscoveryTests(unittest.TestCase):
    def test_scan_matches_known_hash_only(self):
        secret = 'GOCSPX-' + ('A' * 28)
        digest = hashlib.sha256(secret.encode()).hexdigest()
        handle = tempfile.NamedTemporaryFile(delete=False)
        try:
            handle.write(b'prefix\x00' + secret.encode() + b'\x00suffix')
            handle.close()
            self.assertEqual(scan_client_secret(handle.name, hashes=(digest,)), secret)
            self.assertIsNone(scan_client_secret(handle.name, hashes=('00' * 32,)))
        finally:
            os.unlink(handle.name)

    def test_discover_from_env(self):
        with patch.dict(os.environ, {'ANTIGRAVITY_CLIENT_SECRET': 'GOCSPX-from-env'}):
            self.assertEqual(discover_client_secret(paths=[]), 'GOCSPX-from-env')


if __name__ == '__main__':
    unittest.main()
