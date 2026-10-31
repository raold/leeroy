import dataclasses
import logging
import random
import re

import pytest

from conftest import make_mem, make_persona
from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.recall import Recall
from leeroy_mind.personas.card import render_core_card
from leeroy_mind.storage.rows import ChatLineRow, PersonaStateRow, Relationship
from leeroy_mind.talk.budget import DayPhase, day_phase
from leeroy_mind.talk.prompt import (COMMS_RULES, EXTRA_CAP_TOKENS, INNER_FAMILIARITY,
                                     INSTRUCTIONS, knows_real_name,
                                     PromptParts, build_messages, redact_for,
                                     chat_line_text, comms_rules, is_close, mood_line,
                                     quest_help, real_name_line, relationship_card,
                                     render_instruction, render_messages, select_samples,
                                     trim_to_budget)
from leeroy_mind.talk.states import Altered
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import estimate_tokens

# Invented identity: the real name lives only in the owner's local mind.toml.
GM = HumanIdentity(guid=1, character_name="Zanthor", nickname="Zan", real_name="Quillon",
                   is_guild_master=True)
HE, SHE = ("he", "him", "his"), ("she", "her", "her")


def tokens(msgs) -> int:
    return sum(estimate_tokens(m["content"]) for m in msgs)


def parts(**kw) -> PromptParts:
    base = dict(
        card="You are Tarvo, 34, UK. You play Arkon, an Orc Protection Warrior (tank, raider).",
        samples=["ok pulling in 5", "lol no"], rules=comms_rules(GM),
        rel_cards=["Zan (the guild master): you're close, you know him well"],
        memories=["- last night: Zan wiped us on Marrowgar twice."],
        mood="It's evening.", chat=["Zan: anyone seen my hearthstone"],
        own_recent=["ok pulling in 5"],
        instruction=render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan"))
    base.update(kw)
    return PromptParts(**base)


def big_parts(**kw) -> PromptParts:
    base = dict(
        card="C" * 600, samples=[f"sample {i} " + "s" * 50 for i in range(5)],
        rules=comms_rules(GM), rel_cards=[f"R{i}: " + "r" * 200 for i in range(4)],
        memories=[f"- M{i}: " + "m" * 190 for i in range(10)], mood="It's evening.",
        chat=[f"P{i:02}: " + "x" * 240 for i in range(20)],
        own_recent=[f"O{i} " + "o" * 240 for i in range(8)], instruction="INSTRUCTION LAST")
    base.update(kw)
    return parts(**base)


def test_static_prefix_identical_across_turns():
    a = build_messages(parts(memories=["- last night: x"], instruction="A"))
    b = build_messages(parts(memories=["- 3 days ago: y", "- earlier today: z"], instruction="B",
                             chat=["Belra: gz", "Zan: ty"], mood="It's late at night."))
    assert [m["role"] for m in a] == ["system", "user"]
    assert a[0] == b[0]
    assert a[1] != b[1]


def test_message_layout_in_order():
    p = parts()
    sys_, user = (m["content"] for m in build_messages(p))
    # prompts minor 7: the header says they are examples, not lines to reuse
    assert sys_.index(p.card) < sys_.index(
        "How you type (examples of your style; don't repeat them):") \
        < sys_.index("ok pulling in 5") < sys_.index("lol no") < sys_.index(p.rules)
    heads = ["People here:", "You remember:", "Right now:", "Chat:",
             "Your last lines (don't reuse their wording):"]
    at = [user.index(h) for h in heads]
    assert at == sorted(at)
    assert user.index("Zan (the guild master)") < at[1] < user.index("Marrowgar") < at[2]
    assert user.endswith(p.instruction)
    bare = build_messages(parts(memories=[], own_recent=[], rel_cards=[]))[1]["content"]
    assert "You remember:" not in bare and "Your last lines" not in bare
    assert "People here:" not in bare and "Chat:" in bare


def test_budget_trims_in_order():
    p = big_parts()
    msgs = build_messages(p)
    assert tokens(msgs) <= 1300
    t = trim_to_budget(p, budget=1300)
    assert msgs == render_messages(t)
    assert len(t.memories) <= 3 and len(t.chat) <= 5 and len(t.rel_cards) <= 1
    assert t.memories == p.memories[:len(t.memories)]  # best first: the tail goes
    assert t.rel_cards == p.rel_cards[:len(t.rel_cards)]  # the addressee's card stays
    assert t.chat and t.chat[-1] == p.chat[-1]  # the newest line stays
    assert t.own_recent == p.own_recent[len(p.own_recent) - len(t.own_recent):]
    assert msgs[1]["content"].endswith("INSTRUCTION LAST")


def test_trim_order_step_by_step():
    p = big_parts()
    full = tokens(render_messages(p))
    assert trim_to_budget(p, budget=full) == p
    one = trim_to_budget(p, budget=full - 1)  # the least relevant memory goes first
    assert one.memories == p.memories[:9] and one.chat == p.chat
    prev = None
    for budget in range(full, 200, -20):
        t = trim_to_budget(p, budget=budget)
        n = (len(t.memories), len(t.chat), len(t.rel_cards), len(t.own_recent))
        newest = p.chat[len(p.chat) - len(t.chat):]
        assert all(new.startswith(kept) for kept, new in zip(t.chat, newest))  # suffix, maybe cut
        shortened = t.chat != newest
        if n[1] < 20:
            assert n[0] <= 3
        if n[2] < 4:
            assert n[0] <= 3 and n[1] <= 5
        if n[3] < 8:
            assert n[2] <= 1
        if shortened:
            assert n[3] <= 3
        assert n[1] >= 1  # never the line being answered
        assert (t.card, t.samples, t.rules, t.instruction) == (p.card, p.samples, p.rules,
                                                               p.instruction)
        if prev is not None:
            assert all(a <= b for a, b in zip(n, prev))
        prev = n
        # within budget, unless nothing but the newest chat line is left to give up
        assert tokens(render_messages(t)) <= budget or n == (0, 1, 0, 0)


def test_samples_approved_first_and_stable():
    lines = [{"text": "a", "approved": False}, {"text": "b", "approved": True},
             {"text": "c", "approved": False}, {"text": "d", "approved": True},
             {"text": "e", "approved": False}, {"text": "f", "approved": False}]
    p = make_persona(101, "Arkon", sample_lines=lines)
    assert select_samples(p) == ["b", "d", "a", "c", "e"]
    assert select_samples(p) == select_samples(p)
    assert select_samples(p, n=2) == ["b", "d"]
    assert select_samples(make_persona(101, "Arkon", sample_lines=[])) == []


def test_relationship_card_words():
    def r(**kw):
        return Relationship(a=101, b=102, **kw)
    assert "close" in relationship_card("Belra", r(affinity=70, familiarity=60), is_human=False)
    assert "annoyed by" in relationship_card("Belra", r(affinity=-40, familiarity=60),
                                             is_human=False)
    assert "barely know" in relationship_card("Belra", r(familiarity=10), is_human=False)
    assert "(the guild master)" in relationship_card("Zan", r(affinity=50), is_human=True,
                                                     is_guild_master=True)
    assert relationship_card("Zan", r(familiarity=10), is_human=True, is_guild_master=True) \
        .startswith("Zan (the guild master): ")
    # the tag follows his rank, not just being the human
    assert relationship_card("Zan", r(familiarity=10), is_human=True).startswith("Zan: ")
    assert "him" in relationship_card("Zan", r(familiarity=10), is_human=True, forms=HE)
    assert "barely know them" in relationship_card("Zan", r(familiarity=10), is_human=True)
    assert "(the guild master)" not in relationship_card("Belra", r(), is_human=False,
                                                         is_guild_master=True)
    # no row is no history (prompts I6), not a stranger
    assert relationship_card("Belra", None, is_human=False) == (
        "Belra: a guildmate, you know them from raids")
    neutral = relationship_card("Belra", r(familiarity=60, notes="always late " * 30),
                                is_human=False)
    assert "close" not in neutral and "annoyed" not in neutral and "always late" in neutral
    assert len(neutral) < 220
    assert "can't stand" in relationship_card("Belra", r(affinity=-80, familiarity=60),
                                              is_human=False)


def test_comms_rules():
    assert "{human}" in COMMS_RULES
    rules = comms_rules(GM)
    assert COMMS_RULES.format(human="Zan") in rules
    assert "Zan (character Zanthor), the guild master" in rules
    assert "guilt-trip Zan" in rules and "(nothing)" in rules and "2009" in rules
    assert "Quillon" not in rules
    assert 100 <= estimate_tokens(rules) <= 200
    assert not any(c in rules for c in ";—–")


def test_rules_tell_the_model_this_speakers_length():
    """Prompts C1: the model was never told how long this person's lines are."""
    def rules(wpl, split):
        return comms_rules(GM, make_persona(101, "Arkon", words_per_line=list(wpl),
                                            split_rate=split))
    terse = rules((1, 4), 0.1)
    assert "usually 1-4 words" in terse and "now and then two" in terse
    assert "One short line" not in terse
    assert "usually 8-25 words" in rules((8, 25), 0.1)
    assert "now and then two" not in rules((3, 12), 0.0) and "usually 3-12 words" in rules(
        (3, 12), 0.0)
    assert "two lines" in rules((3, 12), 0.6)
    for r in (terse, rules((3, 12), 0.6), comms_rules(GM)):
        assert "no name in front" in r and not any(c in r for c in ";—–")
    # static per speaker: the system message is the same on every turn
    assert rules((1, 4), 0.1) == terse


def test_live_prompt_carries_his_pronouns():
    he = dataclasses.replace(GM, pronouns="he")
    rules = comms_rules(he)
    assert "Zan (character Zanthor), the guild master, he/him" in rules
    assert "he/him" in build_messages(parts(rules=rules))[0]["content"]
    assert "he/him" not in comms_rules(GM)  # unset: nothing is assumed


def test_his_real_name_needs_the_inner_circle():
    """Final fix wave ruling: being close (a friend, affinity 40) is not enough. Only a member
    who knows him very well (familiarity INNER_FAMILIARITY or more) is told his real name, and
    still only now and then; every other close friend is redacted like anyone else."""
    assert INNER_FAMILIARITY == 80
    friend = Relationship(a=101, b=1, affinity=40, trust=40, familiarity=60)
    inner = Relationship(a=101, b=1, affinity=40, trust=40, familiarity=85)
    lover = Relationship(a=101, b=1, romance_stage="acknowledged", familiarity=70)
    assert is_close(friend) and is_close(lover) and is_close(inner)
    assert not knows_real_name(friend) and not knows_real_name(lover)
    assert knows_real_name(inner)
    assert not knows_real_name(Relationship(a=101, b=1, familiarity=95))  # known, not close
    rng = random.Random(5)
    assert not any(real_name_line(GM, friend, rng) for _ in range(2000))
    got = [real_name_line(GM, inner, rng) for _ in range(2000)]
    assert 0.05 <= sum(bool(x) for x in got) / 2000 <= 0.25


def test_cards_read_as_people_not_as_schema():
    """Prompts I6 and minor 13: a guildmate with no row is an ordinary guildmate, not someone
    "you barely know"; a label reads as how the two get on."""
    def r(**kw):
        return Relationship(a=101, b=102, **kw)
    plain = "Belra: a guildmate, you know them from raids"
    assert relationship_card("Belra", r(), is_human=False) == plain
    assert relationship_card("Belra", None, is_human=False) == plain
    clash = relationship_card("Belra", r(affinity=-20, familiarity=50, label="friction",
                                         notes="she undercuts everyone on the AH"),
                              is_human=False)
    assert clash.startswith("Belra: you two clash, you're annoyed by them")
    assert "(friction)" not in clash and "undercuts everyone" in clash
    pals = relationship_card("Belra", r(affinity=40, trust=40, familiarity=60, label="friend"),
                             is_human=False)
    assert pals.startswith("Belra: you're friends, you're close") and "(friend)" not in pals
    assert "barely know them" in relationship_card("Belra", r(familiarity=10), is_human=False)


def test_his_card_shows_her_attitude_and_her_note():
    """Prompts I4: every woman's card for him read "you like him, you know him a bit": on the
    turns her romance text skips, nothing said she wants him. The card carries her attitude
    (the attraction band, from her row or else her stance) and her own note toward him."""
    seeded = Relationship(a=201, b=1, affinity=30, familiarity=30)

    def card(r=seeded, attitude="", forms=HE):
        return relationship_card("Zan", r, is_human=True, is_guild_master=True, forms=forms,
                                 attitude=attitude)
    assert "you want him" in card(attitude="lust")
    assert "you're into him" in card(attitude="drawn")
    assert "you're fond of him" in card(attitude="fond")
    assert "want" not in card() and "into" not in card() and "fond" not in card()
    cooled = Relationship(a=201, b=1, affinity=30, familiarity=30, attraction=45)
    # P1c pre-flight PR21: her stance when she has one, her row's score bands only otherwise
    assert "you want him" in card(cooled, attitude="lust")
    assert "you're into him" in card(cooled)
    note = "he carried my alt through every heroic and never once made a fuss about it"
    with_note = card(Relationship(a=201, b=1, affinity=40, familiarity=60,
                                  notes=note + " " + "x" * 40), attitude="drawn")
    assert note in with_note and len(with_note) < 260
    wrong = card(Relationship(a=201, b=1, affinity=40, familiarity=60,
                              notes="she always knows the strat before anyone"), attitude="fond")
    assert "always knows" not in wrong  # a note that gets his pronouns wrong is left out
    she = card(Relationship(a=201, b=1, notes="she always knows the strat before anyone"),
               forms=SHE)
    assert "always knows" in she


def test_her_own_she_never_drops_her_note_about_him():
    """Rehearsal 2 (judge C 10): a note to him with "she" in it was dropped as a pronoun slip,
    when the she was the woman herself ("Competitive rivalry, but she's drawn to him"). Only
    a note that refers to him with the other pronouns is left out."""
    def card(notes, *, forms=HE, speaker="female"):
        return relationship_card("Zan", Relationship(a=201, b=1, affinity=40, familiarity=60,
                                                     notes=notes),
                                 is_human=True, forms=forms, attitude="drawn", speaker=speaker)
    for note in ("Competitive rivalry, but she's drawn to him",
                 "Drawn to Zan; sees him as the lead in her play",
                 "She lights up when Zan logs on and pretends she doesn't"):
        assert note.rstrip(".") in card(note), note
    slip = "Respects her leadership"  # him as "her": a slip
    assert "leadership" not in card(slip) and "leadership" not in card(slip, speaker="male")
    assert "always knows" not in card("she always knows the strat", speaker="male")
    mirror = "Thinks he's the best healer she knows"  # a man's note to a she/her GM
    assert "best healer" in card(mirror, forms=SHE, speaker="male")
    assert "best healer" not in card("He always knows the strat", forms=SHE, speaker="female")


def test_real_name_only_for_close_members_and_rarely():
    def share(r, human=GM, n=2000):
        rng = random.Random(7)
        got = [real_name_line(human, r, rng) for _ in range(n)]
        hits = [x for x in got if x]
        return len(hits) / n, hits

    far = Relationship(a=101, b=1, affinity=10, familiarity=20, romance_stage="notices")
    by_affinity = Relationship(a=101, b=1, affinity=40, familiarity=85)
    by_stage = Relationship(a=101, b=1, affinity=0, romance_stage="banter", familiarity=85)
    ex = Relationship(a=101, b=1, affinity=0, romance_stage="ex", familiarity=85)
    assert not is_close(far) and not is_close(ex) and not is_close(None)
    assert is_close(by_affinity) and is_close(by_stage)
    assert share(far)[0] == 0 and share(ex)[0] == 0
    assert share(Relationship(a=101, b=1, affinity=39.9, familiarity=85))[0] == 0
    for r in (by_affinity, by_stage):
        s, hits = share(r)
        assert 0.05 <= s <= 0.25  # occasional, not every close prompt
        assert all("Quillon" in x and "Zan" in x and "once in a while" in x for x in hits)
    no_name = HumanIdentity(1, "Zanthor", "Zan", "", True)
    assert share(by_affinity, human=no_name)[0] == 0
    a, b = random.Random(3), random.Random(3)
    assert [real_name_line(GM, by_affinity, a) for _ in range(50)] \
        == [real_name_line(GM, by_affinity, b) for _ in range(50)]


def test_real_name_line_rides_in_the_user_message_only():
    rng = random.Random(1)
    close = Relationship(a=101, b=1, affinity=60, familiarity=85)
    line = next(x for x in (real_name_line(GM, close, rng) for _ in range(200)) if x)
    sys_, user = (m["content"] for m in build_messages(parts(real_name=line)))
    assert "Quillon" not in sys_
    assert user.index("People here:") < user.index("Quillon") < user.index("You remember:")
    assert "Quillon" not in str(build_messages(parts()))
    assert trim_to_budget(big_parts(real_name=line), budget=1300).real_name == line
    alone = build_messages(parts(rel_cards=[], real_name=line))[1]["content"]
    assert alone.startswith("People here:")


def test_instructions_cover_every_turn_kind():
    assert set(INSTRUCTIONS) == set(TurnKind)
    for kind in TurnKind:
        text = render_instruction(kind, addressee="Zan", topic="Lord Marrowgar",
                                  extra="EXTRA BIT.")
        assert "{" not in text and "}" not in text and text.endswith("EXTRA BIT.")
        assert not any(c in text for c in ";—–")
        bare = render_instruction(kind, addressee="Zan")
        assert bare == bare.strip() and "  " not in bare and "{" not in bare
    for kind in (TurnKind.OPEN, TurnKind.CONTINUE, TurnKind.WIPE_REACT, TurnKind.ASIDE):
        assert "(nothing)" in render_instruction(kind, addressee="Zan")
    assert "make sense on its own" in render_instruction(TurnKind.OPEN)
    assert render_instruction(TurnKind.OPEN).startswith("Chat has gone quiet.")
    assert "quiet" not in render_instruction(TurnKind.OPEN, quiet=False)
    assert "actually said" in render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan")
    assert "Lord Marrowgar" in render_instruction(TurnKind.KILL, topic="Lord Marrowgar")
    assert "the boss" in render_instruction(TurnKind.KILL)
    with pytest.raises(ValueError):
        render_instruction(TurnKind.REPLY_HUMAN)  # who is he answering?


def test_a_lead_comes_before_the_turns_instruction():
    """Prompts I5: the romance text read last, so a straight answer to his question came out
    as innuendo first. It leads now; the turn's own instruction is what the model reads last."""
    text = render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", forms=HE,
                              lead="ROMANCE FIRST.", extra="EXTRA LAST.")
    assert text.startswith("ROMANCE FIRST. Answer Zan's last line.")
    assert text.endswith("EXTRA LAST.")
    assert render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan") == render_instruction(
        TurnKind.REPLY_HUMAN, addressee="Zan", lead="")


