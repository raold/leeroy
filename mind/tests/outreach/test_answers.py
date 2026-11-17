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
        self.item, self.told, self.asked, self.lines = offer, [], 0, []

    def offer(self, turn):
        return self.item

    def first_share(self, turn, rng):
        self.asked += 1
        return self.item

    def brought(self, turn, meta, line_id, now):
        self.told.append((meta.outreach_brought, turn.channel, line_id))
        self.lines.append(line_id)

    def release(self, meta):
        pass


def script(want="share", use=NEWS, line="fair enough", lines=()):
    """The beat picks `use` among the offered items by its words (None: nothing), then the line
    (`lines` in order, the last repeating)."""
    left = list(lines) or [line]

    def reply(model_name, messages):
        asked = messages[-1]["content"]
        if beats.MARK not in asked:
            return left.pop(0) if len(left) > 1 else left[0]
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
    line, _ = chat(app, "Fair enough. I might visit that quiet garden later.", owner=False, channel="dm:101")
    meta = TurnMeta("answer")
    meta.outreach_brought = item.id
    app.outreach.brought(answer_turn(), meta, line, app.clock.now())
    told = records.get(app.store, item.id)
    assert told.state == "told" and told.told_via == "dm" and told.line_id == line
    assert app.outreach.offer(answer_turn()) is None
    other, _ = chat(app, "The quiet garden visit again.", owner=False, channel="dm:101")
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


# -- review-365 P2: told only when the line that went out says it --------------------------------

@pytest.mark.parametrize("sent,told", [
    ("Fair enough. I might visit that quiet garden later.", True),
    ("oh. wow. i don't know what to say", False),     # the owed fallback's fresh short line
    ("fair enough, long day here too", False),        # a guard's rewrite that dropped it
])
def test_a_line_that_does_not_say_the_item_tells_nothing(app, sent, told):
    item = plan(app)
    line, _ = chat(app, sent, owner=False, channel="dm:101")
    meta = TurnMeta("answer")
    meta.outreach_brought = item.id
    app.outreach.brought(answer_turn(), meta, line, app.clock.now())
    assert (records.get(app.store, item.id).state == "told") is told
    assert (app.outreach.offer(answer_turn()) is None) is told  # unsaid: still hers to tell


def sent_text(rig, stub):
    return [rig.store.chat_lines([line])[0].text for line in stub.lines]


def test_a_retried_line_is_judged_by_what_went_out(rig):
    """The first draft trips a tell and is asked again: the receipt is asked for once, for the
    stored line, so the service judges the words that were sent."""
    from leeroy_mind.outreach.service import carries
    rig.engine.outreach = stub = Stub()
    said = "ugh, my sister visits friday and i am dreading the drive"
    rig.llm.set_script(script(lines=["happy to help with that", said]))
    assert dm_answer(rig, "long day, glad it is over") == said
    assert sent_text(rig, stub) == [said] and carries(NEWS, said)


def test_the_owed_fallback_line_is_judged_by_what_went_out(rig):
    from leeroy_mind.outreach.service import carries
    rig.engine.outreach = stub = Stub()
    rig.llm.set_script(script(lines=["happy to help with that", "happy to help with that",
                                     "oh. wow. i don't know what to say"]))
    answer = dm_answer(rig, "long day, glad it is over")
    assert sent_text(rig, stub) == [answer] and not carries(NEWS, answer)


# -- no bring-in during an explicit scene ---------------------------------------------------------

def test_his_explicit_overlay_brings_in_nothing_of_hers(store, clock, tmp_path):
    from conftest import seed_test_cast
    from engine_rig import make_rig, save_member
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="lust", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "romance_explicit_whisper.txt").write_bytes(b"WHISPER-OVERLAY\n")
    r = make_rig(store, clock, tmp_path, beat=True)
    try:
        r.engine.outreach = stub = Stub()
        r.llm.set_script(script(want="flirt", use=None, line="mm come here then"))
        r.whisper_from_him(102, "long shift, dead on my feet")
        r.run()
        assert "WHISPER-OVERLAY" in last_user(line_calls(r)[-1])
        assert NEWS not in last_user(beat_calls(r)[-1])           # not offered
        assert stub.asked == 0 and stub.told == []                # nor the first-answer draw
        assert "Bring in:" not in last_user(line_calls(r)[-1])
    finally:
        r.close()
