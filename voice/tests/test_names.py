"""The roster correction of his transcribed lines (stt/names.py): near-miss names and WoW words
put right, ordinary words left alone, the address at the start of a line, and the sound-alike
raid words. No model: rapidfuzz and jellyfish are core dependencies."""
import importlib.util
import sys

import pytest

from leeroy_voice.stt.names import (DIRECT, MAX_RUN, NOT_ADDRESS, PHONETIC, SOUNDALIKES,
                                    RosterCorrector, common_words)

COMMON = frozenset({"chorus", "the", "sing", "in", "you're", "on", "adds", "bella"})


def rc():
    return RosterCorrector(["Belra", "Coros", "Dunna"], ["Kel'Thuzad", "ICC"], common=COMMON)


# -- the brief's ----------------------------------------------------------------------------------

def test_near_miss_names():
    assert rc().correct("bellra you're on adds") == "Belra you're on adds"


def test_common_words_left_alone():
    assert rc().correct("sing in the chorus") == "sing in the chorus"


def test_address_at_the_start():
    assert rc().correct("Chorus, you're on adds") == "Coros, you're on adds"


def test_wow_words():
    assert rc().correct("kel thuzad is up") == "Kel'Thuzad is up"


# -- the rules' edges -----------------------------------------------------------------------------

def test_the_thresholds():
    assert (DIRECT, PHONETIC, MAX_RUN) == (85, 70, 3)
    r = rc()
    assert r.correct("duna") == "Dunna"              # ratio 88.9: on spelling alone
    assert r.correct("bela") == "Belra"              # ratio 88.9, though BL is not BLR
    assert r.correct("koros") == "Coros"             # ratio 80 and both KRS
    assert r.correct("belry") == "Belra"             # ratio 80 and both BLR (Parakeet's, measured)
    assert r.correct("kelthu") == "kelthu"           # ratio 80, but KL0 is not KL0ST


def test_a_metaphone_match_needs_seventy():
    """Belra and "blare" share BLR, but at ratio 60 the sound alone is not enough."""
    assert rc().correct("blare") == "blare"
    assert rc().correct("blare belry") == "blare Belra"


def test_exact_spellings_are_restored():
    r = rc()
    assert r.correct("icc tonight") == "ICC tonight"
    assert r.correct("kelthuzad is up") == "Kel'Thuzad is up"
    assert r.correct("is coros here") == "is Coros here"


def test_common_is_case_blind_and_curly_apostrophes_count():
    r = rc()
    assert r.correct("Sing In The Chorus") == "Sing In The Chorus"
    assert r.correct("you\u2019re on adds") == "you\u2019re on adds"
    assert r.correct("Sing In The Chorus, Bellra") == "Sing In The Chorus, Belra"
    duna = RosterCorrector(["Dunna"], [], common=frozenset({"duna", "is", "here"}))
    assert duna.correct("Duna is here") == "Duna is here"          # 88.9, but common
    shell = RosterCorrector(["Belra"], ["Shell"], common=frozenset({"she'll", "pull"}))
    assert shell.correct("she\u2019ll pull") == "she\u2019ll pull"
    assert shell.correct("she'll pull") == "she'll pull"
    assert shell.correct("shel pull") == "Shell pull"


def test_everything_else_is_kept():
    r = rc()
    assert r.correct("  ok   bellra!  ") == "  ok   Belra!  "
    assert r.correct("bellra? dunna... coros!") == "Belra? Dunna... Coros!"
    assert r.correct("") == ""
    assert r.correct("?! ...") == "?! ..."


def test_a_name_in_both_lists_counts_once_and_ties_go_to_the_roster():
    r = RosterCorrector(["Ebbo", "Arkon"], ["Ebbx", "Arkon"], common=frozenset())
    assert r.correct("ebb") == "Ebbo"                # 85.7 to both: the roster name
    assert r.correct("arkon, go") == "Arkon, go"     # still a roster name for the address


