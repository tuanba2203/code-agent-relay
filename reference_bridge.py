"""Read-only compatibility bridge for codex-auth schema 1.

Input is an envelope produced by a trusted caller at command completion:
{"received_at": <Unix seconds>, "result": <codex-auth list --api --json document>}
This module neither executes codex-auth nor accesses credentials or controls apps.
"""
import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
import time
from coordinator import Profile, Usage


class SnapshotError(ValueError):
    pass


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class Candidate:
    profile: Profile
    usage: Usage
    active: bool
    reason: str


def decode_snapshot(envelope, now, max_age=60, allow_unknown_active=False):
    if not isinstance(envelope, dict):
        raise SnapshotError("Snapshot envelope must be an object")
    received = envelope.get("received_at")
    if not finite_number(received) or not 0 <= now - received <= max_age:
        raise SnapshotError("Snapshot receipt time is missing, stale or in the future")
    document = envelope.get("result")
    if (not isinstance(document, dict) or type(document.get("schema_version")) is not int
            or document.get("schema_version") != 1):
        raise SnapshotError("Unsupported codex-auth schema")
    if document.get("error") or document.get("command") != "list":
        raise SnapshotError("Expected a successful list response")
    accounts = document.get("accounts")
    if not isinstance(accounts, list):
        raise SnapshotError("Accounts must be an array")
    active_key = document.get("active_account_key")
    if (not isinstance(active_key, str) or not active_key) and not (allow_unknown_active and active_key is None):
        raise SnapshotError("Active account is not known")
    seen, candidates = set(), []
    for account in accounts:
        if not isinstance(account, dict):
            raise SnapshotError("Malformed account")
        key = account.get("account_key")
        if not isinstance(key, str) or not key or key in seen:
            raise SnapshotError("Missing or duplicate account identity")
        seen.add(key)
        active = key == active_key
        if type(account.get("active")) is not bool or account["active"] != active:
            raise SnapshotError("Account active flags disagree")
        alias = account.get("alias")
        email=account.get('email')
        label = alias if isinstance(alias, str) and alias else email if isinstance(email,str) and email else key
        raw = account.get("usage") or {}
        if not isinstance(raw, dict):
            raise SnapshotError("Malformed usage")
        refresh = raw.get("refresh") or {}
        if not isinstance(refresh, dict):
            raise SnapshotError("Malformed refresh state")
        reason = ""
        if account.get("auth_mode") != "chatgpt":
            reason = "Unsupported auth mode for subscription quota"
        elif (raw.get("source") != "api" or refresh.get("requested") is not True
              or refresh.get("method") != "api" or refresh.get("status") != "ok"):
            reason = "No successful API refresh in this invocation"
        values = []
        for name in ("primary", "secondary"):
            window = raw.get(name)
            if window is None:
                continue
            if not isinstance(window, dict):
                reason = "Malformed quota window"
                continue
            used = window.get("used_percent")
            if not finite_number(used) or not 0 <= used <= 100:
                reason = "Invalid quota value"
                continue
            reset = window.get("resets_at")
            if reset is not None and (not finite_number(reset) or reset <= now):
                reason = "Expired/invalid window; fetch fresh data rather than infer reset"
            values.append(used)
        if not values:
            reason = reason or "Quota windows unavailable"
        observed = raw.get('observed_at',received)
        if not finite_number(observed) or not 0 <= now-observed:
            observed=received
            reason='Invalid measurement timestamp'
        candidates.append(Candidate(Profile(label, key), Usage(values, observed, bool(reason)),
                                    active, reason))
    if active_key not in seen and not (allow_unknown_active and active_key is None):
        raise SnapshotError("Active account missing from list")
    return candidates


def plan(envelope, now=None, threshold=92, entry_margin=10):
    now = time.time() if now is None else now
    if not finite_number(now) or not finite_number(threshold) or not 1 <= threshold <= 99:
        raise SnapshotError("Invalid clock or threshold; threshold must be 1..99")
    if not finite_number(entry_margin) or not 0 <= entry_margin <= 20:
        raise SnapshotError("Entry margin must be 0..20")
    entry_threshold = max(1, threshold-entry_margin)
    candidates = decode_snapshot(envelope, now)
    active = next(c for c in candidates if c.active)
    available = [c for c in candidates if not c.active and c.usage.eligible(now, entry_threshold)]
    # Maximize the smaller remaining allowance across all reported windows.
    available.sort(key=lambda c: (max(c.usage.windows), c.profile.identity))
    needs_switch = not active.usage.eligible(now, threshold)
    target = available[0] if needs_switch and available else None
    return {
        "schema_version": 1,
        "mode": "plan_only",
        "action": "keep" if not needs_switch else "prepare_handoff" if target else "pause",
        "active": active.profile.identity,
        "target": target.profile.identity if target else None,
        "threshold_used_percent": threshold,
        "entry_threshold_used_percent": entry_threshold,
        "accounts": [{"identity": c.profile.identity, "label": c.profile.name,
                      "used_percent": c.usage.windows,
                      "eligible": c.usage.eligible(now, entry_threshold), "reason": c.reason}
                     for c in candidates],
        "next_steps": (["Freeze new turns in desktop and VS Code",
                        "Drain active tools and save verified handoffs",
                        "Revalidate target quota", "Switch and verify both frontend identities",
                        "Prepare context in a new session"] if target else []),
        "executed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--threshold", type=int, default=92)
    args = parser.parse_args()
    if args.snapshot.stat().st_size > 4 * 1024 * 1024:
        raise SnapshotError("Snapshot too large")
    print(json.dumps(plan(json.loads(args.snapshot.read_text())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError):
        # Do not echo malformed input or paths that might contain secrets.
        print('Snapshot rejected; no changes made.', file=sys.stderr)
        sys.exit(1)
