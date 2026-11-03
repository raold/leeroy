"""Fix wave G6: when he pushes back on a member's line, that member answers first, plainly, in
one line. Seen live: he had just filled the guild bank, a member teased him with the facts
inverted, and every "what are you talking about?" and "are you drunk?" drew another tease, two
lines each. Korvin, Arkon and the rest are invented."""
import asyncio
import logging
import random
from collections import Counter
from dataclasses import replace

import pytest

from conftest import HUMAN, TEST_CAST, make_mem, make_persona, run, seed_test_cast
from engine_rig import engine_clock, make_rig, numbered, save_member
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Cast, Chat, Member
from leeroy_mind.talk.prompt import PromptParts, render_messages, pushback_help, trim_to_budget
from leeroy_mind.talk.select import (ANGER_CUES, DOUBT_CUES, PUSHBACK_WINDOW_S, plan_replies,
                                     plan_reply_turns, pushback_kind)
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import estimate_tokens
from leeroy_mind.wow import CLASSES, RACES

KORVIN = 106
DOUBT = ("Zan is pushing back on what you just said: he didn't follow it or think it's wrong. "
         "Answer him plainly: say what you meant, or admit you got it wrong. No joke this time.")
SORRY = ("Zan is annoyed with you. Take it seriously: apologise and make it right. No joke "
         "this time.")
FIRM = ("Zan is annoyed with you. Take it seriously: stand your ground if you were right, going "
        "only by what was actually said. No joke this time.")
STRAIGHT = "Zan is annoyed with you. Take it seriously and answer him straight. No joke this time."
TEXTS = (DOUBT, SORRY, FIRM, STRAIGHT)
HIS_LINE = "you're trying to bankrupt the guild bank man"  # Korvin's tease

# His lines from the live transcript, shaped with Korvin's name.
DOUBT_LINES = ["what bankrupts the bank? what are you talking about korvin?",
               "wtf? korvin, what have you put in the bank - ever?", "are you drunk?"]
ANGER_LINES = ["you are fixing to get the boot from this guild korvin",
               "was nice knowing you korvin, but you're out my man"]
NOT_PUSHBACK = ["huh, nice drop", "is that enough flasks?",
                "anyone know where the argent dailies start?", "gz korvin!", "what's up korvin",
                "the boots dropped!"]


@pytest.fixture
def clock():
    return engine_clock()


# -- what a pushback is ------------------------------------------------------------------------

@pytest.mark.parametrize("line", DOUBT_LINES)
def test_his_doubt_from_the_transcript(line):
    assert pushback_kind(line) == "doubt"


@pytest.mark.parametrize("line", ANGER_LINES)
def test_his_anger_from_the_transcript(line):
    assert pushback_kind(line) == "anger"


@pytest.mark.parametrize("line", NOT_PUSHBACK)
def test_ordinary_lines_are_no_pushback(line):
    assert pushback_kind(line) is None


@pytest.mark.parametrize("cue", DOUBT_CUES)
def test_every_doubt_cue_in_any_case(cue):
    assert pushback_kind(f"korvin {cue}") == "doubt"
    assert pushback_kind(cue.upper() + "!") == "doubt"


@pytest.mark.parametrize("cue", ANGER_CUES)
def test_every_anger_cue_in_any_case(cue):
    assert pushback_kind(f"{cue} korvin") == "anger"
    assert pushback_kind(cue.upper()) == "anger"


def test_the_cue_lists_are_the_briefs():
    assert len(DOUBT_CUES) == 36 and len(ANGER_CUES) == 14
    assert {"wdym", "wtf", "are you drunk", "certainly not", "ur wrong"} <= set(DOUBT_CUES)
    assert {"get the boot", "you're out", "gkick", "watch your mouth"} <= set(ANGER_CUES)


def test_two_question_marks_in_a_row_are_doubt_anywhere():
    for line in ("i put in 200k gold and over 1000 flasks for yall to use??", "you said WHAT???",
                 "really?? ok", "korvin??"):
        assert pushback_kind(line) == "doubt", line
    for line in ("really?", "ready? pulling", "you up? want to run voa?"):
        assert pushback_kind(line) is None, line


