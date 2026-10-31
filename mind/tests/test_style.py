import random

import pytest

from leeroy_mind.personas.schema import Typing
from leeroy_mind.talk.states import EFFECTS, Altered, effects_for
from leeroy_mind.talk.style import SOFT_OVER, apply_style, line_cap, style_lines
from leeroy_mind.talk.turns import TurnKind


def T(**kw) -> Typing:
    """A Typing whose rates (emoticon, typo, split) default to 0: lower case, no end
    punctuation, no abbreviations, lines up to 60 words."""
    base = dict(case="lower", end_punct="none", emoticons=(), emoticon_rate=0.0,
                abbreviations={}, typo_rate=0.0, words_per_line=(1, 60), split_rate=0.0,
                chars_per_sec=5.0)
    base.update(kw)
    return Typing(**base)


R = random.Random
SOBER_E = EFFECTS[Altered.SOBER]
DRUNK_E = EFFECTS[Altered.DRUNK]


def test_lowercase_keeps_shouting():
    assert apply_style("Let's GOOO Arkon", T(case="lower"), SOBER_E, R(1)) == ["let's GOOO arkon"]


def test_no_end_period_and_abbreviations():
    out = apply_style("Thank you, see you there.", T(abbreviations={"you": "u"}), SOBER_E, R(1))
    assert out == ["thank u, see u there"]
    # word boundaries only, multi-word keys, and the key's case does not matter
    t = T(case="mixed", abbreviations={"you": "u", "to be honest": "tbh"})
    assert apply_style("To be honest your young tank did fine, YOU know", t, SOBER_E, R(1)) \
        == ["tbh your young tank did fine, u know"]


def test_case_and_end_punct_modes():
    assert apply_style("see you there", T(case="proper", end_punct="full"), SOBER_E, R(1)) \
        == ["See you there."]
    assert apply_style("See YOU There!", T(case="mixed", end_punct="full"), SOBER_E, R(1)) \
        == ["See YOU There!"]
    assert apply_style("wait, what?", T(), SOBER_E, R(1)) == ["wait, what?"]  # questions keep "?"
    assert apply_style("well then...", T(), SOBER_E, R(1)) == ["well then..."]  # trailing off
    some = {apply_style("on my way.", T(end_punct="some"), SOBER_E, R(s))[0] for s in range(40)}
    assert some == {"on my way.", "on my way"}


def test_emoticon_rate_one_appends():
    t = T(emoticons=(":P",), emoticon_rate=1.0)
    assert apply_style("nice pull", t, SOBER_E, R(3)) == ["nice pull :P"]
    assert apply_style("nice pull", T(emoticons=(":P",), emoticon_rate=0.0), SOBER_E, R(3)) \
        == ["nice pull"]
    # the emoticon survives the length cap and the split, and is never typo'd
    long = " ".join(["something"] * 30)
    out = apply_style(long, T(emoticons=("xDDD",), emoticon_rate=1.0, typo_rate=0.15,
                              words_per_line=(2, 6), split_rate=1.0), DRUNK_E, R(5))
    assert out[-1].endswith(" xDDD")


WORDS = ("boss tank wipe mana heal pull loot drop gear raid frost tomb fire void dead melee "
         "range flask aggro again").split()


def _sentence() -> str:
    """41 words, all but one of 4-5 letters, well inside the 255-byte line cap."""
    words = (WORDS * 2)[:40]
    return " ".join(words[:20] + ["Arkon"] + words[20:])


def _typos(eff, seed, rate=0.05):
    text = _sentence()
    out = apply_style(text, T(typo_rate=rate), eff, R(seed), protect=frozenset({"Arkon"}))
    assert len(out) == 1
    got, want = out[0].split(), text.lower().split()
    assert len(got) == len(want) and "arkon" in got
    return sum(a != b for a, b in zip(got, want))