def test_numbers_are_left_alone():
    r = RosterCorrector(["Belra"], ["ICC", "Naxx"], common=frozenset())
    assert r.correct("icc 25 tonight") == "ICC 25 tonight"   # 25 never joins a run
    assert r.correct("naxx 10") == "Naxx 10"
    kel = RosterCorrector([], ["Kel'Thuzad"], common=frozenset())
    assert kel.correct("kel 2 thuzad") == "kel 2 thuzad"   # kel2thuzad would be 94.7
    assert kel.correct("kel thuzad") == "Kel'Thuzad"


# -- possessives ------------------------------------------------------------------------------

def test_a_possessive_keeps_its_s():
    r = rc()
    assert r.correct("bellra's pet is out") == "Belra's pet is out"
    assert r.correct("kel thuzad's adds") == "Kel'Thuzad's adds"


def test_a_name_heard_as_a_possessive():
    """Parakeet heard "Coros, pull now." as "Caro's pull now." (measured): "caros" is the
    name, the stem "caro" is not, so the 's goes."""
    assert rc().correct("Caro's pull now.") == "Coros pull now."
    assert rc().correct("Coro's pool now.") == "Coros pool now."


def test_a_common_possessive_is_left_alone():
    assert rc().correct("bella's pet") == "bella's pet"     # its stem is common
    assert rc().correct("bellra's pet") == "Belra's pet"
    bellas = RosterCorrector(["Belra"], ["Bellas"], common=frozenset({"bella", "pet"}))
    assert bellas.correct("bella's pet") == "bella's pet"   # "bellas" would be Bellas at 100


def test_the_stem_wins_a_tie():
    r = RosterCorrector(["Belra"], ["Belras"], common=frozenset({"pet"}))
    assert r.correct("belra's pet") == "Belra's pet"        # Belra's or Belras, both at 100


# -- runs of neighbours -----------------------------------------------------------------------

def test_a_name_split_three_ways():
    assert rc().correct("kel thu zad is up") == "Kel'Thuzad is up"


def test_the_longer_run_wins_a_tie(tmp_path):
    """The built-in lexicon holds Lich and the Lich King: "lich" alone and "lich king" both
    score 100, and the run wins."""
    from leeroy_voice.lexicon import load_lexicon
    words = load_lexicon(tmp_path).words()
    assert {"Lich", "Lich King"} <= set(words)
    r = RosterCorrector(["Belra"], words, common=frozenset({"the", "king", "is", "up"}))
    assert r.correct("the lich king is up") == "the Lich King is up"
    assert r.correct("the lich is up") == "the Lich is up"


def test_the_closer_parse_wins():
    """"dunna" alone scores 100, "dunna a" joined 90.9: the a stays."""
    r = RosterCorrector(["Dunna"], [], common=frozenset({"a", "mage"}))
    assert r.correct("dunna a mage") == "Dunna a mage"


def test_runs_cross_spaces_and_hyphens_only():
    r = RosterCorrector(["Belra"], ["Kel'Thuzad", "Yogg-Saron", "Lich King"],
                        common=frozenset({"is", "up", "the", "king"}))
    assert r.correct("kel-thuzad is up") == "Kel'Thuzad is up"
    assert r.correct("yog saron is up") == "Yogg-Saron is up"
    assert r.correct("the lich king") == "the Lich King"
    assert r.correct("kel, thuzad") == "kel, thuzad"
    assert r.correct("kel. thuzad") == "kel. thuzad"


def test_a_run_needs_a_word_that_is_not_common():
    r = RosterCorrector(["Belra"], ["Lich King"],
                        common=frozenset({"bel", "ra", "lich", "king", "is", "up", "the"}))
    assert r.correct("bel ra is up") == "bel ra is up"
    assert r.correct("the lich king") == "the lich king"
    assert r.correct("belra is up") == "Belra is up"


