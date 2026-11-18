"""His Discord DM's answer prompt (2026-11-15): the DM's history is the prompt's Chat, his
messages whole and hers as their opening words, not packed into a capped instruction; her own
long lines are replayed short; the DM gets its own larger budget, so a long DM fits without the
last-resort trim, and her whispers with him (the elsewhere block) still reach it. Invented
text."""
import logging
import random
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import HUMAN, TEST_CAST, seed_test_cast
from engine_rig import make_rig, save_member
from leeroy_mind.config import DISCORD_WORDS_MAX
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.store import messages
from leeroy_mind.discord.talk import DM_HIS_CHARS, DiscordTalk
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.discord_length import num_predict
from leeroy_mind.talk.prompt import (DM_BUDGET, ELSEWHERE_HEAD, ESTIMATE_SLACK, LIVE_BUDGET,
                                     REPLAY_WORDS, replay)
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.text import estimate_tokens


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)


def rig_for(store, clock, tmp_path, **kw):
    seed_test_cast(store)
    for g in TEST_CAST:
        save_member(store, g, region="US")
    rig = make_rig(store, clock, tmp_path, **kw)
    rig.world.human_online = False
    return rig


def talk_for(rig):
    app = SimpleNamespace(store=rig.store, clock=rig.clock, engine=rig.engine, world=rig.world,
                          personas=rig.engine.personas, human_guid=HUMAN, arbiter=None,
                          realm=SimpleNamespace(cfg=SimpleNamespace(trial_rank=4)),
                          rng=random.Random(1), stopping=False)
    return DiscordTalk(app)


def post(rig, channel, text):
    return dstore.post(rig.store, channel=channel, author=HUMAN, origin="human", text=text,
                       now=rig.clock.now())


def member_post(rig, guid, channel, text):
    return dstore.post(rig.store, channel=channel, author=guid, origin="mind", text=text,
                       now=rig.clock.now())


def answer(rig, talk, mid, seconds=200.0):
    async def go():
        talk.on_his_post(mid)
        await rig.advance(seconds)
    rig.sync(go())


def answers(rig, channel, mid):
    return [m for m in messages(rig.store.conn, channel)
            if m["origin"] == "mind" and m["reply_to"] == mid]


HIS_LONG = "zorblat " + "glimmer " * 70 + "endmark"  # 560 characters, over the old 200
HERS_LONG = " ".join(f"w{i}" for i in range(120))  # 120 words of hers


def sections(prompt: str) -> dict[str, str]:
    """The user message's sections by heading (the text up to the next blank line)."""
    out = {}
    for block in prompt.split("\n\n"):
        head, _, body = block.partition("\n")
        out[head] = body
    return out


def test_replay_keeps_the_opening_words():
    assert replay("a b c") == "a b c"
    long = " ".join(str(i) for i in range(50))
    assert replay(long) == " ".join(str(i) for i in range(REPLAY_WORDS)) + "..."


def test_the_dm_budget_fits_the_live_context_with_a_full_dm_reply():
    room = LIVE.options["num_ctx"] - num_predict(DISCORD_WORDS_MAX)
    assert DM_BUDGET > LIVE_BUDGET
    assert DM_BUDGET * ESTIMATE_SLACK + 64 <= room  # 64: the chat template, generously


def test_the_dm_history_is_the_chat_and_not_the_instruction(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, reply="sure thing")
    talk = talk_for(rig)
    earlier = post(rig, "dm:102", HIS_LONG)
    dstore.post(rig.store, channel="dm:102", author=102, origin="mind", text=HERS_LONG,
                reply_to=earlier, now=rig.clock.now())  # her answer to it
    member_post(rig, 104, "dm:104", "quaxle is another member's dm")
    member_post(rig, 103, "general", "vorpine in general")
    mid = post(rig, "dm:102", "so what do you think?")
    answer(rig, talk, mid)
    assert answers(rig, "dm:102", mid)
    prompt = rig.prompts()[-1]
    chat = sections(prompt)["Chat:"]
    assert "endmark" in chat  # his earlier message whole
    # hers short: the words she ended on (self-play audit 3), not her opening
    assert f"... w{120 - REPLAY_WORDS} " in chat and f"w{120 - REPLAY_WORDS - 1} " not in chat
    assert chat.index("zorblat") < chat.index("w119")  # oldest first
    assert "Recent messages:" not in prompt
    # his message she answers ends the Chat (2026-11-16, the owner's sandbox audit: without it
    # the Chat ended on his previous message and she answered that) and the instruction quotes it
    assert chat.rstrip().splitlines()[-1].endswith("so what do you think?")
    assert prompt.count("so what do you think?") == 2
    assert prompt.rstrip().endswith("so what do you think?") or (
        "Answer his message: so what do you think?" in prompt)
    assert "quaxle" not in prompt and "vorpine" not in prompt  # that DM only
    rig.close()


