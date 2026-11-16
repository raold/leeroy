"""The prompt as a situation (human-likeness core, Task 6): the hard rules in the system message,
"Right now:" next to the instruction, situation templates that take their length from the caller.
Pure prompt functions; nothing calls the situation paths until the pipeline wiring (Task 8)."""
import dataclasses
from dataclasses import replace

import pytest

from conftest import make_persona
from leeroy_mind.prompts import trace_templates
from leeroy_mind.storage.rows import PersonaStateRow
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.prompt import (DM_BUDGET, INSTRUCTIONS, OPEN_TO_HIM_SITUATION,
                                     SITUATION_INSTRUCTIONS, SITUATION_RULES, PromptParts,
                                     comms_rules, mood_line, redact_for, render_instruction,
                                     render_messages, section_tokens, situation_rules,
                                     trim_to_budget)
from leeroy_mind.talk.states import Altered
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import estimate_tokens
from test_prompt import GM, HE

PARTS = PromptParts(card="CARD", samples=["s"], rules="RULES", rel_cards=["Zan: x"], memories=["- m"],
                    mood="It's Thursday evening.", chat=["Zan: hi"], own_recent=["yo"],
                    instruction="INSTR", awareness="")
TELLS = ";—–"  # prompt text must not model repairable tells (filters.py)


def test_default_render_is_unchanged():
    user = render_messages(PARTS)[1]["content"]
    assert user.index("Right now:") < user.index("Chat:")


def test_situation_moves_right_now_before_the_instruction():
    user = render_messages(replace(PARTS, situation=True))[1]["content"]
    assert user.index("Your last lines") < user.index("Right now:") < user.index("INSTR")
    assert user.endswith("INSTR")


def test_situation_instruction_ends_with_length_then_extra():
    text = render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", extra="X", situation=True,
                              length_text="One short line, about 6 words.", forms=("he", "him", "his"))
    assert text == "Reply to Zan's last line, as yourself. One short line, about 6 words. X"


def test_shape_length_replaces_the_game_length():
    text = render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", length_text="Just a word or two this time.")
    assert "one short line" not in text and "Just a word or two this time." in text


def test_situation_on_a_game_channel_needs_a_length():
    with pytest.raises(ValueError):
        render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", situation=True)


def test_state_words_replace_mood_words():
    happy = PersonaStateRow(guid=101, mood=0.8)
    plain = mood_line(happy, Altered.SOBER, None, DayPhase.EVENING)
    assert "You're in a great mood." in plain
    line = mood_line(happy, Altered.SOBER, None, DayPhase.EVENING,
                     state_words="You're tired today.", where="You're at home on the sofa.")
    assert "You're tired today." in line and "great mood" not in line
    assert line == "It's evening. You're at home on the sofa. You're tired today."
    today = "It's Thursday evening."
    dated = mood_line(happy, Altered.DRUNK, "alcohol", DayPhase.EVENING, today=today,
                      where="You're at the pub.")
    assert dated.startswith(today + " You're at the pub. You're in a great mood.")
    assert "drunk" in dated
    # with neither, the line is what it was
    assert mood_line(happy, Altered.SOBER, None, DayPhase.EVENING, where="",
                     state_words="") == plain


def test_situation_rules_are_stable_per_speaker():
    """Ollama's prefix cache: the system message is the same on every turn for a speaker."""
    he = dataclasses.replace(GM, pronouns="he")
    woman = make_persona(201, "Belra", player_gender="female")
    first = situation_rules(he, woman, now=1000.0, channel="guild")
    later = situation_rules(he, woman, now=1000.0 + 86400 * 3 + 7200, channel="guild")
    assert first == later
    assert "usually 3-12 words" not in first and "usually 3-12 words" in comms_rules(he, woman)
    assert ("- You flirt only with Zan. Everyone else is a friend, a rival or just a guildie."
            in first)
    man = situation_rules(he, make_persona(101, "Arkon"))
    assert ("- You don't flirt with anyone in the guild. The women only have eyes for Zan "
            "anyway." in man)


