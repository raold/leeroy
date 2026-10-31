import dataclasses
import logging
import random
import re
from collections import Counter

import pytest

from conftest import _base_persona_dict
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.storage.rows import ROMANCE_STAGES, Relationship
from leeroy_mind.talk import romance
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.romance import (ASIDE_INSTRUCTIONS, ATTITUDE_TEXT, BANDS,
                                      BUILTIN_ROMANCE, FULL_DIAL, STYLE_TEXT, TRIGGER_TEXT,
                                      AsidePlan, JealousyAsides, WhisperPlanner, band,
                                      dial_for, effective_band, rivalry_reply, rivalry_text,
                                      romance_instruction, stage_text)
from leeroy_mind.talk.turns import TurnKind

# Invented identity: the real name lives only in the owner's local mind.toml.
GM = HumanIdentity(1, "Zanthor", "Zan", "Quillon", True, pronouns="he")
R = random.Random
ATTITUDES = ("fond", "drawn", "lust")
CHANNELS = ("guild", "party", "raid", "say", "officer", "whisper")
MODES = ("good_sport", "sulker", "grumbler", "teaser", "play_by_play")
OVERLAY = "LOCAL-OVERLAY"


def woman(guid=201, name="Belra", stance="drawn", style="bold", dial=None):
    d = _base_persona_dict(guid, name)
    d["identity"]["player_gender"] = "female"
    d["romance"] = {"stance": stance, "style": style, "jealousy_mode": None, "dial": dial}
    return persona_from_dict(d)


def man(guid, name, mode, stance="none"):
    d = _base_persona_dict(guid, name)
    d["romance"] = {"stance": stance, "style": None, "jealousy_mode": mode}
    return persona_from_dict(d)


def rel(stage="none", affinity=30.0, a=201):
    return Relationship(a=a, b=1, affinity=affinity, romance_stage=stage)


def ri(p, data_dir, r=None, *, dial=9, channel="guild", to_human=True,
       phase=DayPhase.EVENING, human=GM):
    return romance_instruction(p, r if r is not None else rel(), dial=dial, channel=channel,
                               addressee_is_human=to_human, phase=phase, human=human,
                               data_dir=data_dir)


@pytest.fixture
def overlay_dir(tmp_path):
    d = tmp_path / "with-overlay"
    (d / "prompts").mkdir(parents=True)
    (d / "prompts" / "romance_explicit.txt").write_text(OVERLAY + "\n", encoding="utf-8")
    return d


# -- romance instruction -------------------------------------------------------------------

def test_only_attitudes_and_only_toward_human(tmp_path):
    assert ri(woman(stance="none", style=None), tmp_path, channel="whisper") is None
    for stance in ATTITUDES:
        p = woman(stance=stance)
        for channel in ("guild", "whisper"):
            assert ri(p, tmp_path, channel=channel, to_human=False) is None
            assert ri(p, tmp_path, channel=channel)


def test_band_mapping():
    assert [band(d) for d in (1, 3, 4, 6, 7, 8, 9, 10)] == \
        ["warm"] * 2 + ["flirty"] * 2 + ["suggestive"] * 2 + ["explicit"] * 2
    assert BANDS == ("warm", "flirty", "suggestive", "explicit")


def test_attitude_sets_the_intensity(tmp_path):
    assert [effective_band(s, "none", 8) for s in ATTITUDES] == ["warm", "flirty", "suggestive"]
    # the controller's ruling (the owner's "no brakes" at dial 9): from 9 a drawn woman
    # reaches one band further
    assert [effective_band(s, "none", 9) for s in ATTITUDES] == ["warm", "suggestive",
                                                                  "explicit"]
    assert effective_band("drawn", "none", 10) == "suggestive"
    assert effective_band("drawn", "banter", 9) == "explicit"
    fond, drawn, lust = (ri(woman(stance=s), tmp_path) for s in ATTITUDES)
    assert BUILTIN_ROMANCE["warm"] in fond
    assert BUILTIN_ROMANCE["suggestive"] in drawn
    assert BUILTIN_ROMANCE["flirty"] in ri(woman(stance="drawn"), tmp_path, dial=8)
    assert BUILTIN_ROMANCE["suggestive"] in lust  # explicit without an overlay
    assert len({fond, drawn, lust}) == 3
    assert effective_band("none", "acknowledged", 10) is None