def test_typos_scale_with_drink():
    sober = sum(_typos(SOBER_E, s) for s in range(200)) / 200
    drunk = sum(_typos(DRUNK_E, s) for s in range(200)) / 200
    assert sober > 0
    assert drunk > 2 * sober
    for s in range(100):  # protect holds even at the highest rate
        _typos(DRUNK_E, s, rate=0.15)
    # short words are never touched
    out = apply_style("ok so we go now and win", T(typo_rate=0.15), DRUNK_E, R(1))
    assert out == ["ok so we go now and win"]


def test_length_cap_and_split():
    """The cap is per line: words_per_line[1] x length_mult x the state's length_mult. A text
    with no clause break to cut at stays whole up to SOFT_OVER times the cap, and past that is
    cut between words, never split in two mid-phrase."""
    sentences = "we pulled early. the healer was afk. the tank died first. we wiped again."
    for lm in (1.0, 1.5):
        for eff in (SOBER_E, DRUNK_E, EFFECTS[Altered.HUNGOVER]):
            cap = line_cap(T(words_per_line=(3, 8)), eff, lm)
            assert cap == int(8 * lm * eff.length_mult)
            two = apply_style(sentences, T(words_per_line=(3, 8), split_rate=0.1), eff, R(2),
                              length_mult=lm)
            assert 1 <= len(two) <= 2 and all(len(x.split()) <= cap for x in two)
            run_on = " ".join(f"w{i}" for i in range(40))
            for split in (0.0, 1.0):
                (one,) = apply_style(run_on, T(words_per_line=(3, 8), split_rate=split), eff,
                                     R(2), length_mult=lm)
                assert len(one.split()) == int(cap * SOFT_OVER + 1e-9)
    # too short to split into two lines of words_per_line[0] words
    assert apply_style("gg all", T(words_per_line=(3, 8), split_rate=1.0), SOBER_E, R(1)) \
        == ["gg all"]


def test_cap_prefers_a_sentence_end_and_split_a_clause():
    t = T(words_per_line=(2, 8))
    text = "ok so we pulled early and wiped. then the healer went afk again"
    assert apply_style(text, t, SOBER_E, R(1)) == ["ok so we pulled early and wiped"]
    two = apply_style("nice pull, gg everyone", T(split_rate=1.0), SOBER_E, R(1))
    assert two == ["nice pull", "gg everyone"]
    # a persona who types two lines now and then gets both sentences
    assert apply_style(text, T(words_per_line=(2, 8), split_rate=0.1), SOBER_E, R(1)) \
        == ["ok so we pulled early and wiped", "then the healer went afk again"]


# -- lines from the rehearsal ------------------------------------------------------------------
# The model's real replies from the dress rehearsal (2026-10-30, the live model), with the
# test cast's names and Zan for the human, and invented details where they were a persona's;
# the typing is the speaker's (words_per_line, split_rate). What used to reach chat is noted.