def test_situation_rules_keep_the_hard_rules_and_the_comms_prefix():
    he = dataclasses.replace(GM, pronouns="he")
    woman = make_persona(201, "Belra", player_gender="female")
    rules = situation_rules(he, woman)
    prefix = comms_rules(he, woman).split("\n", 1)[0]
    assert prefix.startswith("In your guild: ") and rules.split("\n", 1)[0] == prefix
    assert SITUATION_RULES.format(human="Zan") in rules
    assert "never say the year" in rules.lower() and "AI" in rules
    assert "no one else's lines" in rules
    assert "guilt-trip Zan" in rules and "Shared history comes only from your memories" in rules
    assert "Quillon" not in rules and "{" not in rules
    assert not any(c in rules for c in TELLS)
    for gone in ("usually", "One short line", "Specific beats polite", "(nothing)", "2009 slang"):
        assert gone not in rules
    # the flirt rule follows the turn as comms_rules has it: away on a turn that isn't his
    away = situation_rules(he, woman, at_him=False)
    assert "You don't flirt with anyone here" in away and "flirt only with Zan" not in away
    assert situation_rules(he) == comms_rules(he).split("\n", 1)[0] + "\n" + SITUATION_RULES.format(
        human="Zan")


def test_situation_rules_carry_the_tuning_and_nudge_suffix(monkeypatch):
    import leeroy_mind.talk.prompt as prompt
    import leeroy_mind.tuning.nudges as nudges
    woman = make_persona(201, "Belra", player_gender="female")
    monkeypatch.setattr(prompt, "tuning_lines", lambda persona, data_dir: ["TUNED LINE."])
    monkeypatch.setattr(nudges, "prompt_lines", lambda persona, **kw: ["NUDGE LINE."])
    for rules in (situation_rules(GM, woman), comms_rules(GM, woman)):
        assert rules.endswith("\n- TUNED LINE.\n- NUDGE LINE.")


def test_situation_rules_are_noted_as_a_template():
    with trace_templates() as used:
        situation_rules(GM, make_persona(201, "Belra", player_gender="female"))
    assert "talk.situation_rules" in used and "talk.comms" not in used


def test_situation_templates_cover_five_kinds_and_read_without_tells():
    assert set(SITUATION_INSTRUCTIONS) == {TurnKind.REPLY_HUMAN, TurnKind.CONTINUE, TurnKind.OPEN,
                                           TurnKind.WHISPER, TurnKind.FLIRT}
    for tmpl in (*SITUATION_INSTRUCTIONS.values(), OPEN_TO_HIM_SITUATION):
        assert "{extra}" in tmpl and not any(c in tmpl for c in TELLS)
        assert "one short line" not in tmpl.lower() and "actually said" not in tmpl
    for kind in SITUATION_INSTRUCTIONS:
        text = render_instruction(kind, addressee="Zan", topic="the new mount", extra="EXTRA BIT.",
                                  forms=HE, situation=True, length_text="A LENGTH.")
        assert "{" not in text and "}" not in text and text.endswith("A LENGTH. EXTRA BIT.")
        assert text == text.strip() and "  " not in text
    for kind in (TurnKind.CONTINUE, TurnKind.OPEN):
        assert "(nothing)" in render_instruction(kind, addressee="Zan", situation=True,
                                                 length_text="L.")


def test_situation_open_and_open_to_him():
    opener = render_instruction(TurnKind.OPEN, topic="your cat", situation=True, length_text="L.")
    assert opener == ("Chat has gone quiet. Bring up your cat, the way you'd type it. It has to "
                      "make sense on its own: nothing in chat is addressed to you. If it isn't "
                      "worth saying, answer (nothing). L.")
    assert render_instruction(TurnKind.OPEN, topic="x", situation=True, length_text="L.",
                              quiet=False).startswith("Bring up x,")
    to_him = render_instruction(TurnKind.OPEN, addressee="Zan", topic="the raid", forms=HE,
                                situation=True, length_text="L.", to_him=True, extra="E.")
    assert to_him == ("Start a conversation with Zan, the way you'd type it: say his name and the "
                      "raid. Nothing in chat is addressed to you. L. E.")


