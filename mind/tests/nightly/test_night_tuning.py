"""The night reads the tuned members, with the values the run started with (phase 3b T3)."""
from dataclasses import replace

from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.schema import Persona
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning.effective import EffectivePersona, dials_of
from leeroy_mind.tuning.lines import BUILTIN_LINES
from leeroy_mind.tuning.state import Tuning


def put(store, *sets):
    pic = C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})
    C.apply_sets(store, [C.Set(*s) for s in sets], pic, source="cli", now=2000.0)


def test_the_night_reads_the_tuned_member_and_writers_the_stored_one(h):
    put(h.store, ("member", "101", "argumentative", 9), ("member", "101", "humour", 9))
    ctx = h.ctx()
    p = ctx.persona(101)
    assert type(p) is EffectivePersona and p.big_five.agreeableness.score == 2
    assert type(ctx.base(101)) is Persona and ctx.base(101).big_five.agreeableness.score == 5
    assert ctx.card(101).endswith(BUILTIN_LINES[("humour", "high")])
    assert BUILTIN_LINES[("humour", "high")] not in ctx.base_card(101)
    assert ctx.card(102) == ctx.base_card(102)  # untouched: no lines


def test_a_change_made_while_a_night_runs_waits_for_the_next_one(h):
    put(h.store, ("member", "101", "talkative", 9))
    ctx = h.ctx()
    ctx = replace(ctx, tuning=ctx.personas.tuning)  # as make_ctx freezes it
    put(h.store, ("member", "101", "talkative", 2))
    ctx.personas.set_tuning(Tuning.load(h.store.conn))  # the live mind takes the change
    assert dials_of(ctx.personas.get(101)).values == {"talkative": 2}
    assert dials_of(ctx.persona(101)).values == {"talkative": 9}


def test_drama_scales_confront_and_opening_up_lets_a_louder_member_confide(h):
    from leeroy_mind.nightly.storyteller import Volition, volitions
    from nightly.test_night_storyteller import ext, feud
    feud(h, 101, 102, 2)
    h.persona(105, disclosures=(("surface", "I keep a garden."),
                               ("deep", "I am afraid of letting my family down.")))
    ext(h, 105, 6)  # too outgoing to confide, as written
    h.rel(105, 101, trust=25)
    plain = volitions(h.ctx())
    assert plain[101] == [Volition(101, "confront", 102, 1.0)]
    assert all(v.kind != "confide" for v in plain.get(105, []))
    put(h.store, ("member", "101", "drama", 10), ("member", "105", "openup", 8))
    tuned = volitions(h.ctx())
    assert tuned[101] == [Volition(101, "confront", 102, 2.0)]
    assert tuned[102] == [Volition(102, "confront", 101, 1.0)]
    confide = [v for v in tuned[105] if v.kind == "confide"]
    assert [(v.target, round(v.weight, 9)) for v in confide] == [(101, 1.44)]
