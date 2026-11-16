"""Persona facts and the first-person lines that contradict them (identity audit, 2026-11-15:
married women said they were single, a cat owner talked about her dog, a mother of a daughter
about her son). Invented households only."""
import pytest

from conftest import make_persona
from leeroy_mind.personas.facts import (Facts, conflicts, facts_from, facts_of, facts_of_json,
                                        first_person, pronoun, pronoun_of, pronoun_pair)
from leeroy_mind.personas.schema import persona_to_dict


@pytest.mark.parametrize("household,partner,kids,pets", [
    ("Lives with her husband and a rescue terrier", "married", "none", {"dog"}),
    ("Two-bed with civil partner and an opinionated collie", "married", "none", {"dog"}),
    ("Married, two toddler twins", "married", "some", set()),
    ("Girlfriend Sue, black cat Sergeant", "partnered", "none", {"cat"}),
    ("Divorced, shares house with 16yo son and a goldfish", "single", "some", {"other"}),
    ("Lives alone; 14-year-old son lives with ex-husband", "single", "some", set()),
    ("Lives alone with a grey cat", "alone", "none", {"cat"}),
    ("Split-level in a quiet town, two beagles (Bolt, Split), no kids", "", "none", {"dog"}),
    ("Shares a flat with two girls from uni", "", "none", set()),
    ("Lives with her nan; grandkids next door", "", "none", set()),
])
def test_a_household_settles_partner_kids_and_pets(household, partner, kids, pets):
    f = facts_from(household=household)
    assert (f.partner, f.kids, set(f.pets)) == (partner, kids, pets)


def test_her_story_can_name_a_child_or_pet_the_household_leaves_out():
    f = facts_from(household="Two-bedroom ranch, one cat, divorced",
                   more=("Her daughter is sixteen and loud",))
    assert f.kids == "some" and f.kid_kinds == {"daughter"}
    assert "any" in facts_from(household="Lives with her brother", more=("the pet helps",)).pets


MARRIED = facts_from(gender="female", household="Lives with her husband and a terrier",
                     region="UK", job="nurse")


@pytest.mark.parametrize("text,codes", [
    ("i'm single lol", ["partner"]),
    ("single, no kids. just me", ["partner"]),
    ("i've never been married", ["partner"]),
    ("i live alone these days", ["partner"]),
    ("my wife would laugh", ["partner"]),
    ("my two daughters keep me busy", ["kids"]),
    ("i have two kids, five and eight", ["kids"]),
    ("my cat knocked my drink over", ["pets"]),
    ("here in the states it's raining", ["region"]),
    ("i'm unemployed right now", ["job"]),
    ("my husband is cooking", []),
    ("my dog wants a walk", []),
    ("she said her son is ill", []),  # about someone else
    ("my mum's cat hates me", []),
    ("you're single? lucky you", []),
])
def test_first_person_lines_that_contradict_her(text, codes):
    assert conflicts(text, MARRIED) == codes


def test_a_partner_allows_not_being_married_and_kids_keep_their_kind():
    f = facts_from(household="Lives with her boyfriend and a cat")
    assert conflicts("i'm not married", f) == []
    assert conflicts("i'm single", f) == ["partner"]
    assert conflicts("my husband is out", f) == ["partner"]
    mum = facts_from(household="Single mum, lives with 6-year-old daughter")
    assert conflicts("my son is my world", mum) == ["kids"]
    assert conflicts("my daughter is my world", mum) == []
    assert conflicts("i don't have any kids", mum) == ["kids"]
    assert conflicts("my husband says hi", mum) == ["partner"]


def test_a_stored_claim_reads_as_her_own_words():
    assert first_person("is single with no children") == "i'm single with no children"
    assert first_person("has two daughters aged five and eight").startswith("i have two")
    assert first_person("loves her husband's cooking") == "i love my husband's cooking"
    assert conflicts("is single with no children", MARRIED, claim=True) == ["partner"]
    assert conflicts("has two daughters aged five and eight", MARRIED, claim=True) == ["kids"]
    assert conflicts("loves the saurfang fight", MARRIED, claim=True) == []


def test_unsettled_facts_are_never_a_conflict():
    """Facts() (no persona row) settles nothing; a stored household that names nobody is
    complete: no kids and no pets."""
    for text in ("i'm single", "my husband", "my two kids", "here in the states", "my cat"):
        assert conflicts(text, Facts()) == []
    assert conflicts("my two kids and my cat", facts_from(household="Lives in a flat")) == [
        "kids", "pets"]


def test_pronouns_and_the_stored_json_agree():
    p = make_persona(101, "Arkon", player_gender="female",
                     household="Lives with her husband and two cats")
    assert pronoun_of(p) == "she" and pronoun("male") == "he" and pronoun("") == ""
    assert pronoun_pair("female") == "she/her" and pronoun_of(None) == ""
    assert facts_of(p) == facts_of_json(persona_to_dict(p))
    assert facts_of_json({}) == facts_from()