def test_huh_what_and_seriously_count_only_as_the_whole_line():
    for line in ("huh?", "What?", "seriously?", " huh ? ", "what?!"):
        assert pushback_kind(line) == "doubt", line
    for line in ("huh", "what", "what? lol", "seriously? nice", "huh? where",
                 "what time is it?", "seriously nice drop", "so what?"):
        assert pushback_kind(line) is None, line


def test_anger_wins_over_doubt():
    assert pushback_kind("wtf, knock it off korvin") == "anger"
    assert pushback_kind("are you serious?? last warning") == "anger"


def test_cues_are_whole_words_and_phrases():
    for line in ("kick your feet up", "you're outside the cave?", "stop iterating on it",
                 "wtfbbq", "your wrong turn", "shut upstairs", "the youre outlaw"):
        assert pushback_kind(line) is None, line


def test_a_status_call_is_not_a_kick():
    """"you're out" is a kick, but "you're out of range" (mana, los...) is a status call."""
    for line in ("heal me, you're out of range", "ur out of mana lol", "youre out of los korvin",
                 "you're out of position on the left", "korvin you're out of line of sight",
                 "ur out of melee range", "youre out of energy already?"):
        assert pushback_kind(line) is None, line
    for line in ("you're out of line korvin", "you're out of this guild", "you're out my man"):
        assert pushback_kind(line) == "anger", line


def test_after_the_chat_normalisation():
    """Typographic apostrophes and spacing count as typed; the names in item links are not
    read."""
    assert pushback_kind("you’re out") == "anger"
    assert pushback_kind("that’s not true") == "doubt"
    assert pushback_kind("it   doesn’t  make sense") == "doubt"
    assert pushback_kind("|cff0070dd|Hitem:1234|h[Shut Up And Kick You]|h|r for sale") is None


def test_the_window_is_two_minutes():
    assert PUSHBACK_WINDOW_S == 120


# -- who answers it ----------------------------------------------------------------------------

