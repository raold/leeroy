"""[talk] texture: members type the way people type in chat (dynamics audit F5, repetition audit
5). Invented test cast only; no model, store or live data."""
import difflib
import random
import re
from dataclasses import replace

import pytest

from leeroy_mind.config import load_config
from leeroy_mind.personas.schema import Typing
from leeroy_mind.talk.states import EFFECTS, Altered
from leeroy_mind.talk.style import (_AUTOCORRECT, _CONTRACTIONS, _FINGERS, _LOOSE, _NEAR,
                                    KEEP_APOSTROPHE, REAL_SHORTHAND, SHORTHAND, Styled, Texture,
                                    _slip, _textured, apply_style, carries, fix_share,
                                    style_lines, texture_of)
from leeroy_mind.talk.words import COMMON_WORDS

SOBER = EFFECTS[Altered.SOBER]
DRUNK = EFFECTS[Altered.DRUNK]
NAMES = [f"Member{i}" for i in range(60)]


def T(**kw) -> Typing:
    base = dict(case="lower", end_punct="none", emoticons=(), emoticon_rate=0.0,
                abbreviations={}, typo_rate=0.0, words_per_line=(1, 60), split_rate=0.0,
                chars_per_sec=5.0)
    base.update(kw)
    return Typing(**base)


def typed(text, t, seed, who="Arkon", **kw):
    kw.setdefault("max_lines", 1)
    rng = random.Random(f"{who}:{seed}")  # draws differ between members, as live
    return " ".join(s.line for s in style_lines(text, t, SOBER, rng, speaker=who, texture=True,
                                                **kw))


BARE = {c.replace("'", "") for c in _CONTRACTIONS}


def contractions(line):
    words = re.findall(r"[a-z']+", line.lower())
    return sum(w in _CONTRACTIONS for w in words), sum(w in BARE for w in words)


# -- the "u're" bug, on both paths --------------------------------------------------------------

@pytest.mark.parametrize("texture", [False, True])
def test_you_inside_a_contraction_is_never_abbreviated(texture):
    """Repetition audit 5: "you're" became "u're" in 145 lines; no one types that."""
    t = T(abbreviations={"you": "u"})
    for seed in range(40):
        for line in apply_style("you're up, you've got it, you'd know, you'll see", t, SOBER,
                                random.Random(seed), texture=texture):
            assert not re.search(r"(?<![\w'])u['’]", line), line
    assert apply_style("you're up, see you", t, SOBER, random.Random(0)) == ["you're up, see u"]
    # a Discord line keeps the phone's apostrophe, and the word inside it too
    (s,) = style_lines("you’re up", t, SOBER, random.Random(0), game=False)
    assert s.line == "you’re up"


# -- the switch -----------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "false", "true", "1", '"true"'])
def test_switch_defaults_off_and_an_invalid_value_is_off(tmp_path, value):
    path = tmp_path / "mind.toml"
    if value is not None:
        path.write_bytes(f"[talk]\ntexture = {value}\n".encode())
    assert load_config(path).talk.texture is (value == "true")


def test_texture_is_seeded_and_fixed_per_member():
    t = T(typo_rate=0.05, abbreviations={"you": "u"}, end_punct="some")
    text = "I'm not sure you're ready. we pulled early. honestly i don't know what happened."
    for seed in range(10):
        assert typed(text, t, seed) == typed(text, t, seed)
    assert texture_of(t, "Arkon") == texture_of(t, "Arkon")
    habits = {texture_of(t, who).habits for who in NAMES}
    assert len(habits) > 40  # members differ
    for who in NAMES:
        tex = texture_of(t, who)
        assert 2 <= len(tex.habits) <= 3
        assert all(SHORTHAND[i][1] in REAL_SHORTHAND for i in tex.extra)
    assert not texture_of(T(typo_rate=0.0), "Arkon").habits  # no typos declared, no habits


# -- apostrophes ---------------------------------------------------------------------------------

