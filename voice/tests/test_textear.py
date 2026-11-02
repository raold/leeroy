import pytest

from leeroy_voice.lexicon import Lexicon
from leeroy_voice.textear import (CHAT_SPEAK, LAUGHS, MAX_SOUNDS, MAX_SPOKEN_CHARS, SOUND_TAGS,
                                  EarText, ear, sentences, strip_markup)


def e(t):
    return ear(t, lexicon=Lexicon({}), engine="kokoro")


def test_chat_speak_expanded():
    assert e("brb, gz! ty np").text == "be right back, grats! thanks no problem"
    assert e("oom").text == "out of mana" and e("ofc idk tbh").text == "of course I don't know to be honest"


def test_the_plans_chat_speak():
    plan = {"brb": "be right back", "gz": "grats", "grats": "grats", "ty": "thanks",
            "thx": "thanks", "np": "no problem", "oom": "out of mana", "ofc": "of course",
            "idk": "I don't know", "tbh": "to be honest", "imo": "in my opinion",
            "omg": "oh my god", "gg": "good game", "pls": "please", "plz": "please",
            "rdy": "ready", "inc": "incoming", "afk": "AFK", "dps": "D P S", "hp": "H P",
            "aoe": "AOE", "u": "you", "ur": "your", "r": "are"}
    assert {k: CHAT_SPEAK[k] for k in plan} == plan
    assert e("BRB, Gz!").text == "be right back, grats!"
    assert e("brbx gzz u2").text == "brbx gzz u2"                  # whole words only


def test_initialisms_with_an_a_are_joined():   # misaki reads a lone "A" as "uh" when tagged DT
    assert e("im afk").text == "I'm AFK" and e("big aoe").text == "big AOE"
    spaced_a = [k for k, v in CHAT_SPEAK.items() if v.split()[0] == "A" and len(v.split()) > 1]
    assert spaced_a == []


def test_r_only_between_words():
    assert e("where r u").text == "where are you"
    assert e("press r.").text == "press r." and e("r").text == "r"
    assert e("r u up").text == "r you up"          # misaki reads a lone r as "are" anyway


def test_contractions_typed_without_the_apostrophe():
    assert e("im sure u dont, thats why").text == "I'm sure you don't, that's why"


def test_lowercase_i_is_the_pronoun():     # misaki stresses a lone lowercase i, not the pronoun I
    assert e("i think i can, i'm sure").text == "I think I can, i'm sure"


def test_words_misaki_misreads():                  # its dictionaries: ur "er", yea "yay", vs "vees"
    assert e("yea ur flasks vs his").text == "yeah your flasks versus his"


def test_laughs_counted_and_dropped_for_kokoro():
    r = e("lol ok :) <3")
    assert r.text == "ok" and r.laughs == 3
    assert {"lol", "lmao", ":)", "<3"} <= LAUGHS


def test_drawn_out_laughs():
    r = e("lmaooo hahaha xD LOL ok \U0001F602\U0001F602")
    assert r.text == "ok" and r.laughs == 6


def test_lull_and_loll_are_words_not_laughs():
    r = e("a lull in the fight, then loll around, lulls and lolls")
    assert r.text == "a lull in the fight, then loll around, lulls and lolls" and r.laughs == 0
    assert e("lulz lolll ok").laughs == 2


def test_empty_after_rewrite():                         # Review Focus 4
    assert e("lol").text == "" and e("<3 :)").text == "" and e("\U0001F602").text == ""
    assert e("lol!!").text == "" and e("").sentences == () and e("...").text == ""


def test_other_emoticons_dropped_uncounted():
    r = e("ok :( fine :/ D: see you o/")
    assert r.text == "ok fine see you" and r.laughs == 0
    r = e("thanks:) at 10:30")
    assert r.text == "thanks at 10:30" and r.laughs == 1


def test_actions_are_not_spoken():
    assert e("*waves* hi all").text == "hi all"
    r = e("*laughs* fine, **pull** now")
    assert r.text == "fine, pull now" and r.laughs == 1


def test_wow_link_read_as_item_name():                  # Review Focus 4
    assert e("grats on |cffa335ee|Hitem:49623:0:0:0|h[Shadowmourne]|h|r!").text == "grats on Shadowmourne!"


