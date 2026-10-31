"""His game questions (fix wave part E): the detector, the raider shorthand written out for the
search, the names a question holds, and who in the guild knows a topic. The lines are the
rehearsal's, the controller's live probe's (fixwave-E-probe.md) and the review's, with invented
names where a line named someone."""
import pytest

from conftest import make_persona
from leeroy_mind.library.chunks import KINDS
from leeroy_mind.talk.questions import ALIASES, DETECTOR_WORDS, expand, fit_tier, game_question

# His game questions, the rehearsal's first: what each is about and so where the library looks.
GAME_QUESTIONS = [
    ("anyone know where the argent tournament dailies start? never did them", "quest"),
    ("where do i turn in this quest?", "quest"),
    ("anyone done |cffffff00|Hquest:13664:80|h[The Black Knight's Fall]|h|r yet?", "quest"),
    ("what drops from marrowgar", "loot"),
    ("anyone know what sindragosa drops on heroic?", "loot"),
    ("hey belra, quick one: is ulduar 10 still worth running for my mage or should i just farm "
     "emblems?", "loot"),
    ("what does |cffa335ee|Hitem:49623:0:0:0:0:0:0:0:80|h[Shadowmourne]|h|r do?", "item"),
    ("any tips for putricide?", "strategy"),
    ("how do we beat the lich king on heroic?", "strategy"),
    ("whats the strat for the gunship in icc?", "strategy"),
    ("how should i spec my mage for icc?", "class"),
    ("which glyphs do you guys use on a frost dk?", "class"),
    ("who was arthas before he took frostmourne?", "lore"),
    ("what's the story with the wrathgate?", "lore"),
    ("where is the wrathgate?", "zone"),
    ("how do i get to sholazar basin from dalaran?", "zone"),
    ("how do i get the glory of the ulduar raider achievement?", "achievement"),
    ("where can i learn tailoring up here?", "profession"),
    ("how do i get to the frozen halls?", "zone"),
    ("should i go fire or arcane for icc?", "class"),
    ("where can i buy frostweave?", "item"),
    ("what's a good tank trinket?", "item"),
    # the re-review (fix round 2): "who drops / starts / gives / sells X" is the game
    ("who drops the reins of the blue drake?", "loot"),
    ("who drops deathbringer's will?", "loot"),
    ("who drops the shadowfrost shard?", "loot"),
    ("who has the best loot table in icc?", "loot"),
    ("who gives the argent tournament dailies?", "quest"),
    ("who starts the onyxia quest?", "quest"),
    ("who sells cold weather flying?", "trainer"),
    ("who sells frostweave bags?", "item"),
    ("where do i buy heirlooms?", "item"),
    # names typed another way, and shorthand the detector did not know
    ("any tips for lanathel?", "strategy"),
    ("what does lanathel drop?", "loot"),
    ("who was malganis?", "lore"),
    ("who were the titans?", "lore"),
    ("how do we kill the valkyr twins?", "strategy"),
    ("how do i get to moaki harbor?", "zone"),
    ("where is quel danas?", "zone"),
    ("whats the trick to the jormungars?", "strategy"),
    ("any tips for vdw?", "strategy"),
    ("any tips for hol?", "strategy"),
    ("what does dbs drop?", "loot"),
    ("how much is cwf?", "trainer"),
    ("where is the mage trainer in orgrimmar?", "trainer"),
    ("where is the argent crusade quartermaster?", "reputation"),
]