def _drop_share(t):
    kept = dropped = 0
    text = "i'm sure it's fine, don't worry. that's what i've said, can't hurt"
    for who in NAMES:
        for seed in range(5):
            k, d = contractions(typed(text, t, seed, who))
            kept, dropped = kept + k, dropped + d
    return dropped / (kept + dropped)


def test_apostrophes_follow_the_declared_style():
    """A lower-case typer with no full stops mostly drops them; a "proper" one keeps them."""
    casual = _drop_share(T())
    careful = _drop_share(T(case="proper", end_punct="full"))
    middle = _drop_share(T(case="mixed", end_punct="some"))
    assert casual > 0.6 and careful < 0.12 and careful < middle < casual


def test_words_that_would_read_as_other_words_keep_their_apostrophe():
    """we're / were, we'll / well, he'll / hell, she'll / shell, we'd / wed, she'd / shed."""
    text = "we're up and we'll pull, he'll tank, she'll heal, we'd win and she'd say so"
    for who in NAMES:
        for seed in range(3):
            line = typed(text, T(), seed, who)
            for word in KEEP_APOSTROPHE:
                assert word in line or word.replace("'", "") not in line, (word, line)


# -- fragments -----------------------------------------------------------------------------------

def _chained(t):
    """Of the full stops between fragments, the share kept."""
    text = "call lapsed. next pull in 30. bring flasks"
    return sum(typed(text, t, seed, who).count(". ") for who in NAMES for seed in range(4)) / (
        2 * len(NAMES) * 4)


def test_casual_typers_run_their_fragments_on_and_careful_ones_keep_full_stops():
    assert _chained(T()) < 0.3
    assert _chained(T(case="proper", end_punct="full")) > 0.9
    seen = set()
    for who in NAMES:
        for seed in range(4):
            line = typed("call lapsed. next pull in 30. bring flasks", T(), seed, who)
            seen.add(line)
            assert re.fullmatch(r"call lapsed(,| and|\.|) next pull in 30(,| and|\.|) bring flasks",
                                line), line
    assert {"call lapsed, next pull in 30, bring flasks",
            "call lapsed next pull in 30 bring flasks"} & seen


def test_run_ons_keep_questions_trailing_dots_names_and_i():
    t = T(case="mixed")
    for who in NAMES:
        for seed in range(4):
            line = typed("Wait, ready? Ok then... Arkon pulls. I pull next. The healer is afk.",
                         t, seed, who, protect=frozenset({"Arkon"}))
            assert "ready?" in line and "then..." in line and "Arkon" in line, line
            assert " I " in f" {line} " or " i " not in f" {line} "
            # a run-on lowers an ordinary first word, never "I" or a name
            assert "pulls, I" in line or "pulls I" in line or "pulls. I" in line or \
                "pulls and I" in line, line


def test_a_line_that_runs_on_says_so_in_its_plain_words_too():
    """Joining is punctuation: the stored words show it, so a burst splits typed and plain
    alike (realism.split_line)."""
    t = T(typo_rate=0.05, abbreviations={"you": "u"})
    for who in NAMES[:20]:
        for seed in range(3):
            for s in style_lines("ok. see you there. bring flasks.", t, SOBER, random.Random(seed),
                                 speaker=who, texture=True):
                assert s.plain.count(".") == s.line.count(".")


# -- shorthand -----------------------------------------------------------------------------------

INVENTED = {"tht", "wt", "nt", "wy", "vr", "gd", "bd", "hl", "n", "2", "4", "th"}
TEXTS = ["that is what i think, not sure why you are asking but whatever",
         "i don't know, to be honest i'm going to be late because of work right now",
         "you want to go? i'm probably going to sleep though, of course"]


