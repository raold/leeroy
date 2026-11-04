"""T4: each dial reaches the named points the proposal maps it to, and at its neutral value
reaches nothing (those paths are the stored persona's: dials_of is NEUTRAL)."""
import random
from datetime import datetime, timezone

import pytest

from conftest import make_persona
from leeroy_mind.guild.play import summon_waves
from leeroy_mind.guild.roles import reliability
from leeroy_mind.human import HumanIdentity
from leeroy_mind.nightly import gossip
from leeroy_mind.nightly.storyteller import LAYER_TRUST, layer_trust, opens_up
from leeroy_mind.storage.rows import PersonaStateRow, Relationship
from leeroy_mind.talk.bits import BitScheduler
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.prompt import comms_rules
from leeroy_mind.talk.questions import fit_tier
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.romance import (FLIRT_CAP, JealousyAsides, WhisperPlanner, attitude_text,
                                      flirt_chance, rivalry_reply)
from leeroy_mind.talk.select import SpeakerContext, persona_keywords, speaker_weight
from leeroy_mind.tuning.effective import dials_of
from leeroy_mind.tuning.lines import BUILTIN_LINES
from leeroy_mind.tuning.state import Tuning

GM = HumanIdentity(1, "Testmage", "Zan", "", True, pronouns="he")


def tune(p, **dials):
    t = Tuning()
    for dial, value in dials.items():
        t = t.with_value("member", str(p.guid), dial, value)
    return t.apply(p)


def woman(guid=102, name="Belra", stance="lust", **kw):
    return make_persona(guid, name, player_gender="female", stance=stance, **kw)


CTX = SpeakerContext(now=10_000.0, topic=set(), opener=False, phase=DayPhase.EVENING)
STATE = PersonaStateRow(guid=101)


# -- talkative ----------------------------------------------------------------------------------

def test_talkative_scales_the_speaker_draw_and_zero_silences_the_draw():
    p = make_persona(101, "Arkon")
    w = speaker_weight(p, STATE, None, CTX, None, 1.3)
    assert speaker_weight(tune(p, talkative=10), STATE, None, CTX, None, 1.3) == pytest.approx(2 * w)
    assert speaker_weight(tune(p, talkative=0), STATE, None, CTX, None, 1.3) == 0.0
    assert speaker_weight(tune(p, humour=9), STATE, None, CTX, None, 1.3) == w


# -- forward / flirty ---------------------------------------------------------------------------

def test_forward_scales_her_flirt_chance_under_the_cap():
    p = woman(stance="drawn")
    base = flirt_chance(p, 1.0)
    assert flirt_chance(tune(p, forward=10), 1.0) == pytest.approx(2 * base)
    assert flirt_chance(tune(p, forward=10), 1.6) == FLIRT_CAP
    assert flirt_chance(tune(p, forward=0), 1.0) == 0.0


def test_forward_at_the_ends_moves_her_attitude_wording_never_her_stance():
    p = woman(stance="lust", extraversion={"score": 5, "behaviour": "x"})
    assert "boldest" not in attitude_text(p)
    bold = tune(p, forward=9)
    assert "boldest" in attitude_text(bold) and bold.romance.stance == "lust"
    assert "more than you let on" in attitude_text(tune(p, forward=1))
    assert attitude_text(tune(p, forward=6)) == attitude_text(p)


def test_forward_weights_her_unprompted_whispers():
    p, rel = woman(), Relationship(a=102, b=1)
    counts = {0: 0, 10: 0}
    for level in counts:
        for seed in range(300):
            plan = WhisperPlanner(random.Random(seed))
            if plan.maybe(1e6, "idle", {102: (tune(p, forward=level), rel)}) is not None:
                counts[level] += 1
    assert counts[0] == 0 and counts[10] > 50


# -- jealousy -----------------------------------------------------------------------------------

def test_a_man_at_jealousy_zero_is_never_the_one_who_reacts():
    teaser = make_persona(103, "Coros", jealousy_mode="teaser")
    sulker = make_persona(105, "Ebbo", jealousy_mode="sulker")
    flirter = woman()
    seen = set()
    for seed in range(60):
        asides = JealousyAsides(random.Random(seed), jealousy=10, human=GM)
        plans = asides.on_trigger("public_flirt", flirter, 1e6, RaidPhase.NONE,
                                  {103: tune(teaser, jealousy=0), 105: sulker})
        seen |= {p.speaker for p in plans if p.counted}
    assert seen == {105}
    asides = JealousyAsides(random.Random(1), jealousy=10, human=GM)
    assert asides.on_trigger("public_flirt", flirter, 1e6, RaidPhase.NONE,
                             {103: tune(teaser, jealousy=0)}) == []