NAMES = {101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna", KORVIN: "Korvin"}
W = {**{g: 1.0 for g in NAMES}, KORVIN: 0.0}  # Korvin just spoke: his weight is 0


class _HighRoll(random.Random):
    """Every roll lands at 0.95, over WHISPER_REPLY_P's 0.9."""

    def random(self):
        return 0.95


def test_an_unnamed_pushback_has_its_target_first_then_the_usual_draw():
    """He names nobody: the one he pushes back on answers first, whatever their weight, and
    counts among the line's answers as a member he named would."""
    for line, sizes in (("are you drunk?", {2, 3}), ("wtf", {1, 2})):
        seen = Counter()
        for seed in range(200):
            got = plan_replies(line, channel="guild", to_guid=0, weights=W, names=NAMES,
                               afk=set(), rng=random.Random(seed), reasons={102}, target=KORVIN)
            assert got[0] == KORVIN and got.count(KORVIN) == 1, (line, seed, got)
            seen[len(got)] += 1
        assert set(seen) == sizes, (line, seen)
    turns = plan_reply_turns("are you drunk?", channel="guild", to_guid=0, weights=W,
                             names=NAMES, afk=set(), rng=random.Random(3), line_id=9,
                             target=KORVIN)
    assert (turns[0].speaker, turns[0].turn_id) == (KORVIN, "reply-9-0")


def test_a_named_pushback_stays_that_member_alone():
    """Fix wave G5's aimed_at_one: a line naming one member is theirs alone, pushback or not."""
    for line in ANGER_LINES + DOUBT_LINES[:2]:
        for seed in range(100):
            got = plan_replies(line, channel="guild", to_guid=0, weights=W, names=NAMES,
                               afk=set(), rng=random.Random(seed), reasons={102, 103},
                               target=KORVIN)
            assert got == [KORVIN], (line, seed, got)


def test_a_named_pushback_that_speaks_to_the_guild_keeps_the_draw_after_him():
    line = "what are you talking about korvin? that gold is for yall to use??"
    sizes = Counter()
    for seed in range(200):
        got = plan_replies(line, channel="guild", to_guid=0, weights=W, names=NAMES, afk=set(),
                           rng=random.Random(seed), reasons={102}, target=KORVIN)
        assert got[0] == KORVIN, (seed, got)
        sizes[len(got)] += 1
    assert max(sizes) >= 2


def test_a_whispered_pushback_is_always_answered():
    """The one he whispers answers his pushback every time, not WHISPER_REPLY_P of the time."""
    def plan(target):
        return plan_replies("wtf?", channel="whisper", to_guid=KORVIN, weights=W, names=NAMES,
                            afk=set(), rng=_HighRoll(1), target=target)
    assert plan(KORVIN) == [KORVIN]
    assert plan(0) == []


def test_a_target_who_cannot_answer_changes_nothing():
    for kw in ({"afk": {KORVIN}}, {"weights": {g: w for g, w in W.items() if g != KORVIN}},
               {"allowed": lambda g: g != KORVIN}):
        args = dict(channel="guild", to_guid=0, weights=W, names=NAMES, afk=set(),
                    reasons={102})
        args.update(kw)
        for seed in range(50):
            got = plan_replies("are you drunk?", rng=random.Random(seed), target=KORVIN, **args)
            assert KORVIN not in got and got == plan_replies(
                "are you drunk?", rng=random.Random(seed), **args), (kw, seed)


# -- what the one he pushes back on is told ----------------------------------------------------

HE = HumanIdentity(guid=HUMAN, character_name="Testmage", nickname="Zan", real_name="",
                   is_guild_master=True, pronouns="he")


def test_the_doubt_text():
    assert pushback_help(HE, "doubt", 5) == DOUBT
    assert pushback_help(HE, "doubt", 9) == DOUBT


@pytest.mark.parametrize("score, text", [(10, SORRY), (9, SORRY), (7, SORRY), (6, STRAIGHT),
                                          (5, STRAIGHT), (4, FIRM), (3, FIRM), (1, FIRM)])
def test_agreeableness_picks_the_anger_text(score, text):
    assert pushback_help(HE, "anger", score) == text


def test_the_texts_take_his_pronouns_as_the_reply_does():
    they = HumanIdentity(guid=HUMAN, character_name="Testmage", nickname="", real_name="",
                         is_guild_master=True, pronouns="")
    assert pushback_help(they, "doubt", 5) == (
        "Testmage is pushing back on what you just said: they didn't follow it or think it's "
        "wrong. Answer them plainly: say what you meant, or admit you got it wrong. No joke "
        "this time.")
    assert pushback_help(they, "anger", 5).endswith("answer them straight. No joke this time.")


# -- her own line stays in the chat her prompt shows -------------------------------------------

OWN = "[1m ago] Korvin: you're trying to bankrupt the guild bank man"
HIS = "[just now] Zan: are you drunk?"


def parts(**kw) -> PromptParts:
    base = dict(card="You are Tarvo. You play Korvin.", samples=["ok pulling in 5"],
                rules="How you talk here: short.", rel_cards=["Zan (the guild master): close"],
                memories=[f"- M{i}: " + "m" * 190 for i in range(5)], mood="It's evening.",
                chat=[OWN] + [f"[{i}m ago] P{i:02}: " + "x" * 200 for i in range(7, 0, -1)]
                + [HIS], own_recent=["ok pulling in 5"], instruction="INSTRUCTION LAST")
    base.update(kw)
    return PromptParts(**base)


def tokens_of(p: PromptParts) -> int:
    return sum(estimate_tokens(m["content"]) for m in render_messages(p))


def test_the_trim_never_cuts_her_own_line():
    p = parts(kept=0)
    middle = trim_to_budget(p, budget=tokens_of(p) - 250)
    assert middle.chat[0] == OWN and middle.chat[-1] == HIS and len(middle.chat) < len(p.chat)
    assert middle.kept == 0
    assert trim_to_budget(p, budget=1).chat == [OWN, HIS]  # the last resort keeps both
    assert trim_to_budget(parts(), budget=1).chat == [HIS]  # without it, as before


def test_her_own_line_is_never_shortened():
    """Four lines, so none is dropped: the trim shortens the older ones, never hers."""
    long_own = "[1m ago] Korvin: " + "y" * 300
    p = parts(kept=0, memories=[f"- M{i}: short" for i in range(3)],
              chat=[long_own, *parts().chat[-3:]])
    budget = tokens_of(p) - 40
    assert trim_to_budget(replace(p, kept=None), budget=budget).chat[0] != long_own
    t = trim_to_budget(p, budget=budget)
    assert t.chat[0] == long_own and t.chat[1:] != p.chat[1:] and t.chat[-1] == HIS


def test_with_facts_his_line_and_her_own_both_stay():
    later = "[just now] Arkon: line 1 from me"
    p = parts(kept=0, chat=[*parts().chat, later], pinned=8, facts=["Fact one."],
              facts_head="Facts you know:")
    last = trim_to_budget(p, budget=1)
    assert last.chat == [OWN, HIS] and (last.kept, last.pinned) == (0, 1)
    assert trim_to_budget(replace(p, kept=None), budget=1).chat == [HIS]  # as before


# -- the engine --------------------------------------------------------------------------------

def korvin_rig(store, clock, tmp_path, *, seed: int = 1, script=None, **persona):
    """The engine rig with a sixth member, Korvin, in the guild and online. `persona` changes
    his persona's fields (agreeableness, split_rate); he never makes a typo."""
    if not store.persona_versions():
        seed_test_cast(store)
    persona.setdefault("typo_rate", 0.0)
    save_persona(store, make_persona(KORVIN, "Korvin", **persona), source="seed", ts=1000.0,
                 race=RACES[2], class_name=CLASSES[1])
    r = make_rig(store, clock, tmp_path, seed=seed, script=script or numbered())
    members = [Member(guid=g, name=n, race=race, class_=c, gender=0, level=80, rank=3,
                      online=True, map=0, zone=10, spec_tab=-1)
               for g, (n, race, c) in TEST_CAST.items()]
    members.append(Member(guid=KORVIN, name="Korvin", race=2, class_=1, gender=0, level=80,
                          rank=3, online=True, map=0, zone=10, spec_tab=-1))
    r.world.apply(Cast(ts=0, members=tuple(members)), clock.now())
    return r


def planned(r) -> list:
    """Every (Turn, TurnMeta) the engine queues as answers to his lines, as it queues them."""
    got = []
    queue = r.engine._queue

    def spy(items):
        got.extend(x for x in items if x[1].why.startswith("reply:"))
        queue(items)
    r.engine._queue = spy
    return got


async def says_as(r, guid: int, text: str, channel: str = "guild", to_guid: int = 0) -> str:
    """A member's line the mind says (the template path): stored, in the world, guarded. The
    line as it went out, in the member's typing."""
    task = asyncio.create_task(r.engine.say_as(guid, channel, text, kind=TurnKind.OPEN,
                                               to_guid=to_guid))
    while not task.done():
        await r.advance(0.5, 0.5)
    assert task.result()
    return r.sent.says()[-1].text


def bridge_line(r, guid: int, name: str, text: str, channel: str = "guild") -> int:
    """A member's line the bridge reported and the mind never said: stored as game chat only,
    as the app does (no world line, no guard). Its id."""
    return r.store.insert_chat_line(Chat(ts=0, channel=channel, from_guid=guid, from_name=name,
                                         to_guid=0, text=text, map=0, zone=10),
                                    session_id=None, origin="game")


def his_aside(r, text: str) -> None:
    """A line of his in the log and the world that the engine is not asked to answer."""
    ev = Chat(ts=0, channel="guild", from_guid=HUMAN, from_name="Testmage", to_guid=0,
              text=text, map=0, zone=10)
    line = r.store.insert_chat_line(ev, session_id=None, origin="human")
    r.world.add_line(r.store.chat_lines([line])[0], r.clock.now())


def told(items) -> list[str]:
    return [t.instruction for t, _ in items]


def korvins(r) -> list[str]:
    return [p for p in r.prompts() if "You play Korvin" in p]


@pytest.mark.parametrize("line, text", [(DOUBT_LINES[0], DOUBT), (DOUBT_LINES[1], DOUBT),
                                        (ANGER_LINES[0], STRAIGHT), (ANGER_LINES[1], STRAIGHT)])
def test_his_pushbacks_from_the_transcript_plan_korvin_first_with_its_text(store, clock,
                                                                            tmp_path, line, text):
    async def main():
        r = korvin_rig(store, clock, tmp_path)
        items = planned(r)
        said = await says_as(r, KORVIN, HIS_LINE)
        await r.advance(20)
        await r.human(line)
        turn, meta = items[0]
        assert turn.speaker == KORVIN and turn.max_lines == 1 and turn.instruction == text
        assert meta.pushback == ("doubt" if text == DOUBT else "anger") and meta.why == (
            "reply:named")
        assert meta.pushback_line == r.engine._last_line_id[KORVIN]  # his line, 20 s ago
        assert [t.speaker for t, _ in items] == [KORVIN]  # he named Korvin: Korvin alone
        await r.advance(45, step=2)
        prompt = r.prompts()[0]
        assert "You play Korvin" in prompt and prompt.rstrip().endswith(text)
        assert f"Korvin: {said}" in prompt  # his own line is in the chat shown
        assert [s.guid for s in r.sent.says()] == [KORVIN, KORVIN]  # his line, one answer
    run(main())


@pytest.mark.parametrize("score, text", [(9, SORRY), (3, FIRM), (5, STRAIGHT)])
def test_korvins_agreeableness_picks_his_anger_text(store, clock, tmp_path, score, text):
    async def main():
        r = korvin_rig(store, clock, tmp_path,
                       agreeableness={"score": score, "behaviour": "as he is"})
        items = planned(r)
        await r.human("you are fixing to get the boot from this guild korvin")
        assert told(items) == [text]
        await r.advance(45, step=2)
        assert korvins(r) and korvins(r)[0].rstrip().endswith(text)
    run(main())


@pytest.mark.parametrize("said", [says_as, bridge_line])
def test_are_you_drunk_with_no_name_goes_to_whoever_spoke_last(store, clock, tmp_path, said):
    """7 s after Korvin's line, his unnamed "are you drunk?" is Korvin's to answer, first and
    with the doubt text, whether the mind said Korvin's line or the bridge reported it; then
    the usual draw, told nothing new."""
    async def main():
        for seed in range(4):
            r = korvin_rig(store, clock, tmp_path, seed=seed)
            items = planned(r)
            text = HIS_LINE
            if said is says_as:
                text = await says_as(r, KORVIN, HIS_LINE)
            else:
                bridge_line(r, KORVIN, "Korvin", HIS_LINE)
            await r.advance(7)
            await r.human("are you drunk?")
            (first, meta), rest = items[0], items[1:]
            assert (first.speaker, first.max_lines, first.instruction) == (KORVIN, 1, DOUBT)
            assert meta.pushback == "doubt" and meta.why == "reply:pushback" and meta.pushback_line
            assert 1 <= len(rest) <= 2, seed  # a question to the guild: 2-3 answers in all
            assert all(t.speaker != KORVIN and t.max_lines == 2 and not t.instruction
                       and not m.pushback for t, m in rest)
            await r.advance(45, step=2)
            prompts = r.prompts()
            assert "You play Korvin" in prompts[0] and DOUBT in prompts[0], seed
            assert f"Korvin: {text}" in prompts[0]
            others = [p for p in prompts if "You play Korvin" not in p]
            assert others and not any(x in p for p in others for x in TEXTS)  # as before
            await r.engine.end_session()
    run(main())


@pytest.mark.parametrize("gap, aimed", [(PUSHBACK_WINDOW_S, True), (PUSHBACK_WINDOW_S + 1, False),
                                        (200, False)])
def test_the_window_before_his_line(store, clock, tmp_path, gap, aimed):
    async def main():
        r = korvin_rig(store, clock, tmp_path)
        items = planned(r)
        bridge_line(r, KORVIN, "Korvin", HIS_LINE)
        await r.advance(gap)
        await r.human("are you drunk?")
        assert items, gap
        assert (items[0][0].speaker == KORVIN and items[0][1].pushback == "doubt") is aimed
        assert any(m.pushback for _, m in items) is aimed
    run(main())


def test_with_no_member_line_there_is_no_pushback(store, clock, tmp_path):
    async def main():
        r = korvin_rig(store, clock, tmp_path)
        items = planned(r)
        await r.human("are you drunk?")  # nobody has said anything
        assert items and not any(m.pushback or t.instruction for t, m in items)
        await r.advance(45, step=2)
        assert not any(x in p for p in r.prompts() for x in TEXTS)
    run(main())


def test_the_last_member_line_counts_not_who_he_answered(store, clock, tmp_path):
    """Any member's line in the channel counts, the one just before his line: Arkon spoke
    after Korvin, so Arkon is the one; a line in another channel or one of his own does not
    count."""
    async def main():
        r = korvin_rig(store, clock, tmp_path)
        items = planned(r)
        bridge_line(r, KORVIN, "Korvin", HIS_LINE)
        await r.advance(5)
        arkons = bridge_line(r, 101, "Arkon", "lol he means it")
        await r.advance(3)
        bridge_line(r, 102, "Belra", "following", channel="party")
        his_aside(r, "i put it all in the bank")
        await r.advance(3)
        await r.human("are you drunk?")
        turn, meta = items[0]
        assert (turn.speaker, meta.pushback, meta.pushback_line) == (101, "doubt", arkons)
    run(main())


def test_a_target_who_cannot_answer_means_no_pushback(store, clock, tmp_path, caplog):
    """Korvin spoke last but is AFK: nobody else gets his pushback (not Arkon, who spoke
    before him), and the line is planned as usual. Offline, or named while away: the same."""
    async def main():
        r = korvin_rig(store, clock, tmp_path)
        items = planned(r)
        bridge_line(r, 101, "Arkon", "gz on the bank")
        await r.advance(2)
        bridge_line(r, KORVIN, "Korvin", HIS_LINE)
        r.world.afk.add(KORVIN)
        await r.advance(5)
        first = await r.human("are you drunk?")
        assert items and not any(m.pushback or t.instruction for t, m in items)
        assert KORVIN not in [t.speaker for t, _ in items]
        r.world.afk.discard(KORVIN)
        r.world.online.discard(KORVIN)  # offline: the same
        await r.advance(60)
        n = len(items)
        second = await r.human("you are fixing to get the boot from this guild korvin")
        assert not any(m.pushback for _, m in items[n:])
        assert KORVIN not in [t.speaker for t, _ in items[n:]]
        for row, kind in ((first, "doubt"), (second, "anger")):
            assert (f"reply_pushback_none: line={row.id} kind={kind} reason=cannot_answer"
                    in caplog.text)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())