def test_dotted_abbreviations():
    assert e("bring flasks, e.g. frost wyrm etc.").text == "bring flasks, for example frost wyrm et cetera"
    assert e("us vs. them, i.e. war").text == "us versus them, that is war"


def test_dotted_initials_left_to_misaki():           # misaki spells "U.S." out; its u is not "you"
    assert e("the U.S. realm").text == "the U.S. realm"


def test_curly_quotes_and_dashes():
    assert e("I\u2019m in, don\u2019t pull").text == "I'm in, don't pull"
    assert e("he said \u201cpull\u201d \u2013 now, 10\u201320").text == (
        "he said pull, now, 10 to 20")


def test_counts_and_ranges():               # the second number larger: a count or a range
    assert e("3/12 hc, 2/3 bosses, 24/7 and 50/50").text == (
        "3 of 12 heroic, 2 of 3 bosses, 24 7 and 50 50")
    assert e("10-15 sec, 10 - 15 sec, a 3-2 score, pull in 3-2-1").text == (
        "10 to 15 sec, 10 to 15 sec, a 3-2 score, pull in 3-2-1")
    assert e("count 1-2-3 on 3/12/2026").text == "count 1-2-3 on 3 12 2026"   # no pair alone


def test_symbols_read_out():
    assert e("boss at 20% & adds +1 tank w/ flasks").text == ("boss at 20 percent and adds plus 1 "
                                                             "tank with flasks")


def test_caps_kept():
    assert e("I said NOT that one").text == "I said NOT that one"


def test_punctuation_tidied():
    assert e("lol, ok . fine !!").text == "ok. fine!"
    assert e("wait.... ok ' fine,").text == "wait... ok fine"


def test_what_a_removed_laugh_leaves_is_tidied():
    assert e("Pull now. lol.").text == "Pull now."
    assert e("ok, lol, fine").text == "ok, fine" and e("ok, lol.").text == "ok."


def test_sentences():
    assert e("Pull now. Healers ready? Go!").sentences == ("Pull now.", "Healers ready?", "Go!")


def test_sentences_split_at_ends_only():
    assert sentences("So... I saved you a spot. Go!") == ["So... I saved you a spot.", "Go!"]
    assert sentences("the U.S. realm is up. Go!") == ["the U.S. realm is up.", "Go!"]
    assert sentences("Mr. T pulls. Dr. Who? mr. bigglesworth ran.") == [
        "Mr. T pulls.", "Dr. Who?", "mr. bigglesworth ran."]
    assert sentences("") == [] and sentences("no end") == ["no end"]


def test_acronyms_left_in_capitals():         # misaki spells an unknown word in capitals
    assert e("lfg icc, need dps wtf").text == "LFG ICC, need D P S WTF"


def test_long_text_cut_at_a_sentence_end():
    r = e("Short one. " * 60)
    assert len(r.text) <= 400 and r.text.endswith(".")
    assert MAX_SPOKEN_CHARS == 400 and r == EarText(r.text, 0, tuple(sentences(r.text)))


def test_long_line_without_a_sentence_end_cut_at_a_word():
    for words in ("lots of words " * 40, "words " * 80):   # the limit on a space, inside a word
        r = e(words)
        assert len(r.text) <= MAX_SPOKEN_CHARS and r.text.endswith(".")
        assert words.startswith(r.text[:-1] + " ")


def test_lexicon_applied():
    r = ear("on Kel'Thuzad tonight", lexicon=Lexicon({"Kel'Thuzad": "kˌɛlθˈuzɑd"}), engine="kokoro")
    assert r.text == "on [Kel'Thuzad](/kˌɛlθˈuzɑd/) tonight"


def test_lexicon_markup_only_for_kokoro():          # no other engine reads misaki's markup
    lx = Lexicon({"Kel'Thuzad": "kˌɛlθˈuzɑd"})
    assert ear("on Kel'Thuzad", lexicon=lx, engine="chatterbox").text == "on Kel'Thuzad"


def turbo(t, lexicon=None):
    return ear(t, lexicon=lexicon or Lexicon({}), engine="chatterbox-turbo")


