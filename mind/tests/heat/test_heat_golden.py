"""Heat 9 is today (owner's ruling 1): with [heat] on and nothing moved, every prompt, line and
weight is byte-identical to the goldens recorded without it; and at every legacy dial the
romance text under a derived heat is the text at that dial.

With [heat] off the goldens are tests/tuning's own, untouched (test_tuning_invariant.py)."""
import itertools
import random
from pathlib import Path

import pytest

from conftest import make_persona
from heat.rig import ON, on
from leeroy_mind import heat as H
from leeroy_mind.heat.table import DIAL_TO_HEAT
from leeroy_mind.human import HumanIdentity
from leeroy_mind.storage.rows import ROMANCE_STAGES, Relationship
from leeroy_mind.talk import romance as R
from leeroy_mind.talk.budget import DayPhase
from tuning.scenario import engine_clock, golden, golden_romance, record, record_romance

HUMAN = HumanIdentity(guid=1, character_name="Zan", nickname="Zan", real_name="",
                      is_guild_master=True)
CHANNELS = ("whisper", "dm:102", "guild", "officer", "party", "raid", "say", "voice:1",
            "discord:general")


@pytest.fixture
def clock():
    return engine_clock()


def test_heat_on_and_unset_is_the_golden(store, clock, tmp_path):
    """[heat] enabled, no world value: heat follows the romance dial (9), so nothing moved."""
    assert record(store, clock, tmp_path, heat=on()) == golden()


def test_heat_nine_set_is_the_golden(store, clock, tmp_path):
    assert record(store, clock, tmp_path, heat=on(sets=[("world", "", 9)])) == golden()


def test_heat_nine_with_romance_realism_is_its_golden(store, clock, tmp_path):
    assert record_romance(store, clock, tmp_path, heat=on(sets=[("world", "", 9)])) \
        == golden_romance()


def test_a_lower_heat_changes_the_prompts(store, clock, tmp_path):
    """The dial does move things once it is moved: the golden is not blind to it."""
    got = record(store, clock, tmp_path, heat=on(sets=[("world", "", 3)]))
    assert got["prompts"] != golden()["prompts"]


def _rel(stage: str, attraction: float = 80.0) -> Relationship:
    return Relationship(a=102, b=1, affinity=40.0, trust=40.0, attraction=attraction,
                        romance_stage=stage, familiarity=50.0)


def _texts(p, rel, *, dial, channel, data_dir, hr=None, seed=3):
    """Everything romance.py says for one turn, with the draws a turn makes."""
    kw = {} if hr is None else {"hr": hr}
    out = []
    for realism, heat, tail_follows in itertools.product((False, True), (False, True),
                                                         (False, True)):
        out.append(R.romance_instruction(
            p, rel, dial=dial, channel=channel, addressee_is_human=True, phase=DayPhase.LATE,
            human=HUMAN, data_dir=data_dir, rng=random.Random(seed), tail_follows=tail_follows,
            scene=["{they} asked about you"], arc_lines=[], rival="Dunna", cooling=True,
            realism=realism, heat=heat, **kw))
    out.append(R.whisper_tail(p, rel, dial=dial, channel=channel, data_dir=data_dir,
                              forms=HUMAN.forms, rng=random.Random(seed), **kw))
    out.append(R.explicit_whisper(p, rel, dial=dial, channel=channel, **kw))
    out.append(R.dm_explicit(p, rel, dial=dial, channel=channel, data_dir=data_dir, **kw))
    return out


def _overlays(data_dir: Path, *, explicit: bool) -> None:
    prompts = data_dir / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    if explicit:  # invented stand-ins for the owner's files: two variants each
        (prompts / "romance_explicit.txt").write_bytes(b"OVERLAY ONE\n---\nOVERLAY TWO\n")
        (prompts / "romance_explicit_whisper.txt").write_bytes(b"TAIL ONE\n---\nTAIL TWO\n")


