"""Deterministic private moments, grounded in delivered game evidence, once per night."""
from dataclasses import replace
from datetime import datetime
import json

from leeroy_mind.nightly.attraction import reflection_row
from leeroy_mind.tuning.effective import dials_of
from .policy import eligible


async def stage_bond(ctx):
    if not ctx.settings.romance_activities or ctx.done('bond', 'all'):
        return
    now, human = ctx.clock.now(), ctx.human
    lo, hi = ctx.window

    def write(store):
        conn = store.conn
        for guid in sorted(ctx.attitudes(fresh=True)):
            persona = ctx.persona(guid)
            if not eligible(persona, ctx.settings.romance_dial):
                continue
            moments = []
            # First recorded shared boss kill, not just the first one in this night.
            first = conn.execute("SELECT id,ts FROM game_event WHERE type='boss' AND ts<=? "
                "AND json_extract(payload,'$.state')='killed' "
                "AND EXISTS (SELECT 1 FROM json_each(payload,'$.members') WHERE value=?) "
                "AND EXISTS (SELECT 1 FROM json_each(payload,'$.members') WHERE value=?) "
                "ORDER BY ts,id LIMIT 1", (hi, human, guid)).fetchone()
            if first and lo < first['ts'] <= hi:
                moments.append(('first_kill', 'first', first['ts'], (f"event:{first['id']}",),
                                'Our first recorded boss kill together.'))
            lines = list(conn.execute("SELECT id,ts,from_guid FROM chat_line WHERE channel='whisper' "
                "AND ts>? AND ts<=? AND ((from_guid=? AND to_guid=?) OR (from_guid=? AND to_guid=?)) "
                "ORDER BY ts,id", (lo, hi, human, guid, guid, human)))
            late = [r for r in lines if datetime.fromtimestamp(r['ts'], ctx.tz).hour >= 23
                    or datetime.fromtimestamp(r['ts'], ctx.tz).hour < 6]
            pair = next(((a, b) for a, b in zip(late, late[1:])
                         if a['from_guid'] != b['from_guid'] and b['ts']-a['ts'] <= 600), None)
            if pair:
                moments.append(('late_whisper', ctx.night, pair[1]['ts'],
                    tuple(f"chat:{r['id']}" for r in pair), 'We traded private whispers late that night.'))
            # Receiving the attached item is evidence; composing or sending mail is not.
            gifts = conn.execute("SELECT m.id,m.taken,e.id AS event_id FROM realm_mail m "
                "JOIN game_event e ON e.type='mail_taken' "
                "AND json_extract(e.payload,'$.item_guid')=m.item_guid "
                "WHERE m.sender=? AND m.state='taken' AND m.taken>? AND m.taken<=? "
                "AND e.ts BETWEEN m.taken-2 AND m.taken+2 ORDER BY m.id,e.id LIMIT 10",
                (guid, lo, hi)).fetchall()
            for gift in gifts:
                moments.append(('gift', str(gift['id']), gift['taken'],
                    (f"event:{gift['event_id']}",), 'He collected the gift I sent him.'))
            for kind, source, ts, refs, text in moments:
                if conn.execute('SELECT 1 FROM bond_moment WHERE guid=? AND human=? AND kind=? AND source=?',
                                (guid, human, kind, source)).fetchone():
                    continue
                row = replace(reflection_row(guid, text, now=now, actors=[guid, human],
                    evidence=refs, key=f'bond:{human}:{kind}:{source}', importance=7,
                    valence=.2 if kind == 'late_whisper' else .6),
                    t_event=int(ts), scope='private', scope_with=human)
                mid = ctx.writer.put(row)
                conn.execute('INSERT INTO bond_moment VALUES (?,?,?,?,?,?)', (guid, human, kind, source, ts, mid))
            last = conn.execute('SELECT max(coalesce(ended,?)) FROM session WHERE started<=?', (now,now)).fetchone()[0]
            rivalry = conn.execute("SELECT level,evidence FROM jealousy WHERE holder=? AND kind='rivalry' "
                'AND updated>? AND updated<=? ORDER BY level DESC LIMIT 1', (guid, lo, now)).fetchone()
            cap = min(1., ctx.settings.jealousy / 10 * dials_of(persona).mult('jealousy'))
            mood, strength = 'quiet', 0.
            if last is not None and now-last >= 3*86400 and not lines:
                mood, strength = 'missing', min(.6, (now-last)/(14*86400))
            elif rivalry and rivalry['level'] > 0 and json.loads(rivalry['evidence'] or '[]') and cap > 0:
                mood, strength = 'jealous', min(cap, rivalry['level']/100)
            elif (any(m[0] in {'first_kill','gift'} for m in moments) or conn.execute(
                "SELECT 1 FROM memory m JOIN memory_actor a ON a.memory_id=m.id WHERE m.owner=? AND a.guid=? "
                "AND m.t_event>? AND m.t_event<=? AND m.valence>=.5 AND m.status='active' "
                "AND m.source NOT IN ('hearsay','offscreen') AND m.evidence!='[]' LIMIT 1",
                (guid,human,lo,hi)).fetchone()) and store.get_relationship(guid, human).affinity > 0:
                mood, strength = 'playful', min(.6, ctx.settings.romance_dial/10)
            conn.execute('INSERT INTO bond_mood VALUES (?,?,?,?,?,?,?) ON CONFLICT(guid,human) DO UPDATE SET '
                'night=excluded.night,mood=excluded.mood,strength=excluded.strength,updated=excluded.updated,expires=excluded.expires',
                (guid, human, ctx.night, mood, strength, now, now+48*3600))
    ctx.commit('bond', 'all', write)
