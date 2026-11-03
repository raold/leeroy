from dataclasses import FrozenInstanceError

import pytest

from leeroy_mind.guild.pick import (
    Decline, Hangout, Lead, LeadOnly, Pick, is_claim, is_end, parse,
)
from leeroy_mind.guild.raids import CATALOG, split_key, validate


OFF = {"arkon": 101, "belra": 102, "beldor": 103}


def pick(text, intent=False, officers=None):
    return parse(text, OFF if officers is None else officers, require_intent=intent)


@pytest.mark.parametrize(("text", "expected"), [
    ("icc 25", Pick("icc", 25, False, None)),
    ("lets do icc25 heroic, arkon leads", Pick("icc", 25, True, Lead(False, 101))),
    ("togc 10", Pick("toc", 10, True, None)),
    ("ICC 10H", Pick("icc", 10, True, None)),
    ("icc25hc", Pick("icc", 25, True, None)),
    ("icc 25man", Pick("icc", 25, False, None)),
    ("toc10m", Pick("toc", 10, False, None)),
    ("icc25n", Pick("icc", 25, False, None)),
    ("naxx", Pick("naxx", None, False, None)),
    ("heroic ruby sanctum 25", Pick("rs", 25, True, None)),
    ("icc 25 then hang out after", Pick("icc", 25, False, None)),
])
def test_pick_size_and_heroic(text, expected):
    assert pick(text) == expected


@pytest.mark.parametrize(("alias", "key"), [
    (alias, raid.key) for raid in CATALOG.values() for alias in raid.aliases
])
def test_every_catalog_alias_matches_whole_words_longest_first(alias, key):
    assert pick(alias).raid_key == key
    assert pick(f"{alias} 25").raid_key == key


@pytest.mark.parametrize("text", [
    "fossils", "my ruby-colored shirt", "holy", "walk", "rsync", "onyx",
    "naxxramases", "trialing", "osborne", "eoeveryone", "iccidental", "uldman",
])
def test_alias_substrings_and_unrelated_prose_are_not_picks(text):
    assert pick(text) is None


@pytest.mark.parametrize("text", ["i'll lead, naxx 10", "I’ll lead, naxx 10", "i'm leading naxx10", "my lead, naxx10", "i'll call it, naxx10"])
def test_human_lead(text):
    assert pick(text) == Pick("naxx", 10, False, Lead(True))


@pytest.mark.parametrize(("text", "lead"), [
    ("arkon lead", Lead(False, 101)),
    ("you lead belra", Lead(False, 102)),
    ("you lead, belra", Lead(False, 102)),
    ("let beldor lead", Lead(False, 103)),
    ("ark leads", Lead(False, 101)),
    ("BELR lead please", Lead(False, 102)),
    ("i lead", Lead(True)),
    ("you pick a lead tonight", Lead(False, None, True)),
    ("rotate the lead", Lead(False, None, True)),
])
def test_lead_only(text, lead):
    assert pick(text) == LeadOnly(lead)


@pytest.mark.parametrize("name", ["bel", "be", "ar", "zork", "arkonn"])
def test_ambiguous_short_or_unknown_lead_ignored(name):
    assert pick(f"{name} leads, icc") == Pick("icc", None, False, None)
    assert pick(f"{name} leads") is None


def test_exact_name_wins_over_longer_prefix_and_mapping_case_is_ignored():
    assert pick("arkon leads icc", officers={"Arkon": 101, "Arkonis": 104}).lead == Lead(False, 101)
    assert pick("ark leads icc", officers={"Arkon": 101, "Arkonis": 104}).lead is None
    assert pick("Élo leads", officers={"Élodie": 105}) == LeadOnly(Lead(False, 105))


def test_conflicting_officer_names_do_not_arbitrarily_choose_one():
    assert pick("arkon leads, belra leads, icc") == Pick("icc", None, False, None)
    assert pick("arkon leads", officers={"Arkon": 101, "arkon": 102}) is None


def test_officer_named_after_raid_alias_is_not_an_extra_raid_choice():
    assert pick("ruby leads icc25", officers={"ruby": 104}) == Pick("icc", 25, False, Lead(False, 104))
    assert pick("ruby leads", officers={"ruby": 104}) == LeadOnly(Lead(False, 104))
    assert pick("icc25, heroic leads", officers={"heroic": 105}) == Pick("icc", 25, False, Lead(False, 105))


@pytest.mark.parametrize("text", [
    "not tonight", "nah maybe tomorrow", "no raid tonight guys", "skip it", "pass",
    "no thanks", "not feeling it", "not today", "nope!", "skip tonight please",
])
def test_decline(text):
    assert pick(text) == Decline()


@pytest.mark.parametrize("text", [
    "hangout night tonight", "no raid, let's just chill", "off night guys", "fishing night",
    "just hang out", "no raid let's fish", "no raid, let's just hang out",
])
def test_hangout_precedes_decline(text):
    assert pick(text) == Hangout()


def test_rotate_lead_wins_over_member_lead():
    assert pick("icc 25, someone else lead") == Pick("icc", 25, False, Lead(False, None, True))
    assert pick("icc 25, arkon leads, someone else can lead") == Pick("icc", 25, False, Lead(False, None, True))