def test_the_measured_hearings():
    """What Parakeet made of Kokoro's lines (the Task 9 probe), with the service's common
    words: the ones the rules can put right."""
    pytest.importorskip("misaki")
    r = RosterCorrector(["Belra", "Coros", "Dunna"], ["Kel'Thuzad", "ICC"],
                        common=common_words())
    assert r.correct("Belro, you're on ads tonight.") == "Belra, you're on adds tonight."
    assert r.correct("Belry, you're on ads tonight.") == "Belra, you're on adds tonight."
    assert r.correct("Koros Poll Now") == "Coros Poll Now"
    assert (r.correct("Kelth UZ is up, dunna, you're on ads.")
            == "Kel'Thuzad is up, Dunna, you're on adds.")


# -- the word before a name (review C1) -------------------------------------------------------

def test_a_run_never_swallows_the_word_before_a_name():
    """A run that starts at a common word wins only when it beats the best match one token on:
    "is kelthuzad" (90 joined) never takes the "is" from Kel'Thuzad (100 alone)."""
    assert rc().correct("you're on kelthuzad") == "you're on Kel'Thuzad"
    assert rc().correct("you're on kel thuzad") == "you're on Kel'Thuzad"
    w = RosterCorrector(["Belra"], ["Sindragosa", "Icecrown", "Marrowgar", "Kel'Thuzad"],
                        common=frozenset({"not", "no", "on", "is", "up", "go", "to", "now",
                                          "stack", "marrow"}))
    assert w.correct("is Kel'Thuzad up?") == "is Kel'Thuzad up?"
    assert w.correct("is kelthuzad up") == "is Kel'Thuzad up"
    assert w.correct("not sindragosa") == "not Sindragosa"
    assert w.correct("no not kelthuzad") == "no not Kel'Thuzad"
    assert w.correct("go to icecrown now") == "go to Icecrown now"
    assert w.correct("stack on marrowgar") == "stack on Marrowgar"
    assert w.correct("marrow gar is up") == "Marrowgar is up"   # a common word still joins when it helps


def test_a_run_that_only_ties_keeps_its_word():
    """A run must score higher than the best match one token on: with both The Lich King and
    the Lich King listed, "the lich king" (100) only ties "lich king" (100), so "the" stays."""
    r = RosterCorrector(["Belra"], ["Lich King", "The Lich King"],
                        common=frozenset({"the", "is", "up"}))
    assert r.correct("the lich king is up") == "the Lich King is up"


def test_any_word_before_a_name_is_kept_or_corrected_alone():
    """Not only a common word: a garbled one is kept ("zugkelthuzad" scores 85.7, Kel'Thuzad
    alone 100), and one that is a near name itself is corrected alone ("dun" is Dunna at 75
    with both TN, though "dunkelthuzad" is 85.7)."""
    r = RosterCorrector(["Dunna"], ["Kel'Thuzad"], common=frozenset({"is", "up"}))
    assert r.correct("is zug kelthuzad up") == "is zug Kel'Thuzad up"
    assert r.correct("dun kelthuzad is up") == "Dunna Kel'Thuzad is up"
    assert r.correct("kel thuzad is up") == "Kel'Thuzad is up"        # a split name still joins


SHORT = ("a i is on to at so no oh ok hi my me we us it if or an as by do go he in of up not for "
         "and the hey yes but").split()


def test_no_word_is_lost_before_a_name(tmp_path):
    """The sweep: each short word before each invented member's name and each word of the
    built-in lexicon, in "stack <word> <name> now", is kept, and the name is spelled right."""
    from leeroy_voice.lexicon import load_lexicon
    cast = ["Arkon", "Belra", "Coros", "Dunna", "Ebbo"]
    words = load_lexicon(tmp_path).words()
    r = RosterCorrector(cast, words, common=frozenset(SHORT) | {"stack", "now"})
    lost = [(w, name) for name in cast + words for w in SHORT
            if r.correct(f"stack {w} {name.lower()} now") != f"stack {w} {name} now"]
    assert lost == []
    assert len(cast + words) * len(SHORT) > 1000


# -- the address ------------------------------------------------------------------------------