def test_only_real_shorthand_is_added_and_some_of_the_time():
    seen = set()
    for who in NAMES:
        for seed in range(3):
            for text in TEXTS:
                line = typed(text, T(), seed, who)
                words = set(re.findall(r"[\w/']+", line))
                assert not words & INVENTED, line
                seen |= words & REAL_SHORTHAND
    assert {"idk", "tbh", "gonna", "tho"} <= seen
    # a careful typer adds none of her own
    for who in NAMES:
        line = typed(TEXTS[1], T(case="proper", end_punct="full"), 0, who)
        assert not set(re.findall(r"\w+", line)) & {"idk", "tbh", "gonna", "rn", "bc"}


def test_her_own_entries_apply_per_use_and_invented_ones_more_rarely():
    t = T(abbreviations={"you": "u", "that": "tht"})
    u = you = tht = that = 0
    for who in NAMES:
        for seed in range(3):
            line = typed("you know that, you said that you would", t, seed, who)
            words = re.findall(r"\w+", line)
            u, you = u + words.count("u"), you + words.count("you")
            tht, that = tht + words.count("tht"), that + words.count("that")
    assert 0.5 < u / (u + you) < 0.99  # not every time
    assert 0.05 < tht / (tht + that) < u / (u + you)  # declared, so used, but rarely
    # "going to" is "gonna" only before a verb
    for who in NAMES:
        for seed in range(3):
            assert "gonna bed" not in typed("i'm going to bed, going to be late", T(), seed, who)


# -- slips ---------------------------------------------------------------------------------------

def _changed_regions(a, b):
    ops = difflib.SequenceMatcher(None, a.split(), b.split()).get_opcodes()
    return sum(op != "equal" for op, *_ in ops)


LINE = "honestly the healer was making chili at that point and the tank went to get more flasks"


@pytest.mark.parametrize("phone", [False, True])
def test_a_line_has_at_most_one_slip(phone):
    """Dynamics audit F5: "maing chili at tat ponit" had three."""
    hits = 0
    for seed in range(600):
        out, slip, fix = _slip(LINE, 0.15 * DRUNK.typo_mult, random.Random(seed), frozenset(),
                               phone)
        assert _changed_regions(LINE, out) <= 1, out
        hits += out != LINE
        if fix:
            assert carries(out, slip) and fix in LINE.split()
    assert 0.3 * 600 < hits < 0.8 * 600  # some lines, not all, even drunk


def test_slips_come_in_some_lines_scaled_by_typo_rate():
    def share(rate):
        return sum(_slip(LINE, rate, random.Random(s), frozenset(), False)[0] != LINE
                   for s in range(2000)) / 2000
    low, high = share(0.02), share(0.05)
    assert 0.05 < low < 0.16 and 0.17 < high < 0.33
    # a short line has a smaller chance
    short = sum(_slip("ok see you", 0.05, random.Random(s), frozenset(), False)[0] != "ok see you"
                for s in range(2000)) / 2000
    assert short < high


def _slips(phone, n=3000):
    for seed in range(n):
        out, slip, fix = _slip(LINE, 0.12, random.Random(seed), frozenset(), phone)
        if out != LINE:
            yield out, slip, fix


def _kind(before, after):
    if len(after.split()) < len(before.split()):
        return "merge"
    a, b = before.split(), after.split()
    (i,) = [k for k in range(len(a)) if a[k] != b[k]][:1] or [None]
    if i is None:
        return "?"
    w, x = a[i], b[i]
    if len(a) == len(b) and [k for k in range(len(a)) if a[k] != b[k]] != [i]:
        return "shift"
    if x in COMMON_WORDS:
        return "auto"
    if len(x) == len(w) - 1:
        return "drop"
    if len(x) == len(w) + 1:
        return "double"
    diff = [k for k in range(len(w)) if w[k] != x[k]]
    if len(diff) == 2 and w[diff[0]] == x[diff[1]]:
        return "swap"
    if len(diff) == 1 and x[diff[0]] in "".join(_NEAR[w[diff[0]]]):
        return "near"
    return "?"