def test_situation_keeps_lead_after_and_tail_around_the_template():
    text = render_instruction(TurnKind.WHISPER, addressee="Zan", forms=HE, situation=True,
                              length_text="L.", lead="LEAD.", extra="E.", after="AFTER.",
                              tail="TAIL.")
    assert text == "LEAD. You're whispering Zan privately. L. E. AFTER. TAIL."
    flirt = render_instruction(TurnKind.FLIRT, addressee="Zan", forms=HE, situation=True,
                               length_text="L.")
    assert flirt == "Say something to Zan in chat, the way you feel about him. L."


def test_situation_falls_back_to_the_game_template_for_other_kinds():
    for kind in set(TurnKind) - set(SITUATION_INSTRUCTIONS):
        assert (render_instruction(kind, addressee="Zan", topic="Lord Marrowgar", situation=True)
                == render_instruction(kind, addressee="Zan", topic="Lord Marrowgar"))


def test_situation_on_discord_takes_its_length_and_length_text_wins():
    dm = render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", forms=HE, situation=True,
                            channel="dm:101", discord_words=200, extra="X")
    assert dm.startswith("Reply to Zan's last line, as yourself. This is your private Discord DM")
    assert dm.endswith(" X")
    shaped = render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", forms=HE, situation=True,
                                channel="dm:101", discord_words=200, extra="X",
                                length_text="Just a word or two this time.")
    assert shaped == "Reply to Zan's last line, as yourself. Just a word or two this time. X"
    plain = render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", channel="discord:general",
                               discord_words=90, length_text="Two lines.")
    assert "Discord" not in plain and "Two lines." in plain and "one short line" not in plain
    with pytest.raises(ValueError):  # Discord with no words gives no length either
        render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", situation=True,
                           channel="discord:general", discord_words=0)


def test_length_text_wins_over_long_and_is_never_cut():
    long_len = "LEN " * 400
    text = render_instruction(TurnKind.WHISPER, addressee="Zan", forms=HE, long=True,
                              length_text=long_len)
    assert "more than your usual one line" not in text and "One short line" not in text
    assert text.count("LEN") == 400
    assert text.startswith(
        "You're whispering Zan privately. Say it the way you'd really whisper him.")


def test_old_calls_are_unchanged():
    for kind in TurnKind:
        assert render_instruction(kind, addressee="Zan", topic="T", extra="E", length_text="",
                                  situation=False) == render_instruction(kind, addressee="Zan",
                                                                        topic="T", extra="E")
    assert set(INSTRUCTIONS) == set(TurnKind)


def test_situation_instructions_are_noted_by_their_own_names():
    with trace_templates() as used:
        render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", situation=True, length_text="L.")
        render_instruction(TurnKind.GREET, addressee="Zan", situation=True)
    assert "talk.situation_instruction.reply_human" in used
    assert "talk.instruction.reply_human" not in used and "talk.instruction.greet" in used
    with trace_templates() as used:
        render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", channel="dm:101",
                           discord_words=200, length_text="L.")
    assert "talk.discord_length" not in used  # the length the line reads is the caller's


def test_situation_right_now_is_never_cut_and_is_counted():
    big = replace(PARTS, situation=True, memories=[f"- M{i} " + "m" * 400 for i in range(8)],
                  chat=[f"P{i}: " + "x" * 300 for i in range(12)], mood="It's late. " + "w" * 200,
                  awareness="AWARE")
    t = trim_to_budget(big, budget=600)
    assert t.situation and t.mood == big.mood and t.instruction == big.instruction
    user = render_messages(t)[1]["content"]
    assert user.endswith("Right now:\n" + big.mood + "\n\nINSTR")
    assert section_tokens(big) == section_tokens(replace(big, situation=False))
    assert redact_for(big, GM, close=False).situation


def test_situation_dm_prompt_fits_its_budget_with_right_now_whole():
    p = replace(PARTS, situation=True, memories=[f"- M{i} " + "m" * 300 for i in range(10)],
                chat=[f"P{i}: " + "x" * 600 for i in range(20)], mood="It's late. " + "w" * 300)
    t = trim_to_budget(p, budget=DM_BUDGET)
    assert t.mood == p.mood and t.instruction == p.instruction
    assert sum(estimate_tokens(m["content"]) for m in render_messages(t)) <= DM_BUDGET