@pytest.mark.parametrize("text", [
    "can we do icc 10", "raid? icc 10", "let's do icc10", "how about icc10",
    "wanna run icc10?", "could we go icc10", "up for icc 10", "feel like icc10?",
])
def test_casual_mode_requires_explicit_intent(text):
    assert pick(text, True) == Pick("icc", 10, False, None)


@pytest.mark.parametrize("intent", [False, True])
@pytest.mark.parametrize("text", [
    "icc was rough yesterday", "we did icc last night", "i remember icc 25",
    "i don't want to do icc", "don't do icc25", "can we not do icc",
    "let's skip icc", "not icc, naxx instead", "no icc tonight", "never icc",
    "arkon doesn't lead", "don't let arkon lead", "arkon leads were useful yesterday",
    "we had a fishing night yesterday", "don't make it a chill night", "pass me the salt",
    "maybe tomorrow is the deadline", "is it an off night?", "lol nice",
])
def test_narrated_negated_and_unrelated_messages_do_not_trigger_actions(text, intent):
    assert pick(text, intent) is None


def test_bare_raid_only_answers_an_existing_prompt():
    assert pick("icc 25", True) is None
    assert pick("icc 25") == Pick("icc", 25, False, None)


@pytest.mark.parametrize("text", [
    "icc or naxx", "icc10 then toc25", "icc 10 or 25", "icc20", "icc 40",
    "icc25n heroic", "togc10n", "icc25hh", "icc25heroics", "\"icc\"", "icc" * 400,
])
def test_ambiguous_or_malformed_requests_do_not_choose_a_default(text):
    assert pick(text) is None


@pytest.mark.parametrize("text", [
    "mine", "can i have that", "i'll take it", "need", "me pls", "gimme",
    "I’ll take that!", "can i get it?", "mine.",
])
def test_claims_are_complete_short_requests(text):
    assert is_claim(text)


@pytest.mark.parametrize("text", [
    "mine was better", "i don't need", "need a drink", "don't gimme that",
    "he said mine", "can i have that later", "\"mine\"", "mine; need", "",
])
def test_claims_reject_substrings_and_extra_instructions(text):
    assert not is_claim(text)


@pytest.mark.parametrize("text", [
    "calling it", "call it", "that's it for tonight", "done for tonight",
    "i'm out", "I’m out!", "gn", "gn all.",
])
def test_end_is_a_whole_declaration(text):
    assert is_end(text)


@pytest.mark.parametrize("text", [
    "is that it?", "calling it?", "not calling it", "don't call it", "he's calling it",
    "i'm out of mana", "gn all but i'll stay", "\"calling it\"", "that's it for tonight?", "",
])
def test_end_rejects_questions_negation_quotes_and_substrings(text):
    assert not is_end(text)


def test_catalog_exact_map_zone_progression_and_boss_counts():
    expected = {
        "icc": (631, 4812, True, 16, 12), "toc": (649, 4722, True, 15, 5),
        "ulduar": (603, 4273, False, 14, 14), "naxx": (533, 3456, False, 13, 15),
        "os": (615, 4493, False, 13, 1), "eoe": (616, 4500, False, 13, 1),
        "voa": (624, 4603, False, 13, 4), "ony": (249, 2159, False, 13, 1),
        "rs": (724, 4987, True, 17, 1),
    }
    assert set(CATALOG) == set(expected)
    for key, values in expected.items():
        raid = CATALOG[key]
        assert raid.key == key and raid.sizes == (10, 25)
        assert (raid.map_id, raid.zone_id, raid.heroic, raid.min_tier, raid.bosses) == values


def test_validate_checks_mode_then_progression():
    assert validate(CATALOG["rs"], 25, False, 16) == "tier:17"
    assert validate(CATALOG["ulduar"], 25, True, 16) == "no_heroic"
    assert validate(CATALOG["icc"], 25, True, 16) is None
    assert validate(CATALOG["icc"], 20, False, 16) == "bad_size"
    assert validate(CATALOG["ulduar"], 20, True, 0) == "bad_size"
    assert validate(CATALOG["ulduar"], 25, True, 0) == "no_heroic"
    assert validate(CATALOG["icc"], True, False, 16) == "bad_size"
    assert validate(CATALOG["icc"], 10.0, False, 16) == "bad_size"


@pytest.mark.parametrize("key", list(CATALOG))
def test_split_supports_each_canonical_size_and_legal_heroic_mode(key):
    raid = CATALOG[key]
    for size in raid.sizes:
        assert split_key(f"{key}{size}") == (raid, size, False)
        assert split_key(f"{key.upper()}{size}") == (raid, size, False)
        if raid.heroic:
            assert split_key(f"{key}{size}h") == (raid, size, True)


@pytest.mark.parametrize("key", [
    "", "icc", "icc20", "icc0", "icc025", "icc25hc", "icc25hh", "icc 25", " icc25",
    "icc25\n", "naxx10h", "ulduar25h", "voa10h", "togc10", "foo25", "icc25/rs25", None, 25,
])
def test_split_key_rejects_malformed_or_unsupported_modes_consistently(key):
    with pytest.raises(ValueError):
        split_key(key)


def test_catalog_and_results_are_immutable_slotted_values():
    with pytest.raises(FrozenInstanceError):
        CATALOG["icc"].map_id = 0
    result = pick("icc25")
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.size = 10