def test_explicit_on_any_channel_and_any_stage(overlay_dir):
    p = woman(stance="lust")
    for channel in CHANNELS:
        for stage in ROMANCE_STAGES:
            assert OVERLAY in ri(p, overlay_dir, rel(stage), channel=channel)


def test_stage_colours_but_never_caps(tmp_path):
    for stance in ATTITUDES:
        for dial in range(1, 11):
            floor = BANDS.index(effective_band(stance, "none", dial))
            for stage in ROMANCE_STAGES:
                got = BANDS.index(effective_band(stance, stage, dial))
                assert floor <= got <= BANDS.index(band(dial))
    # a deeper stage brings more intimacy
    assert effective_band("drawn", "acknowledged", 9) == "explicit"
    assert effective_band("fond", "vulnerable", 9) == "suggestive"
    p = woman(stance="drawn")
    texts = {stage: ri(p, tmp_path, rel(stage)) for stage in ROMANCE_STAGES}
    assert len(set(texts.values())) == len(ROMANCE_STAGES)  # every stage reads differently
    for stage in ROMANCE_STAGES:
        if stage_text("drawn", stage):
            assert stage_text("drawn", stage).format(name="Zan", them="him",
                                                     their="his") in texts[stage]


def test_the_opening_stage_reads_by_attitude(tmp_path):
    for stance in ("drawn", "lust"):  # into him from the start: no "starting to notice"
        text = ri(woman(stance=stance), tmp_path, rel("notices"))
        assert "caught yourself" not in text and "had your eye on Zan" in text
    fond = ri(woman(stance="fond", style=None), tmp_path, rel("notices"))
    assert "caught yourself noticing Zan" in fond


def test_men_read_as_friends(tmp_path):
    for stance in ATTITUDES:  # a seed man may be marked fond: no romance frame for him
        assert ri(man(301, "Coros", "teaser", stance), tmp_path, rel(a=301)) is None
    cold = {301: (man(301, "Coros", "teaser", "drawn"), rel("notices", 0.0, a=301))}
    assert all(WhisperPlanner(R(i)).maybe(1000.0, "idle", cold) is None for i in range(300))
    drawn = {201: woman(201, "Belra", "drawn"), 301: man(301, "Coros", "teaser", "lust"),
             202: woman(202, "Dunna", "lust")}
    hits = {rivalry_reply(201, drawn, 10, R(i)) for i in range(500)}
    assert hits == {None, 202}


def test_dial_is_the_ceiling(overlay_dir):
    lust = woman(stance="lust")
    for dial in (1, 2, 3):
        text = ri(lust, overlay_dir, dial=dial)
        assert BUILTIN_ROMANCE["warm"] in text and OVERLAY not in text
    assert BUILTIN_ROMANCE["suggestive"] in ri(lust, overlay_dir, dial=7)
    assert OVERLAY not in ri(lust, overlay_dir, dial=8)
    assert OVERLAY in ri(lust, overlay_dir, dial=9)


def test_explicit_uses_local_overlay(overlay_dir, tmp_path, caplog):
    assert BUILTIN_ROMANCE["explicit"] == BUILTIN_ROMANCE["suggestive"]
    p = woman(stance="lust")
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        text = ri(p, overlay_dir, channel="guild")
    assert OVERLAY in text and BUILTIN_ROMANCE["suggestive"] not in text
    assert caplog.text == ""
    bare = tmp_path / "no-overlay"
    bare.mkdir()
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        texts = [ri(p, bare, channel=c) for c in ("guild", "whisper", "raid")]
    assert all(BUILTIN_ROMANCE["suggestive"] in t and OVERLAY not in t for t in texts)
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "romance_explicit" in warned[0].getMessage()