# The controller's live probe (26 questions on the real library): each is a game question.
PROBE = [
    ("anyone know where the argent tournament dailies start? never did them", "quest"),
    ("what drops from marrowgar?", "loot"),
    ("whats the trick to saurfang, we keep losing it to the beasts", "strategy"),
    ("is it worth doing the ulduar meta for the drake or nah", "achievement"),
    ("where is the entrance to icc?", "zone"),
    ("should i go fire or arcane for icc?", "class"),
    ("how do i get to the frozen halls?", "zone"),
    ("anyone know what drops the deathbringer's will trinket?", "loot"),
    ("who was arthas before he became the lich king?", "lore"),
    ("any tips for putricide?", "strategy"),
    ("anyone know where the icc entrance is?", "zone"),
    ("where is the ruby sanctum?", "zone"),
    ("how many emblems do i need for t10 gloves?", "loot"),
    ("where do i get the frost wyrm mount from?", "mount"),
    ("whats the best way to get exalted with the ebon blade?", "reputation"),
    ("how do i start the onyxia quest again?", "quest"),
    ("where do i turn in emblems of frost?", "loot"),
    ("what does sindragosa drop?", "loot"),
    ("any tips for blood queen?", "strategy"),
    ("how do you do the gunship fight?", "strategy"),
    ("what's the deal with festergut, how does the gas work?", "strategy"),
    ("what's a good gem for fire mage?", "item"),
    ("how do i get to wintergrasp?", "zone"),
    ("who is tirion fordring?", "lore"),
    ("where can i learn cold weather flying?", "trainer"),
    ("what's the rep reward from the kalu'ak?", "reputation"),
]

# Lines that are not: chat, a goodbye, banter that names a class or the raid, people questions.
NOT_GAME_QUESTIONS = [
    "evening all", "night all, gotta sleep", "long day at work, finally home", "lol",
    "hey belra, heard you won the pie contest again ;) what does a mage have to do to get a "
    "slice after raid?",
    "where is everyone?", "need a tank for icc?", "anyone up for naxx tonight?", "who pulled?",
    "what time is raid?", "gz on the drop!", "how was your day?", "who is the raid leader tonight?",
    "is it just me or is the server lagging?", "",
    "my twins are sick, can anyone cover for me tonight?", "what do you play, mage or priest?",
    "where can i get a summon?", "any1 got a portal to dalaran?", "who's up for icc tonight?",
    "look at my new |cffa335ee|Hitem:49623:0:0:0:0:0:0:0:80|h[Shadowmourne]|h|r",
    # the review's (minor 2): a drop that is not loot, people in a place or on a boss, a mount
    # or a title that is not an achievement
    "did that drop?", "can someone drop group?", "should i drop this quest?",
    "where in dalaran are you?", "who's tanking sindragosa?", "lol who pulled marrowgar?",
    "is that a new mount?", "what's the meta tonight?", "anyone know the title of that song?",
    # the re-review (fix round 2): a standing, a mount, a trainer, a database, a plan, a story
    "i'd be honored, when do we start?", "honored to be here lol, who else is on?",
    "gotta rep my school, anyone else from ohio?", "anyone got a spare tabard?",
    "where did you get that charger for your phone?",
    "got my motorcycle license today, anyone else ride?", "got a new mount today, wanna see?",
    "should i back up the dbs before the update?", "anyone know if the dbs backed up ok?",
    "anyone want to do lk tonight?", "anyone up for ony later?", "how was the gunship?",
    "need a tank for icc hc?", "what do you want to achieve this year?",
    "my trainer at the gym is brutal, anyone else sore?",
    "where do i get a good pizza around here?", "where do i get my car fixed?",
    "this boss is pos, any tips?", "hol up, who's on tonight?", "is it 10m or 25m tonight?",
    "where can i learn guitar?", "who wants to run toc?", "who pulled marrowgar?",
]


@pytest.mark.parametrize("line,topic", GAME_QUESTIONS + PROBE)
def test_his_game_questions_are_found_with_what_they_are_about(line, topic):
    q = game_question(line)
    assert q is not None and q.topic == topic, (line, q)
    assert q.kinds and set(q.kinds) <= set(KINDS)
    assert "|H" not in q.query and "|c" not in q.query  # links come through as their names
    assert q.query.strip()


def test_a_question_searches_the_kinds_of_its_topic():
    assert game_question(GAME_QUESTIONS[0][0]).kinds[0] == "quest"
    assert game_question("what drops from marrowgar").kinds[0] == "loot"
    assert game_question("any tips for putricide?").kinds == ("strategy",)
    link = next(line for line, topic in GAME_QUESTIONS if topic == "item")
    assert "[Shadowmourne]" in game_question(link).query
    # the probe: class blurbs and boss tactics came back for a spec question about a raid
    assert game_question("should i go fire or arcane for icc?").kinds == ("class",)
    assert game_question("what's the rep reward from the kalu'ak?").kinds[0] == "loot"
    assert "item" in game_question("where can i learn cold weather flying?").kinds
    assert game_question("where do i get the frost wyrm mount from?").kinds[0] == "item"