REHEARSAL = [
    # sent: "welcome bak zan!" / "still stuck in ashenvale or did you finally get"
    ((3, 12), 0.4, "welcome back zan! still stuck in the barrens or did you finally get free?",
     ["welcome back zan", "still stuck in the barrens or did you finally get free?"]),
    # sent: "you haven't done the argent tournament?! it's north of the wrathgate, the whole zone is"
    ((5, 15), 0.3, "you haven't done the argent tournament?! it's north of the wrathgate, the "
                   "whole zone is basically a graveyard. i'll take you. i'll take you anywhere, "
                   "honestly, i'm not picky.",
     ["you haven't done the argent tournament?",
      "it's north of the wrathgate, the whole zone is basically a graveyard. i'll take you"]),
    # sent: "you shoulda done them like a" / "month ago laughing out loud i"
    ((3, 12), 0.4, "you shoulda done them like a month ago lol i woulda walked you there if you "
                   "asked \U0001F618",
     ["you shoulda done them like a month ago lol", "i woulda walked you there if you asked"]),
    # sent: "so you've been sitting hee all" / "evening and nobody's said a word..."
    ((3, 12), 0.4, "so you've been sitting here all evening and nobody's said a word... i swear "
                   "i'm carrying this whole chat on my back",
     ["so you've been sitting here all evening and nobody's said a word...",
      "i swear i'm carrying this whole chat on my back"]),
    # sent: "run it for <name>" (the reason, the half that answers him, was cut)
    ((2, 6), 0.1, "run it for the trinket. emblems alone take forever.",
     ["run it for the trinket", "emblems alone take forever"]),
    # sent: "still haven't figured out why the quest giver is wearing that ridiculous"
    ((4, 12), 0.2, "still haven't figured out why the quest giver is wearing that ridiculous "
                   "hat. like he lost a bet with a gnome.",
     ["still haven't figured out why the quest giver is wearing that ridiculous hat",
      "like he lost a bet with a gnome"]),
    # sent: "zan if you need someone to hold your hand through that quest" (the punchline cut)
    ((3, 12), 0.4, "zan if you need someone to hold your hand through that quest i'm available ;)",
     ["zan if you need someone to hold your hand through that quest i'm available ;)"]),
    # sent: "it's like he raided the" / "clearance rack at a gnome boutique"
    ((5, 15), 1.0, "it's like he raided the clearance rack at a gnome boutique",
     ["it's like he raided the clearance rack at a gnome boutique"]),
    # sent: "we wipe when the healer chainbreaks. healers, stack on me every 30s"
    ((4, 12), 0.1, "we wipe when the healer chainbreaks. healers, stack on me every 30s. dps: "
                   "stop rushing the stacks. go again.",
     ["we wipe when the healer chainbreaks. healers, stack on me every 30s",
      "dps: stop rushing the stacks. go again"]),
    # sent: "congrats zan. bone collection is a grind"
    ((2, 8), 0.3, "congrats zan. bone collection is a grind. dont let it kill you.",
     ["congrats zan. bone collection is a grind", "dont let it kill you"]),
    # sent: "bracers, zan? you're making" / "me blush over here"
    ((3, 12), 0.4, "bracers, zan? you're making me blush over here. come here then, i want to "
                   "see them on you ;)",
     ["bracers, zan? you're making me blush over here",
      "come here then, i want to see them on you ;)"]),
]


@pytest.mark.parametrize("wpl, split, raw, want", REHEARSAL)
def test_rehearsal_lines_are_never_cut_mid_phrase(wpl, split, raw, want):
    t = T(words_per_line=wpl, split_rate=split)
    for seed in range(12):  # the split dice never cut a whole clause in two
        assert apply_style(raw, t, SOBER_E, R(seed)) == want, seed


def test_a_long_reply_drops_whole_clauses_and_keeps_the_answer():
    """One line for the quest answer: whole clauses, and the half that answers him stays."""
    raw = REHEARSAL[1][2]
    t = T(words_per_line=(5, 15), split_rate=0.3)
    assert apply_style(raw, t, SOBER_E, R(0), max_lines=1) \
        == ["you haven't done the argent tournament?! it's north of the wrathgate"]


def test_front_loaded_filler_goes_before_the_punchline():
    """A short typist: the model's filler opener is what gives way, not the line's point."""
    t = T(words_per_line=(1, 4), split_rate=0.3)
    assert apply_style("lol yeah. that pull was brutal", t, SOBER_E, R(0), max_lines=1) \
        == ["that pull was brutal"]
    assert apply_style("lol yeah. that pull was brutal", t, SOBER_E, R(0)) \
        == ["lol yeah", "that pull was brutal"]
    out = apply_style("lol yeah that pull was a mess, we should've waited for the healers", t,
                      SOBER_E, R(0))
    assert out == ["that pull was a mess"]
    # no clause break at all: the words that fit, never ending on a little word
    (cut,) = apply_style("honestly the gunship is the worst fight in icc", t, SOBER_E, R(0))
    assert cut == "the gunship is the worst"


