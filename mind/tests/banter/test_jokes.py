from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.banter.night import reward_jokes, stage_jokes
from leeroy_mind.banter.topics import jokes


def reward(h,line,*,value=1,cap=1,funny=1,thumb=1):
    c=h.store.conn
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='talk_reward'").fetchone():
        # Task 30 is optional on this base; these are its read-only columns used by banter.
        c.execute("CREATE TABLE talk_reward(line_id INTEGER PRIMARY KEY,value REAL,cap_weight REAL)")
    columns={r[1] for r in c.execute("PRAGMA table_info(talk_reward)")}
    if "weights_version" in columns:
        c.execute("INSERT INTO learn_weights(created,weights) VALUES(?, '{}')",(h.T,))
        version=c.execute("SELECT max(version) FROM learn_weights").fetchone()[0]
        c.execute("INSERT INTO talk_reward(line_id,guid,value,cap_weight,parts,weights_version,scored_at) "
            "VALUES(?,102,?,?, '{}',?,?)",(line,value,cap,version,h.T))
    else:
        c.execute("INSERT INTO talk_reward VALUES(?,?,?)",(line,value,cap))
    for kind,v in (("explicit",thumb),("reason_funny",funny)):
        c.execute("INSERT INTO talk_signal(line_id,guid,kind,value,ts,source) VALUES(?,102,?,?,?,'explicit')",
            (line,kind,v,h.T))


def app(h):
    return SimpleNamespace(store=h.store)


@pytest.mark.parametrize("channel",["guild","discord:general"])
def test_high_reward_funny_line_keeps_actual_public_origin_and_its_medium(h,channel):
    line=h.say(h.T,102,"pulling with my face again",channel=channel)
    reward(h,line)
    ctx=h.ctx()
    run(stage_jokes(ctx))
    assert not ctx.failures
    row,=h.store.conn.execute("SELECT * FROM lexicon WHERE kind='joke'")
    assert row["origin_ref"]==f"chat:{line}"
    found=list(jokes(app(h),channel,h.T))
    assert len(found)==1 and "pulling with my face again" in found[0].evidence
    other="guild" if channel!="guild" else "discord:general"
    assert not list(jokes(app(h),other,h.T))
    run(stage_jokes(h.ctx()))
    assert h.store.conn.execute("SELECT count(*) FROM lexicon").fetchone()[0]==1


@pytest.mark.parametrize("change",["low","capped","not_funny","down","whisper","officer","dm","private_input","unrecorded"])
def test_reward_never_launders_private_or_unrewarded_jokes(h,change):
    channel={"whisper":"whisper","officer":"officer","dm":"dm:102"}.get(change,"guild")
    line=h.say(h.T,102,"pulling with my face again",channel=channel,to=1 if change in {"whisper","dm"} else 0)
    reward(h,line,value=.2 if change=="low" else 1,cap=.1 if change=="capped" else 1,
        funny=0 if change=="not_funny" else 1,thumb=-1 if change=="down" else 1)
    if change=="unrecorded":
        h.store.conn.execute("DELETE FROM utterance")
    if change=="private_input":
        mem=h.mem(102,"a private invented remark",scope="private",scope_with=1)
        h.store.conn.execute("UPDATE utterance SET memory_ids=?",(f"[{mem}]",))
    assert reward_jokes(h.ctx())==0
    assert not list(jokes(app(h),"guild",h.T))


def test_missing_rewards_does_not_disable_existing_origin_jokes(h):
    from nightly.test_night_language_births import laugh_line
    from leeroy_mind.nightly.language_births import jokes as birth_jokes
    laugh_line(h)
    ctx=h.ctx()
    birth_jokes(ctx)
    assert reward_jokes(ctx)==0
    assert len(list(jokes(app(h),"guild",h.T)))==1


def test_vetoed_or_newly_private_origin_is_no_longer_a_topic(h):
    line=h.say(h.T,102,"pulling with my face again")
    reward(h,line)
    assert reward_jokes(h.ctx())==1
    h.store.conn.execute("UPDATE chat_line SET channel='whisper',to_guid=1 WHERE id=?",(line,))
    assert not list(jokes(app(h),"guild",h.T))