def test_instruction_extra_is_capped_and_logged(caplog):
    huge = "PRIVATEWORD " * 2000
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        text = render_instruction(TurnKind.FLIRT, addressee="Zan", extra=huge)
    base = render_instruction(TurnKind.FLIRT, addressee="Zan")
    assert estimate_tokens(text) <= estimate_tokens(base) + EXTRA_CAP_TOKENS + 1
    assert "capped" in caplog.text and "PRIVATEWORD" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        render_instruction(TurnKind.FLIRT, addressee="Zan", extra="a short overlay line.")
    assert caplog.text == ""


def test_last_resort_trim_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        trim_to_budget(big_parts(), budget=1300)
    assert "last-resort" not in caplog.text
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        t = trim_to_budget(big_parts(), budget=700)
    assert "last-resort" in caplog.text and len(t.chat) >= 1


def test_non_close_speakers_never_see_his_real_name():
    rng = random.Random(1)
    line = next(x for x in (real_name_line(GM, Relationship(a=101, b=1, affinity=60,
                                                             familiarity=85), rng)
                            for _ in range(200)) if x)
    leaky = parts(
        chat=["Belra: Quillon you coming tonight?", "Zan: yeah in 5"],
        rel_cards=[relationship_card("Belra", Relationship(a=105, b=102, familiarity=50,
                                                         notes="Quillon lent her gold"),
                                     is_human=False)],
        memories=["- last night: Quillon carried the raid on Marrowgar."],
        own_recent=["ok Quillon"], real_name=line,
        instruction=render_instruction(TurnKind.CONTINUE, addressee="Belra"))
    assert "Quillon" in str(build_messages(leaky))  # positive control
    far = redact_for(leaky, GM, close=False)
    msgs = build_messages(far)
    assert "Quillon" not in str(msgs)
    user = msgs[1]["content"]
    assert "Belra: Zan you coming tonight?" in user and "Zan lent her gold" in user
    assert "- last night: Zan carried the raid" in user
    assert far.real_name == ""
    # a close speaker's prompt is left alone; for anyone else the Title and UPPER forms go
    assert redact_for(leaky, GM, close=True) == leaky
    assert redact_for(parts(chat=["Belra: Quillon and QUILLON"]), GM, close=False).chat \
        == ["Belra: Zan and Zan"]
    # a name that is also a word: the lower-case word survives
    bill = dataclasses.replace(GM, real_name="Bill")
    assert redact_for(parts(chat=["Belra: who pays the bill, Bill?"]), bill, close=False).chat \
        == ["Belra: who pays the bill, Zan?"]


