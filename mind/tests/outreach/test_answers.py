"""Things she means to tell him, folded into her answers to his whisper or DM, and DM openers
kept for members who have one. The test cast and invented lines only."""
import json
import random
import re
from types import SimpleNamespace

import pytest

from test_think_private import beat_calls, clock, dm_answer, last_user, line_calls, rig  # noqa: F401
from test_live import app, chat, plan  # noqa: F401
from leeroy_mind.outreach import store as records
from leeroy_mind.talk import beat as beats
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

NEWS = "Your sister visits on Friday and you are dreading the drive."


class Stub:
    """The outreach service as the pipeline asks it."""
    def __init__(self, offer=("o7", NEWS)):
        self.item, self.told, self.asked = offer, [], 0

    def offer(self, turn):
        return self.item

    def first_share(self, turn, rng):
        self.asked += 1
        return self.item

    def brought(self, turn, meta, line_id, now):
        self.told.append((meta.outreach_brought, turn.channel, line_id))

    def release(self, meta):
        pass


def script(want="share", use=NEWS, line="fair enough"):
    """The beat picks `use` among the offered items by its words (None: nothing), then the line."""
    def reply(model_name, messages):
        asked = messages[-1]["content"]
        if beats.MARK not in asked:
            return line
        found = re.search(r"^(\d+)\. " + re.escape(use), asked, re.M) if use else None
        return json.dumps({"read": "he is checking in", "lands": "warm", "want": want,
                           "use": int(found.group(1)) if found else None})
    return reply


def answer_turn(guid=101, channel=None):
    return Turn(TurnKind.REPLY_HUMAN, guid, channel or f"dm:{guid}", to_guid=1, addressee=1)


def test_her_answer_can_bring_in_what_she_means_to_tell_him_once(rig):
    rig.engine.outreach = stub = Stub()
    rig.llm.set_script(script())
    assert dm_answer(rig, "long day, glad it is over") == "fair enough"
    assert NEWS in last_user(beat_calls(rig)[-1])  # offered to the beat beside her memories
    assert "Bring in: " + NEWS in last_user(line_calls(rig)[-1])
    (ident, channel, line), = stub.told
    assert ident == 7 and channel == "dm:102" and line
    ledger = json.loads(rig.store.get_meta(beats.ledger_key(102, "dm:102", 1)))
    assert [e["use"] for e in ledger] == ["o7"]
    dm_answer(rig, "anyway, that was my day")
    assert NEWS not in last_user(beat_calls(rig)[-1])  # the conversation's ledger: not again
    assert len(stub.told) == 1


def test_the_first_answer_of_a_conversation_may_carry_it_when_nothing_else_fits(rig):
    rig.engine.outreach = stub = Stub()
    rig.llm.set_script(script(use=None))
    dm_answer(rig, "long day, glad it is over")
    assert stub.asked == 1 and "Bring in: " + NEWS in last_user(line_calls(rig)[-1])
    assert [t[0] for t in stub.told] == [7]
    dm_answer(rig, "anyway, that was my day")  # her second turn there: the ledger is not empty
    assert stub.asked == 1 and len(stub.told) == 1


@pytest.mark.parametrize("his,want", [("did you see the patch notes?", "share"),
                                      ("long day, glad it is over", "vent"),
                                      ("long day, glad it is over", "boundary")])
def test_his_question_or_her_own_trouble_comes_first(rig, his, want):
    rig.engine.outreach = stub = Stub()
    rig.llm.set_script(script(want=want, use=None))
    dm_answer(rig, his)
    assert stub.asked == 0 and stub.told == []
    assert "Bring in:" not in last_user(line_calls(rig)[-1])


def test_nothing_ready_or_outreach_off_is_today(rig):
    rig.llm.set_script(script(use=None))
    dm_answer(rig, "long day, glad it is over")
    plain = last_user(line_calls(rig)[-1])
    rig.engine.outreach = Stub(offer=None)
    dm_answer(rig, "long day, glad it is over")
    assert "Bring in:" not in plain and "Bring in:" not in last_user(line_calls(rig)[-1])


# -- the service ---------------------------------------------------------------------------------

def test_offer_is_for_his_private_turn_with_her_only(app):
    item = plan(app)
    assert app.outreach.offer(answer_turn()) == (f"o{item.id}", item.about)
    assert app.outreach.offer(answer_turn(channel="whisper")) == (f"o{item.id}", item.about)
    assert app.outreach.offer(answer_turn(102)) is None            # another member's turn
    assert app.outreach.offer(answer_turn(101, "dm:102")) is None  # not her DM
    for public in ("guild", "party", "discord:general", "gdm:1"):
        assert app.outreach.offer(answer_turn(channel=public)) is None
    app.owed.add(101)  # the answer she owes him is this one
    assert app.outreach.offer(answer_turn()) is not None
    app.engine.cfg.talk.chattiness = 0
    assert app.outreach.offer(answer_turn()) is None


def test_first_share_follows_chattiness(app):
    plan(app)
    draws = lambda: sum(app.outreach.first_share(answer_turn(), random.Random(i)) is not None
                        for i in range(400))
    assert 150 < draws() < 250          # chattiness 4: about half
    app.engine.cfg.talk.chattiness = 8
    assert draws() == 400
    app.engine.cfg.talk.chattiness = 0
    assert draws() == 0


def test_a_nudge_is_never_folded_into_an_answer(app):
    now = app.clock.now()
    records.add(app.store, guid=101, kind="dm_nudge", ref="dm_nudge:1", about="Fixture nudge.",
                audience="pair", weight=6, created=now, not_before=now, expires=now + 60)
    assert app.outreach.offer(answer_turn(channel="whisper")) is None


def test_the_stored_answer_tells_the_item_once_and_a_refusal_costs_nothing(app):
    item = plan(app)
    line, _ = chat(app, "A fixture answer.", owner=False, channel="dm:101")
    meta = TurnMeta("answer")
    meta.outreach_brought = item.id
    app.outreach.brought(answer_turn(), meta, line, app.clock.now())
    told = records.get(app.store, item.id)
    assert told.state == "told" and told.told_via == "dm" and told.line_id == line
    assert app.outreach.offer(answer_turn()) is None
    other, _ = chat(app, "Another fixture answer.", owner=False, channel="dm:101")
    app.outreach.brought(answer_turn(), meta, other, app.clock.now())  # a second line retells nothing
    assert records.get(app.store, item.id).line_id == line
    meta.outreach_brought = 999
    app.outreach.brought(answer_turn(), meta, other, app.clock.now())  # unknown: no error


def test_an_item_he_raised_first_is_not_told_by_her_answer(app):
    item = plan(app)
    records.drop(app.store, item.id, "he_raised")
    line, _ = chat(app, "A fixture answer.", owner=False, channel="dm:101")
    meta = TurnMeta("answer")
    meta.outreach_brought = item.id
    app.outreach.brought(answer_turn(), meta, line, app.clock.now())
    assert records.get(app.store, item.id).state == "dropped"
