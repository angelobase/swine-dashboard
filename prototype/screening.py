"""Screening rules for scheduled sessions. Pure functions: no camera, no network, no database.

Per capture, every known pig gets one row:
  posture            'Lying' | 'Not lying' | 'Unknown' (pig not detected in this capture)
  lying_s            estimated length of the current lying bout = consecutive lying captures x interval
  behavioral_status  'Abnormal' once the current bout reaches the configured number of captures,
                     'Normal' otherwise, 'No data' when the pig was not detected
  thermal_status     'Elevated' at or above the threshold, 'Normal' below it, 'No result' without a valid reading

After the session, a pig is flagged when BOTH hold:
  - its longest lying bout reached the configured number of consecutive captures, and
  - its final temperature is at or above the threshold. The final temperature is the most recent valid
    reading, but only if it came from one of the last `lookback` captures; otherwise it is 'No result'.
This is a screening rule, not a diagnosis.
"""
from datetime import datetime

BEHAVIOR = ('Normal', 'Abnormal', 'No data')
THERMAL = ('Normal', 'Elevated', 'No result')


def thermal_status(temp_c, threshold_c):
    if temp_c is None:
        return 'No result'
    return 'Elevated' if temp_c >= threshold_c else 'Normal'


def new_track(seq):
    return {'streak': 0, 'max_streak': 0, 'lying_captures': 0, 'last_temp': None, 'last_temp_seq': None,
            'first_seq': seq, 'last_seen_seq': None}


def step(track, seq, observed, lying, temp_c, interval_s, rule_n, threshold_c):
    """Advance one pig's track by one capture and return that capture's row. Mutates `track`.

    A capture in which the pig is not detected ends its lying bout: unobserved time is never counted as lying.
    """
    if observed:
        track['last_seen_seq'] = seq
        if lying:
            track['streak'] += 1
            track['lying_captures'] += 1
            track['max_streak'] = max(track['max_streak'], track['streak'])
        else:
            track['streak'] = 0
        if temp_c is not None:
            track['last_temp'], track['last_temp_seq'] = float(temp_c), seq
        behavioral = 'Abnormal' if track['streak'] >= rule_n else 'Normal'
        posture = 'Lying' if lying else 'Not lying'
    else:
        track['streak'] = 0
        behavioral, posture, temp_c = 'No data', 'Unknown', None
    return {'posture': posture, 'lying_s': track['streak'] * interval_s, 'surface_temp_c': temp_c,
            'behavioral_status': behavioral, 'thermal_status': thermal_status(temp_c, threshold_c) if observed else 'No result'}


def evaluate(tracks, final_seq, interval_s, rule_n, threshold_c, lookback):
    """End-of-session result per pig. `tracks` maps pig id (str or int) -> track dict."""
    pigs = []
    for pid, t in sorted(tracks.items(), key=lambda kv: int(kv[0])):
        recent = t['last_temp_seq'] is not None and t['last_temp_seq'] > final_seq - lookback
        final = t['last_temp'] if recent else None
        abnormal = t['max_streak'] >= rule_n
        elevated = final is not None and final >= threshold_c
        if abnormal and elevated:
            reason = 'Prolonged lying and elevated final temperature'
        elif not abnormal:
            reason = 'Lying bout below the rule'
        elif final is None:
            reason = 'No recent valid temperature'
        else:
            reason = 'Final temperature below the threshold'
        pigs.append({'pig_id': int(pid), 'longest_lying_s': t['max_streak'] * interval_s,
                     'longest_lying_captures': t['max_streak'], 'lying_captures': t['lying_captures'],
                     'final_temp_c': final, 'final_temp_seq': t['last_temp_seq'] if recent else None,
                     'behavioral_status': 'Abnormal' if abnormal else 'Normal',
                     'thermal_status': thermal_status(final, threshold_c),
                     'alert': bool(abnormal and elevated), 'reason': reason})
    flagged = [p['pig_id'] for p in pigs if p['alert']]
    return {'pigs': pigs, 'flagged': flagged, 'any_alert': bool(flagged), 'final_seq': final_seq,
            'rule': {'threshold_c': threshold_c, 'consecutive_captures': rule_n, 'lookback_captures': lookback,
                     'interval_s': interval_s}}


def fmt_hm(seconds):
    if seconds < 60:  # short test schedules
        return f'{int(round(seconds))}s'
    m = int(round(seconds / 60))
    h, m = divmod(m, 60)
    return f'{h}h{m:02d}m' if h else f'{m}m'


def septets(text):
    return sum(2 if c in '^{}\\[~]|' else 1 for c in text)


def build_sms(pen, result, ended_epoch, duration_s, test=False, limit=160):
    """One SMS (at most `limit` GSM septets) listing flagged pigs, hottest first."""
    pen = ''.join(c for c in str(pen) if c.isalnum() or c in '-_')[:32] or 'PEN'
    flagged = sorted((p for p in result['pigs'] if p['alert']), key=lambda p: -(p['final_temp_c'] or 0))
    head = ('[TEST] ' if test else '') + f'PIG ALERT {pen}'
    lines = [f"Pig {p['pig_id']}: {p['final_temp_c']:.1f}C, lying {fmt_hm(p['longest_lying_s'])}" for p in flagged]
    when = datetime.fromtimestamp(ended_epoch).strftime('%d %b %H:%M')
    tail = [f'{fmt_hm(duration_s)} session ended {when}', 'Screening only, not a diagnosis']
    shown = len(lines)
    while True:
        more = [f'+{len(lines) - shown} more pigs'] if shown < len(lines) else []
        text = '\n'.join([head] + lines[:shown] + more + tail)
        if septets(text) <= limit:
            return text
        if shown > 1:
            shown -= 1
        elif len(tail) > 1:
            tail = tail[:1]
        else:
            return text[:limit]