@pytest.mark.parametrize("line", NOT_GAME_QUESTIONS)
def test_chat_banter_and_people_questions_are_not_game_questions(line):
    assert game_question(line) is None, line


# -- raider shorthand, written out for the search ----------------------------------------------

@pytest.mark.parametrize("line,written", [
    ("where is the entrance to icc?", "where is the entrance to Icecrown Citadel?"),
    ("how many emblems for t10 gloves?", "how many emblems for Tier 10 gloves?"),
    ("any tips for bq?", "any tips for Blood-Queen Lana'thel?"),
    ("any tips for blood queen?", "any tips for Blood-Queen Lana'thel?"),
    ("dbs on hc 25m tonight?", "Deathbringer Saurfang on heroic 25-player tonight?"),
    ("best way to exalted with the ebon blade?",
     "best way to exalted with the Knights of the Ebon Blade?"),
    ("what's the rep reward from the kalu'ak?", "what's the reputation reward from the kalu'ak?"),
])
def test_raider_shorthand_is_written_out(line, written):
    assert expand(line) == written


def test_a_name_already_written_out_is_not_expanded_again():
    assert expand("any tips for blood queen lana'thel?") == "any tips for blood queen lana'thel?"
    assert expand("rep with the knights of the ebon blade") == (
        "reputation with the knights of the ebon blade")
    assert expand("piccolo and tick are words, icc is not") == (
        "piccolo and tick are words, Icecrown Citadel is not")
    assert all(k == k.casefold() and v for k, v in ALIASES.items())


def test_a_question_carries_the_written_out_query_and_its_names():
    q = game_question("anyone know where the icc entrance is?")
    assert q.query == "anyone know where the Icecrown Citadel entrance is?"
    assert "icecrown citadel" in q.names and "icecrown" not in q.names  # the longest name
    assert "tier 10" in game_question("how many emblems do i need for t10 gloves?").names
    assert "emblem of frost" in game_question("where do i turn in emblems of frost?").names
    assert game_question("should i go fire or arcane for icc?").names == ("icecrown citadel",)
    assert game_question("what's a good gem for fire mage?").names == ()  # a class is no name
    assert "kalu'ak" in game_question("what's the rep reward from the kalu'ak?").names
    assert "northrend" not in game_question("where can i learn cold weather flying in "
                                            "northrend?").names  # a continent is too broad
    assert game_question("who was arthas before he became the lich king?").names == (
        "arthas", "lich king")  # the longest name at each place, never "the lich"
    assert "heroic" not in game_question("how do we beat the lich king on heroic?").names


@pytest.mark.parametrize("line,name", [
    ("any tips for lanathel?", "lana'thel"), ("what does lanathel drop?", "lana'thel"),
    ("who was malganis?", "mal'ganis"), ("who was nerzhul?", "ner'zhul"),
    ("who is kaelthas?", "kael'thas"), ("where is lights hope chapel?", "light's hope chapel"),
    ("how do i do the hallows end dailies?", "hallow's end"), ("who were the titans?", "titan"),
    ("where do i buy heirlooms?", "heirloom"), ("what's marrowgar's bone storm do?", "marrowgar"),
    ("how do i get to moaki harbor?", "moa'ki harbor"), ("where is quel danas?", "quel'danas"),
])
def test_a_name_typed_another_way_is_read_as_the_list_spells_it(line, name):
    """The re-review (Important 1): a dropped apostrophe, a plural, a possessive; the gate then
    finds the name as the library writes it."""
    assert name in game_question(line).names, game_question(line)


