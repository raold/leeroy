"""What a private conversation has established so far (talk/sofar.py): his news, where and how
she said she is, what he asked and is still owed, plans; bounded, rule-built, and in the prompt
of her DM and her whisper with him. Invented lines only."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import HUMAN
from discord.test_dm_prompt import answer, answers, post, rig_for, talk_for
from engine_rig import numbered
from leeroy_mind.discord.talk import DM_CHAT_TOKENS, DM_HIS_TURNS, DM_OLD_WORDS
from leeroy_mind.protocol import Chat
from leeroy_mind.talk import sofar
from leeroy_mind.talk.prompt import SO_FAR_HEAD, PromptParts, redact_for, render_messages, trim_to_budget
from leeroy_mind.talk.sofar import IDLE_S, Line, SoFar, block, conversation
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.text import estimate_tokens

HE = ("he", "him", "his")


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)


def talk(*pairs, start=1000.0, step=30.0):
    """Lines from ("h"|"s", text) pairs, oldest first, `step` seconds apart."""
    return [Line(i + 1, start + i * step, who == "h", text)
            for i, (who, text) in enumerate(pairs)]


def test_his_news_is_kept_and_the_newest_statement_on_a_subject_wins():
    lines = talk(("h", "my uncle went into hospital this morning, they think its his heart"),
                 ("s", "oh no. is he ok"),
                 ("h", "lol the tank pulled without the healer again"),
                 ("h", "what are you doing later?"),
                 ("h", "my boss moved the deadline to friday so im stuck late"),
                 ("s", "ugh"),
                 ("h", "my uncle is home now, they let him out"))
    facts = [t for _, t in sofar.his_facts(lines)]
    assert facts == ["my boss moved the deadline to friday so im stuck late",
                     "my uncle is home now, they let him out"]  # the newer uncle line replaced
    assert not any("tank" in f or "later" in f for f in facts)  # not about him; a question
    assert sofar.his_facts(lines, skip=7)[0][1].startswith("my uncle went into hospital")
    many = talk(*[("h", f"my {who} is visiting this weekend") for who in
                  ("dad", "mum", "brother", "sister", "cousin", "aunt", "nan", "son")])
    kept = sofar.his_facts(many)
    assert len(kept) == sofar.FACTS and kept[-1][1].startswith("my son")  # the newest six
    long = talk(("h", "my sister " + "really " * 30 + "moved to the coast"))
    assert len(sofar.his_facts(long)[0][1].split()) == sofar.LINE_WORDS


def test_only_news_about_himself_is_kept_and_the_least_important_goes_first():
    """Review of #368, P2-6: flirts, bare declarations and lines with only a time or a plan
    word are not news; "ill" is "I'll"; when the block is full his health and his people stay
    and places go."""
    no = ["i want you so bad tonight", "ill be on at eight", "im gonna get you tomorrow",
          "i miss you", "my god that was good", "we should run naxx on friday",
          "thinking about you in bed all morning honestly", "i love my new mount so much"]
    assert sofar.his_facts(talk(*[("h", x) for x in no])) == []
    yes = ["been ill all week with this flu", "my sister had her baby on sunday",
           "got the job, i start at the new office monday"]
    assert [t for _, t in sofar.his_facts(talk(*[("h", x) for x in yes]))] == yes
    many = talk(("h", "stuck at the airport for another two hours"),
                ("h", "the gym near me closed for good"),
                ("h", "my hotel lost the booking again"),
                ("h", "the station was shut so i walked"),
                ("h", "my dad is having surgery on friday"),
                ("h", "i have an interview at the bank tomorrow"),
                ("h", "my brother moved in with us"),
                ("h", "the pub quiz is back on thursdays for me"))
    kept = [t for _, t in sofar.his_facts(many)]
    assert len(kept) == sofar.FACTS
    assert "my dad is having surgery on friday" in kept and "my brother moved in with us" in kept
    assert "stuck at the airport for another two hours" not in kept  # the oldest place went


def test_her_claims_come_from_the_coherence_extractor_when_asked_for(monkeypatch):
    """talk.position.claims_so_far is the reader of what she has said of herself when the
    caller asks for her claims (`claims`: [talk] coherence with a core switch on, the
    pipeline's call); otherwise her place and state, as before."""
    import sys
    import types
    lines = talk(("s", "im at the studio till ten and i start at six tomorrow"), ("h", "ok"))
    assert block(lines, forms=HE)[0].startswith("- You said you're at the studio")
    fake = types.ModuleType("leeroy_mind.talk.position")
    fake.claims_so_far = lambda hers, limit=3: [f"claim of {len(hers)} lines"]
    monkeypatch.setitem(sys.modules, "leeroy_mind.talk.position", fake)
    assert block(lines, forms=HE)[0].startswith("- You said you're at the studio")
    assert block(lines, forms=HE, claims=True) == ["- You said: claim of 1 lines."]
    assert sofar.her_said(lines, claims=True) == ["claim of 1 lines"]


def test_her_place_state_open_questions_and_plans():
    lines = talk(("s", "im at the studio till ten, the producer is in a mood"),
                 ("h", "what are you wearing?"),
                 ("s", "im wearing the green jumper you hate. back home now anyway"),
                 ("h", "ok. talk thursday at 11pm then"),
                 ("s", "deal, thursday"),
                 ("h", "did you eat yet?"),
                 ("h", "also how was the gig"),
                 ("h", "anyway i missed you"))
    assert sofar.her_place(lines) == "home now anyway"
    assert sofar.her_state(lines) == "im wearing the green jumper you hate"
    assert sofar.her_place(lines[:2]) == "at the studio till ten"
    assert sofar.her_said(lines) == ["home now anyway", "im wearing the green jumper you hate"]
    assert sofar.open_questions(lines, skip=8) == ["did you eat yet?", "also how was the gig"]
    assert sofar.open_questions(lines[:5]) == []  # she answered since
    assert sofar.plans(lines) == [(True, "talk thursday at 11pm then"), (False, "deal, thursday")] \
        or sofar.plans(lines)[0] == (True, "talk thursday at 11pm then")
    for mood in ("im in a mood today", "im on my way", "im in love with this song",
                 "im in the raid already"):
        assert sofar.her_place(talk(("s", mood))) == "", mood


def test_the_block_is_bounded_and_drops_his_oldest_news_first():
    lines = talk(*[("h", f"my {who} is in town this week and wants to see the new place we got")
                   for who in ("dad", "mum", "brother", "sister", "cousin", "aunt")],
                 ("s", "im at work until six, closing up after"),
                 ("h", "lets talk tonight after raid"),
                 ("h", "did you see the patch notes?"),
                 ("h", "anyway"))
    out = block(lines, forms=HE, skip=10, lead=("A line the caller always wants first.",))
    assert estimate_tokens("\n".join(out[1:])) <= sofar.MAX_TOKENS  # the conversation's lines
    assert out[0] == "- A line the caller always wants first."
    assert out[1] == "- You said you're at work until six."
    assert "- He also asked: did you see the patch notes?" in out
    told = [x for x in out if x.startswith("- He told you:")]
    assert told and "my aunt" in told[-1]
    crowd = talk(*[("h", f"my {who} is in town this week and wants to see the new place we got "
                         "before the weekend is over") for who in
                   ("dad", "mum", "brother", "sister", "cousin", "aunt")],
                 ("s", "im at work until six, closing up after the last table leaves tonight"),
                 *[("h", f"did you ever try the {x} place by the station on the corner?")
                   for x in "ab"])
    kept = [x for x in block(crowd, forms=HE) if x.startswith("- He told you:")]
    assert 0 < len(kept) < 6 and "my aunt" in kept[-1] and not any("my dad" in x for x in kept)
    assert block([], forms=HE) == [] and block([], forms=HE, lead=("x",)) == ["- x"]
    assert block(talk(("h", "lol nice"), ("s", "ikr")), forms=HE) == []
    they = block(talk(("h", "my dog is at the vet today")), forms=("they", "them", "their"))
    assert they == ["- They told you: my dog is at the vet today"]


def test_a_conversation_ends_after_three_idle_hours():
    lines = talk(("h", "my sister is in town this week"), ("s", "what"),
                 ("h", "my landlord is useless"), start=0.0, step=60.0)
    late = [*lines, Line(9, 120.0 + IDLE_S + 1, True, "my shift was fine")]
    assert [x.id for x in conversation(late, late[-1].ts + 5)] == [9]
    assert [x.id for x in conversation(lines, 200.0)] == [1, 2, 3]
    assert conversation(lines, 120.0 + IDLE_S + 1) == []
    reads = []

    def read(guid):
        reads.append(guid)
        return late
    kept = SoFar(read)
    first = kept.lines(7, late[-1].ts + 5, forms=HE)
    assert first == ["- He told you: my shift was fine"]
    assert kept.lines(7, late[-1].ts + 6, forms=HE) is first  # kept for the same newest line
    assert kept.lines(7, late[-1].ts + 6, forms=HE, skip=9) == []  # another turn's own
    assert kept.lines(7, late[-1].ts + IDLE_S + 60, forms=HE) == [] and 7 not in kept._kept
    again = SoFar(lambda guid: lines)  # a restart: built again from the stored lines
    assert again.lines(7, 200.0, forms=HE) == [
        "- He told you: my sister is in town this week",
        "- He told you: my landlord is useless"]
    assert again.her_said(7, 200.0) == []


def test_so_far_renders_after_the_chat_is_redacted_and_never_trimmed():
    from leeroy_mind.human import HumanIdentity
    parts = PromptParts(card="card", samples=[], rules="rules", rel_cards=["x: ok"],
                        memories=[f"- memory {i} " + "word " * 40 for i in range(8)], mood="fine",
                        chat=[f"A: line {i} " + "word " * 30 for i in range(30)], own_recent=[],
                        instruction="Answer.", so_far=["- He told you: Tomasz is moving house"])
    text = render_messages(parts)[1]["content"]
    assert text.index("Chat:") < text.index(SO_FAR_HEAD) < text.index("Answer.")
    assert SO_FAR_HEAD == sofar.HEAD
    cut = trim_to_budget(parts, budget=400)
    assert cut.so_far == parts.so_far and len(cut.chat) < 30
    human = HumanIdentity(guid=1, character_name="Testmage", real_name="Tomasz", nickname="", is_guild_master=True)
    assert redact_for(parts, human, False).so_far == [
        f"- He told you: {human.called} is moving house"]
    assert redact_for(parts, human, True).so_far == parts.so_far


def his_dm(rig, text, guid=102):
    """His DM post as the desk stores it: the Discord message and its chat line."""
    mid = post(rig, f"dm:{guid}", text)
    ev = Chat(ts=0, channel=f"dm:{guid}", from_guid=HUMAN, from_name="Testmage", to_guid=guid,
              text=text, map=0, zone=0)
    line = rig.store.insert_chat_line(ev, session_id=None, origin="human")
    rig.store.set_meta(f"discord:line:{mid}", str(line))
    return mid


def section(prompt, head):
    tail = prompt.split(head + "\n", 1)
    return tail[1].split("\n\n", 1)[0] if len(tail) == 2 else ""


def test_a_dm_remembers_his_news_from_many_turns_back(store, clock, tmp_path):
    """Twelve of his turns, her answers two messages each: his news from turn 1 has left the
    old 12-line window. The Chat is now sized by his turns, and the "so far" block carries his
    news whatever the window."""
    rig = rig_for(store, clock, tmp_path, script=numbered("mm {n}. tell me more about it then"))
    talk = talk_for(rig)
    news = "my uncle went into hospital this morning"
    lines = [news, "anyway how was your shift", "did the delivery turn up"] + [
        f"filler thought number {i}" for i in range(8)] + ["right i have to head off soon"]
    for text in lines[:-1]:
        mid = his_dm(rig, text)
        answer(rig, talk, mid)
        assert answers(rig, "dm:102", mid)
    mid = his_dm(rig, lines[-1])
    answer(rig, talk, mid)
    prompt = rig.prompts()[-1]
    chat = section(prompt, "Chat:")
    his = [x for x in chat.splitlines() if "(Discord DM to Belra)" in x]
    assert len(his) == DM_HIS_TURNS + 1  # his ten turns before it, and the message she answers
    assert "did the delivery turn up" in chat and news not in chat  # his 10 turns before it
    far = section(prompt, SO_FAR_HEAD)
    assert f"- He told you: {news}" in far  # ...and his news from 11 turns back is still there
    assert "head off soon" not in far  # the message she answers is the instruction's
    assert estimate_tokens("\n".join(far.splitlines()[1:])) <= sofar.MAX_TOKENS
    assert estimate_tokens(chat) <= DM_CHAT_TOKENS + 40
    rig.close()


def test_older_messages_of_hers_are_shorter_in_the_dm_chat(store, clock, tmp_path):
    said = iter(range(100))

    def reply(model, messages):  # sixty words, different ones each time
        n = next(said)
        return " ".join(f"w{i}" if i < 30 else f"x{n}y{i}" for i in range(60))
    rig = rig_for(store, clock, tmp_path, script=reply)
    talk = talk_for(rig)
    for i in range(6):
        mid = his_dm(rig, f"question number {i} for you")
        answer(rig, talk, mid)
    chat = section(rig.prompts()[-1], "Chat:").splitlines()
    hers = [x for x in chat if "y59" in x]  # each shows the words she ended on
    assert len(hers[0].split("... ")[1].split()) == DM_OLD_WORDS
    assert len(hers[-1].split("... ")[1].split()) == 30  # her recent ones keep 30 words
    rig.close()


def test_a_whisper_carries_so_far_too_and_a_restart_rebuilds_it(store, clock, tmp_path):
    async def main():
        rig = rig_for(store, clock, tmp_path, script=numbered("mm {n}, go on"))
        rig.world.human_online = True
        await rig.session()
        await rig.human("my sister is moving to the coast next month", channel="whisper",
                        to_guid=102)
        await rig.advance(60)
        for i in range(9):
            await rig.human(f"small talk line {i}", channel="whisper", to_guid=102)
            await rig.advance(60)
        prompt = rig.prompts()[-1]
        assert "- He told you: my sister is moving to the coast next month" in section(
            prompt, SO_FAR_HEAD)
        assert "small talk line 8" not in section(prompt, SO_FAR_HEAD)
        guild = len(rig.prompts())
        await rig.human("anyone up for a heroic", channel="guild")
        await rig.advance(60)
        assert all(SO_FAR_HEAD not in p for p in rig.prompts()[guild:])  # private turns only
        rig.engine.so_far = SoFar(rig.engine._so_far_read)  # as a restart leaves it: empty
        await rig.human("ok one more thing", channel="whisper", to_guid=102)
        await rig.advance(60)
        assert "my sister is moving to the coast" in section(rig.prompts()[-1], SO_FAR_HEAD)
        rig.close()
    asyncio.run(main())


def test_an_over_budget_dm_lets_its_oldest_chat_rows_go_before_style_and_memories():
    """Review of #368 (budget): chat older than his sixth-last turn goes first; her style
    notes, the awareness card and her memories are not cut for it."""
    chat = [f"{'Zan' if i % 2 == 0 else 'Belra'}: line {i} " + "word " * 25 for i in range(24)]
    parts = PromptParts(card="card", samples=[], rules="rules", rel_cards=["Zan: close"],
                        memories=[f"- memory {i} " + "word " * 20 for i in range(5)],
                        mood="fine", chat=chat, own_recent=[], instruction="Answer.",
                        style=["a style note " + "word " * 10], old_chat=12)
    whole = sum(estimate_tokens(m["content"]) for m in render_messages(parts))
    cut = trim_to_budget(parts, budget=whole - 150)
    assert len(cut.chat) < 24 and cut.chat[-12:] == chat[-12:]  # his last six turns whole
    assert cut.memories == parts.memories and cut.style == parts.style
    none = trim_to_budget(parts, budget=whole + 10)
    assert none.chat == chat
    plain = trim_to_budget(PromptParts(**{**parts.__dict__, "old_chat": 0}), budget=whole - 150)
    assert plain.style == []  # as before without old rows to give: the style note goes first