def test_a_woman_at_jealousy_zero_never_cuts_in_and_ten_weights_her_up():
    a, b = woman(104, "Dunna", stance="drawn"), woman(105, "Ebbo", stance="drawn")
    picks = [rivalry_reply(102, {104: tune(a, jealousy=0), 105: b}, 10, random.Random(s))
             for s in range(200)]
    assert 104 not in picks and 105 in picks


# -- humour (how much) --------------------------------------------------------------------------

class _Bits:
    def last_bit_use(self, guid, bit_id):
        return None

    def voice_bit_used_on(self, night):
        return False


def test_humour_scales_how_often_bits_come_out():
    p = make_persona(101, "Arkon")
    local = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)

    def fired(q):
        s = BitScheduler(_Bits(), None, random.Random(5))
        return sum(s.pick(q, context=set(), raid=RaidPhase.NONE, mood=0.0, local=local) is not None
                   for _ in range(2000))
    base = fired(p)
    assert fired(tune(p, humour=0)) == 0
    assert fired(tune(p, humour=10)) > 1.6 * base


# -- theorycrafter and lore ---------------------------------------------------------------------

def test_interest_dials_move_the_topic_pull_and_who_fits_his_questions():
    lore = make_persona(101, "Arkon", thinks_as="lore")
    assert "stat" not in persona_keywords(lore)
    assert "stat" in persona_keywords(tune(lore, theorycraft=8))
    assert "lore" not in persona_keywords(tune(lore, lore=2))
    assert fit_tier("gear", lore) is None and fit_tier("item", tune(lore, theorycraft=8)) == 0
    assert fit_tier("lore", tune(lore, lore=3)) is None and fit_tier("lore", lore) == 1
    assert fit_tier("lore", tune(lore, lore=4)) == 1


def test_wording_dials_add_their_line_to_the_rules_block_and_neutral_adds_nothing(tmp_path):
    p = make_persona(101, "Arkon")
    assert comms_rules(GM, p, data_dir=tmp_path) == comms_rules(GM, p)
    far = tune(p, theorycraft=1, argumentative=9)
    rules = comms_rules(GM, far, data_dir=tmp_path)
    assert rules.startswith(comms_rules(GM, p))
    assert BUILTIN_LINES[("theorycraft", "low")] in rules
    assert BUILTIN_LINES[("argumentative", "high")] in rules
    assert comms_rules(GM, tune(p, talkative=10)) == comms_rules(GM, p)  # frequency only


# -- drama --------------------------------------------------------------------------------------

def test_drama_scales_the_tell_score_after_the_gates():
    traits = {"extraversion": 6.0, "neuroticism": 5.0}
    base = gossip.tell_score(6, 0.5, traits)
    assert gossip.tell_score(6, 0.5, traits, drama=2.0) == pytest.approx(2 * base)
    assert gossip.tell_score(6, 0.5, traits, drama=1.0) == base


# -- opening up ---------------------------------------------------------------------------------

def test_opening_up_moves_the_trust_a_layer_needs_and_who_confides():
    p = make_persona(101, "Arkon")
    assert layer_trust(p, "middle") == LAYER_TRUST["middle"]
    assert layer_trust(tune(p, openup=10), "middle") == LAYER_TRUST["middle"] - 25
    assert layer_trust(tune(p, openup=1), "middle") == LAYER_TRUST["middle"] + 20
    assert opens_up(p, 4.0) and not opens_up(p, 5.0)
    assert opens_up(tune(p, openup=9), 8.0) and not opens_up(tune(p, openup=9), 9.5)
    assert not opens_up(tune(p, openup=0), 1.0)


# -- reliability --------------------------------------------------------------------------------

def test_reliability_overrides_the_accessor_only():
    p = make_persona(101, "Arkon")
    late = tune(p, reliability=2)
    assert reliability(p) == 6 and reliability(late) == 2
    assert late.big_five.conscientiousness.score == 6  # discretion and the rest stay
    on_time, waves = summon_waves([101], {101: late}, random.Random(1), (5, 10))
    assert on_time == [] and [g for g, _ in waves] == [101]


# -- argumentative ------------------------------------------------------------------------------

def test_argumentative_is_agreeableness_the_other_way_up():
    p = make_persona(101, "Arkon")
    assert tune(p, argumentative=10).big_five.agreeableness.score == 1
    assert tune(p, argumentative=1).big_five.agreeableness.score == 10
    assert dials_of(p).values == {}