def test_the_address_needs_its_comma():
    r = rc()
    assert r.correct("Chorus you're on adds") == "Chorus you're on adds"
    assert r.correct("Chorus you're on adds, ok") == "Chorus you're on adds, ok"
    assert r.correct("Chorus , you're on adds") == "Coros , you're on adds"
    assert r.correct("Bella, you're on adds") == "Belra, you're on adds"   # common or not


def test_only_the_first_token_is_an_address():
    assert rc().correct("go now, chorus, go") == "go now, chorus, go"
    assert rc().correct("chorus, go now, chorus, go") == "Coros, go now, chorus, go"


def test_the_address_is_matched_against_roster_names_alone():
    r = RosterCorrector(["Belra"], ["Thorim"], common=frozenset({"thorium", "pull", "now"}))
    assert r.correct("Thorium, pull now") == "Thorium, pull now"   # 92.3 to Thorim, a WoW word
    assert r.correct("Thorim, pull now") == "Thorim, pull now"
    assert r.correct("Belro, pull now") == "Belra, pull now"


def test_an_address_that_matches_no_name_falls_through():
    assert rc().correct("Icc, tonight") == "ICC, tonight"


def test_an_interjection_is_never_an_address():
    r = RosterCorrector(["Belra", "Coros", "Dunna"], ["Kel'Thuzad", "ICC"],
                        common=COMMON | {"dunno", "maybe", "tomorrow"})
    assert r.correct("Dunno, maybe tomorrow.") == "Dunno, maybe tomorrow."   # 80 to Dunna today
    assert r.correct("Chorus, you're on adds") == "Coros, you're on adds"    # still an address
    assert r.correct("Bella, you're on adds") == "Belra, you're on adds"
    assert {"well", "so", "okay", "dunno", "dear", "melee", "adds", "pull", "stop"} <= NOT_ADDRESS
    assert not {"chorus", "bella"} & NOT_ADDRESS


def test_the_stoplist():
    """Closed classes only (review I1): Task 15's voice commands are on it, no member is, and
    each word is as `common` holds it."""
    assert {"pull", "go", "hold", "stop", "follow", "stay", "attack"} <= NOT_ADDRESS
    assert {"hey", "guys", "tank", "heals", "babe", "dear", "melee", "dunno", "ma'am"} <= NOT_ADDRESS
    assert not {"arkon", "belra", "coros", "dunna", "ebbo"} & NOT_ADDRESS
    assert all(w == w.casefold() and " " not in w and "\u2019" not in w for w in NOT_ADDRESS)
    assert len(NOT_ADDRESS) > 200


@pytest.mark.parametrize("word", ["dear", "melee", "dunno", "well", "pull", "stop"])
def test_a_listed_word_never_takes_a_name_near_it(word):
    """Each word against a member one step from it, made up here ("Dearxq" is 80 from "dear"):
    today it would be the address; listed, it stays, and the member is still addressed."""
    twin = word.capitalize() + "xq"
    r = RosterCorrector(["Belra", twin], [], common=frozenset({word, "pull", "now"}))
    assert r.correct(f"{word.capitalize()}, pull now") == f"{word.capitalize()}, pull now"
    assert r.correct(f"{twin.lower()}, pull now") == f"{twin}, pull now"


def test_a_listed_word_is_never_a_name_anywhere():
    """A listed word left the address and is then ordinary: rc()'s COMMON lacks "dunno", and it
    still never becomes Dunna, first or mid-line."""
    r = rc()
    assert r.correct("Dunno, maybe tomorrow.") == "Dunno, maybe tomorrow."
    assert r.correct("i dunno") == "i dunno"
    assert r.correct("duna") == "Dunna"                            # not listed: corrected


def test_a_listed_possessive_is_never_a_name():
    """A listed word's possessive is as ordinary as the word: "pally" is listed, though not in
    this corrector's common words. The member one letter from it is made up here ("pally" is
    90.9 from Pallyx)."""
    r = RosterCorrector(["Belra", "Pallyx"], [], common=frozenset({"the", "bubble"}))
    assert r.correct("the pally's bubble") == "the pally's bubble"
    assert r.correct("the pallyx's bubble") == "the Pallyx's bubble"


