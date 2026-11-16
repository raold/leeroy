"""Server seeds under [talk] recall_diversity (server/seeds.py `varied`): a topic seed whose subject
began a conversation within a week is passed over while any other seed is available, and the
officers' weekly attendance thread is the only attendance seed, at most once a week guild-wide.
Receipts hold hashed words, never a fact's text. Invented seeds for the test cast only."""
import asyncio
from dataclasses import replace
import json
import random

import pytest

from conftest import run
from selfies.fakes import mind_app
from leeroy_mind.discord import store as dstore
from leeroy_mind.memory.diversity import name_words
from leeroy_mind.server import engine as se, records, runtime, seeds
from leeroy_mind.server.seeds import Seed

DAY = 86400.0
BREAD = Seed('purpose:food:2026-11-16', 'purpose', ('food',), 'Baking rye bread in the kitchen')
BREAD_AGAIN = Seed('purpose:food:2026-11-17', 'purpose', ('food',), 'Baking rye bread in the kitchen')
KNIT = Seed('life:7', 'life', ('irl',), 'Knitting a scarf for the winter market', (101,))
ATTEND = Seed('officer:attendance:2026-W47', 'officer', ('officer-chat',),
              'Attendance this week: Arkon missed 3 or more of the last 4 raids', (101,), True)
BENCHED = Seed('life:9', 'life', ('irl',), 'Got benched again for the frost run', (103,))
PURPOSE_BENCH = Seed('purpose:raid-planning:2026-11-16', 'purpose', ('raid-planning',),
                     'Raid lineups, bench and signups for the next raid nights.')
LINEUP = Seed('decision:4:settled', 'decision', ('raid-planning',),
              'Lineup settled: 10 in, 2 on the bench', (101,))
RAID = Seed('raid_after:5', 'raid_after', ('general',), 'Last raid: a test raid 10, 3 kills, 1 wipe')


def test_attendance_seeds():
    assert seeds.attendance(ATTEND) and seeds.attendance(BENCHED)
    assert seeds.attendance(replace(KNIT, fact='Sat out the whole raid again'))
    assert not any(seeds.attendance(s) for s in (PURPOSE_BENCH, LINEUP, RAID, KNIT, BREAD))


def test_only_the_weekly_attendance_thread_and_once_a_week(clock):
    now = clock.now()
    every = [ATTEND, BENCHED, PURPOSE_BENCH, LINEUP, KNIT]
    kept = seeds.varied(every, subjects=[], attendance_at=None, now=now)
    assert kept == [ATTEND, PURPOSE_BENCH, LINEUP, KNIT]
    # one began six days ago, whatever its week: none this week
    assert ATTEND not in seeds.varied(every, subjects=[], attendance_at=now - 6 * DAY, now=now)
    assert ATTEND in seeds.varied(every, subjects=[], attendance_at=now - 7 * DAY, now=now)


def test_a_spent_topic_is_passed_over_while_anything_else_is_available(clock):
    now = clock.now()
    done = [seeds.subject_words(BREAD)]
    assert seeds.varied([BREAD_AGAIN, KNIT], subjects=done, attendance_at=None, now=now) == [KNIT]
    assert seeds.varied([BREAD_AGAIN, RAID], subjects=done, attendance_at=None, now=now) == [RAID]
    # nothing else: it stays
    assert seeds.varied([BREAD_AGAIN], subjects=done, attendance_at=None, now=now) == [BREAD_AGAIN]
    # an event seed is never spent by its subject (each raid is a new one)
    raided = [seeds.subject_words(RAID)]
    assert seeds.varied([RAID, KNIT], subjects=raided, attendance_at=None, now=now) == [RAID, KNIT]


def test_names_are_no_subject():
    names = name_words(['Arkon', 'Belra'])
    a = Seed('officer:trial:101:2026-W47', 'officer', ('applications',), 'Arkon is a trial member')
    b = Seed('officer:trial:102:2026-W47', 'officer', ('applications',), 'Belra is a trial member')
    assert seeds.subject_words(a, names) == seeds.subject_words(b, names)
    assert seeds.subject_words(a) != seeds.subject_words(b)


