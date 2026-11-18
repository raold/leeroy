"""The night and a member's voice sheet (review of #367, F1): the critic, the weekly evaluation
and what the night writes (NightContext.card) read the sheet's public part in place of the trait
sentences, never how she is with him nor her stance; and the week her sheet changes is a new
baseline for drift: lines from before it are not flagged, flags from before it are not counted,
and the weekly check neither judges that week nor compares the next with the one before. Every
sheet here is invented (tests/sheets.py)."""
import json
from dataclasses import replace

from conftest import run
from leeroy_mind.eval import persona as persona_eval
from leeroy_mind.nightly import critic
from leeroy_mind.nightly.growth import undo
from leeroy_mind.nightly.store import ledger_rows
from leeroy_mind.personas import voices
from leeroy_mind.personas.card import render_night_card, render_prompt_card
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.speech import public_block
from nightly.test_night_critic import answer, context, flag, flags, note, rows, sent
from sheets import GRUFF, sheet

VOICE = "Your voice: plain, clipped, a bit gruff; short flat statements, three to seven words."
PRIVATE = (GRUFF["with_him"], GRUFF["stance"], *GRUFF["samples"].values())
WEEK = 7 * 86400


def asked(ctx) -> str:
    return json.dumps(ctx.reasoner.llm.calls, default=str)


# -- the card the night reads ------------------------------------------------------------------------

def test_the_night_card_is_the_prompt_card_without_a_sheet(h):
    p = load_persona(h.store, 101)
    kind = dict(race="Human", class_name="Warrior")
    assert render_night_card(p, **kind) == render_prompt_card(p, **kind)


def test_the_night_card_has_the_sheets_public_part_in_place_of_the_traits(h):
    p = replace(load_persona(h.store, 101), speech=sheet(household="lives with a flatmate"))
    kind = dict(race="Human", class_name="Warrior")
    card = render_night_card(p, **kind)
    assert card == render_prompt_card(p, voice=True, **kind) + "\n" + public_block(p.speech)
    assert VOICE in card and "You say, only now and then: noted," in card
    assert "Never: awesome, lol." in card and "Unasked, you bring up: the allotment;" in card
    assert "Your humour is" not in card and "chats easily in guild" not in card
    for private in PRIVATE:  # never how she is with him, her stance or a sample
        assert private not in card
    assert "With " not in public_block(p.speech)


def test_the_critic_judges_her_against_her_sheet_never_its_private_lines(h):
    voices.set_voice(h.store, 101, sheet(), now=h.T - 30 * 86400)
    ids = sent(h)
    ctx = context(h, answer(5, 5))
    run(critic.critic_member(ctx, 101, rows(h, ids[:2])))
    text = asked(ctx)
    assert "plain, clipped, a bit gruff" in text and "You say, only now and then" in text
    assert "chats easily in guild" not in text
    for private in PRIVATE:
        assert private not in text


def test_what_the_night_writes_for_her_reads_her_voice(h):
    voices.set_voice(h.store, 101, sheet(), now=h.T - 30 * 86400)
    ctx = h.ctx()
    card = ctx.card(101)
    assert VOICE in card and all(private not in card for private in PRIVATE)
    assert "Your voice:" not in ctx.card(102)  # a member without a sheet: the card as before
    assert "Your voice:" not in ctx.base_card(101)  # the looks and photo passes: her facts only


def test_the_weekly_evaluation_reads_the_same_card(h):
    voices.set_voice(h.store, 101, sheet(), now=h.T - 30 * 86400)
    _, card = persona_eval._card(h.store, 101)
    assert VOICE in card and all(private not in card for private in PRIVATE)
    assert "Your voice:" not in persona_eval._card(h.store, 102)[1]


# -- a new baseline when her voice changes -----------------------------------------------------------

def test_when_her_voice_last_changed(h):
    assert voices.changed_at(h.store, 101) is None
    first = voices.set_voice(h.store, 101, sheet(), now=h.T - 5 * 86400)
    assert voices.changed_at(h.store, 101) == h.T - 5 * 86400
    assert undo(h.store, first, now=h.T - 86400)  # an undo changes her voice too
    assert voices.changed_at(h.store, 101) == h.T - 86400
    assert voices.changed_at(h.store, 102) is None


def test_flags_from_before_her_sheet_do_not_count_toward_a_rollback(h):
    """Two flags against her old card, then a sheet: tonight's third flag rolls nothing back."""
    flag(h, h.T - 4 * 86400)
    flag(h, h.T - 3 * 86400)
    note(h)
    voices.set_voice(h.store, 101, sheet(), now=h.T - 86400)
    sent(h)
    run(critic.stage_critic(context(h)))
    assert len(flags(h)) == 3  # tonight's line was flagged against her sheet
    assert h.store.get_state(101).self_notes[0]["text"] == "I can be more patient."
    assert not [r for r in ledger_rows(h.store, 101) if r["kind"] == "note_rolled_back"]


def test_without_a_sheet_change_three_flags_still_roll_back(h):
    flag(h, h.T - 4 * 86400)
    flag(h, h.T - 3 * 86400)
    note(h)
    sent(h)
    run(critic.stage_critic(context(h)))
    assert h.store.get_state(101).self_notes == ()


def test_a_line_from_before_her_sheet_is_scored_but_never_flagged(h):
    ids = sent(h)  # said at h.T .. h.T + 3
    voices.set_voice(h.store, 101, sheet(), now=h.T + 2)  # imported between her lines
    ctx = context(h, answer(1, 1, 1, 1))
    run(critic.critic_member(ctx, 101, rows(h, ids)))
    flagged = sorted(r["text"] for r in flags(h))
    assert flagged == ["Synthetic guild line 2.", "Synthetic guild line 3."]
    assert h.store.conn.execute("SELECT n FROM critic_score WHERE guid=101").fetchone()[0] == 4


def _scores(h, this, last=None):
    for week, value in (("2026-W44", this), ("2026-W43", last)):
        if value is not None:
            h.store.conn.execute("INSERT INTO eval_score VALUES (?,?,'consistency',?)",
                                 (week, 101, value))
    h.store.put_state(replace(h.store.get_state(101), self_notes=(
        {"text": "A new note.", "evidence": [], "added": h.T - 2 * 86400},)))


def _drift(h):
    return persona_eval.drift_check(h.store, "2026-W44", "2026-W43", since=h.T - WEEK, now=h.T)


def test_the_week_her_sheet_changed_is_not_checked_for_drift(h):
    _scores(h, 0.3, 0.9)  # far under the floor and far down
    voices.set_voice(h.store, 101, sheet(), now=h.T - 2 * 86400)
    assert _drift(h) == [] and len(h.store.get_state(101).self_notes) == 1


def test_the_week_after_has_no_baseline_from_before_the_sheet_but_keeps_the_floor(h):
    _scores(h, 0.7, 0.95)  # down 0.25 on a week scored against her old card
    voices.set_voice(h.store, 101, sheet(), now=h.T - WEEK - 86400)
    assert _drift(h) == [] and len(h.store.get_state(101).self_notes) == 1


def test_the_floor_still_applies_the_week_after(h):
    _scores(h, 0.5, 0.95)
    voices.set_voice(h.store, 101, sheet(), now=h.T - WEEK - 86400)
    assert _drift(h) == [101] and h.store.get_state(101).self_notes == ()


def test_two_weeks_on_and_without_a_sheet_the_check_is_as_before(h):
    _scores(h, 0.7, 0.95)
    voices.set_voice(h.store, 101, sheet(), now=h.T - 2 * WEEK - 86400)
    assert _drift(h) == [101]
