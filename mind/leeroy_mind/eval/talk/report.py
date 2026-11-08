"""Rates use distinct flagged lines; repeated rules never inflate the numerator."""
from collections import defaultdict
from dataclasses import asdict
import math

from .common import day

VERSION = 1
DETECTORS = ('contradiction', 'repetition', 'nonsequitur', 'unawareness')
TOP_COUNT = 10


def rate(count, total):
    return round(100 * count / total, 6) if total else 0.0


def summary(lines, flags):
    ids = {line.id for line in lines}
    flagged = {flag.line_id for flag in flags if flag.line_id in ids}
    return dict(lines=len(ids), flagged_lines=len(flagged), per_100_lines=rate(len(flagged), len(ids)))


def build(lines, results, *, since, until, days, fake, judged, embedding_fingerprint):
    members, dates = defaultdict(list), defaultdict(list)
    for line in lines:
        members[line.guid].append(line)
        dates[day(line.ts)].append(line)
    all_flags = [flag for flags, _ in results.values() for flag in flags]
    report = dict(version=VERSION, fake=fake, judged=judged, since_days=days,
                  window=dict(since=since, until=until), embedding_fingerprint=embedding_fingerprint,
                  overall=summary(lines, all_flags), members=len(members), detectors={},
                  per_member={str(guid): summary(own, all_flags) for guid, own in sorted(members.items())},
                  by_day={date: summary(own, all_flags) for date, own in sorted(dates.items())})
    for detector in DETECTORS:
        flags, coverage = results[detector]
        unique = sorted(set(flags), key=lambda flag: (flag.line_id, flag.rule, flag.related_id or 0))
        by_rule = defaultdict(set)
        for flag in unique:
            by_rule[flag.rule].add(flag.line_id)
        report['detectors'][detector] = dict(
            **summary(lines, unique), coverage=coverage, hits=[asdict(flag) for flag in unique],
            rules={key: dict(flagged_lines=len(ids), per_100_lines=rate(len(ids), len(lines)))
                   for key, ids in sorted(by_rule.items())},
            per_member={str(guid): summary(own, unique) for guid, own in sorted(members.items())},
            by_day={date: summary(own, unique) for date, own in sorted(dates.items())})
    report['top_offenders'] = [dict(guid=int(guid), **stats) for guid, stats in sorted(
        report['per_member'].items(), key=lambda item: (-item[1]['flagged_lines'],
                                                       -item[1]['per_100_lines'], int(item[0])))[:TOP_COUNT]
        if stats['flagged_lines']]
    return report


def compare(report, baseline):
    for key in ('version', 'fake', 'judged', 'since_days', 'embedding_fingerprint', 'parameters'):
        if baseline.get(key) != report[key]:
            raise ValueError('baseline_incompatible')
    changes = {}
    for detector in DETECTORS:
        old = baseline['detectors'][detector]
        current = report['detectors'][detector]
        value = old['per_100_lines']
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('baseline_invalid_rate')
        changes[detector] = dict(before=value, after=current['per_100_lines'],
                                delta_per_100_lines=round(current['per_100_lines'] - value, 6))
    return changes


def print_summary(report):
    print(f"talk_eval lines={report['overall']['lines']} members={report['members']} fake={int(report['fake'])}")
    for detector in DETECTORS:
        stats = report['detectors'][detector]
        print(f"{detector} flagged_lines={stats['flagged_lines']} per_100_lines={stats['per_100_lines']:.6f}")
    for row in report['top_offenders']:
        print(f"member guid={row['guid']} lines={row['lines']} flagged_lines={row['flagged_lines']} per_100_lines={row['per_100_lines']:.6f}")
    for date, row in report['by_day'].items():
        print(f"day date={date} lines={row['lines']} flagged_lines={row['flagged_lines']} per_100_lines={row['per_100_lines']:.6f}")
    for detector, change in report.get('baseline_changes', {}).items():
        print(f"change detector={detector} delta_per_100_lines={change['delta_per_100_lines']:.6f}")