def test_invariants_always_present(tmp_path):
    for stance in ATTITUDES:
        for style in (None, "shy", "competitive"):
            p = woman(stance=stance, style=style)
            for dial in (1, 5, 8, 9, 10):
                for channel in CHANNELS:
                    for stage in ROMANCE_STAGES:
                        for phase in DayPhase:
                            text = ri(p, tmp_path, rel(stage), dial=dial, channel=channel,
                                      phase=phase)
                            assert "two adults" in text and "you both want" in text
                            assert "only ever with Zan" in text
                            assert "Quillon" not in text and "{" not in text
                            assert not any(c in text for c in ";—–")


def humoured(guid, humour, *, stance="lust", extraversion=7, style=None, dial=None):
    d = _base_persona_dict(guid, f"W{guid}")
    d["identity"]["player_gender"] = "female"
    d["humour"] = humour
    d["big_five"]["extraversion"]["score"] = extraversion
    d["romance"] = {"stance": stance, "style": style, "jealousy_mode": None, "dial": dial}
    return persona_from_dict(d)


def test_a_woman_without_a_style_gets_one_from_her_humour(tmp_path):
    """Prompts I5: 6 of 15 lust and 9 of 22 drawn women have no style, so their blocks were
    word for word the same. Each humour now gives its own way, the same every time."""
    texts = {h: ri(humoured(250 + i, h), tmp_path)
             for i, h in enumerate(("dry", "teasing", "absurd", "self-deprecating", "puns",
                                    "dark"))}
    assert len(set(texts.values())) == 6
    assert texts == {h: ri(humoured(250 + i, h), tmp_path)
                     for i, h in enumerate(texts)}  # deterministic
    assert "puns and double meanings" in texts["puns"]
    assert "putting yourself down" in texts["self-deprecating"]
    fill = dict(name="Zan", them="him", their="his")
    assert STYLE_TEXT["theatrical"].format(**fill) in texts["absurd"]
    # a style of her own still wins
    assert STYLE_TEXT["shy"].format(**fill) in ri(humoured(260, "puns", style="shy"), tmp_path)


def test_not_every_lust_woman_is_the_most_forward(tmp_path):
    """Prompts I5: all fifteen lust women were told "nobody in the guild is more forward".
    How forward she is follows her extraversion."""
    loud, mid, quiet = (ri(humoured(270 + e, "dry", extraversion=e), tmp_path)
                        for e in (9, 5, 2))
    assert len({loud, mid, quiet}) == 3
    assert not any("more forward" in t for t in (loud, mid, quiet))
    assert "one of the boldest" in loud and "more than you let on" in quiet
    drawn_loud = ri(humoured(280, "dry", stance="drawn", extraversion=8), tmp_path)
    drawn_quiet = ri(humoured(281, "dry", stance="drawn", extraversion=3), tmp_path)
    assert drawn_loud != drawn_quiet


def test_the_romance_block_reads_in_order(tmp_path):
    """Prompts I5: her attitude first, the hook on what's happening last, and the line about
    two adults in the middle, where it reads as a frame and not as a line to say back."""
    text = ri(woman(stance="lust", style="bold"), tmp_path)
    assert text.startswith("You want Zan")
    adults = text.index("two adults")
    assert text.index("Your way") < adults < text.index("actually happening")
    assert text.rstrip().endswith("never a canned line.")