def test_two_names_or_nobody_mean_no_pushback(store, clock, tmp_path, caplog):
    async def main():
        r = korvin_rig(store, clock, tmp_path)
        items = planned(r)
        alone = await r.human("are you drunk?")  # nobody has spoken yet
        await r.advance(30)
        bridge_line(r, KORVIN, "Korvin", HIS_LINE)
        await r.advance(5)
        n = len(items)
        two = await r.human("korvin and arkon, what are you talking about?")
        assert [t.speaker for t, _ in items[n:]][:2] == [KORVIN, 101]
        assert not any(m.pushback or t.instruction for t, m in items)
        assert f"reply_pushback_none: line={alone.id} kind=doubt reason=nobody" in caplog.text
        assert f"reply_pushback_none: line={two.id} kind=doubt reason=two_names" in caplog.text
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())


def test_a_pushback_whispered_to_him_is_his_to_answer_every_time(store, clock, tmp_path):
    """His whispered "what do you mean?" is answered by the one he whispers, with the doubt
    text and their last whisper to him in the chat shown, even on the roll WHISPER_REPLY_P
    would leave out."""
    async def main():
        r = korvin_rig(store, clock, tmp_path)
        items = planned(r)
        await says_as(r, KORVIN, "the bank is cursed you know", channel="whisper",
                      to_guid=HUMAN)
        await r.advance(20)
        r.engine.rng = _HighRoll(1)
        await r.human("what do you mean?", channel="whisper", to_guid=KORVIN)
        (turn, meta), = items
        assert (turn.speaker, turn.channel, turn.instruction, meta.why) == (
            KORVIN, "whisper", DOUBT, "reply:whisper")
        await r.advance(45, step=2)
        prompt = korvins(r)[0]
        assert "Korvin (whisper to Zan): the bank is cursed" in prompt and DOUBT in prompt
    run(main())


