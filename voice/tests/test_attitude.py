import pytest

from leeroy_voice.attitude import Levers, kokoro_markup, levers_for
from leeroy_voice.wire import ATTITUDES, STATES


def test_speed():
    assert levers_for("lust", "sober", "flirt").speed_mult == 0.86
    assert levers_for("drawn", "sober", "whisper").speed_mult == 0.92
    assert levers_for("fond", "sober", "reply_human").speed_mult == 1.0
    assert levers_for("none", "sober", "callout").speed_mult == 1.08
    assert levers_for("none", "drunk", "open").speed_mult == pytest.approx(0.90)
    assert levers_for("none", "wired", "open").speed_mult == pytest.approx(1.05)
    assert levers_for("none", "hungover", "open") == Levers(0.95, 0, 0, -2.0)


def test_states():
    speed = {s: levers_for("none", s, "open").speed_mult for s in STATES}
    assert speed == pytest.approx({"sober": 1.0, "tipsy": 0.95, "drunk": 0.90, "high": 0.95,
                                   "wired": 1.05, "hungover": 0.95})
    assert levers_for("lust", "tipsy", "flirt").speed_mult == pytest.approx(0.86 * 0.95)
    assert levers_for("drawn", "sober", "urgent") == Levers(1.08, 0, 0, 0.0)


@pytest.mark.parametrize("attitude", ATTITUDES)
@pytest.mark.parametrize("kind", ["callout", "urgent"])
def test_raid_calls_are_sober(kind, attitude):     # P1b's RAID_CALLS; no attitude either (ruling)
    for state in STATES:
        assert levers_for(attitude, state, kind) == Levers(1.08, 0, 0, 0.0)


def test_stress_and_pauses_per_attitude():
    got = {a: (levers_for(a, "sober", "reply_human").stress,
               levers_for(a, "sober", "reply_human").ellipses) for a in ATTITUDES}
    assert got == {"none": (0, 0), "fond": (1, 0), "drawn": (2, 1), "lust": (2, 2)}
    assert levers_for("none", "drunk", "open").ellipses == 1
    assert levers_for("lust", "drunk", "flirt").ellipses == 3


def test_unknown_values_refused():
    with pytest.raises(ValueError, match="attitude"):
        levers_for("smitten", "sober", "open")
    with pytest.raises(ValueError, match="state"):
        levers_for("none", "stoned", "open")


def test_markup():
    dr = levers_for("drawn", "sober", "whisper")
    assert kokoro_markup("So, I saved you a spot.", dr, to_human=True) == "So... I saved [you](+2) a spot."
    assert kokoro_markup("I saved you a spot.", levers_for("fond", "sober", "reply_human"),
                         to_human=True) == "I saved [you](+1) a spot."
    assert kokoro_markup("I saved you a spot.", dr, to_human=False) == "I saved you a spot."


def test_markup_pauses_in_order_and_one_stress():
    lust = levers_for("lust", "sober", "flirt")
    assert kokoro_markup("I brought flasks, food buffs, and the spare gear. Can you check it "
                         "with me?", lust, to_human=True) == (
        "I brought flasks... food buffs... and the spare gear. Can [you](+2) check it with me?")
    fond = levers_for("fond", "sober", "reply_human")
    assert kokoro_markup("You're up, and you know your part.", fond, to_human=True) == (
        "[You're](+1) up, and you know your part.")
    drawn = levers_for("drawn", "sober", "whisper")
    assert kokoro_markup("1,000 gold, you said?", drawn, to_human=False) == (
        "1,000 gold... you said?")
    assert kokoro_markup("Let's meet, you tank and I heal.", fond, to_human=True) == (
        "Let's meet, [you](+1) tank and I heal.")            # whole words: not the me of meet


def test_markup_leaves_lexicon_markup_alone():
    dr = levers_for("drawn", "sober", "whisper")
    assert kokoro_markup("[Kel'Thuzad](/kˌɛlθˈuzɑd/), then me", dr, to_human=True) == (
        "[Kel'Thuzad](/kˌɛlθˈuzɑd/)... then [me](+2)")
    # a member may be called "Me": her name's markup is not the "me" aimed at him
    assert kokoro_markup("[Me](/mˈi/) saw you", dr, to_human=True) == "[Me](/mˈi/) saw [you](+2)"


def test_plain_levers_change_nothing():
    plain = levers_for("none", "sober", "open")
    assert kokoro_markup("So, you saw me.", plain, to_human=True) == "So, you saw me."
    raid_call = levers_for("lust", "drunk", "urgent")
    assert kokoro_markup("Adds, you tank.", raid_call, to_human=True) == "Adds, you tank."
