"""A member's voice sheet (Persona.speech, 2026-11-17): the schema field and its checks, the
block a prompt shows, which samples a turn gets, and the card that leaves out what the sheet
says instead. Every sheet here is invented (tests/sheets.py)."""
import json
import random
from dataclasses import replace

import pytest

from conftest import make_persona
from leeroy_mind.life.seed import seeded
from leeroy_mind.personas import speech as sp
from leeroy_mind.personas.card import render_core_card, render_prompt_card
from leeroy_mind.personas.io import canonical_json
from leeroy_mind.personas.schema import (PersonaError, persona_from_dict, persona_json_schema,
                                         persona_to_dict, speech_from_dict, speech_to_dict,
                                         validate_persona)
from leeroy_mind.talk.intent import Intent
from leeroy_mind.text import estimate_tokens
from sheets import GRUFF, BRIGHT, sheet


def member(data=GRUFF, gender="male", **over):
    return replace(make_persona(101, "Arkon", player_gender=gender), speech=sheet(data, **over))


# -- the schema ------------------------------------------------------------------------------------

def test_a_persona_without_a_sheet_is_stored_exactly_as_before():
    p = make_persona(101, "Arkon")
    d = persona_to_dict(p)
    assert p.speech is None and "speech" not in d
    assert "speech" not in json.loads(canonical_json(p))
    assert persona_from_dict(d) == p and validate_persona(p) == []


def test_a_sheet_round_trips_and_validates():
    p = member()
    d = persona_to_dict(p)
    assert d["speech"] == speech_to_dict(p.speech) == {**GRUFF}
    assert persona_from_dict(json.loads(json.dumps(d))) == p
    assert validate_persona(p) == [] and validate_persona(member(BRIGHT, "female")) == []


def test_no_model_is_asked_for_a_sheet():
    assert "speech" not in persona_json_schema()["properties"]
    assert "speech" in persona_json_schema(enrichment=True)["properties"]


@pytest.mark.parametrize("change,problem", [
    ({"register": "x" * 61}, "speech.register: must be at most 60 characters"),
    ({"shape": "y" * 121}, "speech.shape: must be at most 120 characters"),
    ({"says": ["a", "b", "c"]}, "speech.says: must have at least 4 items"),
    ({"says": [f"w{i}" for i in range(9)]}, "speech.says: must have at most 8 items"),
    ({"says": ["noted", "sound as", "on my tally", "z" * 31]},
     "speech.says[3]: must be at most 30 characters"),
    ({"brings_up": ["b" * 91]}, "speech.brings_up[0]: must be at most 90 characters"),
    ({"never": ["a", "b", "c", "d", "e"]}, "speech.never: must have at most 4 items"),
    ({"brings_up": []}, "speech.brings_up: must have at least 1 items"),
    ({"brings_up": ["a", "b", "c", "d", "e"]}, "speech.brings_up: must have at most 4 items"),
    ({"samples": {**GRUFF["samples"], "answer": "w " * 111}},
     "speech.samples.answer: must be at most 220 characters"),
    ({"samples": {"tease": "you first"}}, "speech.samples: needs an answer sample"),
    ({"samples": {"answer": "ok", "sulky": "hmph"}},
     "speech.samples.sulky: a sample kind is one of answer, tease, tired, flirt, warm, annoyed"),
    ({"samples": {"answer": "ok", "flirt": "come here", "warm": "good to see you"}},
     "speech.samples: flirt or warm, not both"),
    ({"samples": {"answer": "ok", "flirt": "come here"}},
     "speech.samples.flirt: men stay friends; use warm"),
    ({"never": ["Noted"]}, "speech.never[0]: is also among the words she says"),
    ({"says": ["noted", "Noted", "on my tally", "well so"]}, "speech.says: the same word twice"),
    ({"register": "dry\nexact"}, "speech.register: one line of plain text (no line breaks, no |)"),
    ({"with_him": "teases |cffff0000him|r"},
     "speech.with_him: one line of plain text (no line breaks, no |)"),
    ({"register": " "}, "speech.register: must not be empty"),
    ({"register": "As an AI language model I talk plainly"},
     "speech.register: reads like an assistant, not a person"),
])
def test_what_the_schema_refuses(change, problem):
    assert problem in validate_persona(member(**change))


def test_with_him_and_stance_may_be_empty():
    assert validate_persona(member(with_him="", stance="")) == []