def test_a_long_dm_fits_its_budget_without_the_last_resort(store, clock, tmp_path, caplog):
    # the shape of the logged trims: his long messages, her 150-200 word answers in her own
    # last lines, a dozen messages of history
    rig = rig_for(store, clock, tmp_path)
    rig.llm.set_script(lambda model, messages: " ".join(
        f"{w}{len(rig.chat_calls())}" for w in ("so", "and", "then", "well") * 45))
    talk = talk_for(rig)
    for i in range(6):
        mid = post(rig, "dm:102", f"his {i} " + "longish words here " * 30)
        answer(rig, talk, mid)
        assert answers(rig, "dm:102", mid)
    mid = post(rig, "dm:102", "tell me more " + "please " * 60)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        answer(rig, talk, mid)
    assert answers(rig, "dm:102", mid)
    assert "last-resort" not in caplog.text
    used = sum(estimate_tokens(m["content"]) for m in rig.chat_calls()[-1]["messages"])
    assert LIVE_BUDGET < used <= DM_BUDGET  # more than a game prompt holds, within the DM's
    parts = sections(rig.prompts()[-1])
    chat = parts["Chat:"]
    assert chat.count("Belra (Discord DM to Zan)") >= 5  # her side of the conversation
    assert all(len(line) <= DM_HIS_CHARS + 80 for line in chat.splitlines())
    # her last lines are in the Chat, so the prompt does not show them twice (2026-11-17)
    assert "Your last lines (don't make the same move again):" not in parts
    rig.close()


def test_her_own_last_long_line_is_replayed_short(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, reply=HERS_LONG)
    talk = talk_for(rig)
    first = post(rig, "dm:102", "how was work?")
    answer(rig, talk, first)
    assert answers(rig, "dm:102", first)
    rig.script("glad you asked")
    second = post(rig, "dm:102", "and after that?")
    answer(rig, talk, second)
    parts = sections(rig.prompts()[-1])
    assert "Your last lines (don't make the same move again):" not in parts  # it is in the Chat
    hers = [x for x in parts["Chat:"].splitlines() if "w119" in x]
    assert len(hers) == 1 and len(hers[0].split()) <= REPLAY_WORDS + 12  # ...and short there
    rig.close()


def test_her_whispers_with_him_reach_the_dm(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, reply="lol yes")
    talk = talk_for(rig)
    now = rig.clock.now()
    for frm, to, text in ((HUMAN, 102, "plinkett whisper from him"),
                          (102, HUMAN, "plinkett whisper back")):
        store.insert_chat_line(Chat(ts=int(now * 1000), channel="whisper", from_guid=frm,
                                    from_name="x", to_guid=to, text=text, map=0, zone=0),
                               session_id=None, origin="human" if frm == HUMAN else "mind",
                               ts=now)
    mid = post(rig, "dm:102", "remember what you whispered?")
    answer(rig, talk, mid)
    elsewhere = sections(rig.prompts()[-1])[ELSEWHERE_HEAD]
    assert "plinkett whisper from him" in elsewhere and "plinkett whisper back" in elsewhere
    rig.close()


def test_a_channel_answer_keeps_its_recent_messages_in_the_instruction(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, reply="im up for it")
    talk = talk_for(rig)
    member_post(rig, 103, "general", "brindle raid tonight")
    mid = post(rig, "general", "@Arkon you coming?")
    answer(rig, talk, mid)
    prompt = rig.prompts()[-1]
    assert "Recent messages:" in prompt and "brindle raid tonight" in prompt
    assert "Chat:" not in prompt
    rig.close()


def test_her_answer_never_copies_his_earlier_dm_message(store, clock, tmp_path):
    """Review #325: the copy check reads his earlier messages now in the DM's Chat (the audit's
    answer that sent his earlier message back word for word)."""
    rig = rig_for(store, clock, tmp_path)
    rig.script("i am having trouble sleeping again tonight honestly", "aw, warm milk helps me")
    talk = talk_for(rig)
    post(rig, "dm:102", "i am having trouble sleeping again tonight honestly")
    mid = post(rig, "dm:102", "want a drink?")
    answer(rig, talk, mid)
    assert len(rig.chat_calls()) == 2
    [m] = answers(rig, "dm:102", mid)
    assert "warm milk" in m["text"]
    rig.close()
