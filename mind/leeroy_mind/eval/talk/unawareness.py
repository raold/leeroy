"""Narrow assertions contradicted by earlier positive, time-qualified evidence.

Missing raid rows, current roster membership and free-text facts are not a
complete historical world ledger. They must never be treated as negative proof.

`load_facts` only reads (safe on a live read connection) and returns a `Facts`
object a caller keeps and reuses; `check_line` is pure over it; `detect` is the
batch loop. Known limits: the full-sentence patterns fire rarely (near-zero recall on
real history), and kills are matched by boss name only, so a claim about a heroic or
other difficulty ("never killed heroic X") is not distinguished from a normal-mode kill.
"""
from collections import defaultdict
from dataclasses import dataclass, field
import json
import re

from leeroy_mind.text import strip_wow_markup
from leeroy_mind.wow import BOSS_NAMES

from .common import Flag, day

POSITION_MAX_AGE_SECONDS = 300
_NEVER_KILLED = re.compile(r"we (?:have )?never (?:killed|defeated) (.+)", re.I)
_NO_RAID = re.compile(r"we (?:did not|didn't) raid on (\d{4}-\d{2}-\d{2})", re.I)
_LOCATION = re.compile(r"(?:i am|i'm) in (.+)", re.I)


@dataclass
class Facts:
    kills: dict
    raids: dict
    zones: dict
    observations: dict
    counts: dict = field(default_factory=dict)


def load_facts(conn):
    raw_guild = conn.execute("SELECT value FROM meta WHERE key='guild_id'").fetchone()
    guild = int(raw_guild[0]) if raw_guild and str(raw_guild[0]).isdigit() else 0
    kills = defaultdict(list)
    for row in conn.execute(
            "SELECT k.map,k.boss_id,k.published_ms/1000.0,k.source FROM realm_kill k "
            "JOIN realm_fact_origin o ON o.fact=k.fact WHERE k.guild=? AND o.state='active' "
            "AND o.assurance='verified_event' AND o.policy='verified_raid_kill_v1'", (guild,)):
        name = BOSS_NAMES.get((row[0], row[1]))
        if name and guild:
            kills[name.casefold()].append((row[2], row[3]))
    raids = defaultdict(list)
    for row in conn.execute(
            'SELECT raid_id,started,ended FROM guild_raid WHERE ended>started AND (kills>0 OR wipes>0)'):
        raids[day(row[1])].append((row[2], row[0]))
    zones, observations = defaultdict(list), defaultdict(list)
    malformed = 0
    for row in conn.execute("SELECT id,ts,guid,payload FROM game_event WHERE type='zone' ORDER BY ts,id"):
        try:
            payload = json.loads(row[3])
            zone, name = payload['zone_id'], payload['zone_name']
            if type(zone) is not int or zone <= 0 or not isinstance(name, str) or not name.strip():
                raise ValueError('invalid_zone')
            zones[strip_wow_markup(name).casefold()].append((row[1], zone))
            observations[row[2]].append((row[1], zone, row[0]))
        except (ValueError, KeyError, TypeError):
            malformed += 1
    counts = dict(kill_claims_checked=0, raid_denials_checked=0, position_claims_checked=0,
                  verified_kill_facts=sum(map(len, kills.values())), combat_raid_records=sum(map(len, raids.values())),
                  zone_observations=sum(map(len, observations.values())), malformed_zone_events=malformed,
                  membership_history_checks=0, absent_raid_checks=0,
                  position_max_age_seconds=POSITION_MAX_AGE_SECONDS)
    return Facts(kills, raids, zones, observations, counts)


def check_line(facts, line, counts=None):
    """Flags for one outgoing line; pure over preloaded facts, never reads a table.

    Pass `counts` to accumulate coverage; by default a throwaway counter is used.
    """
    flags = []
    kills, raids, zones, observations = facts.kills, facts.raids, facts.zones, facts.observations
    counts = defaultdict(int) if counts is None else counts
    # Full-sentence forms only: no questions, quoted/reported speech or
    # hypotheses. A respawn claim is not the same as denying any previous kill.
    text = strip_wow_markup(line.text).strip().rstrip('.!').strip()
    if match := _NEVER_KILLED.fullmatch(text):
        evidence = [(ts, key) for ts, key in kills.get(match[1].casefold(), ()) if ts <= line.ts]
        counts['kill_claims_checked'] += 1
        if evidence:
            flags.append(Flag(line.id, line.guid, 'never_killed_after_verified_kill', min(evidence)[1]))
    if match := _NO_RAID.fullmatch(text):
        evidence = [(ts, key) for ts, key in raids.get(match[1], ()) if ts <= line.ts]
        counts['raid_denials_checked'] += 1
        if evidence:
            flags.append(Flag(line.id, line.guid, 'raid_denial_after_recorded_combat', min(evidence)[1]))
    if match := _LOCATION.fullmatch(text):
        targets = {zone for ts, zone in zones.get(match[1].casefold(), ()) if ts <= line.ts}
        if len(targets) != 1:
            return flags
        actual, evidence_id = line.zone, line.id
        if not actual:
            known = [entry for entry in observations.get(line.guid, ())
                     if 0 <= line.ts - entry[0] <= POSITION_MAX_AGE_SECONDS]
            if known:
                _, actual, evidence_id = known[-1]
        if actual:
            counts['position_claims_checked'] += 1
            if actual != next(iter(targets)):
                flags.append(Flag(line.id, line.guid, 'location_mismatch', evidence_id))
    return flags


def detect(conn, lines):
    facts = load_facts(conn)
    flags = []
    for line in lines:
        flags.extend(check_line(facts, line, facts.counts))
    return flags, facts.counts