def test_the_block_has_a_hard_token_cap():
    """A long sheet is trimmed for the turn, never refused for its lists; only a register,
    shape, with_him and stance that leave no room are refused."""
    wordy = member(says=[f"{'w' * 20} {i}" for i in range(8)], never=["n" * 28 + str(i) for i in range(4)],
                   brings_up=[f"{'t' * 80} {i}" for i in range(4)])
    assert validate_persona(wordy) == []
    s = wordy.speech
    for to_him, private in ((False, False), (True, False), (True, True)):
        block = sp.voice_block(s, "Zan", to_him=to_him, private=private)
        assert estimate_tokens(block) <= sp.BLOCK_TOKENS
        assert block.startswith("Your voice: plain, clipped, a bit gruff; ")
        assert ("With Zan: ribs him over his sums." in block) is to_him
    keep, size = sp.fitted(s)
    assert keep < (8, 4, 4) and keep[0] >= 4 and keep[2] >= 1 and size <= sp.BLOCK_TOKENS
    assert sp.fitted(s, to_him=False, private=False)[0] >= keep  # a public turn has more room
    core = member(register="r" * 60, shape="s" * 120, with_him="w" * 120, stance="t" * 140)
    assert any(e.startswith("speech: the block is ") and "at most 120" in e
               for e in validate_persona(core))
    for data, gender in ((GRUFF, "male"), (BRIGHT, "female")):
        s = member(data, gender).speech
        assert sp.fitted(s)[0] == (len(s.says), len(s.never), len(s.brings_up))  # nothing cut
        assert sp.block_tokens(s) == estimate_tokens(
            sp.voice_block(s, sp.NOMINAL_HUMAN, to_him=True, private=True)) <= sp.BLOCK_TOKENS


def test_the_trim_order_gives_up_the_fourth_topic_then_words_then_never_words():
    assert sp.TRIMS[0] == (8, 4, 4) and sp.TRIMS[1] == (8, 4, 3) and sp.TRIMS[3] == (6, 4, 3)
    assert all(a >= b for x, y in zip(sp.TRIMS, sp.TRIMS[1:]) for a, b in zip(x, y))
    assert sp.TRIMS[-1] == (4, 0, 1)


def test_a_sheet_of_the_wrong_shape_is_a_persona_error():
    with pytest.raises(PersonaError, match="speech.says: must be a list"):
        speech_from_dict({**GRUFF, "says": "noted"})
    with pytest.raises(PersonaError, match="speech.humour: unknown field"):
        speech_from_dict({**GRUFF, "humour": "dry"})
    with pytest.raises(PersonaError, match="speech.register: missing"):
        speech_from_dict({k: v for k, v in GRUFF.items() if k != "register"})
    with pytest.raises(PersonaError, match="speech: must be an object"):
        speech_from_dict(["noted"])


# -- the block -------------------------------------------------------------------------------------

def test_the_block_on_a_public_turn_has_neither_with_him_nor_stance():
    s = sheet()
    block = sp.voice_block(s, "Zan", samples=["depends. which boss was it"])
    assert block.splitlines() == [
        "Your voice: plain, clipped, a bit gruff; short flat statements, three to seven words.",
        "You say, only now and then: noted, sound as, on my tally, well so. "
        "Never: awesome, lol.",
        "Unasked, you bring up: the allotment; night shifts at the depot.",
        sp.SAMPLES_HEAD,
        "- depends. which boss was it"]
    assert "not lines to reuse" in sp.SAMPLES_HEAD


def test_with_him_only_on_his_turns_and_stance_only_where_only_he_reads():
    s = sheet()
    his = sp.voice_block(s, "Zan", to_him=True)
    assert "With Zan: ribs him over his sums." in his and "content with it" not in his
    private = sp.voice_block(s, "Zan", to_him=True, private=True)
    assert "With Zan: ribs him over his sums. solo and content with it." in private
    assert "With Zan" not in sp.voice_block(s, "Zan", private=True)  # never without to_him
    assert sp.SAMPLES_HEAD not in private  # no samples, no heading


def test_a_sheet_without_never_words_says_nothing_of_them():
    assert "Never:" not in sp.voice_block(sheet(never=[]), "Zan")


# -- which samples ---------------------------------------------------------------------------------

def test_every_want_maps_onto_a_sample_kind():
    assert set(sp.WANT_SAMPLE) == {i.value for i in Intent}
    assert set(sp.WANT_SAMPLE.values()) == {"answer", "tease", "tired", "flirt", "annoyed"}


@pytest.mark.parametrize("want,kind", [
    ("tease", "tease"), ("gossip", "tease"), ("vent", "tired"), ("deflect", "tired"),
    ("boundary", "annoyed"), ("share", "answer"), ("ask", "answer"), ("react", "answer"),
    ("small_talk", "answer"), ("logistics", "answer"), ("", "answer"), ("unknown", "answer")])
def test_the_first_sample_is_the_wants(want, kind):
    s = sheet()
    picked = sp.pick_samples(s, want, random.Random(3))
    assert picked[0] == s.samples[kind] and len(picked) == 2 and picked[1] != picked[0]


def test_flirt_reads_as_warm_for_a_sheet_without_it_and_is_only_shown_to_him():
    plain, her = sheet(), sheet(BRIGHT)
    assert sp.pick_samples(plain, "flirt", random.Random(1))[0] == plain.samples["warm"]
    assert sp.pick_samples(plain, "reassure_seek", random.Random(1))[0] == plain.samples["warm"]
    assert sp.pick_samples(her, "flirt", random.Random(1), to_him=True)[0] == her.samples["flirt"]
    for seed in range(40):  # never on a turn that is not his, first or drawn
        shown = sp.pick_samples(her, "flirt", random.Random(seed))
        assert her.samples["flirt"] not in shown and shown[0] == her.samples["answer"]