def test_style_never_expands_an_abbreviation():
    """85 of 106 stored maps run backwards ("lol" -> "laughing out loud"): only shortening
    entries apply."""
    t = T(abbreviations={"lol": "laughing out loud", "u": "you", "you": "u",
                         "be right back": "brb", "afk": "away"})
    out = apply_style("lol you still afk? be right back", t, SOBER_E, R(0))
    assert out == ["lol u still afk? brb"]
    raw = REHEARSAL[2][2]
    rehearsed = T(words_per_line=(3, 12), split_rate=0.4,
                  abbreviations={"idk": "i don't know", "lol": "laughing out loud"})
    for seed in range(10):
        said = " ".join(apply_style(raw, rehearsed, SOBER_E, R(seed)))
        assert "laughing out loud" not in said and " lol " in f" {said} "


def test_every_outgoing_line_is_ascii():
    """No emoji and no other non-ASCII reaches chat, from the model or from the persona's own
    emoticons; accented letters and typographic marks are folded, not dropped."""
    t = T(emoticons=("☕",), emoticon_rate=1.0)
    assert apply_style("another heal chain break. of course.", t, SOBER_E, R(0)) \
        == ["another heal chain break. of course"]
    t = T(emoticons=("☕", "\U0001F3AD", ":P"), emoticon_rate=1.0)
    for seed in range(10):
        assert apply_style("night already.", t, SOBER_E, R(seed)) == ["night already :P"]
    out = apply_style("café run — naïve “plan”… ßo "
                      "ıs it æther", T(case="mixed"), SOBER_E, R(0))
    assert out == ['cafe run - naive "plan"... sso is it aether']
    for _, _, raw, _ in REHEARSAL:
        for seed in range(5):
            for line in apply_style(raw, T(emoticons=("✨", "<3"), emoticon_rate=0.5,
                                           typo_rate=0.1), DRUNK_E, R(seed)):
                assert line.isascii()


def test_style_lines_keep_the_words_before_typos_and_abbreviations():
    """What was said, as the speaker's own lines show it: the kept words, cased and cut as
    sent, without the typos, abbreviations and emoticon (repeats are judged on the words)."""
    t = T(typo_rate=0.15, abbreviations={"you": "u"}, words_per_line=(2, 6), split_rate=0.1,
          emoticons=(":P",), emoticon_rate=1.0)
    got = style_lines("Thank you all. see you there tomorrow night.", t, SOBER_E, R(4))
    assert [s.plain for s in got] == ["thank you all", "see you there tomorrow night"]
    assert all(" u " in f" {s.line} " for s in got) and got[-1].line.endswith(" :P")
    assert [s.line for s in got] == apply_style("Thank you all. see you there tomorrow night.",
                                                t, SOBER_E, R(4))


def test_output_is_bridge_safe():
    for text in ("a|b\nc", "x" * 600, "\u00e9" * 300 + " end", "line one\r\n\tline two"):
        out = apply_style(text, T(), SOBER_E, R(0))
        assert 1 <= len(out) <= 2
        for line in out:
            assert line and "|" not in line and "\n" not in line and "\r" not in line
            assert len(line.encode("utf-8")) <= 255


def test_strips_quotes_own_name_prefix_and_emoji():
    assert apply_style('Arkon: "lol same 😂"', T(), SOBER_E, R(0), speaker="Arkon") == ["lol same"]
    assert apply_style('"[Guild] Belra: gz"', T(), SOBER_E, R(0), speaker="Belra") == ["gz"]
    assert apply_style("Belra (raid): on it 👍", T(), SOBER_E, R(0), speaker="Belra") == ["on it"]
    assert apply_style("😂🔥", T(), SOBER_E, R(0)) == []  # nothing left to say
    # the prompt's chat frame, and a tag with her real first name, on any path (templates too)
    assert apply_style("[just now] Belra (whisper to Zan): ok", T(), SOBER_E, R(0),
                       speaker="Belra") == ["ok"]
    assert apply_style("Tarvo: ok then", T(), SOBER_E, R(0), speaker="Belra",
                       aliases=("Tarvo",)) == ["ok then"]
    # a deliberate address to another member keeps its name
    assert apply_style("Belra: you're up", T(), SOBER_E, R(0), speaker="Arkon") \
        == ["belra: you're up"]