def test_slips_are_the_kinds_people_make():
    """Neighbouring keys, a letter dropped, doubled or swapped, a space left out; now and then
    the first letter and short words ("teh"); never another common word at a keyboard."""
    kinds, first, short = {}, 0, 0
    for out, _, _ in _slips(False):
        k = _kind(LINE, out)
        kinds[k] = kinds.get(k, 0) + 1
        for w, x in zip(LINE.split(), out.split()):
            if w != x and len(w) == len(x) and k not in ("shift", "merge"):
                first += w[0] != x[0]
                short += len(w) == 3
                assert x not in COMMON_WORDS
    assert set(kinds) >= {"near", "drop", "swap", "double", "merge", "shift"} and "?" not in kinds
    assert "auto" not in kinds
    assert first > 0 and short > 0


def test_a_phone_slip_is_often_autocorrect():
    autos = [out for out, _, fix in _slips(True) if _kind(LINE, out) == "auto"]
    assert len(autos) > 50
    for out in autos:
        (w, x), = [(w, x) for w, x in zip(LINE.split(), out.split()) if w != x]
        assert x in _AUTOCORRECT[w]


def test_only_a_slip_that_matters_carries_a_fix():
    for out, slip, fix in _slips(False):
        kind = _kind(LINE, out)
        if kind in ("double", "merge", "shift"):
            assert not fix and not slip
        if fix:
            assert len(fix) >= 4 and fix not in ("that", "more") and carries(out, slip)
    assert any(fix for _, _, fix in _slips(False))


def test_names_links_marks_and_shorthand_never_slip():
    text = "Arkon got [Shadowmourne] lol xD and Belra says tbh"
    skip = frozenset({"arkon", "belra", "lol", "tbh"})
    for seed in range(400):
        out, _, _ = _slip(text, 0.15, random.Random(seed), skip, False)
        for keep in ("Arkon", "[Shadowmourne]", "lol", "xD", "Belra", "tbh"):
            assert keep in out, out


def test_textured_lines_keep_protected_and_real_name_spelling():
    t = T(typo_rate=0.15, abbreviations={"you": "u"})
    for who in NAMES[:20]:
        for seed in range(10):
            text = "Quillon, Arkon thinks you're ready. definitely. Arkon pulls"
            line = " ".join(apply_style(text, t, DRUNK, random.Random(seed), speaker=who,
                                        texture=True, protect=frozenset({"Arkon"}),
                                        keep_case=frozenset({"Quillon"})))
            assert line.startswith("Quillon") and line.count("arkon") == 2, line


# -- habits --------------------------------------------------------------------------------------

def test_habits_are_her_own_some_of_the_time_and_spelling_only_for_careful_typers():
    t = T(typo_rate=0.02)
    careful = T(case="proper", end_punct="full", typo_rate=0.02)
    slips = {w for w, _ in _FINGERS + _LOOSE}
    for who in NAMES:
        assert not {w for w, _, _ in texture_of(careful, who).habits} & slips
    tex = Texture(casual=1, apostrophes=0, join=0, commas=0, shorthand=0,
                  habits=(("definitely", "definately", 0.7), ("the", "teh", 0.15)))
    got = [_textured("definitely the best, the tank said definitely", t, tex, SOBER,
                     random.Random(s), frozenset(), phone=False)[0] for s in range(400)]
    definately = sum(g.count("definately") for g in got) / 800
    teh = sum(g.count("teh") for g in got) / 800
    assert 0.55 < definately < 0.85 and 0.05 < teh < 0.3


# -- corrections (realism) -----------------------------------------------------------------------

def test_a_careful_typer_corrects_more_often():
    assert fix_share(T(case="proper", end_punct="full")) > fix_share(T(case="mixed")) \
        > fix_share(T())
    assert 0.1 <= fix_share(T()) and fix_share(T(case="proper", end_punct="full")) <= 0.35