def test_his_quick_pushback_is_answered_inside_the_reply_guard(store, clock, tmp_path):
    """"are you drunk?" 3 s after Korvin's line: the per-person reply guard (REPLY_S) would
    hold Korvin back, and his answer is the point of the pushback."""
    async def main():
        r = korvin_rig(store, clock, tmp_path)
        await says_as(r, KORVIN, HIS_LINE)
        await r.advance(3)
        await r.human("are you drunk?")
        await r.advance(45, step=2)
        assert [s.guid for s in r.sent.says()][:2] == [KORVIN, KORVIN]
        assert not [u for u in r.utterances() if u["guid"] == KORVIN and u["dropped"]]
        # a line of his that pushes back on nobody: the guard keeps its job
        r.engine.guards.record("t-guild", KORVIN, r.clock.now(), TurnKind.OPEN)
        await r.human("korvin you there?")
        await r.advance(30)
        assert [u["dropped"] for u in r.utterances() if u["guid"] == KORVIN][-1] == "guards"
    run(main())


def test_his_own_line_is_shown_however_old(store, clock, tmp_path):
    """Nine lines came after Korvin's (more than the chat's eight): his line is pinned in."""
    async def main():
        r = korvin_rig(store, clock, tmp_path)
        said = await says_as(r, KORVIN, HIS_LINE)
        for i in range(9):
            await r.advance(5)
            his_aside(r, f"note to self {i}")
        await r.human("wtf? korvin, what have you put in the bank - ever?")
        await r.advance(45, step=2)
        prompt = korvins(r)[0]
        assert f"Korvin: {said}" in prompt and "note to self 8" in prompt
        assert prompt.index(f"Korvin: {said}") < prompt.index("note to self 8")
        assert "note to self 0" not in prompt  # the chat shown is still the last eight
    run(main())