def test_laugh_cue_for_an_engine_that_has_one():
    assert set(SOUND_TAGS) == {"chatterbox-turbo"}
    assert turbo("lol ok").text == "[chuckle] ok"
    r = turbo("ok lol :) see you")
    assert r.text == "ok [chuckle] see you" and r.laughs == 2
    assert turbo("lol <3").text == ""                       # a laugh alone is not a line


# -- sounds: round 3's mapper (the owner's ruling), for an engine that reads the tags ------------

@pytest.mark.parametrize("typed, spoken", [
    ("lol ok", "[chuckle] ok"),
    ("lmao no way", "[laugh] no way"),
    ("LMAO what", "[laugh] what"),
    ("lmfao what", "[laugh] what"),
    ("rofl what", "[laugh] what"),
    ("hahaha ok", "[laugh] ok"),
    ("Ha ha, did you see that", "[laugh] did you see that"),
    ("hehe ok", "[chuckle] ok"),
    ("ugh. again", "[sigh] again"),
    ("Ugh, again", "[sigh] again"),
    ("ughhh again", "[sigh] again"),
    ("omg wow", "[gasp] wow"),
    ("omgggg what", "[gasp] what"),
    ("wtf is that", "[gasp] is that"),
    ("WTF is that", "[gasp] is that"),
    ("see you there haha ok", "see you there [laugh] ok"),
    ("*laughs* fine", "[chuckle] fine"),
    (":) fine", "[chuckle] fine"),
])
def test_cues_become_sounds(typed, spoken):
    assert turbo(typed).text == spoken


@pytest.mark.parametrize("typed, spoken", [
    ("that was dumb lol", "that was dumb. [chuckle]"),
    ("great lol!", "great! [chuckle]"),
    ("wait what lol?", "wait what? [chuckle]"),
    ("Pull now. lol.", "Pull now. [chuckle]"),
    ("ok, lol.", "ok. [chuckle]"),
    ("ok, lol, fine", "ok, [chuckle] fine"),
])
def test_a_sound_after_the_words_closes_their_sentence(typed, spoken):
    assert turbo(typed).text == spoken


@pytest.mark.parametrize("typed, spoken", [
    ("ugh... fine", "[sigh]... fine"),
    ("so... I was thinking...", "so... I was thinking..."),
    ("well\u2026 ok", "well... ok"),
])
def test_an_ellipsis_stays_a_pause(typed, spoken):
    assert turbo(typed).text == spoken


@pytest.mark.parametrize("typed, spoken", [
    ("mm. keep talking", "Mm... keep talking"),
    ("mmm ok", "Mm... ok"),
    ("Hmm, ok", "Hmm... ok"),
    ("hmmm", "Hmm..."),
])
def test_a_leading_hum_stays_a_sound(typed, spoken):
    assert turbo(typed).text == spoken


def test_a_hum_is_only_a_hum_at_the_start():
    assert turbo("ok mm fine").text == "ok mm fine"
    assert e("mm. keep talking").text == "mm. keep talking"           # Kokoro: as it was


def test_at_most_two_sounds_a_line():
    assert MAX_SOUNDS == 2
    assert turbo("lol omg ugh").text == "[chuckle] [gasp] ugh"        # past the limit: its word
    assert turbo("ugh omg wtf ok").text == "[sigh] [gasp] WTF ok"
    r = turbo("lol ok haha fine lmao done")                           # a laugh has no word
    assert r.text == "[chuckle] ok [laugh] fine done" and r.laughs == 3


def test_a_run_of_one_sound_is_one_sound():
    assert turbo("ok lol lol lmao :) done").text == "ok [chuckle] [laugh] done"


def test_only_tags_the_tokenizer_lists():
    tags = SOUND_TAGS["chatterbox-turbo"]
    assert {"chuckle", "laugh", "sigh", "gasp"} <= tags and len(tags) == 19


def test_a_line_of_cues_alone_keeps_its_words():      # nobody loses a line to a lone sound
    assert turbo("ugh").text == "ugh" and turbo("omg").text == "oh my god"
    assert turbo("wtf!").text == "WTF!" and turbo("lol ugh").text == "[chuckle] ugh"


def test_a_laugh_alone_stays_nothing():               # Review Focus 4, as for Kokoro
    assert turbo("lol").text == "" and turbo("haha!").text == "" and turbo("*laughs*").text == ""


