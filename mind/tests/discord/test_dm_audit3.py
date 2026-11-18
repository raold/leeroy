"""The third self-play audit's findings in his DMs (2026-11-17): one answer covers every post of
his still unanswered, his game logout never cancels a DM answer, her return from an away counts
as talking, a bracketed attachment is never speech, and Discord lines carry no game item links.
Invented lines only."""
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import HUMAN
from discord.test_discord_talk import answers, post, rig_for, talk_for
from discord.test_own_time import at, hand, run_for
from engine_rig import numbered
from leeroy_mind.discord import owntime
from leeroy_mind.discord.talk import SUPERSEDED
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.life import config as life_config
from leeroy_mind.life.interrupt import Away
from leeroy_mind.talk.filters import fake_attachment, find_tells, history_claim
from leeroy_mind.talk.pipeline import ATTACHMENT_HINT, NO_PHOTO_NOW, OWED_HARD_TELLS, TurnMeta
from leeroy_mind.talk.prompt import SO_FAR_HEAD, replay_tail
from leeroy_mind.talk.turns import Turn, TurnKind


def line_calls(rig):
    return [c for c in rig.chat_calls() if c["format"] is None]


def test_one_answer_covers_every_unanswered_post_of_his(store, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path, script=numbered("ha {n}, ok then"))
    talk = talk_for(rig)
    first = post(rig, "dm:101", "did the parcel ever turn up")
    second = post(rig, "dm:101", "also how was the dentist")
    third = post(rig, "dm:101", "anyway im back")
    for mid in (first, second, third):
        hand(rig, talk, mid)
    run_for(rig, talk, 200)
    mine = [m for m in rig.store.conn.execute(
        "SELECT reply_to FROM discord_message WHERE channel = 'dm:101' AND origin = 'mind'")]
    assert [r[0] for r in mine] == [third]  # one reply, to his newest post
    user = line_calls(rig)[0]["messages"][-1]["content"]
    assert "He wrote 3 messages you have not answered yet" in user
    assert user.index("1. did the parcel ever turn up") < user.index(
        "2. also how was the dentist") < user.index("3. anyway im back")
    assert len(line_calls(rig)) == 1  # the other two turns were withdrawn before the model
    assert caplog.text.count(f"reason={SUPERSEDED}") == 2
    assert "parcel" not in caplog.text
    # the next post is one message again
    fourth = post(rig, "dm:101", "what are you up to")
    hand(rig, talk, fourth)
    run_for(rig, talk, 200)
    assert len(answers(rig, "dm:101", fourth)) == 1
    assert "have not answered yet" not in line_calls(rig)[-1]["messages"][-1]["content"]
    rig.close()


def test_unanswered_reads_past_her_brb_and_stops_at_her_answer(store, tmp_path):
    from leeroy_mind.discord import store as dstore
    from leeroy_mind.discord.transport import PAIR_LINE_TURN
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path)
    talk = talk_for(rig)
    conn, now = rig.store.conn, rig.clock.now()
    a = post(rig, "dm:101", "old question")
    dstore.post(rig.store, channel="dm:101", author=101, origin="mind", text="old answer",
                reply_to=a, now=now)
    b = post(rig, "dm:101", "first new one")
    dstore.post(rig.store, channel="dm:101", author=101, origin="mind", text="brb",
                turn=PAIR_LINE_TURN + "x", now=now)
    c = post(rig, "dm:101", "second new one")
    assert [t for _, t in talk.unanswered(conn, "dm:101", None, c)] == [
        "first new one", "second new one"]
    assert [i for i, _ in talk.unanswered(conn, "dm:101", None, b)] == [b]
    rig.close()


def test_his_game_logout_never_cancels_a_dm_answer(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path, script=numbered("ha {n}, ok then"))
    talk = talk_for(rig)

    async def go():
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)
        mid = post(rig, "dm:101", "did the parcel ever turn up")
        talk.on_his_post(mid)
        guild = Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN, delay=30.0)
        rig.engine._queue([(guild, TurnMeta(rig.engine._new_id(), answers_human=True))])
        assert len(rig.engine.discord_answer_jobs()) == 1
        rig.world.human_online = False
        await rig.engine.end_session()  # he logs out of the game mid-answer
        await rig.advance(120)
        return mid
    mid = rig.sync(go())
    assert len(answers(rig, "dm:101", mid)) == 1
    assert not any(s.channel == "guild" and s.guid == 102 for s in rig.sent.says()[-1:])
    rig.close()