def test_his_answer_is_one_line_whatever_his_split_rate(store, clock, tmp_path):
    """Korvin splits 8 lines in 10 into two; his answer to the pushback is never split, and a
    second line the model wrote is dropped. Each answer is its own (one store: a repeat of
    his would be a tell); each has a clause break, and fits one of his lines (12 words)."""
    ordinary = ["honestly it was fine, work was slow today",
                "long shift again, the warehouse was packed",
                "pretty good day, got home early for once",
                "not bad at all, cooked a decent dinner", "kind of rough, the boss kept us late",
                "quiet one really, mostly watched a film"]
    answers = ["sorry i was joking, the bank is full now",
               "my bad man, i meant the flasks are great", "ok fair point, i got that one wrong",
               "nah i was kidding, thanks for the gold", "that was a dumb joke, i take it back",
               "fine i was wrong, the raid is stocked"]

    async def main():
        splits = Counter()
        for seed, line in enumerate(ordinary):
            r = korvin_rig(store, clock, tmp_path, seed=seed, split_rate=0.8,
                           emoticon_rate=0.0, script=lambda m, msgs, line=line: line)
            await r.human("korvin how was your day")  # no pushback: he splits as usual
            await r.advance(45, step=2)
            splits[len([s for s in r.sent.says() if s.guid == KORVIN])] += 1
            await r.engine.end_session()
        assert splits[2] >= 1, splits  # the control: his ordinary answer does split
        for seed, line in enumerate(answers):
            r = korvin_rig(store, clock, tmp_path, seed=seed, split_rate=0.8,
                           emoticon_rate=0.0,
                           script=lambda m, msgs, line=line: f"{line}\nlol so serious man")
            await r.human("what are you talking about korvin?")
            await r.advance(45, step=2)
            said = [s.text for s in r.sent.says() if s.guid == KORVIN]
            assert said == [line], (seed, said)
            await r.engine.end_session()
    run(main())


