"""An owed answer's deadline (talk/owed.py) over generated in-flight loads and clock runs."""
from types import SimpleNamespace

import pytest

pytest.importorskip("hypothesis", reason='Hypothesis comes with the test extra: pip install -e ".[test]"')

from hypothesis import given, strategies as st

from leeroy_mind.talk.owed import (OWED_DEADLINE_CEILING_S, answers_in_flight, extend_owed_deadline,
                                   owed_deadline)
from leeroy_mind.talk.turns import Turn, TurnKind

HIM = 1
CHANNELS = ("whisper", "guild", "party", "dm:7", "discord:general", "gdm:3")


def family(channel):
    return "discord" if channel.startswith(("dm:", "discord:", "gdm:")) else "game"


turn_kinds = st.sampled_from((TurnKind.REPLY_HUMAN, TurnKind.CONTINUE, TurnKind.WHISPER, TurnKind.OPEN,
                              TurnKind.GREET, TurnKind.FLIRT))
others = st.lists(st.tuples(turn_kinds, st.sampled_from(CHANNELS), st.sampled_from((0, HIM, 42))), max_size=12)


@given(st.sampled_from(CHANNELS), st.floats(0, 900), st.lists(st.tuples(st.floats(0, 400), others), max_size=8))
def test_an_owed_deadline_only_moves_later_and_never_past_its_first_cap(channel, start, ticks):
    """extend_owed_deadline over any run of extensions (time passing, other answers to him
    queued or done): the turn's deadline never moves earlier, never past the hard cap set at its
    first extension (its now plus the medium's ceiling) unless it already lay beyond it, and
    after each extension it is at least what the load in flight asks for, within that cap."""
    turn = Turn(TurnKind.REPLY_HUMAN, 5, channel, addressee=HIM, deadline=start)
    meta = SimpleNamespace(owed_cap=0.0)
    now = 0.0
    cap = None
    for dt, planned in ticks:
        now += dt
        queue = [turn, *(Turn(k, 6, c, to_guid=to, addressee=to) for k, c, to in planned)]
        pipe = SimpleNamespace(human=SimpleNamespace(guid=HIM), planned_turns=lambda q=queue: q)
        before = turn.deadline
        extend_owed_deadline(pipe, turn, meta, now)
        if cap is None:
            cap = now + OWED_DEADLINE_CEILING_S[family(channel)]
        assert meta.owed_cap == cap
        assert turn.deadline >= before
        assert turn.deadline <= max(cap, start)
        assert turn.deadline >= min(cap, now + owed_deadline(channel, answers_in_flight(pipe)))