def test_mood_line():
    assert mood_line(PersonaStateRow(guid=101), Altered.SOBER, None, DayPhase.EVENING) \
        == "It's evening."
    st = PersonaStateRow(guid=101, mood=0.7, life_thread="saving for a new car")
    late = mood_line(st, Altered.DRUNK, "alcohol", DayPhase.LATE, kind=TurnKind.OPEN)
    assert "late" in late and "great mood" in late and "drunk" in late
    assert "saving for a new car" in late
    sour = mood_line(PersonaStateRow(guid=101, mood=-0.8), Altered.HUNGOVER, "alcohol",
                     DayPhase.DAY)
    assert "bad mood" in sour and "hungover" in sour
    smoker = PersonaStateRow(guid=101)
    assert "smok" in mood_line(smoker, Altered.SOBER, "tobacco", DayPhase.DEAD,
                               kind=TurnKind.OPEN)


def test_a_morning_prompt_says_morning():
    """Prompts minor 10: the small hours ran to 10:00, so a 09:00 prompt said "It's the small
    hours." """
    line = mood_line(PersonaStateRow(guid=101), Altered.SOBER, None, DayPhase.MORNING)
    assert line == "It's morning."
    assert "small hours" in mood_line(PersonaStateRow(guid=101), Altered.SOBER, None,
                                      DayPhase.DEAD)


