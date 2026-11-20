"""Her decision (policy.py) and her instruction (words.py): she goes along more readily the better
she likes him and the freer her day is, never when she dislikes him or a fence holds her; her
instruction never has her on her way unless the move was taken."""
import pytest

from leeroy_mind.invites import policy, spots as S, words as W
from leeroy_mind.life.presence import Attention

FORMS = ("he", "him", "his")
INN = S.BY_NAME["dal_inn"]
ON_MY_WAY = ("on your way", "you're coming", "coming")


def test_willingness():
    assert policy.willingness(policy.Facts(affinity=40)) > policy.willingness(policy.Facts(affinity=5))
    assert policy.willingness(policy.Facts(affinity=5)) > policy.willingness(policy.Facts(affinity=-10))
    assert policy.willingness(policy.Facts(affinity=-30)) == 0
    assert policy.willingness(policy.Facts(affinity=40, stance="lust")) <= 0.98
    assert policy.willingness(policy.Facts(affinity=5, stance="drawn")) > policy.willingness(
        policy.Facts(affinity=5))
    for blocked in ("in_instance", "his_instance", "pending", "afk", "not_online", "cap"):
        assert policy.willingness(policy.Facts(affinity=50, blocked=blocked)) == 0
    assert policy.willingness(policy.Facts(affinity=50, away=True)) == 0
    assert policy.willingness(policy.Facts(affinity=50, attention=Attention.NONE)) == 0
    assert (policy.willingness(policy.Facts(affinity=50, attention=Attention.GLANCING))
            < policy.willingness(policy.Facts(affinity=50, attention=Attention.HALF))
            < policy.willingness(policy.Facts(affinity=50, attention=Attention.FULL)))


def test_reasons():
    assert policy.reason(policy.Facts(blocked="in_instance")) == "in_instance"
    assert policy.reason(policy.Facts(away=True)) == "away"
    assert policy.reason(policy.Facts(attention=Attention.NONE)) == "away"
    assert policy.reason(policy.Facts(attention=Attention.GLANCING)) == "busy"
    assert policy.reason(policy.Facts(affinity=-50)) == "dislike"
    assert policy.reason(policy.Facts(affinity=10)) == "not_now"


def test_the_decision_is_the_same_for_the_same_line_and_member():
    f = policy.Facts(affinity=10)
    assert {policy.decide(f, line=7, guid=101) for _ in range(5)} == {policy.decide(f, line=7, guid=101)}
    yes = sum(policy.decide(f, line=n, guid=101)[0] for n in range(400))
    assert 300 < yes < 380  # about 85%


def test_moves_left_counts_the_last_hour():
    now = 10_000.0
    assert policy.moves_left([], now) == policy.MOVES_PER_HOUR
    assert policy.moves_left([now - 10] * 6, now) == 0
    assert policy.moves_left([now - 4000] * 6 + [now - 5], now) == 5
    assert {policy.how_many(n) for n in range(50)} == {1, 2}


def text(**kw):
    base = dict(what="meet", place=INN, later=False, accept=True, reason="", steps=[("go_to", "dal_inn", 0)],
                outcomes=[], failed="", done=False, note="", forms=FORMS)
    base.update(kw)
    return W.instruction(**base)


def test_a_taken_move_is_what_she_says():
    said = text(outcomes=[("go_to", "done", "went")], done=True)
    assert "the Legerdemain Lounge in Dalaran" in said and "waiting for him there" in said
    came = text(what="come", place=None, steps=[("come_to_me",)], outcomes=[("come_to_me", "done", "came")],
                done=True)
    assert "beside him now" in came


@pytest.mark.parametrize("kw", [
    dict(accept=False, reason="busy", steps=[]),
    dict(accept=False, reason="dislike", steps=[]),
    dict(outcomes=[("go_to", "refused", "combat")], failed="combat", done=True),
    dict(outcomes=[("go_to", "refused", "busy:realm_owner")], failed="busy:realm_owner", done=True),
    dict(outcomes=[("go_to", "unknown", "unknown")], failed="unknown", done=True),
    dict(),  # not carried out yet
    dict(later=True, steps=[]),
    dict(place=S.UNKNOWN, accept=False, reason="unknown_place", steps=[]),
])
def test_never_on_her_way_unless_the_move_was_taken(kw):
    said = text(**kw)
    assert "don't say you're on your way" in said.lower() or "don't claim you're on your way" in said.lower()
    assert "you've gone" not in said and "beside him now" not in said


def test_refusals_are_her_own_reason_in_words():
    assert "you're in the middle of a fight" in text(
        outcomes=[("go_to", "refused", "combat")], failed="combat", done=True)
    assert "tied up with something for the guild" in text(
        outcomes=[("go_to", "refused", "busy:training_owner")], failed="busy:training_owner", done=True)
    assert "can't make it right now" in text(
        outcomes=[("go_to", "refused", "invites_off")], failed="invites_off", done=True)
    assert "busy with something away from the game" in text(accept=False, reason="busy", steps=[])
    assert "which place he means" in text(place=S.UNKNOWN, accept=False, reason="unknown_place", steps=[])


def test_a_second_step_that_failed_is_said_beside_the_first():
    fish = text(what="fish", place=None, steps=[("come_to_me",), ("fish",)],
                outcomes=[("come_to_me", "done", "came"), ("fish", "refused", "no_fishing")],
                failed="no_fishing", done=True)
    assert "beside him now" in fish and "never learned to fish" in fish


def test_pronouns_follow_his():
    said = text(accept=False, reason="dislike", steps=[], forms=("they", "them", "their"))
    assert said.startswith("They invited you") and "not with them" in said and "{" not in said


def test_follow_up_says_only_what_is_new():
    move = [("go_to", "dal_inn", 0)]
    went = [("go_to", "done", "went")]
    assert W.follow_up(what="meet", place=INN, steps=move, outcomes=went, failed="", told_done=1,
                       forms=FORMS) is None  # her answer already said it
    late = W.follow_up(what="meet", place=INN, steps=move, outcomes=went, failed="", told_done=0,
                       forms=FORMS)
    assert "waiting for him there" in late
    failed = W.follow_up(what="meet", place=INN, steps=move, outcomes=[("go_to", "refused", "combat")],
                         failed="combat", told_done=0, forms=FORMS)
    assert "didn't work out" in failed and "fight" in failed
    fish = W.follow_up(what="fish", place=None, steps=[("come_to_me",), ("fish",)],
                       outcomes=[("come_to_me", "done", "came"), ("fish", "refused", "no_water")],
                       failed="no_water", told_done=1, forms=FORMS)
    assert "no water" in fish and "beside him" not in fish
    assert W.follow_up(what="meet", place=INN, steps=move, outcomes=[("go_to", "unknown", "unknown")],
                       failed="unknown", told_done=0, forms=FORMS) is None


def test_discord_and_other_members():
    assert "only agree to meet up in game later" in W.later_dm("fish", S.BY_NAME["dal_fountain"], FORMS)
    assert "not you" in W.not_asked(FORMS)