def test_a_long_overlay_is_capped_on_its_own(tmp_path, caplog):
    """Prompts minor 6: the built-ins used about 134 of the 300-token cap on the whole extra,
    so a long overlay lost its end. The overlay is capped by itself; nothing else is cut."""
    d = tmp_path / "long"
    (d / "prompts").mkdir(parents=True)
    words = " ".join(f"word{i}" for i in range(400))
    (d / "prompts" / "romance_explicit.txt").write_text(words + "\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        text = ri(woman(stance="lust"), d)
        ri(woman(stance="lust"), d)
    assert "word0 " in text and "word399" not in text
    assert "two adults" in text and text.rstrip().endswith("never a canned line.")
    capped = [r for r in caplog.records if "overlay" in r.getMessage()]
    assert len(capped) == 1 and "word" not in capped[0].getMessage()


def test_romance_follows_his_pronouns(tmp_path):
    for stance in ATTITUDES:
        he = ri(woman(stance=stance), tmp_path)
        assert re.search(r"\b(him|his)\b", he)
        she = ri(woman(stance=stance), tmp_path,
                 human=dataclasses.replace(GM, pronouns="she"))
        assert re.search(r"\bher\b", she) and not re.search(r"\b(him|his)\b", she)
        unset = ri(woman(stance=stance), tmp_path, human=dataclasses.replace(GM, pronouns=""))
        assert re.search(r"\b(them|their)\b", unset) and not re.search(r"\b(him|his)\b", unset)


def test_style_and_occasion_in_the_text(tmp_path):
    fill = dict(name="Zan", them="him", their="his")
    for style, text in STYLE_TEXT.items():
        assert text.format(**fill) in ri(woman(style=style), tmp_path)
    no_style = ri(woman(style=None), tmp_path)  # her humour is dry: the guarded way
    assert STYLE_TEXT["guarded"].format(**fill) in no_style
    whisper = ri(woman(), tmp_path, channel="whisper")
    guild = ri(woman(), tmp_path, channel="guild")
    assert "private" in whisper and "private" not in guild
    late = ri(woman(), tmp_path, phase=DayPhase.LATE)
    assert "late" in late and "late" not in ri(woman(), tmp_path, phase=DayPhase.DAY)
    assert "late" not in ri(woman(), tmp_path, phase=DayPhase.MORNING)  # 09:00 isn't late
    assert "actually happening" in guild  # specific, never a canned line


def test_no_brakes_left():
    for gone in ("FlirtBudget", "STAGE_ALLOWS", "STAGE_CAP"):
        assert not hasattr(romance, gone)


# -- the men's jealousy asides ---------------------------------------------------------------

def _men(*modes):
    names = ["Coros", "Dunna", "Ebbo", "Fendrel", "Gruk", "Tamsk"]
    return {301 + i: man(301 + i, names[i], m) for i, m in enumerate(modes)}


def test_asides_budget_and_combat():
    j = JealousyAsides(R(1), jealousy=5, human=GM)
    men = _men(*MODES)
    belra = woman()
    starts = []
    for minute in range(180):
        now = 10_000.0 + minute * 60
        plans = j.on_trigger("public_flirt", belra, now, RaidPhase.NONE, men)
        if plans:
            starts.append(now)
    for t in starts:  # never more than 2 in any rolling hour
        assert sum(t <= s < t + 3600 for s in starts) <= 2
    assert 4 <= len(starts) <= 6
    assert j.on_trigger("public_flirt", belra, 1e9, RaidPhase.COMBAT, men) == []
    assert j.on_trigger("loot_cheer", belra, 1e9, RaidPhase.WIPE_SILENCE, men) == []
    quiet = JealousyAsides(R(2), jealousy=0, human=GM)
    assert all(quiet.on_trigger("public_flirt", belra, 10_000.0 + i * 4000, RaidPhase.NONE,
                                men) == [] for i in range(300))
    j.set_jealousy(0)
    assert j.on_trigger("public_flirt", belra, 1e9, RaidPhase.NONE, men) == []


def test_aside_chance_and_budget_follow_jealousy():
    belra, men = woman(), _men(*MODES)

    def rate(jealousy):
        return sum(bool(JealousyAsides(R(i), jealousy=jealousy, human=GM).on_trigger(
            "public_flirt", belra, 10_000.0, RaidPhase.TRASH, men)) for i in range(1000)) / 1000

    assert 0.45 <= rate(5) <= 0.55 and 0.85 <= rate(10) <= 0.95 and 0.15 <= rate(2) <= 0.25
    j = JealousyAsides(R(3), jealousy=10, human=GM)
    got = sum(bool(j.on_trigger("public_flirt", belra, 10_000.0 + i, RaidPhase.NONE, men))
              for i in range(200))
    assert got == 4  # round(2 * 10 / 5) an hour


def test_aside_speakers_by_mode_weight():
    belra, men = woman(), _men(*MODES)
    c = Counter()
    for i in range(3000):
        plans = JealousyAsides(R(i), jealousy=10, human=GM).on_trigger(
            "loot_cheer", belra, 10_000.0, RaidPhase.NONE, men)
        if plans:
            c[men[plans[0].speaker].romance.jealousy_mode] += 1
    assert c["play_by_play"] > 1.8 * c["good_sport"]
    assert c["teaser"] > 1.4 * c["good_sport"]
    assert set(c) == set(MODES)


def test_good_sport_defuses_sometimes():
    belra, men = woman(), _men("sulker", "good_sport")
    after_sulk = defused = 0
    for i in range(2000):
        plans = JealousyAsides(R(i), jealousy=10, human=GM).on_trigger(
            "loot_cheer", belra, 10_000.0, RaidPhase.NONE, men)
        if not plans:
            continue
        assert all(isinstance(x, AsidePlan) and x.kind is TurnKind.ASIDE for x in plans)
        if plans[0].speaker == 301:
            after_sulk += 1
            if len(plans) == 2:
                defused += 1
                assert plans[1].speaker == 302 and "Coros" in plans[1].instruction
        else:
            assert len(plans) == 1  # a good sport's own aside needs no defusing
    assert 0.4 <= defused / after_sulk <= 0.6
    alone = _men("sulker")
    assert all(len(JealousyAsides(R(i), jealousy=10, human=GM).on_trigger(
        "public_flirt", belra, 10_000.0, RaidPhase.NONE, alone)) <= 1 for i in range(200))


def test_aside_instructions_are_envy_never_cruel():
    assert set(ASIDE_INSTRUCTIONS) == {*MODES, "defuse"}
    belra = woman()
    seen = {}
    for trigger in ("public_flirt", "loot_cheer"):
        for mode in MODES:
            for i in range(50):
                plans = JealousyAsides(R(i), jealousy=10, human=GM).on_trigger(
                    trigger, belra, 10_000.0, RaidPhase.NONE, _men(mode))
                if plans:
                    seen[(trigger, mode)] = plans[0].instruction
                    break
    assert len(seen) == 10
    for (trigger, mode), text in seen.items():
        assert "never cruel" in text and "Belra" in text and "Zan" in text
        assert "Quillon" not in text and "{" not in text and not any(c in text for c in ";—–")
    assert "loot" in seen[("loot_cheer", "teaser")] and "flirting" in seen[("public_flirt",
                                                                             "teaser")]
    with pytest.raises(ValueError):
        JealousyAsides(R(1), jealousy=5, human=GM).on_trigger(
            "a_wink", belra, 0.0, RaidPhase.NONE, _men("teaser"))


def test_every_aside_trigger_is_one_the_engine_fires():
    """Spec minor 2: "went_quiet" was defined and never fired. The asides answer what the
    engine sets off (reactions.py): a public flirt and a loot cheer."""
    assert set(TRIGGER_TEXT) == {"public_flirt", "loot_cheer"}
    with pytest.raises(ValueError):
        JealousyAsides(R(1), jealousy=5, human=GM).on_trigger(
            "went_quiet", woman(), 0.0, RaidPhase.NONE, _men("teaser"))


def test_asides_fit_their_mode_and_never_flirt():
    """Prompts minor 2 and privacy minor 6: envy is the sour and teasing modes' (a good sport
    or a commentator isn't told he is envious); nothing assumes a history ("again"); nothing
    contradicts the teaser; and the man is told he never flirts with her or anyone himself."""
    belra = woman()
    for mode in MODES:
        for trigger in ("public_flirt", "loot_cheer"):
            plans = next(p for i in range(200) if (p := JealousyAsides(
                R(i), jealousy=10, human=GM).on_trigger(trigger, belra, 10_000.0,
                                                        RaidPhase.NONE, _men(mode))))
            text = plans[0].instruction
            assert "never flirt with her or anyone else yourself" in text
            assert ("envious" in text) == (mode in ("sulker", "grumbler", "teaser")), mode
            assert " again" not in text and "beyond the game" not in text


def test_the_woman_never_answers_her_own_flirt():
    belra = woman(guid=301)
    men = _men("teaser", "sulker")  # 301 is a key here too, as if she were listed
    for i in range(300):
        plans = JealousyAsides(R(i), jealousy=10, human=GM).on_trigger(
            "public_flirt", belra, 10_000.0, RaidPhase.NONE, men)
        assert all(x.speaker != 301 for x in plans)


# -- the women's rivalry ---------------------------------------------------------------------

def test_rivalry_reply():
    drawn = {201: woman(201, "Belra", "drawn"), 202: woman(202, "Dunna", "lust"),
             203: woman(203, "Ebba", "fond", None)}
    got = [rivalry_reply(201, drawn, 10, R(i)) for i in range(3000)]
    hits = [g for g in got if g is not None]
    assert 0.26 <= len(hits) / 3000 <= 0.34
    assert set(hits) == {202}  # never the flirter, never a fond woman
    assert all(rivalry_reply(201, drawn, 0, R(i)) is None for i in range(300))
    half = sum(rivalry_reply(201, drawn, 5, R(i)) is not None for i in range(3000)) / 3000
    assert 0.12 <= half <= 0.18
    assert rivalry_reply(201, {201: drawn[201]}, 10, R(1)) is None
    text = rivalry_text(drawn[201], GM)
    assert "Belra" in text and "Zan" in text and "his attention" in text
    assert "never nasty" in text and not any(c in text for c in ";—–")


# -- unprompted whispers ---------------------------------------------------------------------

def test_whisper_pacing_without_caps():
    cands = {201: (woman(201, "Belra", "drawn"), rel("none", 0.0, a=201)),
             202: (woman(202, "Dunna", "lust"), rel("notices", 5.0, a=202)),
             203: (woman(203, "Ebba", "fond", None), rel("none", 10.0, a=203))}
    w = WhisperPlanner(R(4))
    w.start_session()
    last: dict[int, float] = {}
    by: Counter = Counter()
    for minute in range(240):
        now = 50_000.0 + minute * 60
        g = w.maybe(now, ("after_kill", "idle", "late")[minute % 3], cands)
        if g is None:
            continue
        assert now - last.get(g, -1e9) >= 600  # one woman, 10 minutes apart at least
        last[g] = now
        by[g] += 1
    assert sum(by.values()) > 5  # no per-session cap
    assert set(by) == {201, 202, 203}  # no stage or affinity threshold
    with pytest.raises(ValueError):
        w.maybe(0.0, "bored", cands)


def test_whisper_chance_and_who_has_a_reason():
    one = {201: (woman(201, "Belra", "drawn"), rel("none", 0.0))}
    n = sum(WhisperPlanner(R(i)).maybe(1000.0, "idle", one) is not None for i in range(2000))
    assert 0.26 <= n / 2000 <= 0.34
    cold = {301: (man(301, "Coros", "teaser"), rel("none", 0.0, a=301))}
    assert all(WhisperPlanner(R(i)).maybe(1000.0, "idle", cold) is None for i in range(300))
    friend = {301: (man(301, "Coros", "teaser"), rel("none", 60.0, a=301))}
    assert any(WhisperPlanner(R(i)).maybe(1000.0, "late", friend) == 301 for i in range(50))


def test_whisper_pacing_survives_a_relog():
    one = {201: (woman(201, "Belra", "lust"), rel())}
    w = WhisperPlanner(R(1))
    t = 1000.0
    while w.maybe(t, "idle", one) is None:
        t += 1
    w.start_session()  # he logged out and back in
    assert all(w.maybe(t + 60 + i, "idle", one) is None for i in range(500))
    assert any(w.maybe(t + 600 + i, "idle", one) == 201 for i in range(100))


def test_an_aside_never_said_gives_its_place_in_the_hour_back():
    """romance.py:247: an aside counted when it was planned, so one that was dropped (the
    guards, the model's silence, a pull) still used up the hour. Released, it never did."""
    j = JealousyAsides(R(1), jealousy=2, human=GM)  # one an hour
    men, belra, t = _men(*MODES), woman(), 10_000.0
    plans = []
    while not plans:
        plans = j.on_trigger("public_flirt", belra, t, RaidPhase.NONE, men)
        t += 10
    assert all(j.on_trigger("public_flirt", belra, t + i, RaidPhase.NONE, men) == []
               for i in range(100))  # it counts while its turn is pending
    j.release(plans[0])
    j.release(plans[0])  # twice is once: it frees no one else's place
    again = next(p for i in range(500)
                 if (p := j.on_trigger("public_flirt", belra, t + 200 + i, RaidPhase.NONE, men)))
    assert all(j.on_trigger("public_flirt", belra, t + 800 + i, RaidPhase.NONE, men) == []
               for i in range(100))  # and the new one holds the hour
    assert again[0].counted and not AsidePlan(301, "x", counted=False).counted


def test_a_new_identity_keeps_the_asides_hourly_count():
    j = JealousyAsides(R(5), jealousy=5, human=GM)  # 2 an hour
    men, belra = _men(*MODES), woman()
    t, started = 10_000.0, []
    while len(started) < 2:
        if j.on_trigger("public_flirt", belra, t, RaidPhase.NONE, men):
            started.append(t)
        t += 10
    j.set_human(HumanIdentity(1, "Zanthor", "Zed", "Quillon", True, pronouns="he"))
    assert all(j.on_trigger("public_flirt", belra, t + i, RaidPhase.NONE, men) == []
               for i in range(100))  # still the same hour
    later = next(p for i in range(500)
                 if (p := j.on_trigger("public_flirt", belra, started[0] + 3600 + i,
                                       RaidPhase.NONE, men)))
    assert "Zed" in later[0].instruction and "Zan " not in later[0].instruction


# -- the whisper overlay (rehearsal 2, judge C 1-2) -------------------------------------------

def test_the_whisper_overlay_is_read_only_at_the_explicit_band_in_a_whisper(tmp_path):
    from leeroy_mind.talk.romance import OVERLAY_CAPS, WHISPER_OVERLAY, whisper_tail
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / f"{WHISPER_OVERLAY}.txt").write_text("WHISPER-OVERLAY\n",
                                                               encoding="utf-8")
    lust, drawn, fond = (woman(stance=s) for s in ("lust", "drawn", "fond"))

    def tail(p, *, dial=9, channel="whisper", r=None):
        return whisper_tail(p, r or rel(), dial=dial, channel=channel, data_dir=tmp_path)
    assert tail(lust) == "WHISPER-OVERLAY"
    assert tail(lust, channel="guild") == "" and tail(lust, dial=8) == ""
    assert tail(drawn) == "" and tail(drawn, r=rel("banter")) == "WHISPER-OVERLAY"
    assert tail(fond) == ""
    bare = tmp_path / "bare"
    bare.mkdir()
    assert whisper_tail(lust, rel(), dial=9, channel="whisper", data_dir=bare) == ""
    assert OVERLAY_CAPS[WHISPER_OVERLAY] == romance.OVERLAY_CAP_TOKENS
    assert {f"romance_{b}" for b in BANDS} < set(OVERLAY_CAPS)