def test_the_second_sample_rotates_on_its_own_seeded_stream():
    s = sheet()
    one = sp.pick_samples(s, "tease", seeded("voice", "2026-10-29", 101, 1200, 7))
    again = sp.pick_samples(s, "tease", seeded("voice", "2026-10-29", 101, 1200, 7))
    assert one == again  # the same turn, the same samples
    seconds = {sp.pick_samples(s, "tease", seeded("voice", "2026-10-29", 101, m, 7))[1]
               for m in range(1200, 1260)}
    assert len(seconds) >= 3 and s.samples["tease"] not in seconds
    # one draw from the caller's stream, whatever it picks
    rng, twin = random.Random(5), random.Random(5)
    sp.pick_samples(s, "tease", rng)
    twin.randrange(4)
    assert rng.random() == twin.random()


def test_a_sample_she_already_sent_is_never_shown_again():
    s = sheet()
    sent = ["Noted. Not doing that twice!", "and then: depends, which boss was it?"]
    for seed in range(30):
        shown = sp.pick_samples(s, "boundary", random.Random(seed), sent=sent)
        assert s.samples["annoyed"] not in shown and s.samples["answer"] not in shown
        assert shown and len(shown) <= 2
    everything = list(s.samples.values())
    assert sp.pick_samples(s, "tease", random.Random(1), sent=everything) == []


def test_one_sample_when_asked_for_one():
    assert sp.pick_samples(sheet(), "vent", random.Random(1), n=1) == [GRUFF["samples"]["tired"]]


# -- her line length -------------------------------------------------------------------------------

@pytest.mark.parametrize("shape,found", [
    ("short flat statements, three to seven words", (3, 7)), ("fragments of 3-8 words", (3, 8)),
    ("Three or Five words, rarely more", (3, 5)), ("ten to 4 words", (4, 10)),
    ("runs two thoughts together, then stops short", None), ("", None),
    ("about 99 to 100 words", None)])
def test_the_line_length_a_shape_names(shape, found):
    assert sp.word_range(shape) == found


# -- the import's own screen -----------------------------------------------------------------------

@pytest.mark.parametrize("change,code", [
    ({"samples": {**GRUFF["samples"], "tease": "ask Belra, she pulled early"}}, "member_name"),
    ({"samples": {**GRUFF["samples"], "tired": "lol. brain is soup"}}, "never_in_sample"),
    ({"brings_up": ["the allotment", "her rizz"]}, "later_era"),
    ({"stance": "single since 2019"}, "later_era"),
    ({"says": ["noted", "sound as", "on my tally", "you absolute menace"]}, "says_slop"),
])
def test_what_the_import_screen_refuses_by_code(change, code):
    assert sp.screen(member(**change), characters=["Arkon", "Belra", "Coros"]) == [code]


def test_a_clean_sheet_passes_the_screen_and_her_own_name_is_no_member():
    assert sp.screen(member(), characters=["Arkon", "Belra"]) == []
    own = member(samples={**GRUFF["samples"], "warm": "arkon reporting in. well so"})
    assert sp.screen(own, characters=["Arkon", "Belra"]) == []
    assert sp.screen(make_persona(101, "Arkon")) == []


# -- the card --------------------------------------------------------------------------------------

def test_the_live_card_leaves_out_what_the_sheet_says_instead():
    plain = make_persona(101, "Arkon")
    p = member()
    kind = dict(race="Human", class_name="Mage")
    usual = render_prompt_card(plain, **kind)
    assert render_prompt_card(p, **kind) == usual == render_prompt_card(plain, voice=True, **kind)
    assert render_core_card(p, **kind) == render_core_card(plain, **kind)  # the stored card too
    voiced = render_prompt_card(p, voice=True, **kind)
    gone = ["chats easily in guild. rarely rattled.", "You trust people easily. Your humour is dry."]
    assert all(line in usual.splitlines() for line in gone)
    assert voiced.splitlines() == [line for line in usual.splitlines() if line not in gone]


def test_nothing_comes_back_into_the_room_the_traits_leave():
    """A card whose opinions were dropped for the budget stays without them when the sheet takes
    the trait sentences' place: the sheet replaces them, it does not make room for more."""
    long = make_persona(101, "Arkon", opinions=["o" * 160, "p" * 160, "q" * 160],
                        wants=["w" * 100, "x" * 100, "y" * 100, "z" * 100])
    kind = dict(race="Human", class_name="Mage")
    usual = render_prompt_card(long, **kind)
    assert "Opinions:" not in usual and "In game" not in usual
    voiced = render_prompt_card(replace(long, speech=sheet()), voice=True, **kind)
    assert "Opinions:" not in voiced and "In game" not in voiced and len(voiced) < len(usual)