def test_an_opener_has_her_own_life_and_her_evening_to_bring_up():
    """Prompts I9 and spec I3: persona_state.life_thread is empty in P1b, so "On your mind
    lately" never showed. The persona's own life thread fills in, and what she has been up to
    tonight comes along; only on openers."""
    st = PersonaStateRow(guid=101)
    line = mood_line(st, Altered.SOBER, None, DayPhase.EVENING, kind=TurnKind.OPEN,
                     life_thread="learning to cook", lately="you won Bryntroll")
    assert "On your mind lately: learning to cook." in line
    assert "Tonight so far: you won Bryntroll." in line
    lived = PersonaStateRow(guid=101, life_thread="moving house next week")
    assert "moving house" in mood_line(lived, Altered.SOBER, None, DayPhase.EVENING,
                                       kind=TurnKind.OPEN, life_thread="learning to cook")
    for kind in (TurnKind.REPLY_HUMAN, TurnKind.ASIDE, TurnKind.KILL):
        other = mood_line(st, Altered.SOBER, None, DayPhase.EVENING, kind=kind,
                          life_thread="learning to cook", lately="you won Bryntroll")
        assert "cook" not in other and "Bryntroll" not in other


def test_mood_line_keeps_idle_detail_to_idle_turns():
    st = PersonaStateRow(guid=101, life_thread="saving for a new car")
    assert "saving for a new car" in mood_line(st, Altered.SOBER, None, DayPhase.EVENING,
                                               kind=TurnKind.OPEN)
    assert "smok" in mood_line(st, Altered.SOBER, "tobacco", DayPhase.EVENING, kind=TurnKind.OPEN)
    # an aside answers a flirt: no life story there (prompts lens, minor 8)
    for kind in (None, TurnKind.REPLY_HUMAN, TurnKind.KILL, TurnKind.GREET, TurnKind.ASIDE):
        line = mood_line(st, Altered.SOBER, "tobacco", DayPhase.EVENING, kind=kind)
        assert "saving" not in line and "smok" not in line
    # raid calls carry no altered state at all
    for kind in (TurnKind.CALLOUT, TurnKind.URGENT):
        line = mood_line(st, Altered.DRUNK, "alcohol", DayPhase.LATE, kind=kind)
        assert "drunk" not in line and "late" in line
    assert "drunk" in mood_line(st, Altered.DRUNK, "alcohol", DayPhase.LATE,
                                kind=TurnKind.REPLY_HUMAN)


