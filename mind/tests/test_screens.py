import pytest

from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.screens import check_no_real_name, check_romance

H = HumanIdentity(1, "Testmage", "Testy", "Bill", True)  # a real name that is also a word


def _bad(data, path):
    with pytest.raises(ValueError) as e:
        check_no_real_name(data, H)
    assert path in str(e.value) and "Bill" not in str(e.value) and "bill" not in str(e.value)


@pytest.mark.parametrize("text", ["line one\nBill is fine", "tab\tBill", "told Bill.",
                                  "Bill's raid", "(Bill)", "Bill"])
def test_the_capitalised_name_is_caught_in_free_text(text):
    _bad({"secret": text}, "secret")


def test_error_names_the_field_path_not_the_name():
    _bad({"sample_lines": [{"text": "a", "approved": False}, {"text": "b", "approved": False},
                           {"text": "c", "approved": False}, {"text": "hi Bill", "approved": False}]},
         "sample_lines[3].text")
    _bad({"typing": {"abbreviations": {"Bill": "x"}}}, "typing.abbreviations.<key>")


@pytest.mark.parametrize("text", ["pay the bill", "the gas bill came", "split the bill",
                                  "Billy", "billing"])
def test_ordinary_lowercase_words_pass(text):
    check_no_real_name({"secret": text, "opinions": [text]}, H)


def test_name_fields_match_case_insensitively():
    _bad({"identity": {"real_name": "bill Holt"}}, "identity.real_name")
    _bad({"relationships": [{"other": "BILL", "kind": "friend", "note": "x"}]},
         "relationships[0].other")


def test_no_real_name_configured_means_no_check():
    check_no_real_name({"secret": "Bill"}, HumanIdentity(1, "Testmage", "", "", True))


def _romance(data, names=("Arkon",)):
    check_romance(data, H, names)


@pytest.mark.parametrize("text", [
    "kiss of death", "flirts with disaster", "crushes him on the meters",
    "friends dating back to vanilla", "cat lover", "kissing up to Arkon", "kiss up to Arkon",
    "Arkon is a lover of fishing", "Arkon dated nobody"])
def test_innocent_raider_talk_with_a_member_passes(text):
    _romance({"secret": text})
    _romance({"opinions": [text, "x", "y"]})


@pytest.mark.parametrize("text", [
    "has a crush on Arkon", "crushes on Arkon", "flirting with Arkon", "kissed Arkon",
    "dating Arkon", "Arkon is her ex", "Arkon, his ex-wife", "fancies Arkon",
    "has a thing for Arkon", "snogged Arkon", "married to Arkon", "a date with Arkon",
    "Arkon is her secret lover", "in love with Arkon", "hooked up with Arkon",
    "Arkon is an ex-husband", "got a girlfriend, Arkon"])
def test_romance_involving_a_member_is_refused(text):
    with pytest.raises(ValueError):
        _romance({"secret": text})
    with pytest.raises(ValueError):
        _romance({"game": {"wants": [text]}})


def test_items_are_checked_one_by_one():
    _romance({"opinions": ["Arkon pulls well", "has a crush on someone", "x"]})
    with pytest.raises(ValueError, match=r"opinions\[1\]"):
        _romance({"opinions": ["x", "Arkon has a crush on Bela", "x"]}, names=("Arkon", "Bela"))


def test_member_names_match_case_sensitively_and_whole_word():
    _romance({"secret": "has a crush on arkon"})
    _romance({"secret": "has a crush on Arkonian"})


def test_romance_toward_the_human_passes():
    _romance({"secret": "has a crush on Testy", "relationships": [
        {"other": "@human", "kind": "stance", "note": "flirts with the GM"},
        {"other": "Testmage", "kind": "stance", "note": "in love"}]})


@pytest.mark.parametrize("text", [
    "jealous when Arkon flirts with Testy", "hates it when Arkon flirts with Testmage",
    "sulks whenever Arkon has a crush on @human"])
def test_rivalry_over_the_human_that_names_a_member_passes(text):
    _romance({"secret": text})
    _romance({"opinions": ["x", text, "y"]})
    _romance({"game": {"wants": [text]}})


@pytest.mark.parametrize("note", ["always flirting with Testy, it's annoying",
                                  "has a crush on Testmage and everyone knows",
                                  "keeps flirting with @human"])
def test_a_note_about_a_members_romance_toward_the_human_passes(note):
    _romance({"relationships": [{"other": "Arkon", "kind": "friction", "note": note}]})


@pytest.mark.parametrize("data", [
    {"secret": "has a crush on Arkon"},
    {"game": {"wants": ["to kiss Arkon at the raid party"]}},
    {"secret": "has a crush on Arkon and gets testy about it"},  # a lowercase word, not him
    {"relationships": [{"other": "Arkon", "kind": "friend", "note": "has a crush on Arkon"}]},
    {"relationships": [{"other": "Arkon", "kind": "friend", "note": "secretly dating"}]}])
def test_romance_aimed_at_a_member_is_still_refused(data):
    with pytest.raises(ValueError):
        _romance(data)