def test_the_query_spells_his_names_as_the_library_does():
    """The probe after round 2: "what does lanathel drop?" never reached the boss's loot tables,
    since the search looked for "lanathel"; a plural or a possessive stays as he typed it."""
    assert game_question("what does lanathel drop?").query == "what does lana'thel drop?"
    assert game_question("where is lights hope chapel?").query == (
        "where is light's hope chapel?")
    assert game_question("who were the titans?").query == "who were the titans?"
    assert game_question("what's marrowgar's bone storm do?").query == (
        "what's marrowgar's bone storm do?")
    assert game_question("any tips for bq?").query == "any tips for Blood-Queen Lana'thel?"


def test_every_shorthand_is_a_word_the_detector_knows():
    """The re-review (minor 5): "vdw", "bpc", "ldw", "sindra" and "jormungars" were written out
    for the search but never made a line a game question."""
    assert [a for a in ALIASES if not any(r.fullmatch(a) for r in DETECTOR_WORDS)] == []
    for line in ("any tips for vdw?", "any tips for bpc?", "any tips for ldw?",
                 "any tips for sindra?", "any tips for the jormungars?"):
        assert game_question(line).topic == "strategy", line


@pytest.mark.parametrize("line,written", [
    ("hol up, where is the icc entrance?", "hol up, where is the Icecrown Citadel entrance?"),
    ("this boss is pos, any tips for icc?", "this boss is pos, any tips for Icecrown Citadel?"),
    ("brb 10m, what drops from marrowgar?", "brb 10m, what drops from marrowgar?"),
    ("should i back up the dbs before the update?", "should i back up the dbs before the update?"),
    ("gotta rep my school, anyone know where icc is?",
     "gotta rep my school, anyone know where Icecrown Citadel is?"),
    ("any tips for hol?", "any tips for Halls of Lightning?"),
    ("pos hc tips?", "Pit of Saron heroic tips?"),
    ("icc 10m or 25m?", "Icecrown Citadel 10-player or 25-player?"),
    ("tips for 25m icc?", "tips for 25-player Icecrown Citadel?"),
    ("afk for 10m, what drops from marrowgar?", "afk for 10m, what drops from marrowgar?"),
])
def test_shorthand_that_is_also_a_word_is_written_out_only_as_the_game(line, written):
    """The re-review (minor 5): "hol up" became "Halls of Lightning up", and its facts passed."""
    assert expand(line) == written


def test_slang_shorthand_never_names_the_line():
    assert game_question("hol up, where is the icc entrance?").names == ("icecrown citadel",)
    assert game_question("brb 10m, what drops from marrowgar?").query == (
        "brb 10m, what drops from marrowgar?")


# -- who knows ---------------------------------------------------------------------------------

def test_a_topic_is_answered_best_by_who_its_persona_says_knows_it():
    scholar = make_persona(101, "Arkon", officer_title="Guild Historian", thinks_as="social")
    lore = make_persona(102, "Belra", thinks_as="lore")
    numbers = make_persona(103, "Coros")  # the test cast thinks as a theorycrafter
    collector = make_persona(104, "Dunna", thinks_as="collector")
    helper = make_persona(105, "Ebbo", player_gender="female", thinks_as="social")
    assert [fit_tier("lore", p) for p in (scholar, lore, numbers)] == [0, 1, None]
    assert [fit_tier("strategy", p) for p in (scholar, numbers, lore)] == [0, 1, None]
    assert [fit_tier("loot", p) for p in (numbers, scholar, lore)] == [0, 1, None]
    assert [fit_tier("mount", p) for p in (collector, scholar, numbers)] == [0, 1, None]
    assert fit_tier("reputation", scholar) == 0 and fit_tier("trainer", numbers) == 0
    assert fit_tier("quest", helper) == 0 and fit_tier("quest", scholar) is None


@pytest.mark.parametrize("line, plan", [
    ("anyone wanna farm the drake tonight?", True), ("need a tank for icc hc?", True),
    ("anyone up for ony later?", True), ("who gets the even weeks for bank duty again?", False),
    ("how long has the potion feud been going?", False), ("what drops from marrowgar?", False)])
def test_is_plan_reads_an_invitation_or_a_call_for_people(line, plan):
    """Fix wave G3: his question about the guild's past is never a plan."""
    from leeroy_mind.talk.questions import is_plan
    assert is_plan(line) is plan