def test_her_answer_to_a_whispered_pushback_is_one_line_at_the_explicit_band(store, clock,
                                                                             tmp_path):
    """At the explicit band her whisper to him may run to a line or two; her answer to his
    pushback holds one, and its instruction says one short line."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", split_rate=0.0,
                    typo_rate=0.0, emoticon_rate=0.0, abbreviations={}, words_per_line=[3, 12])
        r = make_rig(store, clock, tmp_path,
                     reply="i meant the raid gold, not yours\nnow come and find me after")
        items = planned(r)
        his = await r.human("what do you mean?", channel="whisper", to_guid=102)
        (turn, meta), = items
        assert (turn.speaker, turn.max_lines, meta.pushback) == (102, 1, "doubt")
        await r.advance(45, step=2)
        user = r.chat_calls()[0]["messages"][1]["content"]
        assert DOUBT in user and "in one short line" in user and "a line or two" not in user
        assert [s.text for s in r.sent.says()] == ["i meant the raid gold, not yours"]
        msgs, _ = await r.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102,
                                              channel="whisper", to_guid=HUMAN,
                                              addressee=HUMAN, trigger_line=his.id))
        assert "in a line or two" in msgs[1]["content"]  # her ordinary whisper back, as before
    run(main())


def test_his_pushback_that_asks_the_past_is_still_its_targets_first(store, clock, tmp_path):
    """"how long has the potion feud been going??" just after Korvin's line about it: one who
    remembers the feud would answer first as a keeper of the guild's past (engine._plan_lore),
    but it pushes back on Korvin, so Korvin does, and the keeper's plan stays out of it as it
    does for a line that names someone."""
    async def main():
        t = int(clock.now()) - 400 * 86400
        store.insert_memory(make_mem(owner=104, text="Belra and I have had a potion feud since "
                                     "the cauldron fire in 2008.", actors=(HUMAN, 104),
                                     t_event=t, t_created=t, t_accessed=t, source="owner",
                                     evidence=("owner:G3",)))
        r = korvin_rig(store, clock, tmp_path)
        items = planned(r)
        bridge_line(r, KORVIN, "Korvin", "the potion feud is why the bank is empty")
        await r.advance(5)
        await r.human("how long has the potion feud been going??")
        assert (items[0][0].speaker, items[0][1].pushback) == (KORVIN, "doubt")
        assert not any(m.lore for _, m in items)
    run(main())


def test_his_nothing_stands(store, clock, tmp_path):
    """Silence from the one he pushes back on is accepted: no retry."""
    async def main():
        r = korvin_rig(store, clock, tmp_path, script=lambda m, msgs: "(nothing)")
        await r.human("you are fixing to get the boot from this guild korvin")
        await r.advance(45, step=2)
        assert len(korvins(r)) == 1 and r.sent.says() == []
        assert [u["dropped"] for u in r.utterances()] == ["silent"]
    run(main())
