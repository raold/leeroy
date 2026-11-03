"""Independent collision and action-boundary checks for raid commands."""

import pytest

from leeroy_mind.guild.pick import Lead, LeadOnly, Pick, is_claim, is_end, parse
from leeroy_mind.guild.raids import CATALOG, split_key, validate


OFFICERS = {"arkon": 101, "Ruby": 201, "Trial": 202, "Maly": 203, "Rubina": 204}


def _parse(text, intent=False):
    return parse(text, OFFICERS, require_intent=intent)


@pytest.mark.parametrize(("text", "expected"), [
    ("ruby leads icc25", Pick("icc", 25, False, Lead(False, 201))),
    ("let trial lead, naxx10", Pick("naxx", 10, False, Lead(False, 202))),
    ("you lead, maly, ulduar25", Pick("ulduar", 25, False, Lead(False, 203))),
    ("ruby lead", LeadOnly(Lead(False, 201))),
    ("let trial lead", LeadOnly(Lead(False, 202))),
    ("you lead maly", LeadOnly(Lead(False, 203))),
])
def test_review_officer_named_after_a_raid_alias_is_still_a_lead(text, expected):
    assert _parse(text) == expected


def test_review_alias_officer_name_does_not_hide_a_real_second_pick():
    assert _parse("icc25, ruby leads, halion25") is None
    assert _parse("ruby25, arkon leads") == Pick("rs", 25, False, Lead(False, 101))
    assert _parse("rub leads, icc25") == Pick("icc", 25, False, None)


@pytest.mark.parametrize("text", [
    "icc25; calling it", "calling it; icc25", "icc25; mine", "mine; icc25",
    "i'm out; mine", "mine; i'm out", "gn all, let's do icc25",
])
def test_review_combined_actions_are_not_partially_consumed(text):
    assert _parse(text) is None
    assert not is_claim(text)
    assert not is_end(text)


@pytest.mark.parametrize("text", [
    "not tonight; icc25", "no raid, icc25", "icc25? don't do it",
    "we didn't run icc25", "icc25, never again", "could we not run icc25",
    "don't let ruby lead, icc25", "not calling it, icc25",
])
@pytest.mark.parametrize("require_intent", [False, True])
def test_review_negation_survives_compact_forms_and_separators(text, require_intent):
    assert _parse(text, require_intent) is None
    assert not is_end(text)


@pytest.mark.parametrize("text", [
    "undo icc25", "ongoing icc25", "runner icc25", "raidwide icc25",
    "forego icc25", "overdo icc25", "icc25 was a good run",
])
def test_review_intent_substrings_and_past_prose_cannot_open_a_raid(text):
    assert _parse(text, True) is None


def test_review_all_catalog_aliases_accept_compact_size_without_changing_raid():
    for key, raid in CATALOG.items():
        for alias in raid.aliases:
            expected_heroic = alias == "togc"
            assert _parse(f"can we do {alias}25") == Pick(key, 25, expected_heroic, None)
            assert _parse(f"can we do {alias}10hc", True) == Pick(key, 10, True, None)
            # Unsupported heroic requests remain a parsed pick for validation feedback.
            expected_error = None if raid.heroic else "no_heroic"
            assert validate(raid, 10, True, 99) == expected_error


def test_review_configuration_keys_are_canonical_even_when_chat_accepts_aliases():
    for key, raid in CATALOG.items():
        parsed, size, heroic = split_key(f"{key.upper()}25")
        assert (parsed.key, size, heroic) == (key, 25, False)
        for alias in raid.aliases:
            if alias != key:
                with pytest.raises(ValueError):
                    split_key(f"{alias}25")
    # VoA is a valid catalog key; the offline configuration layer excludes it.
    assert split_key("voa10") == (CATALOG["voa"], 10, False)


@pytest.mark.parametrize("text", [None, b"icc25", 25, ["icc25"], "icc25\x00", "mine\x00"])
def test_review_nontext_and_embedded_control_suffixes_never_become_commands(text):
    assert _parse(text) is None
    assert not is_claim(text)
    assert not is_end(text)