def test_a_burst_keeps_the_slip_with_its_part():
    from leeroy_mind.talk.realism import split_line
    line = Styled("we pulled early. the healer was amking chili. then we wiped",
                  "we pulled early. the healer was making chili. then we wiped", "amking", "making")
    parts = split_line(line)
    assert len(parts) == 3
    assert [(p.slip, p.fix) for p in parts] == [("", ""), ("amking", "making"), ("", "")]


# -- review of #321 -----------------------------------------------------------------------------

SWEAR_BAIT = ["give me a sec to log", "nice shirt you have on", "count me in",
              "we one shot the boss", "the duck pond", "sick pull", "that was bigger",
              "i like it a lot", "pass the flask", "think about it, regard the boss"]


@pytest.mark.parametrize("phone", [False, True])
def test_a_slip_never_types_a_swear_or_sexual_word_she_did_not_write(phone):
    """P3-1: "sec" -> "sex", "shirt" -> "shit", "count" -> "cunt", "duck" -> "dick"."""
    from leeroy_mind.talk.style import NEVER_TYPED
    changed = 0
    for line in SWEAR_BAIT:
        for seed in range(3000):
            out, _, _ = _slip(line, 0.12, random.Random(seed), frozenset(), phone)
            changed += out != line
            assert not set(re.findall(r"[a-z]+", out.lower())) & NEVER_TYPED, (line, out)
    assert changed > 1000  # these lines still get their ordinary slips


def test_the_old_typo_path_never_types_one_either():
    from leeroy_mind.talk.style import NEVER_TYPED, _typo
    for word in ("count", "shirt", "duck", "sick", "shot", "bigger", "pass", "fact", "shitt"):
        for seed in range(3000):
            out = _typo(word, random.Random(seed))
            assert out == word or out.casefold() not in NEVER_TYPED, (word, out)
    # the switch-off path as a whole: "count" is typo'd, but never into "cunt"
    t = T(typo_rate=0.15)
    outs = {apply_style("count me in", t, DRUNK, random.Random(s))[0] for s in range(2000)}
    assert len(outs) > 1 and "cunt me in" not in outs


def test_a_one_or_two_word_fragment_runs_on_with_a_comma_never_a_space():
    """P3-3: "stop. pull the boss" must never become "stop pull the boss"."""
    seen = set()
    for who in NAMES:
        for seed in range(6):
            for text, short in (("stop. pull the boss", "stop"), ("pull the boss. now", "boss"),
                                ("ok sure. we go after the next pull", "sure")):
                line = typed(text, T(), seed, who)
                rest = line.replace(f"{short}, ", "").replace(f"{short}. ", "")
                assert f"{short} " not in rest, line
                seen.add(line)
    assert {"stop, pull the boss", "pull the boss, now",
            "ok sure, we go after the next pull"} <= seen


def test_quotes_and_web_addresses_are_typed_as_written():
    """P3-4: shorthand, habits, apostrophes, slips and autocorrect never touch a quoted phrase
    or a web address."""
    t = T(abbreviations={"you": "u", "because": "bc"}, typo_rate=0.15)
    tex = Texture(casual=1, apostrophes=1, join=0, commas=0, shorthand=1,
                  habits=(("weird", "wierd", 1.0), ("were", "where", 1.0)))
    kept = ['"i don\'t know because you were weird"', "“you were weird”",
            "‘because you were weird’", "'you were weird'",
            "https://www.weird.com/because/you"]
    for quote in kept:
        changed = 0
        for seed in range(300):
            text = f"you said {quote} because you were weird"
            out, _, _ = _textured(text, t, tex, DRUNK, random.Random(seed), frozenset(),
                                  phone=bool(seed % 2))
            assert quote in out, (quote, out)
            changed += out.endswith("bc u where wierd") or "u said" in out
        assert changed == 300  # outside it, all of them apply
    # run-ons too: a quoted phrase keeps its full stops
    line = typed('he said "stop. pull now" ok. go', T(), 0, "Member1")
    assert '"stop. pull now"' in line
