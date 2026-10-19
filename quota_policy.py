"""Shared ranking policy for preview and freshly verified rotation candidates."""
import math


def entry_ceiling(options):
    ceiling=max(1,100-options['reserve']-options['margin'])
    return min(70,ceiling) if options.get('strategy','reset_first')=='reset_first' else ceiling


def priority(short,weekly,now,strategy='reset_first'):
    if not short or not weekly:return (None,0)
    sr,wr=100-short['used'],100-weekly['used']
    bottleneck=min(sr,wr)
    if strategy=='balanced':return (bottleneck,0)
    def urgency(window):
        reset=window.get('resets_at')
        if not isinstance(reset,(int,float)) or isinstance(reset,bool) or not math.isfinite(reset) or reset<=now:return 0
        return max(0,1-(reset-now)/(window['minutes']*60))
    # Weekly quota carries 65 points, five-hour quota 25, continuity 10.
    bonus=wr*.20*urgency(weekly)+sr*.05*urgency(short)
    total=wr*.45+sr*.20+bottleneck*.10+bonus
    rounded=lambda value:math.floor(value*100+.5)/100
    return rounded(total),rounded(bonus)


def reset_wait(windows,threshold,now,horizon=300):
    """Wait only when EVERY limiting window can reset soon; never infer a refill."""
    limiting=[w for w in windows if w.get('used',0)>=threshold]
    if not limiting:return None
    resets=[]
    for window in limiting:
        reset=window.get('resets_at')
        if (not isinstance(reset,(int,float)) or isinstance(reset,bool)
                or not math.isfinite(reset) or not 0<reset-now<=horizon):return None
        resets.append(reset)
    return max(resets)+1
