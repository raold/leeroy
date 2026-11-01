"""Adversarial review cases for Task 12, using only the synthetic nightly harness."""
import asyncio
import json

import pytest

from leeroy_mind.nightly.attraction import stage_attraction
from leeroy_mind.nightly.growth import undo
from leeroy_mind.nightly.romance import jealousy_level, known_acts, stage_jealousy
from leeroy_mind.personas.io import load_persona


run = asyncio.run
DAY = 86400


def pair(h):
    for guid, score in ((101, 45), (102, 59)):
        h.woman(guid, "drawn")
        h.rel(guid, 1, attraction=score, romance_stage="banter")
    h.seen(101, 102)


def public_line(h, at, text="belra that meant a lot to me"):
    line = h.say(at, 1, text)
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience='[101,102]' WHERE id=?", (line,))
    return line


def late_nudges(h, *, processed, source, positive=True):
    if positive:
        h.store.add_rel_delta(102, 1, "attraction", 2, "subtle warmth",
                              [f"chat:{source}"], processed, applied=False)
    h.store.add_rel_delta(101, 1, "attraction", -3, "watching a rival",
                          [f"chat:{source}"], processed, applied=False)


@pytest.mark.parametrize("delay", [0, 3600])
def test_late_band_and_negative_rivalry_count_in_source_night_only(h, delay):
    pair(h)
    line = public_line(h, h.T)
    ctx = h.ctx()
    now = ctx.window[1] + delay
    h.clock.set(now)
    late_nudges(h, processed=now, source=line)
    run(stage_attraction(ctx))
    assert not ctx.failures
    assert load_persona(h.store, 102).romance.stance == "lust"
    band_ids = json.loads(h.store.get_meta("attraction_band_changes:2026-10-29"))
    assert len(band_ids) == 1
    assert known_acts(h.store, 101, 102, ctx.window) == [f"chat:{line}"]
    # A restart between committed stages must retain the current-night band spike.
    resumed = h.ctx()
    run(stage_jealousy(resumed))
    assert not resumed.failures
    assert jealousy_level(h.store, 101, 102) == 19  # one observed act and one band spike
    assert h.store.conn.execute(
        "SELECT gained FROM attraction_log WHERE night=? AND guid=102", (ctx.night,)
    ).fetchone()[0] == 2

    later = public_line(h, h.T + DAY, "belra thanks for spending time with me")
    second = h.ctx(night="2026-10-30")
    h.clock.set(now + DAY)
    late_nudges(h, processed=h.clock.now(), source=later, positive=False)
    run(stage_attraction(second))
    run(stage_jealousy(second))
    assert not second.failures
    assert known_acts(h.store, 101, 102, second.window) == [f"chat:{later}"]
    assert jealousy_level(h.store, 101, 102) == pytest.approx(19 * .85 + 4)
    # Repeating either committed stage must not add another observation or decay.
    run(stage_attraction(h.ctx(night="2026-10-30")))
    run(stage_jealousy(h.ctx(night="2026-10-30")))
    assert jealousy_level(h.store, 101, 102) == pytest.approx(19 * .85 + 4)


def test_undo_between_nightly_attraction_and_jealousy_cancels_band_spike(h):
    pair(h)
    line = public_line(h, h.T)
    ctx = h.ctx()
    h.clock.set(ctx.window[1] + 60)
    late_nudges(h, processed=h.clock.now(), source=line)
    run(stage_attraction(ctx))
    (band_id,) = json.loads(h.store.get_meta("attraction_band_changes:2026-10-29"))
    assert undo(h.store, band_id, now=h.clock.now())
    assert load_persona(h.store, 102).romance.stance == "drawn"
    assert h.store.get_relationship(102, 1).attraction == 59
    resumed = h.ctx()
    run(stage_jealousy(resumed))
    assert not resumed.failures
    assert jealousy_level(h.store, 101, 102) == 4  # the observation remains, the band does not


@pytest.mark.parametrize("private,known", [(False, False), (True, False), (True, True)])
def test_late_negative_rivalry_uses_source_time_and_visibility(h, private, known):
    pair(h)
    line = h.say(h.T, 1, "belra you're cute", channel="whisper" if private else "guild",
                 to=102 if private else 0)
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience=? WHERE id=?",
                             (json.dumps([102] if private else [101, 102]), line))
    if known:
        from leeroy_mind.nightly.knowledge import learn
        learn(h.store, f"chat:{line}", 101, "told", via=102, ts=h.T + 60)
    ctx = h.ctx()
    h.clock.set(ctx.window[1] + 3600)
    late_nudges(h, processed=h.clock.now(), source=line, positive=False)
    run(stage_attraction(ctx))
    run(stage_jealousy(ctx))
    assert not ctx.failures
    expected = 4 if not private or known else 0
    assert jealousy_level(h.store, 101, 102) == expected
    assert h.store.get_relationship(101, 1).attraction == 45
    assert h.count("attraction_act") == h.count("emotion") == 0
    h.clock.advance(DAY)
    run(stage_jealousy(h.ctx(night="2026-10-30")))
    assert jealousy_level(h.store, 101, 102) == pytest.approx(expected * .85)


def test_live_receipt_failure_rolls_back_gain_and_retry_counts_once(h, monkeypatch):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=40)
    h.seen(101)
    ev = h.his(h.T, "arkon you're cute")
    save_meta = h.store.set_meta

    def fail_receipt(key, value):
        if key.startswith("attraction_chat:"):
            raise RuntimeError("synthetic receipt failure")
        return save_meta(key, value)

    monkeypatch.setattr(h.store, "set_meta", fail_receipt)
    with pytest.raises(RuntimeError, match="synthetic receipt failure"):
        run(h.live().on_chat(ev))
    assert h.count("attraction_act") == 0
    assert h.store.get_relationship(101, 1).attraction == 40
    assert load_persona(h.store, 101).romance.stance == "drawn"
    monkeypatch.setattr(h.store, "set_meta", save_meta)
    run(h.live().on_chat(ev))
    run(h.live().on_chat(ev))
    assert h.count("attraction_act") == 1
    assert h.store.get_relationship(101, 1).attraction == 60


def test_one_public_line_has_independent_receipts_for_each_recipient(h):
    pair(h)
    ev = h.his(h.T, "arkon belra you're cute")
    run(h.live().on_chat(ev))
    assert h.store.get_relationship(101, 1).attraction == 65
    assert h.store.get_relationship(102, 1).attraction == 79
    run(h.live().on_chat(ev))
    assert h.count("attraction_act") == 2
