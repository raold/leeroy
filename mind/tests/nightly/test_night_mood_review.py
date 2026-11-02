"""Independent checks of failed-night replay and remembered privacy behind sent chat."""
import json

import pytest

from conftest import run
from leeroy_mind.nightly import mood
from leeroy_mind.nightly import knowledge


def emotion(h, refs):
    return h.store.conn.execute(
        "INSERT INTO emotion(guid,night,type,target,intensity,why,source,evidence,ts) "
        "VALUES (101,?,'anger',103,3,'The private news still bothers me.','reflect',?,?)",
        (h.night, json.dumps(refs), h.clock.now())).lastrowid


def test_failed_emotional_write_retries_once_without_duplicate_guild_feelings(h, monkeypatch):
    event = h.boss(h.T, 631, 12, "killed", (101, 102))
    emotion(h, [f"event:{event}"])
    ctx = h.ctx()
    put = ctx.writer.put

    def fail_after_write(row, **kwargs):
        put(row, **kwargs)
        raise RuntimeError("independent simulated commit failure")

    monkeypatch.setattr(ctx.writer, "put", fail_after_write)
    run(mood.stage_mood(ctx))
    assert not ctx.done("mood", "all")
    assert h.count("emotion") == 1
    assert h.count("guild_mood") == h.count("pad_mood") == 0
    assert h.store.keyed_memory(101, "emotion:103") is None

    monkeypatch.setattr(ctx.writer, "put", put)
    run(mood.stage_mood(ctx))
    assert ctx.done("mood", "all")
    assert h.count("emotion") == 1 + len(ctx.members(include_away=True))
    assert h.count("guild_mood") == 1
    first = h.store.keyed_memory(101, "emotion:103")
    pad = [tuple(row) for row in h.store.conn.execute("SELECT * FROM pad_mood ORDER BY guid")]
    feelings = [tuple(row) for row in h.store.conn.execute("SELECT * FROM emotion ORDER BY id")]
    h.clock.advance(86400)
    run(mood.stage_mood(h.ctx()))
    assert h.store.keyed_memory(101, "emotion:103") == first
    assert [tuple(row) for row in h.store.conn.execute("SELECT * FROM pad_mood ORDER BY guid")] == pad
    assert [tuple(row) for row in h.store.conn.execute("SELECT * FROM emotion ORDER BY id")] == feelings


@pytest.mark.parametrize("scope", ["private", "secret"])
def test_emotion_does_not_launder_recalled_private_context_through_public_chat(h, scope):
    words = "Keep this between us, my sister is ill." if scope == "secret" else "My sister is ill."
    whisper = h.say(h.T, 1, words, channel="whisper", to=101)
    memory = h.mem(101, "Private family news.", scope=scope, scope_with=1,
                   evidence=(f"chat:{whisper}",))
    public = h.say(h.T + 1, 101, "That has been on my mind.")
    utterance = h.store.conn.execute(
        "SELECT id FROM utterance WHERE guid=101 AND ts=? ORDER BY id DESC LIMIT 1", (h.T + 1,)
    ).fetchone()[0]
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE id=?",
                         (json.dumps([memory]), utterance))
    emotion(h, [f"chat:{public}"])
    ctx = h.ctx()
    run(mood.stage_mood(ctx))
    assert ctx.done("mood", "all")
    result = h.store.keyed_memory(101, "emotion:103")
    assert result is None or (result.scope == scope and result.scope_with in (0, 1))


def probe(h, *, age=0, n=36, rate=0):
    h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate) VALUES (?,?,?,0,?)",
                         (h.clock.now() - age * 86400, h.LIVE, n, rate))


@pytest.mark.parametrize("subject,allowed", [(101, True), (102, False), (None, False)])
def test_new_secret_emotion_without_probe_requires_proved_self_subject(h, subject, allowed):
    encounter = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,private) "
        "VALUES (?,0,101,103,'camp','done',1)", (h.night,)).lastrowid
    ref = f"encounter:{encounter}"
    if subject is not None:
        knowledge.add_fact(h.store, kind="confession", root=ref, about=subject,
                           text="A personal confidence.", secret=1, now=h.T)
    emotion(h, [ref])
    ctx = h.ctx()
    run(mood.stage_mood(ctx))
    assert ctx.done("mood", "all")
    assert (h.store.keyed_memory(101, "emotion:103") is not None) is allowed
    assert (h.store.get_meta("nightly:emotion-pending:101:103") is None) is allowed


@pytest.mark.parametrize("age,n,rate", [(36, 36, 0), (0, 35, 0), (0, 36, 0.11)])
def test_secret_copy_rejects_stale_insufficient_or_failed_probe(h, age, n, rate):
    source = h.mem(101, "A held confidence.", scope="secret", scope_with=103, evidence=())
    probe(h, age=age, n=n, rate=rate)
    ctx = h.ctx()
    assert not mood.secret_memory_ok(ctx, 101, [f"memory:{source}"])
    emotion(h, [f"memory:{source}"])
    run(mood.stage_mood(ctx))
    assert h.store.keyed_memory(101, "emotion:103") is None


def test_pending_secret_emotion_writes_next_night_after_probe_without_replaying_mood(h):
    whisper = h.say(h.T, h.human, "Keep this between us: my sister is sick.", channel="whisper", to=101)
    emotion(h, [f"chat:{whisper}"])
    ctx = h.ctx()
    run(mood.stage_mood(ctx))
    assert ctx.done("mood", "all") and h.store.keyed_memory(101, "emotion:103") is None
    assert h.store.get_meta("nightly:emotion-pending:101:103") is not None
    original_emotions = h.count("emotion")
    h.clock.advance(86400)
    probe(h)
    later = h.ctx(night="2026-10-30")
    run(mood.stage_mood(later))
    memory = h.store.keyed_memory(101, "emotion:103")
    assert memory is not None and (memory.scope, memory.scope_with) == ("secret", h.human)
    assert h.store.get_meta("nightly:emotion-pending:101:103") is None
    assert h.store.get_meta("nightly:emotion-night:101:103") == h.night
    assert h.count("emotion") == original_emotions
    run(mood.stage_mood(later))
    assert h.store.keyed_memory(101, "emotion:103") == memory


def test_old_pending_secret_emotion_expires_without_copy_when_probe_eventually_passes(h):
    source = h.mem(101, "A held confidence.", scope="secret", scope_with=103, evidence=())
    emotion(h, [f"memory:{source}"])
    run(mood.stage_mood(h.ctx()))
    assert h.store.get_meta("nightly:emotion-pending:101:103") is not None
    h.clock.advance(7 * 86400)
    probe(h)
    run(mood.stage_mood(h.ctx(night="2026-11-05")))
    assert h.store.keyed_memory(101, "emotion:103") is None
    assert h.store.get_meta("nightly:emotion-pending:101:103") is None