@pytest.mark.parametrize("engine", ["kokoro", "chatterbox", "indextts2", "fake"])
def test_no_other_engine_gets_a_tag(engine):
    r = ear("lol omg ugh hmm haha what", lexicon=Lexicon({}), engine=engine)
    assert "[" not in r.text and "chuckle" not in r.text


def test_kokoro_reads_cues_as_before():
    assert e("omg wow").text == "oh my god wow" and e("ugh. again").text == "ugh. again"
    assert e("wtf is that").text == "WTF is that" and e("Ha ha ok").text == "Ha ha ok"


def test_a_sound_stays_with_its_sentence():
    assert turbo("That was dumb. lol").sentences == ("That was dumb. [chuckle]",)
    r = turbo("lmao did you see it. haha we're all dead")
    assert r.sentences == ("[laugh] did you see it.", "[laugh] we're all dead")
    assert turbo("no way lol. fine").sentences == ("no way.", "[chuckle] fine")


def test_plain_is_each_sentence_without_its_sounds():
    r = turbo("lmao did you see it. we're all dead. ugh... fine")
    assert r.plain == ("did you see it.", "we're all dead.", "fine")
    assert len(r.plain) == len(r.sentences)
    assert turbo("no way lol").plain == ("no way.",)
    assert e("no way lol").plain == ()                                 # Kokoro needs none


@pytest.mark.parametrize("line", [
    "did you see that?", "no way!", "you got the storm capper?! no way",
    "are you coming tonight, or not?", "wait, really?", "pull now!",
    "so... i was thinking... maybe we could run something after raid? just us?",
    "you sounded tired on vent tonight, dont stay up too late on my account ok?",
])
@pytest.mark.parametrize("engine", ["kokoro", "chatterbox-turbo"])
def test_questions_and_exclamations_reach_the_engine(line, engine):
    """Round 1: he marked exactly the lines where these read flat."""
    said = ear(line, lexicon=Lexicon({}), engine=engine).text
    assert said.count("?") == line.count("?") and said.count("!") == line.count("!")


def test_a_question_keeps_its_mark_beside_a_sound():
    assert turbo("omg you got it?! no way").sentences == ("[gasp] you got it?!", "no way")
    assert turbo("wanna come lol?").text == "wanna come? [chuckle]"
    assert turbo("really? lol").text == "really? [chuckle]"


def test_a_run_of_marks_is_one():
    assert turbo("what??").text == "what?" and turbo("no way!!!").text == "no way!"


def test_the_marks_never_reach_the_engine():
    for line in ("ugh " * 200, "lol " * 200 + "ok", "omg " * 150, "ok " * 200 + "omg wtf ugh"):
        r = turbo(line)
        assert not any(0xE000 <= ord(c) <= 0xF8FF for c in r.text + "".join(r.plain))


def test_ipa_is_for_kokoro_alone():
    lexicon = Lexicon({"Kel'Thuzad": "k\u02cc\u025bl\u03b8\u02c8uz\u0251d"})
    assert turbo("on Kel'Thuzad tonight", lexicon).text == "on Kel'Thuzad tonight"


def test_a_respelling_reaches_the_engine_and_only_it():
    lexicon = Lexicon({}, respellings={"chatterbox-turbo": {"Testy": "Testai"}})
    r = turbo("hey testy, is that Testy's? lol", lexicon)
    assert r.text == "hey Testai, is that Testai's? [chuckle]"
    assert r.plain == ("hey testy, is that Testy's?",)                 # Kokoro has its own reading
    assert ear("hey Testy", lexicon=lexicon, engine="kokoro").text == "hey Testy"
    assert ear("hey Testy", lexicon=lexicon, engine="chatterbox").text == "hey Testy"


def test_strip_markup():
    assert strip_markup("|cffa335ee|Hitem:49623:0:0:0|h[Shadowmourne]|h|r "
                        "|TInterface\\Icons\\INV_Misc_Coin_01:16|t ok") == "Shadowmourne ok"
    assert strip_markup("tank/heals (brb) ~yay~ \U0001F602 #1") == "tank heals brb yay 1"
    assert strip_markup("ok :) <3 I\u2019m in") == "ok I'm in"
    kept = "well, ok... fine: go; now - Yogg-Saron's!"
    assert strip_markup(kept) == kept