def chat_row(**kw) -> ChatLineRow:
    base = dict(id=1, ts=0.0, channel="guild", from_guid=1, from_name="Zanthor", to_guid=0,
                text="anyone seen my |cffffffff|Hitem:6948:0:0:0:0:0:0:0:80|h[Hearthstone]|h|r",
                map=0, zone=0, session_id=None, thread_id=None, origin="game")
    base.update(kw)
    return ChatLineRow(**base)


def test_chat_line_text():
    names = {1: "Zan", 101: "Arkon"}
    assert chat_line_text(chat_row(), names) == "Zan: anyone seen my [Hearthstone]"
    assert chat_line_text(chat_row(from_guid=102, from_name="Belra", channel="raid",
                                   text="on  it"), names) == "Belra (raid): on it"
    assert chat_line_text(chat_row(channel="whisper", to_guid=101, text="hey"), names) \
        == "Zan (whisper to Arkon): hey"


def test_chat_lines_carry_their_age():
    """Prompts I2 and the final I3 ruling: a line from an hour ago never reads as current."""
    names = {1: "Zan", 102: "Belra"}
    row = chat_row(ts=1000.0, text="gz", from_guid=102, from_name="Belra", channel="raid")
    assert chat_line_text(row, names, now=1030.0) == "[just now] Belra (raid): gz"
    assert chat_line_text(row, names, now=1000.0 + 3 * 60 + 20) == "[3m ago] Belra (raid): gz"
    assert chat_line_text(row, names, now=1000.0 + 59 * 60) == "[59m ago] Belra (raid): gz"
    assert chat_line_text(row, names, now=1000.0 + 2 * 3600 + 5) == "[2h ago] Belra (raid): gz"
    assert chat_line_text(row, names, now=1000.0 + 3 * 86400) == "[3d ago] Belra (raid): gz"
    assert chat_line_text(row, names) == "Belra (raid): gz"  # no time given, no age


