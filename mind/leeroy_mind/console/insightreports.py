"""Bounded projections of private talk-audit reports; never return excerpts or paths."""
import json
import math
import os
import stat
import threading
import time
from collections import OrderedDict

DETECTORS = ('contradiction', 'repetition', 'nonsequitur', 'unawareness')
MAX_FILES, MAX_BYTES, MAX_RUNS = 256, 4 * 1024 * 1024, 8
COMMAND = 'python -m leeroy_mind eval talk --data-dir D --since 14 --fake --json'
_CACHE = OrderedDict()
_LOCK = threading.Lock()


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _summary(value):
    if not isinstance(value, dict):
        raise ValueError('summary')
    keys = ('lines', 'flagged_lines', 'per_100_lines')
    if any(not number(value.get(k)) for k in keys):
        raise ValueError('summary')
    if value['flagged_lines'] > value['lines'] or value['per_100_lines'] > 100:
        raise ValueError('summary')
    return {k: value[k] for k in keys}


def _project(value):
    if value.get('version') != 1 or type(value.get('fake')) is not bool or type(value.get('judged')) is not bool:
        raise ValueError('version')
    until = value['window']['until']
    if not number(until):
        raise ValueError('window')
    members = value['per_member']
    if not isinstance(members, dict) or len(members) > 10000:
        raise ValueError('members')
    out = {}
    for guid, summary in members.items():
        if not guid.isascii() or not guid.isdigit() or not 0 < int(guid) <= 0xFFFFFFFF:
            raise ValueError('guid')
        out[guid] = {'overall': _summary(summary), 'rates': {}}
        for name in DETECTORS:
            item = value['detectors'][name]['per_member'].get(guid)
            out[guid]['rates'][name] = _summary(item) if item is not None else None
    # Match the audit's comparable run settings; these stay private, never in the API.
    signature = json.dumps({k: value.get(k) for k in ('version', 'fake', 'judged', 'since_days',
                            'embedding_fingerprint', 'parameters')}, sort_keys=True)
    return {'until': until, 'fake': value['fake'], 'judged': value['judged'],
            'members': out, 'signature': signature}


def reports(data_dir, *, now):
    # A roster and its member poll share one bounded scan for thirty seconds.
    # Bounded to eight data roots; nothing is written and no request text enters here.
    key = (str(data_dir.resolve()), int(now // 30))
    with _LOCK:
        if key not in _CACHE:
            _CACHE[key] = _read_reports(data_dir, now=now)
            while len(_CACHE) > 8:
                _CACHE.popitem(last=False)
        return _CACHE[key]


def _read_reports(data_dir, *, now):
    folder = data_dir / 'scratch'
    out, skipped, capped = [], 0, False
    try:
        if folder.is_symlink() or folder.is_junction():
            return [], 'Talk reports are unavailable: the scratch directory is a link.'
        with os.scandir(folder) as entries:
            candidates = []
            for index, entry in enumerate(entries):
                if index >= MAX_FILES:
                    capped = True
                    break
                if not entry.name.endswith('.json') or entry.is_symlink():
                    continue
                info = entry.stat(follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES
                        or getattr(info, 'st_file_attributes', 0) & 0x400):
                    continue
                candidates.append((info.st_mtime, entry.path))
        consumed, deadline = 0, time.monotonic() + 1.0
        for _, path in sorted(candidates, reverse=True):
            if consumed >= 16 * 1024 * 1024 or time.monotonic() > deadline:
                return [], 'Talk reports exceed the read budget; archive older files first.'
            try:
                with open(path, 'rb') as stream:
                    raw = stream.read(MAX_BYTES + 1)
                consumed += len(raw)
                if len(raw) > MAX_BYTES:
                    raise ValueError('size')
                report = _project(json.loads(raw))
                if report['until'] > now + 300:
                    raise ValueError('future')
                out.append(report)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                skipped += 1
    except FileNotFoundError:
        return [], 'No talk-quality report yet.'
    except OSError:
        return [], 'Talk reports could not be read.'
    # An incomplete directory scan cannot honestly identify the latest report.
    if capped:
        return [], 'Too many scratch files to identify the latest report; archive old files first.'
    out.sort(key=lambda r: r['until'], reverse=True)
    why = 'Some report files were unreadable or incompatible.' if skipped else None
    return out, why


def quality(runs, guid, *, now, why=None):
    if not runs:
        return {'state': 'missing', 'why': why or 'No talk-quality report yet.', 'command': COMMAND}
    latest = runs[0]
    member = latest['members'].get(str(guid))
    if member is None:
        return {'state': 'missing', 'why': 'The latest talk report has no lines for this member.'}
    ordered = sorted(latest['members'].items(), key=lambda item: (
        -item[1]['overall']['flagged_lines'], -item[1]['overall']['per_100_lines'], int(item[0])))
    trend = [{'until': r['until'], **r['members'][str(guid)]} for r in runs
             if r['signature'] == latest['signature'] and str(guid) in r['members']][:MAX_RUNS]
    return {'state': 'stale' if now - latest['until'] > 7 * 86400 else 'ok',
            'why': 'Report is more than seven days old.' if now - latest['until'] > 7 * 86400 else why,
            'until': latest['until'], 'fake': latest['fake'], 'judged': latest['judged'],
            **member, 'trend': trend, 'rank': next(i + 1 for i, (g, _) in enumerate(ordered) if g == str(guid)),
            'guild_members': len(ordered)}


def repetition_badges(runs, *, now):
    if not runs or now - runs[0]['until'] > 7 * 86400:
        return {}
    eligible = {g: m['rates']['repetition'] for g, m in runs[0]['members'].items()
                if m['rates']['repetition'] and m['rates']['repetition']['lines'] > 0}
    rates = sorted(m['per_100_lines'] for m in eligible.values())
    if not rates:
        return {}
    threshold = rates[math.ceil(.9 * len(rates)) - 1]
    return {g: int(m['flagged_lines']) for g, m in eligible.items() if m['per_100_lines'] > threshold}
