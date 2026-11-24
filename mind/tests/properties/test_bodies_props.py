"""The body ledger (bodies.py) over generated evidence: sources of holds (some retained, some on
every body), claims of every rank, and claims that expire."""
import pytest

pytest.importorskip("hypothesis", reason='Hypothesis comes with the test extra: pip install -e ".[test]"')

from hypothesis import given, strategies as st

from leeroy_mind.bodies import ALL, RANKS, BodyHold, BodyLedger

OWNERS = tuple(RANKS)
GUIDS = st.integers(101, 112)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def now(self):
        return self.value


@st.composite
def source_rows(draw):
    rows = []
    for i in range(draw(st.integers(0, 3))):
        owner = draw(st.sampled_from(OWNERS))
        guids = ALL if draw(st.integers(0, 9)) == 0 else draw(st.frozensets(GUIDS, max_size=4))
        rows.append(BodyHold(f"scope-{i}", owner, RANKS[owner], guids, draw(st.booleans())))
    return tuple(rows)


evidence = st.tuples(
    st.lists(source_rows(), max_size=3),
    st.lists(st.tuples(st.sampled_from(OWNERS), st.frozensets(GUIDS, max_size=4), st.integers(1, 120)),
             max_size=5),
    st.integers(0, 90),
)


def build(sources, claims, wait):
    clock = Clock()
    ledger = BodyLedger(clock)
    for n, rows in enumerate(sources):
        ledger.add_holder_source(f"source-{n}", lambda rows=rows: rows)
    for owner, guids, until in claims:
        ledger.claim(owner, guids, rank=owner, until=clock.now() + until)
    clock.value += wait  # some claims expire
    return ledger


@given(evidence, st.frozensets(GUIDS, max_size=8), st.sampled_from(OWNERS),
       st.frozensets(st.sampled_from(OWNERS), max_size=3))
def test_batch_reads_answer_as_the_one_member_reads_and_a_held_body_is_never_free(ev, guids, owner, besides):
    """For any ledger state (sources, live and expired claims):
    - holders_of(guids) is holder(g) for each member, others_of is others, movable is the members
      may_move allows and may_move_all is all of them, each from one settled view;
    - a body a source holds, or a live claim holds, always has a holder (one of the top rank):
      it never reads as free;
    - a retained hold lets nobody move the body; a body nobody holds anyone may move;
    - only the top-ranked holder may move a held body, and only while it holds it alone."""
    sources, claims, wait = ev
    ledger = build(sources, claims, wait)
    batch = ledger.holders_of(guids)
    assert batch == {g: ledger.holder(g) for g in guids}
    assert ledger.others_of(guids, besides=besides) == {g: ledger.others(g, besides=besides) for g in guids}
    movable = ledger.movable(owner, guids)
    assert movable == frozenset(g for g in guids if ledger.may_move(owner, g))
    assert ledger.may_move_all(owner, guids) == all(ledger.may_move(owner, g) for g in guids)
    for g in guids:
        rows = [r for rows in sources for r in rows if r.guids is ALL or g in r.guids]
        claimed = {o for o in OWNERS if g in ledger.claims_for(o)}
        holders = {r.owner for r in rows} | claimed
        if rows or claimed:
            assert batch[g] in holders and RANKS[batch[g]] == min(RANKS[o] for o in holders)
        else:
            assert batch[g] is None and all(ledger.may_move(o, g) for o in OWNERS)
        if any(r.retained for r in rows):
            assert not any(ledger.may_move(o, g) for o in OWNERS)
        elif holders:
            top = min(RANKS[o] for o in holders)
            leaders = {o for o in holders if RANKS[o] == top}
            for o in OWNERS:
                assert ledger.may_move(o, g) == (leaders == {o})