def test_a_greeting_is_never_asked_about_last_time():
    """Prompts minor 1 and the rehearsal: every greeter was told to pick up "something from
    last time" and the model made a history up. Only the follow-up instruction, given to one
    greeter with a memory of him, asks for it."""
    plain = render_instruction(TurnKind.GREET, addressee="Zan", forms=HE)
    assert "last time" not in plain and "remember" not in plain and "Greet him" in plain


def test_example_prompt_from_fixtures(clock):
    """A whole live prompt from fixtures; memories come from Recall.format, so their times
    follow the mind clock's zone."""
    p = make_persona(101, "Arkon", sample_lines=[
        {"text": "ok pulling in 5", "approved": True},
        {"text": "who keeps standing in the fire lol", "approved": True},
        {"text": "brb tea", "approved": False}])
    card = render_core_card(p, race="Orc", class_name="Warrior")
    recall = Recall(None, None, clock, tau_days=5)
    now = int(clock.now())
    memories = [recall.format(make_mem(owner=101, t_event=now - 2 * 86400, text=(
        "Zan pulled Marrowgar before the healers were in and blamed the lag."))),
        recall.format(make_mem(owner=101, t_event=now - 1800, confidence=0.6, text=(
            "Belra said she is selling her spare frost orbs.")))]
    names = {1: "Zan", 101: "Arkon", 102: "Belra"}
    chat = [chat_line_text(chat_row(id=i, from_guid=g, from_name=n, text=t), names)
            for i, (g, n, t) in enumerate([
                (102, "Belra", "who wants frost orbs, cheap"),
                (1, "Zanthor", "arkon you tanking tonight or what")])]
    rel = [relationship_card("Zan", Relationship(a=101, b=1, affinity=45, familiarity=70,
                                                 trust=75), is_human=True,
                             is_guild_master=True),
           relationship_card("Belra", Relationship(a=101, b=102, affinity=-20, familiarity=50,
                                                   label="friction",
                                                   notes="she undercuts everyone on the AH"),
                             is_human=False)]
    rng = random.Random(0)
    real = next(x for x in (real_name_line(GM, Relationship(a=101, b=1, affinity=45,
                                                             familiarity=85), rng)
                            for _ in range(100)) if x)
    pp = PromptParts(card=card, samples=select_samples(p), rules=comms_rules(GM),
                     rel_cards=rel, memories=memories,
                     mood=mood_line(PersonaStateRow(guid=101, mood=0.3), Altered.TIPSY,
                                    "alcohol", day_phase(clock.local())),
                     chat=chat, own_recent=["ok pulling in 5"],
                     instruction=render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan"),
                     real_name=real)
    msgs = build_messages(pp)
    assert tokens(msgs) <= 1300
    user = msgs[1]["content"]
    assert "- two days ago: Zan pulled Marrowgar" in user
    assert "- 30 minutes ago: Belra said" in user and "(heard)" in user
    assert "Zan: arkon you tanking tonight or what" in user
    assert "Quillon" in user and "Quillon" not in msgs[0]["content"]