def test_receipts_hold_hashed_words_and_age_out(store, clock):
    now = clock.now()
    seeds.note_subject(store, BREAD, now=now - 8 * DAY)
    seeds.note_subject(store, KNIT, now=now - DAY)
    seeds.note_subject(store, RAID, now=now)  # no receipt for an event seed
    rows = dict(store.conn.execute("SELECT key,value FROM meta WHERE key GLOB 'server:subject:*'"))
    assert set(rows) == {'server:subject:' + BREAD.key, 'server:subject:' + KNIT.key}
    assert all('bread' not in v and 'scarf' not in v and 'Knitting' not in v for v in rows.values())
    assert seeds.recent_subjects(store.conn, now=now) == [seeds.subject_words(KNIT)]
    store.set_meta('server:subject:broken', 'not json')
    records.prune(store, clock.local().date().isoformat(), now)
    assert [k for (k,) in store.conn.execute("SELECT key FROM meta WHERE key GLOB 'server:subject:*'")] == [
        'server:subject:' + KNIT.key]


def test_last_attendance_reads_the_weekly_and_older_receipts(store, clock):
    now = clock.now()
    assert seeds.last_attendance(store.conn) is None
    seeds.mark_used(store, ATTEND, now=now - 3 * DAY)
    store.set_meta('server:seed:officer:attendance:5:101:2026-W46', str(now - 2 * DAY))
    seeds.mark_used(store, KNIT, now=now)
    assert seeds.last_attendance(store.conn) == now - 2 * DAY


# -- the engine ---------------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path, monkeypatch):
    app = mind_app(tmp_path, toml='[server]\nenabled=true\n[guards]\nenabled=true\n')
    app.server.lease._held = True
    app.engine._login_at = float('-inf')
    app.world.human_online = False
    app.server.engine.rng = random.Random(19)
    observed = run(runtime.snapshot(app, app.clock.now()))
    observed.seeds = [BREAD, KNIT]

    async def snapshot(*args, **kwargs):
        await asyncio.sleep(0)
        return observed
    monkeypatch.setattr(runtime, 'snapshot', snapshot)
    monkeypatch.setattr(se.rhythm, 'length', lambda rng: 2)
    # the first seed offered and its first channel, so only `varied` decides what is left
    monkeypatch.setattr(se.rhythm, 'choose_seed_and_channel',
                        lambda seeds, **kw: (seeds[0], seeds[0].channels[0]) if seeds else None)
    monkeypatch.setattr(se, 'answer_delay', lambda state, rng: 0)
    app.server.engine.rng.random = lambda: .5
    app.observed, app.turns = observed, []

    async def speak(turn, meta):
        app.turns.append((turn, meta))
        meta.turn_id = 'test-' + str(len(app.turns))
        with app.store.transaction():
            message = dstore.post(app.store, channel=turn.channel.removeprefix('discord:'),
                                  author=turn.speaker, origin='mind', text='An invented thought',
                                  turn=meta.turn_id, reply_to=meta.discord.reply_to,
                                  now=app.clock.now())
            records.posted(app.store, app.clock.local().date().isoformat(), app.clock.now(),
                           conversation=meta.server_conversation)
        meta.discord_messages.append(message)
        return ['An invented thought']
    monkeypatch.setattr(app.engine, 'speak_meta', speak)
    yield app
    run(app.stop())


def started(app, on):
    """The seed kinds of two conversations begun one after the other."""
    app.engine.cfg = replace(app.engine.cfg, talk=replace(app.engine.cfg.talk, recall_diversity=on))
    engine, kinds = app.server.engine, []

    async def go():
        for _ in range(2):
            engine.next_start = 0
            app.observed.seeds = [BREAD] if not kinds else [BREAD_AGAIN, KNIT]
            await engine.tick(app.clock.now())
            assert len(engine.live()) == 1
            kinds.append(engine.live()[0]['seed'])
            for _ in range(200):
                if not engine.live():
                    break
                await asyncio.sleep(0)
    run(go())
    return kinds


def test_the_engine_passes_over_a_subject_it_just_talked_about(app):
    assert started(app, True) == ['purpose', 'life']
    assert app.store.get_meta('server:subject:' + BREAD.key) is not None


def test_off_the_engine_keeps_no_subject_receipt(app):
    assert started(app, False) == ['purpose', 'purpose']
    assert app.store.conn.execute("SELECT count(*) FROM meta WHERE key GLOB 'server:subject:*'").fetchone()[0] == 0