def test_her_return_from_an_away_counts_as_talking(store, tmp_path):
    rig = rig_for(store, at(2026, 11, 18, 12), tmp_path)  # a workday noon: at work
    cfg = rig.engine.cfg
    rig.engine.apply_config(replace(cfg, life=replace(cfg.life, availability=6,
                                                      dm_wait_cap_s=0)))
    talk = talk_for(rig)
    now = rig.clock.now()
    mid = post(rig, "dm:101", "so where were we")
    assert talk._due_of(101, mid, now) > now + 30  # cold: her day decides
    talk._due.clear()
    rig.engine.life._away[101] = Away(since=now - 1500, until=now - 100, channel="dm:101",
                                      reason="work", night="n", minute=0, done=True)
    assert talk._due_of(101, mid, now) == now  # back 100 s ago: still in the conversation
    talk._due.clear()
    rig.engine.life._away[101].until = now - owntime.TALKING_S - 60
    assert talk._due_of(101, mid, now) > now + 30
    assert life_config.dm_wait_cap() == 0
    rig.close()


@pytest.mark.parametrize("text,fake", [
    ("[sends a photo of my desk]", True),
    ("here you go [photo: a desk with two monitors]", True),
    ("<image attached>", True),
    ("*sends pic*", True),
    ("ok one sec [Attachment: portrait.png]", True),
    ("i sent the form to the bank this morning", False),
    ("[Shadowmourne] dropped again lol", False),
    ("took a photo of the cat earlier, shes a menace", False),
    ("*sighs* fine", False),
])
def test_a_bracketed_attachment_is_never_speech(text, fake):
    assert fake_attachment(text) is fake
    tells = {t.reason: t for t in find_tells(text)}
    assert ("attachment" in tells) is fake
    if fake:
        assert tells["attachment"].fatal and "attachment" in OWED_HARD_TELLS


def test_an_owed_dm_answer_with_a_fake_attachment_is_rewritten_and_told_why(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path)
    rig.script("here you go [sends a photo of my desk] told you", "cant send pics from here, its a mess anyway")
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "send me a pic of your desk")
    hand(rig, talk, mid)
    run_for(rig, talk, 200)
    got = answers(rig, "dm:101", mid)
    assert [m["text"] for m in got] == ["cant send pics from here, its a mess anyway"]
    first, retry = line_calls(rig)[:2]
    assert NO_PHOTO_NOW.format(they="he") in first["messages"][-1]["content"]
    assert ATTACHMENT_HINT in retry["messages"][-1]["content"]
    plain = post(rig, "dm:101", "how was the dentist")
    hand(rig, talk, plain)
    run_for(rig, talk, 200)
    assert "no photo or file to send" not in line_calls(rig)[-1]["messages"][-1]["content"]
    rig.close()


def test_discord_lines_carry_no_game_item_links(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path)
    seen = []
    rig.engine.items = SimpleNamespace(
        canonical=lambda text, level: seen.append(text) or text.replace("hearthstone",
                                                                        "[Hearthstone]"))
    meta = TurnMeta("x", answers_human=True)
    dm = Turn(TurnKind.REPLY_HUMAN, 101, "dm:101", to_guid=HUMAN, addressee=HUMAN)
    assert rig.engine._link_items(dm, meta, "lost my hearthstone again") == (
        "lost my hearthstone again")
    guild = Turn(TurnKind.REPLY_HUMAN, 101, "guild", addressee=HUMAN)
    assert rig.engine._link_items(guild, meta, "lost my hearthstone again") == (
        "lost my [Hearthstone] again")
    assert seen == ["lost my hearthstone again"]
    rig.close()


def test_a_dated_you_told_me_is_a_claim_of_shared_history():
    for text in ("like you said last week, the tank was late", "you told me yesterday you hated it",
                 "you promised me the other day"):
        assert history_claim(text), text
    for text in ("like you said, its late", "you told me to wait so i waited",
                 "as you said: more dots"):
        assert not history_claim(text), text


def test_the_block_says_to_admit_not_remembering_and_her_long_messages_show_their_end():
    assert "say you don't remember, never guess" in SO_FAR_HEAD
    long = " ".join(f"w{i}" for i in range(50))
    assert replay_tail(long, 5) == "... w45 w46 w47 w48 w49" and replay_tail("a b c") == "a b c"
