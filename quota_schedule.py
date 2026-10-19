"""Per-profile polling decisions; persisted state contains quota metadata only."""
import math


def fingerprint(usage):
    return [[(usage.get(name) or {}).get(field) for field in ('used_percent','resets_at','window_minutes')]
            for name in ('primary','secondary')]+[(usage.get('refresh') or {}).get('status')]


def interval(active, disabled, usage, last_changed, now, low_interval):
    if disabled or not active:
        return 1800, 'disabled' if disabled else 'standby'
    if now-last_changed>1800:
        return 1800, 'inactive'
    percentages=[(usage.get(name) or {}).get('used_percent') for name in ('primary','secondary')]
    values=[v for v in percentages if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)]
    if not values:
        return 120, 'unknown'
    remaining=100-max(values)
    if remaining>50:
        return 300, 'remaining_above_50'
    if remaining>=30:
        return 120, 'remaining_30_to_50'
    return low_interval, 'remaining_below_30'