def test_item_links_keep_case_and_spelling():
    text = "Grats on [Shadowmourne] Arkon, enjoy [Bryntroll, the Bone Arbiter] too"
    for s in range(100):
        out = " ".join(apply_style(text, T(typo_rate=0.15), DRUNK_E, R(s)))
        assert "[Shadowmourne]" in out and "[Bryntroll, the Bone Arbiter]" in out
    # a link is one word: never cut or split through
    t = T(words_per_line=(1, 3), split_rate=1.0)
    out = apply_style("need [Bryntroll, the Bone Arbiter] for my offspec", t, SOBER_E, R(1))
    assert sum("[Bryntroll, the Bone Arbiter]" in x for x in out) == 1
    assert all(x.count("[") == x.count("]") for x in out)


def test_curly_apostrophes_and_unusual_case_folds():
    assert apply_style("Let’s go, it’s fine", T(), SOBER_E, R(0)) == ["let's go, it's fine"]
    # the Turkish dotted and dotless i are folded to ASCII before any abbreviation sees them
    t = T(case="mixed", abbreviations={"is": "iz"})
    assert apply_style("\u0130s it ok", t, SOBER_E, R(0)) == ["iz it ok"]
    assert apply_style("\u0131s it ok", t, SOBER_E, R(0)) == ["iz it ok"]
    assert apply_style("Is it ok", t, SOBER_E, R(0)) == ["iz it ok"]


def test_long_lines_are_cut_between_words():
    word = "abcdefghi"  # 9 bytes: 40 of them pass the bridge's 255
    out = apply_style(" ".join([word] * 40), T(emoticons=(":P",), emoticon_rate=1.0),
                      SOBER_E, R(0))
    assert len(out) == 1 and len(out[0].encode("utf-8")) <= 255
    assert out[0].endswith(" :P") and set(out[0][:-3].split()) == {word}


def test_raid_calls_are_never_slurred():
    text = "MOVE out of the fire now"
    assert apply_style(text, T(), DRUNK_E, R(0), kind=TurnKind.URGENT) == [text]
    t = T(typo_rate=0.1, words_per_line=(1, 4))
    for kind in (TurnKind.URGENT, TurnKind.CALLOUT):
        for s in range(50):
            assert apply_style(text, t, DRUNK_E, R(s), kind=kind) \
                == apply_style(text, t, SOBER_E, R(s))
    # positive control: outside a raid call, drink shows
    assert any(apply_style(text, t, DRUNK_E, R(s)) != apply_style(text, t, SOBER_E, R(s))
               for s in range(50))
    assert effects_for(Altered.DRUNK, TurnKind.URGENT) == SOBER_E
    assert effects_for(Altered.DRUNK, TurnKind.REPLY_HUMAN) == DRUNK_E


@pytest.mark.parametrize("seed", range(5))
def test_seeded_and_deterministic(seed):
    t = T(emoticons=(":)", ":P"), emoticon_rate=0.5, typo_rate=0.1, split_rate=0.5,
          end_punct="some", words_per_line=(2, 12))
    text = "Honestly that tank pulled before anyone was ready, again."
    assert apply_style(text, t, DRUNK_E, R(seed)) == apply_style(text, t, DRUNK_E, R(seed))