# -- a member's own dial (fix wave G4) ---------------------------------------------------------

def test_her_own_dial_counts_only_for_a_woman_with_an_attitude():
    for stance in ATTITUDES:
        assert dial_for(woman(stance=stance, dial=10), 3) == 10
        assert dial_for(woman(stance=stance, dial=1), 9) == 1
        assert dial_for(woman(stance=stance), 7) == 7  # none of her own: the owner's
    assert dial_for(woman(stance="none", style=None, dial=10), 3) == 3
    friend = man(301, "Coros", "teaser", "lust")
    friend = dataclasses.replace(friend, romance=dataclasses.replace(friend.romance, dial=10))
    assert dial_for(friend, 3) == 3


def test_her_own_dial_stands_in_for_the_owners_in_everything_she_reads(tmp_path):
    """The band, the no-brakes lift for a drawn woman and the stage's depth all follow her dial
    when she has one: the text is the one the owner's dial at that setting gives her."""
    for stance in ATTITUDES:
        for own in range(1, 11):
            mine = woman(stance=stance, dial=own)
            for owners in (1, 5, 9, 10):
                for stage in ("none", "banter", "acknowledged"):
                    assert ri(mine, tmp_path, rel(stage), dial=owners) == ri(
                        woman(stance=stance), tmp_path, rel(stage), dial=own), (
                        stance, own, owners, stage)