def test_quest_help_is_no_standing_rule():
    """Rehearsal 2 (judge B 3): QUEST_HELP in every woman's standing rules primed a quest in
    every greeting. It ends her reply to his quest line instead (pipeline), and the rules carry
    no quest for anyone."""
    he = dataclasses.replace(GM, pronouns="he")
    woman = make_persona(201, "Belra", player_gender="female")
    man = make_persona(101, "Arkon")
    assert "quest" not in comms_rules(he, woman) and "quest" not in comms_rules(he, man)
    # prompts I7: eager when he brings a quest up, and no claims about what he has done
    text = quest_help(he)
    assert "Zan brought up a quest, and you jump at it" in text and "help him" in text
    assert "has and hasn't done" not in text and "if he told you" in text
    assert text.find("offer to help") < text.find("You only know")
    assert not any(c in text for c in ";—–") and "Quillon" not in text
    she = quest_help(dataclasses.replace(GM, pronouns="she"))
    assert "help her" in she and "him" not in she


def test_every_speakers_rules_keep_flirting_on_him():
    """Prompts I10: "romance only with him" lived only in the romance text, on turns to him.
    It is a standing rule now, for women and for men."""
    he = dataclasses.replace(GM, pronouns="he")
    women = comms_rules(he, make_persona(201, "Belra", player_gender="female"))
    men = comms_rules(he, make_persona(101, "Arkon"))
    assert ("- You flirt only with Zan. Everyone else is a friend, a rival or just a guildie."
            in women)
    assert ("- You don't flirt with anyone in the guild. The women only have eyes for Zan "
            "anyway." in men)
    assert "flirt" not in comms_rules(he)  # no persona, no such line
    assert not any(c in women + men for c in ";—–")