def test_keep_case_restores_a_name_through_lower_case():
    """A capitalised use keeps the name's spelling through lower-case typing; a word written
    in lower case is left the ordinary word it may be (Task 16 re-review: "who pays the bill"
    became "who pays the Bill" for a close member when the name was Bill)."""
    keep = frozenset({"Quillon"})
    assert apply_style("QUILLON you legend, ask Quillon or QuilLON", T(), SOBER_E, R(1),
                       keep_case=keep) == ["Quillon you legend, ask Quillon or Quillon"]
    assert apply_style("Bill, who pays the bill?", T(), SOBER_E, R(1),
                       keep_case=frozenset({"Bill"})) == ["Bill, who pays the bill?"]
    assert apply_style("Quillonia and aquillon stay", T(), SOBER_E, R(1),
                       keep_case=keep) == ["quillonia and aquillon stay"]  # whole words only
    assert apply_style("hey Mary jane, o'Neil and O'neil", T(), SOBER_E, R(1),
                       keep_case=frozenset({"Mary Jane", "O'Neil"})) == [
        "hey Mary Jane, o'neil and O'Neil"]
    typo = T(typo_rate=0.15)
    for seed in range(200):  # never misspelt
        (line,) = apply_style("Quillon carried the whole raid tonight", typo, DRUNK_E, R(seed),
                              keep_case=keep)
        assert line.startswith("Quillon ")


# -- marks and typos (fix wave G1, item 9; rehearsal 2, judge A 4, judge C 4) ------------------

def test_only_a_mark_is_added_and_never_after_one_or_an_interjection():
    """Rehearsal 2: "great." became "great aww", "dream of me ;)" became "dream of me ;) lmao",
    "see you soon :)" became "see you soon :) xD"."""
    t = T(emoticons=("lmao", "aww", ":)"), emoticon_rate=1.0)
    for seed in range(30):
        assert apply_style("nice pull", t, SOBER_E, R(seed)) == ["nice pull :)"]
    marked = T(emoticons=(":)", "xD"), emoticon_rate=1.0)
    for line in ("see you soon :)", "dream of me ;)", "nobody wins lol", "ty all gg",
                 "that was cute aww"):
        assert apply_style(line, marked, SOBER_E, R(1)) == [line], line
    assert style_lines("dream of me", marked, SOBER_E, R(1), no_emoticon=True)[0].line == \
        "dream of me"


def test_a_mark_keeps_its_case_in_lower_case():
    """Rehearsal 2: a member's "xD" went out as "xd"."""
    assert apply_style("nobody WINS xD", T(case="lower"), SOBER_E, R(1)) == ["nobody WINS xD"]
    assert apply_style("Fine :D Okay", T(case="lower"), SOBER_E, R(1)) == ["fine :D okay"]


def test_typos_swap_and_drop_and_never_make_a_real_word():
    """Rehearsal 2: doubled letters ("reead", "thhat") looked machine-made, and "still" became
    "sill"."""
    from leeroy_mind.talk.style import _typo
    from leeroy_mind.talk.words import COMMON_WORDS
    doubled = 0
    for seed in range(400):
        out = _typo("planet", R(seed))
        doubled += len(out) == 7
        for word in ("still", "good", "your", "form", "lose", "then", "with", "they", "ready"):
            typo = _typo(word, R(seed))
            assert typo == word or typo.casefold() not in COMMON_WORDS, (word, typo)
            assert typo[0] == word[0]
    assert doubled < 0.2 * 400
    assert {_typo("still", R(s)) for s in range(200)} >= {"sitll", "stll"}  # still typo'd


@pytest.mark.parametrize("line, only", [
    ("/w Belra", True), ("/roll", True), ("/invite arkon", True), ("/roll /dance", False),
    ("/hug", False), ("/hug belra", False), ("/w Belra hey there", False), ("hey /roll", False),
    ("", False), ("lol", False)])
def test_a_line_of_only_commands_that_are_no_emote_is_nothing_to_say(line, only):
    """Rehearsal 3, finding 5: a line that is only a command the game would print as text is
    dropped; an emote is the emote (split_emotes)."""
    from leeroy_mind.talk.style import command_only
    assert command_only(line) is only