def test_her_own_dial_raises_and_lowers_her_band_past_the_owners(overlay_dir):
    assert OVERLAY in ri(woman(stance="lust", dial=9), overlay_dir, dial=1)
    lowered = ri(woman(stance="lust", dial=3), overlay_dir, dial=10)
    assert BUILTIN_ROMANCE["warm"] in lowered and OVERLAY not in lowered
    assert BUILTIN_ROMANCE["suggestive"] in ri(woman(stance="drawn", dial=9), overlay_dir, dial=1)
    assert BUILTIN_ROMANCE["flirty"] in ri(woman(stance="drawn", dial=8), overlay_dir, dial=10)
    # the owner's dial still governs a woman with none of her own
    assert OVERLAY in ri(woman(stance="lust"), overlay_dir, dial=9)
    assert OVERLAY not in ri(woman(stance="lust"), overlay_dir, dial=3)


def test_at_her_own_dial_ten_a_lust_woman_is_the_boldest_whatever_her_extraversion(tmp_path):
    bold = ATTITUDE_TEXT["lust"]["bold"].format(name="Zan", them="him", their="his")
    for e in range(1, 11):
        mine = humoured(300 + e, "dry", extraversion=e, dial=FULL_DIAL)
        assert bold in ri(mine, tmp_path, dial=1)
    # at the owner's dial 10, and at her own 9, her extraversion still decides
    assert bold not in ri(humoured(320, "dry", extraversion=2), tmp_path, dial=FULL_DIAL)
    assert "more than you let on" in ri(humoured(320, "dry", extraversion=2), tmp_path,
                                        dial=FULL_DIAL)
    assert bold not in ri(humoured(321, "dry", extraversion=2, dial=9), tmp_path, dial=FULL_DIAL)


def test_her_own_dial_governs_her_whisper_overlay(tmp_path):
    from leeroy_mind.talk.romance import WHISPER_OVERLAY, explicit_whisper, whisper_tail
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / f"{WHISPER_OVERLAY}.txt").write_text("WHISPER-OVERLAY\n",
                                                               encoding="utf-8")

    def tail(p, dial):
        return whisper_tail(p, rel(), dial=dial, channel="whisper", data_dir=tmp_path)
    raised, lowered, plain = (woman(stance="lust", dial=d) for d in (9, 8, None))
    assert explicit_whisper(raised, rel(), dial=1, channel="whisper")
    assert tail(raised, 1) == "WHISPER-OVERLAY"
    assert not explicit_whisper(lowered, rel(), dial=10, channel="whisper")
    assert tail(lowered, 10) == ""
    assert tail(plain, 9) == "WHISPER-OVERLAY" and tail(plain, 8) == ""
