"""DKP pricing, loot rules and decay (guild/dkp.py, decisions/loot_rules.py) over generated
bidders, balances, prices and rates."""
import pytest

pytest.importorskip("hypothesis", reason='Hypothesis comes with the test extra: pip install -e ".[test]"')

from hypothesis import given, strategies as st

from leeroy_mind.decisions.loot_rules import ROLES, SLOTS, award_with, rule_from_value
from leeroy_mind.guild.dkp import Bidder, award, bid, decay_entries

HUMAN = 1
balances = st.one_of(st.integers(-500, 5000).map(lambda n: n / 10),
                     st.floats(-1000, 10000, allow_nan=False, allow_infinity=False))
gains = st.one_of(st.none(), st.integers(1, 80))  # upgrade_gain: None or a positive gain


@st.composite
def bidder_lists(draw, max_size=8):
    guids = draw(st.lists(st.integers(1, 40), unique=True, max_size=max_size))
    return [Bidder(g, draw(balances), draw(gains), draw(st.booleans())) for g in guids]


def interested(bidders):
    return [b for b in bidders if b.guid != HUMAN and (b.gain is not None or b.wanted)]


def priority(b):
    return (0 if b.wanted else 1) if b.gain is not None else 2


rules = st.builds(
    lambda mode, first: rule_from_value({"mode": mode, "role_first": first}),
    st.sampled_from(["dkp", "loot_council", "gear_priority"]),
    st.dictionaries(st.sampled_from(SLOTS), st.lists(st.sampled_from(ROLES), unique=True, min_size=1),
                    max_size=3))


@given(st.integers(0, 200), bidder_lists(), st.booleans(), st.randoms(use_true_random=False))
def test_dkp_award_follows_its_documented_second_price(price, bidders, claimed, rnd):
    """Invariants of today's DKP award:
    - order-free: the same bidders in any order get the same award;
    - his claim always wins, at the item's price, over whoever would have won (the runner-up);
    - otherwise the winner is an interested member (an upgrade or a want), never him, with the
      best priority (wanted upgrade, upgrade, want only) and the highest bid within it; nobody
      interested, no award;
    - the winner pays at least the price (or all of her bid when it is lower), at most her bid,
      which is at most her balance and never below 0; against a runner-up of the same priority
      she pays one more than the runner-up's bid (second price), capped at her bid; a close
      award is exactly a same-priority runner-up within 10% of a positive bid."""
    result = award(price, bidders, human_claimed=claimed, human_guid=HUMAN)
    shuffled = list(bidders)
    rnd.shuffle(shuffled)
    assert award(price, shuffled, human_claimed=claimed, human_guid=HUMAN) == result
    open_award = award(price, bidders, human_claimed=False, human_guid=HUMAN)
    if claimed:
        assert (result.guid, result.paid, result.bid, result.priority, result.close) == (
            HUMAN, float(price), float(price), -1, False)
        assert result.runner_up == (open_award.guid if open_award else None)
        return
    pool = interested(bidders)
    if not pool:
        assert result is None
        return
    by_guid = {b.guid: b for b in pool}
    winner = by_guid[result.guid]
    bids = {b.guid: bid(b.balance, price, b.gain, b.wanted) for b in pool}
    best = min(priority(b) for b in pool)
    assert priority(winner) == best == result.priority
    assert result.bid == bids[winner.guid] == max(bids[b.guid] for b in pool if priority(b) == best)
    assert 0 <= result.paid <= result.bid <= max(winner.balance, 0)
    assert result.paid >= min(price, result.bid)
    rivals = [b for b in pool if b.guid != winner.guid]
    same = result.runner_up is not None and priority(by_guid[result.runner_up]) == best
    if same:
        assert result.paid == min(result.bid, max(min(price, result.bid), result.runner_up_bid + 1))
    else:
        assert result.paid == min(price, result.bid)
        assert all(priority(b) > best for b in rivals)
    assert result.close == (same and result.runner_up_bid >= 0.9 * result.bid and result.bid > 0)


@given(rules, st.integers(0, 200), bidder_lists(), st.booleans(), st.sampled_from(SLOTS),
       st.data(), st.randoms(use_true_random=False))
def test_every_loot_rule_lets_his_claim_win_and_charges_within_the_bid(rule, price, bidders, claimed,
                                                                      slot, data, rnd):
    """Invariants of every loot rule (award_with: DKP, loot council, gear priority):
    - order-free, as today's award;
    - DKP is exactly today's award;
    - his claim always wins, charged the rule's price (nothing under a council);
    - otherwise the winner is an interested member, never him; a council charges nothing; gear
      priority goes to the slot's first role, then the biggest upgrade, then DKP's priority, and
      charges at most the winner's bid, itself within her balance and never below 0."""
    guids = [b.guid for b in bidders]
    roles = {g: data.draw(st.one_of(st.none(), st.sampled_from(ROLES))) for g in guids}
    shares = {g: data.draw(st.floats(0, 1)) for g in guids}
    result = award_with(rule, price, bidders, human_claimed=claimed, human_guid=HUMAN, slot=slot,
                        roles=roles, shares=shares)
    shuffled = list(bidders)
    rnd.shuffle(shuffled)
    assert award_with(rule, price, shuffled, human_claimed=claimed, human_guid=HUMAN, slot=slot,
                      roles=roles, shares=shares) == result
    if rule.mode == "dkp":
        assert result == award(price, bidders, human_claimed=claimed, human_guid=HUMAN)
    council = rule.mode == "loot_council"
    if claimed:
        assert result.guid == HUMAN and result.paid == (0.0 if council else float(price))
        return
    pool = interested(bidders)
    if not pool:
        assert result is None
        return
    winner = next(b for b in pool if b.guid == result.guid)
    if council:
        assert result.paid == 0.0
        return
    assert 0 <= result.paid <= result.bid <= max(winner.balance, 0)
    if rule.mode == "gear_priority":
        order = rule.role_first.get(slot, ())

        def rank(b):
            role = roles.get(b.guid)
            return (order.index(role) if role in order else len(order),
                    -(b.gain if b.gain is not None else 0), priority(b))
        assert rank(winner) == min(rank(b) for b in pool)


@given(st.dictionaries(st.integers(1, 40), balances, max_size=6), st.integers(0, 10),
       st.floats(0, 1, exclude_max=True))
def test_decay_moves_every_balance_toward_zero_and_never_past_it(start, times, rate):
    """Weekly decay (decay_entries, any finite balances, any rate in [0, 1), any number of missed
    weeks): each balance only moves toward 0, never crosses it (a member with DKP is never put in
    debt by decay, nor his debt turned into credit), at most one entry per missed week, and no
    entry for a balance of 0."""
    entries = decay_entries(start, times, rate, 1.0)
    after = dict(start)
    count = {}
    for e in entries:
        assert e.reason == "decay" and e.delta != 0
        after[e.guid] += e.delta
        count[e.guid] = count.get(e.guid, 0) + 1
    for guid, before in start.items():
        assert count.get(guid, 0) <= times
        assert abs(after[guid]) <= abs(before)
        assert after[guid] * before >= 0, (before, after[guid])
        if before == 0:
            assert guid not in count