@pytest.mark.parametrize("text", [
    "flirts with Testy", "Arkon flirts with Testy", "jealous when Arkon flirts with Testy",
    "jealous when Arkon flirts with the GM", "jealous when Arkon flirts with the gm",
    "Arkon keeps flirting with GM", "Arkon flirts with the guild master",
    "Arkon flirts with the Guild Master's alt",
    "flirts with Testy all through every single raid night, so Arkon sulks"])  # past the window
def test_romance_toward_the_human_with_a_member_nearby_passes(text):
    _romance({"secret": text})
    _romance({"opinions": ["x", text, "y"]})
    _romance({"game": {"wants": [text]}})


@pytest.mark.parametrize("text", [
    "flirts with Arkon to make Testy jealous", "has a crush on Arkon but tells Testy otherwise",
    "kissed Arkon, then Testmage", "dated Arkon before Testy joined",
    "flirts with Arkon to make the GM jealous", "in love with Arkon, not the guild master",
    "keeps flirting with @human and with Arkon"])
def test_romance_aimed_at_a_member_is_refused_even_when_the_human_is_named(text):
    for data in ({"secret": text}, {"opinions": ["x", text, "y"]}, {"game": {"wants": [text]}},
                 {"relationships": [{"other": "Bela", "kind": "friend", "note": text}]}):
        with pytest.raises(ValueError):
            _romance(data)


def test_a_note_aiming_romance_at_its_own_member_is_refused_even_naming_the_human():
    with pytest.raises(ValueError):
        _romance({"relationships": [{"other": "Bela", "kind": "friend",
                                     "note": "dating Bela behind the GM's back"}]})
    _romance({"relationships": [{"other": "Bela", "kind": "friction",
                                 "note": "always flirting with the GM, it's annoying"}]})


def test_a_lowercase_gm_does_not_name_the_human():
    with pytest.raises(ValueError):
        _romance({"secret": "has a crush on Arkon, gm knows"})


def test_the_gm_names_the_human_only_when_he_is_the_guild_master():
    not_gm = HumanIdentity(1, "Testmage", "Testy", "", False)
    with pytest.raises(ValueError):
        check_romance({"secret": "jealous when Arkon flirts with the GM"}, not_gm, ("Arkon",))
    check_romance({"secret": "jealous when Arkon flirts with Testy"}, not_gm, ("Arkon",))


from leeroy_mind.personas.screens import redact_real_name  # noqa: E402


@pytest.mark.parametrize("configured", ["bill", "BILL", "Bill"])
def test_free_text_screen_works_for_any_configured_casing(configured):
    h = HumanIdentity(1, "Testmage", "Testy", configured, True)
    for caught in ("told Bill so", "BILL!!!", "Bill's raid"):
        with pytest.raises(ValueError):
            check_no_real_name({"secret": caught}, h)
    for fine in ("pay the bill", "the gas bill came", "a bill to pay"):
        check_no_real_name({"secret": fine}, h)
    with pytest.raises(ValueError):
        check_no_real_name({"identity": {"real_name": "bill Holt"}}, h)


@pytest.mark.parametrize("configured", ["Bill", "bill", "BILL"])
def test_redact_real_name_takes_the_title_and_upper_forms_not_the_word(configured):
    h = HumanIdentity(1, "Testmage", "Testy", configured, True)
    assert redact_real_name("Bill paid the bill. BILL! Billy is billing", h, "Testy") == \
        "Testy paid the bill. Testy! Billy is billing"
    assert redact_real_name("bIlL", HumanIdentity(1, "Testmage", "Testy", "bIlL", True),
                            "Testy") == "Testy"  # the configured spelling, whatever it is
    assert redact_real_name("Bill", HumanIdentity(1, "Testmage", "Testy", "", True),
                            "Testy") == "Bill"  # nothing configured, nothing to take
    check_no_real_name({"secret": "paid the bill"}, h)  # persona screens: capitals only


def test_error_path_redacts_only_matching_segments():
    h = HumanIdentity(1, "Testmage", "Testy", "Max", True)
    with pytest.raises(ValueError) as e:
        check_no_real_name({"habits": [{"max_state": "Max"}]}, h)
    assert "habits[0].max_state" in str(e.value) and "Max " not in str(e.value).split("mentions")[0]


@pytest.mark.parametrize("text", ["his ex-guildmate", "their ex-guild", "his ex officer",
                                  "fancies himself the better tank", "fancies themselves"])
def test_more_innocent_phrases_pass(text):
    _romance({"secret": text + " Arkon"})
    _romance({"relationships": [{"other": "Arkon", "kind": "friend", "note": text}]})


@pytest.mark.parametrize("text", ["crushed on Arkon", "slept with Arkon", "dated Arkon",
                                  "married Arkon"])
def test_more_romance_misses_are_caught(text):
    with pytest.raises(ValueError):
        _romance({"secret": text})
    with pytest.raises(ValueError):
        _romance({"relationships": [{"other": "Arkon", "kind": "friend", "note": text}]})


def test_the_retry_text_follows_his_pronouns():
    from leeroy_mind.personas.screens import real_name_error
    assert real_name_error("secret", H).endswith("refer to them by their nickname")
    he = HumanIdentity(1, "Testmage", "Testy", "Bill", True, pronouns="he")
    assert real_name_error("secret", he).endswith("refer to him by his nickname")