@pytest.mark.parametrize("explicit", (True, False))
def test_at_every_legacy_dial_the_text_under_heat_is_the_text_at_that_dial(tmp_path, explicit):
    """For each stance, stage, channel and own dial: romance_instruction, the whisper tail and
    the explicit flags with `hr` (heat derived from the dial, his lines having led) equal the
    ones without it, byte for byte. Heat 9 (dial 9) is one row of it."""
    _overlays(tmp_path, explicit=explicit)
    checked = 0
    for stance, stage, own in itertools.product(("fond", "drawn", "lust"), ROMANCE_STAGES,
                                                 (None, 4, 10)):
        p = make_persona(102, "Belra", player_gender="female", stance=stance, dial=own)
        rel = _rel(stage)
        for dial, channel in itertools.product(range(1, 11), CHANNELS):
            hr = H.resolve(DIAL_TO_HEAT[dial], H.Member(stance=stance, own_dial=own),
                           private=channel in ("whisper", "dm:102"), legacy=dial, his_heat=99)
            assert _texts(p, rel, dial=dial, channel=channel, data_dir=tmp_path, hr=hr) == \
                _texts(p, rel, dial=dial, channel=channel, data_dir=tmp_path), (
                    stance, stage, own, dial, channel)
            checked += 1
    assert checked == 3 * len(ROMANCE_STAGES) * 3 * 10 * len(CHANNELS)


def test_heat_nine_needs_no_lead_so_it_is_today_from_his_first_line(tmp_path):
    _overlays(tmp_path, explicit=True)
    p = make_persona(104, "Dunna", player_gender="female", stance="lust")
    for channel in CHANNELS:
        hr = H.resolve(9, H.Member(stance="lust"), private=channel in ("whisper", "dm:102"),
                       legacy=9, his_heat=0)
        assert _texts(p, _rel("whispers"), dial=9, channel=channel, data_dir=tmp_path, hr=hr) \
            == _texts(p, _rel("whispers"), dial=9, channel=channel, data_dir=tmp_path)


def test_heat_band_is_the_band_at_the_tiers_dial_and_frank_only_under_explicit():
    for stance, stage in itertools.product(("fond", "drawn", "lust"), ROMANCE_STAGES):
        for heat in range(1, 11):
            hr = H.resolve(heat, H.Member(stance=stance), his_heat=99)
            got = R.heat_band(stance, stage, hr)
            if heat == 8:  # frank: where dial 9 would be explicit, else her own lower band
                top = R.effective_band(stance, stage, 8) if stance == "drawn" else \
                    R.effective_band(stance, stage, 9)
                reach = R.effective_band(stance, stage, 10 if stance != "drawn" else 8)
                assert got == ("frank" if (stance != "drawn" and top == "explicit") or (
                    stance == "drawn" and R.ATTITUDE_REACH["drawn"] + R.STAGE_DEPTH[stage] >= 3)
                    else reach), (stance, stage)
            else:
                assert got == R.effective_band(stance, stage, hr.dial), (stance, stage, heat)
        assert R.heat_band(stance, stage, H.resolve(0, H.Member(stance=stance))) is None
    assert R.heat_band("none", "none", H.resolve(9, H.Member(stance="lust"))) is None


def test_heat_zero_has_no_romance_text_at_all(tmp_path):
    p = make_persona(104, "Dunna", player_gender="female", stance="lust")
    hr = H.resolve(0, H.Member(stance="lust"))
    assert R.romance_instruction(p, _rel("whispers"), dial=9, channel="whisper",
                                 addressee_is_human=True, phase=DayPhase.EVENING, human=HUMAN,
                                 data_dir=tmp_path, hr=hr) is None
    assert R.whisper_tail(p, _rel("whispers"), dial=9, channel="whisper", data_dir=tmp_path,
                          forms=HUMAN.forms, hr=hr) == ""


def test_on_is_a_config_with_the_cap_off():
    assert ON.enabled and not ON.public_cap