def test_the_aside_template_leaves_the_mode_to_the_aside():
    """Prompts minor 2: "Tease, never cruel" sat over every mode (sulker, grumbler,
    commentator, good sport)."""
    text = render_instruction(TurnKind.ASIDE, extra="MODE.")
    assert "Tease" not in text and "(nothing)" in text and text.endswith("MODE.")


def test_templates_follow_his_pronouns():
    def say(kind, forms=None):
        kw = {} if forms is None else {"forms": forms}
        return render_instruction(kind, addressee="Zan", **kw)
    assert "what he actually said" in say(TurnKind.REPLY_HUMAN, HE)
    assert "what she actually said" in say(TurnKind.REPLY_HUMAN, SHE)
    assert "what they actually said" in say(TurnKind.REPLY_HUMAN)  # unset: nothing assumed
    assert "Greet him" in say(TurnKind.GREET, HE) and "Greet them" in say(TurnKind.GREET)
    assert "whisper her" in say(TurnKind.WHISPER, SHE)
    assert "whisper them" in say(TurnKind.WHISPER)
    # spec minor 4: the gz template hard-coded "them" for a he/him guild master
    assert "Congratulate him" in say(TurnKind.GZ, HE) and "Congratulate her" in say(
        TurnKind.GZ, SHE)
    for kind in TurnKind:  # no template assumes him any more
        assert not re.search(r"\b(he|him|his)\b", say(kind)), kind
    flirt = say(TurnKind.FLIRT, HE)
    assert "warm and playful" not in flirt and "him" in flirt  # the attitude sets the tone
    assert "barely know her" in relationship_card("Zan", Relationship(a=101, b=1),
                                                  is_human=True, forms=SHE)


def test_real_name_line_follows_his_pronouns():
    close = Relationship(a=101, b=1, affinity=60, familiarity=85)

    def line(human):
        rng = random.Random(1)
        return next(x for x in (real_name_line(human, close, rng) for _ in range(300)) if x)
    assert "know his real name" in line(dataclasses.replace(GM, pronouns="he"))
    assert "know her real name" in line(dataclasses.replace(GM, pronouns="she"))
    assert "know their real name" in line(GM)