def test_a_tie_between_two_names_is_no_address():
    for roster in (["Arkon", "Coros"], ["Coros", "Arkon"]):          # "carkos": 72.7 to both
        r = RosterCorrector(roster, [], common=frozenset())
        assert r.correct("Carkos, pull now") == "Carkos, pull now"
        assert r.correct("Coros, pull now") == "Coros, pull now"


def test_a_tie_between_two_names_is_no_match():
    """Mid-line too: equal evidence picks no member. The second member, one letter from Belra,
    is made up here."""
    twin = "Belrx"
    for roster in (["Belra", twin], [twin, "Belra"]):
        r = RosterCorrector(roster, ["Kel'Thuzad"], common=frozenset({"is", "here"}))
        assert r.correct("is belr here") == "is belr here"             # 88.9 to both: neither
        assert r.correct("is belra here") == "is Belra here"
        assert r.correct("is belrx here") == "is Belrx here"


# -- sound-alikes -----------------------------------------------------------------------------

def test_soundalikes():
    """Parakeet wrote "ads" for every "adds" of the loopback (12 renders of 12): in a raid it
    is the adds, common word or not, in the token's own case."""
    assert SOUNDALIKES == {"ads": "adds"}
    r = rc()
    assert r.correct("you're on ads") == "you're on adds"
    assert r.correct("Ads left side") == "Adds left side"
    assert r.correct("ADS") == "ADDS"
    common = RosterCorrector(["Dunna"], [], common=frozenset({"ads", "on"}))
    assert common.correct("on ads") == "on adds"
    assert common.correct("dunna ads") == "Dunna adds"         # never part of a run


# -- the service's common words ---------------------------------------------------------------

def test_common_words_come_from_misakis_us_dictionary():
    pytest.importorskip("misaki")
    words = common_words()
    assert isinstance(words, frozenset) and len(words) > 100_000
    assert {"chorus", "you're", "ads", "adds", "bella", "the", "pull"} <= words
    assert not {"belra", "coros", "dunna", "kelthuzad", "kel'thuzad"} & words
    assert all(w == w.casefold() and "\u2019" not in w for w in words)


def test_common_words_without_misaki(monkeypatch):
    if importlib.util.find_spec("misaki") is not None:
        assert common_words()                          # with it, the dictionary
    monkeypatch.setitem(sys.modules, "misaki", None)  # an import of it now fails
    assert common_words() == frozenset()


# -- the address, within his room (review-task9 rule (c)) -----------------------------------------

def test_the_address_is_matched_only_against_the_room():
    """He can address only someone who hears him. Elsewhere the rules for any word still
    apply."""
    r = rc()
    assert r.correct("Chorus, you're on adds", present={"Coros"}) == "Coros, you're on adds"
    assert r.correct("Chorus, you're on adds", present={"Belra"}) == "Chorus, you're on adds"
    assert r.correct("Chorus, you're on adds", present=()) == "Chorus, you're on adds"
    assert r.correct("Chorus, you're on adds") == "Coros, you're on adds"  # no room given


def test_a_tie_is_broken_by_the_room():
    r = RosterCorrector(["Tarra", "Tarla"], [], common=frozenset({"tara"}))
    assert r.correct("Tara, go") == "Tara, go"                       # a tie: no match
    assert r.correct("Tara, go", present={"Tarla"}) == "Tarla, go"   # one of them is here


def test_new_names_make_a_new_corrector():
    """The roster changes on the event loop while a press is corrected on the recognizer's
    thread: with_names leaves the old one as it was, and shares `common`."""
    r = rc()
    s = r.with_names(["Ebbo"])
    assert s.correct("ebo there") == "Ebbo there" and s.correct("bellra") == "bellra"
    assert s.correct("kelthuzad") == "Kel'Thuzad"                    # the lexicon's words stay
    assert r.correct("bellra") == "Belra" and r.correct("ebo there") == "ebo there"
    assert s._common is r._common
