import pytest
import random
from collections import Counter

from conftest import make_persona
from leeroy_mind.storage.rows import PersonaStateRow, Relationship
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.select import (NON_CORE_FACTOR, SpeakerContext, classify_human_line,
                                     has_reason, mentioned, persona_keywords, pick_speakers, plan_replies,
                                     plan_reply_turns, speaker_weight, topic_words,
                                     typing_delay, is_quest_talk, whisper_reply_p)
from leeroy_mind.talk.turns import TurnKind

NAMES = {101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna", 105: "Ebbo"}
W = {g: 1.0 for g in NAMES}


def ctx(**kw):
    base = dict(now=10_000.0, topic=set(), opener=False,
                phase=DayPhase.EVENING)
    base.update(kw)
    return SpeakerContext(**base)


def weight(p=None, *, state=None, rel=None, last=None, mult=1.0, **kw):
    p = p or make_persona(101, "Arkon")
    return speaker_weight(p, state or PersonaStateRow(guid=p.guid), rel, ctx(**kw), last, mult)


def test_weight_zero_within_60s_and_damped_within_5min():
    base = weight()
    assert weight(last=10_000.0 - 30) == 0
    assert abs(weight(last=10_000.0 - 120) - 0.3 * base) < 1e-9
    assert weight(last=10_000.0 - 400) == base


def test_introvert_opener_penalty_and_non_core_share():
    intro = make_persona(101, "Arkon", extraversion={"score": 3, "behaviour": "quiet"})
    assert abs(weight(intro, opener=True) / weight(intro, opener=False) - 0.25) < 1e-9
    loud = make_persona(102, "Belra")
    assert weight(loud, opener=True) == weight(loud, opener=False)
    core = make_persona(1, "Core")
    non = make_persona(2, "Non", core=False)
    assert abs(weight(non) / weight(core) - NON_CORE_FACTOR) < 1e-9
    w = {g: weight(make_persona(g, f"P{g}", core=g <= 25)) for g in range(1, 107)}
    picks = Counter()
    rng = random.Random(4)
    for _ in range(10_000):
        picks.update(pick_speakers(w, 1, rng))
    share = sum(c for g, c in picks.items() if g <= 25) / 10_000
    assert 0.75 <= share <= 0.85


def test_night_owls_favoured_late():
    owl = make_persona(101, "Arkon", night_owl=True)
    lark = make_persona(102, "Belra", night_owl=False)
    ev = weight(owl, phase=DayPhase.EVENING)
    assert abs(weight(owl, phase=DayPhase.LATE) / ev - 1.5) < 1e-9
    assert abs(weight(lark, phase=DayPhase.LATE) / weight(lark, phase=DayPhase.EVENING)
               - 0.5) < 1e-9


def test_topic_and_closeness_and_mood_raise_weight():
    p = make_persona(101, "Arkon")
    kw = persona_keywords(p)
    assert "harbour" in kw and "quiet" in kw
    base = weight(p)
    assert weight(p, topic={"harbour"}) > base
    assert weight(p, topic={"harbour", "quiet", "tea"}) > weight(p, topic={"harbour"})
    rel = Relationship(a=101, b=102, affinity=50.0, familiarity=100.0)
    assert abs(weight(p, rel=rel) / base - 2.0) < 1e-9
    assert weight(p, state=PersonaStateRow(guid=101, mood=1.0)) > base
    assert weight(p, mult=0) == 0


def test_topic_words_drops_filler():
    t = topic_words(["the harbour was Quiet tonight", "we had a go at it"])
    assert "harbour" in t and "quiet" in t and "the" not in t and "at" not in t


def test_pick_speakers_unique_and_bounded():
    rng = random.Random(1)
    got = pick_speakers({1: 1.0, 2: 0.0, 3: 2.0, 4: 1.0}, 3, rng)
    assert len(got) == 3 and len(set(got)) == 3 and 2 not in got
    assert len(pick_speakers({1: 1.0, 2: 0.0}, 5, rng)) == 1
    assert pick_speakers({}, 2, rng) == []


def test_classify():
    c = classify_human_line
    assert c("evening all") == "greeting" and c("hey") == "greeting"
    assert c("gn guys") == "goodbye" and c("ok logging off") == "goodbye"
    assert c("anyone seen the key?") == "question_all" and c("who has flasks") == "question_all"
    assert c("that boss was rough") == "remark"


def test_classify_goodbye_and_greeting_edges():
    c = classify_human_line
    for t in ("gn all", "night guys", "logging off", "gotta go", "gnight", "nite all",
              "ok logging out now", "have a good night", "cya tomorrow", "see you, night"):
        assert c(t) == "goodbye", t
    for t in ("raid night!", "is it raid night?", "what a night", "last night was wild",
              "night elves are lame", "anyone know a good night elf rogue", "blogging lol",
              "back from logging in", "fun raid tonight"):
        assert c(t) != "goodbye", t
    assert c("this evening was rough") == "remark"
    assert c("good evening all") == "greeting" and c("hey all") == "greeting"
    assert c("hey, anyone up?") == "question_all"


def test_ordinary_words_that_open_a_goodbye_or_a_hello():
    """Task 13 carry: "later", "night" and "gotta go" opened goodbyes in ordinary lines, and
    "evening" opened a greeting in "evening was fun"."""
    c = classify_human_line
    for t in ("later today we should raid", "night shift sucks", "gotta go fast",
              "night shift again tomorrow, ugh"):
        assert c(t) != "goodbye", t
    for t in ("later all", "laters", "later guys", "night night", "night all, see you tomorrow",
              "night zan", "gotta go guys", "ok gotta go, work in the morning"):
        assert c(t) == "goodbye", t
    for t in ("evening was fun", "morning raids are the worst"):
        assert c(t) == "remark", t
    for t in ("evening", "evening all", "morning!", "evening zan", "good morning everyone"):
        assert c(t) == "greeting", t


def test_reply_count_distribution():
    rng = random.Random(11)
    n = Counter(len(plan_replies("that boss was rough", channel="guild", to_guid=0, weights=W,
                                 names=NAMES, afk=set(), rng=rng)) for _ in range(10_000))
    for k, p in ((0, 0.18), (1, 0.52), (2, 0.30)):
        assert abs(n[k] / 10_000 - p) <= 0.02, n
    assert set(n) <= {0, 1, 2}


def test_greeting_question_and_goodbye_get_2_to_3():
    rng = random.Random(2)
    for text in ("evening all", "anyone up for the vault?", "gn everyone"):
        sizes = {len(plan_replies(text, channel="guild", to_guid=0, weights=W, names=NAMES,
                                  afk=set(), rng=rng)) for _ in range(300)}
        assert sizes == {2, 3}, (text, sizes)


def test_replies_skip_zero_weight_and_afk():
    rng = random.Random(3)
    w = {**W, 101: 0.0}
    for _ in range(200):
        got = plan_replies("evening all", channel="guild", to_guid=0, weights=w, names=NAMES,
                           afk={102}, rng=rng)
        assert 101 not in got and 102 not in got and len(got) == len(set(got))


def test_mention_routes_to_person_unless_afk():
    rng = random.Random(5)
    for _ in range(100):
        got = plan_replies("arkon you up?", channel="guild", to_guid=0, weights=W, names=NAMES,
                           afk=set(), rng=rng)
        assert got == [101]  # fix wave G5: the one he named, and no one else
        got = plan_replies("arkon you up?", channel="guild", to_guid=0, weights=W, names=NAMES,
                           afk={101}, rng=rng)
        assert 101 not in got


def test_mention_prefix_needs_4_unique_letters():
    assert mentioned("ark you up", NAMES) == []
    assert mentioned("arko you up", NAMES) == [101]
    assert mentioned("hi Arkon and belra", NAMES) == [101, 102]
    assert mentioned("dun", NAMES) == []
    assert mentioned("mar", {1: "Marvik", 2: "Marlune"}) == []
    assert mentioned("marv", {1: "Marvik", 2: "Marlune"}) == [1]
    assert mentioned("marl marv", {1: "Marvik", 2: "Marlune"}) == [2, 1]
    assert mentioned("mark", {1: "Marvik", 2: "Markel"}) == [2]
    assert mentioned("ebbo", {1: "Ebbo", 2: "Ebbon"}) == [1]


def test_mention_ignores_common_words_markup_and_handles_possessives():
    pool = {1: "Haverm", 2: "Tanklezz", 3: "Raidan", 4: "Bossus", 5: "Zelna"}
    assert mentioned("have you got the tank for the raid boss", pool) == []
    assert mentioned("loot |cff0070dd|Hitem:1234|h[Zelnar Blade]|h|r dropped", pool) == []
    assert mentioned("that was [Zelnar] again", pool) == []
    assert mentioned("zelna's idea was good", pool) == [5]
    assert mentioned("hey Tanklezz", pool) == [2]


def test_stemmed_topic_matching():
    assert topic_words(["raiding tonight", "the raids"]) >= {"raid"}
    p = make_persona(101, "Arkon", likes=["raids"])
    assert weight(p, topic=topic_words(["raiding again"])) > weight(p)


def reasoned_weights(n=100):
    return {g: 1.0 for g in range(1, n + 1)}


def test_reasons_come_first_for_replies():
    rng = random.Random(21)
    w = reasoned_weights()
    hits = 0
    for _ in range(500):
        got = pick_speakers(w, 2, rng, reasons={7, 8})
        hits += set(got) == {7, 8}
    assert hits == 500
    got = pick_speakers(w, 3, rng, reasons={7})
    assert got[0] == 7 and len(got) == 3
    n = Counter(pick_speakers(w, 1, rng, reasons=set())[0] for _ in range(5000))
    assert max(n.values()) < 5000 * 0.03


def test_reply_prefers_reason_pool_over_weight():
    rng = random.Random(22)
    w = {**reasoned_weights(), 50: 0.05}
    c = Counter()
    for _ in range(300):
        c.update(plan_replies("evening all", channel="guild", to_guid=0, weights=w,
                              names={}, afk=set(), rng=rng, reasons={50}))
    assert c[50] == 300


def test_has_reason():
    p = make_persona(101, "Arkon", likes=["raids"])
    c0 = ctx()
    assert not has_reason(p, None, c0)
    assert has_reason(p, None, ctx(topic=topic_words(["raiding"])))
    assert has_reason(p, Relationship(a=101, b=2, affinity=30.0), c0)
    assert not has_reason(p, Relationship(a=101, b=2, affinity=10.0), c0)
    h = Relationship(a=101, b=1, affinity=45.0, interactions=4)  # lived
    assert not has_reason(p, None, c0, rel_to_human=h)
    assert has_reason(p, None, c0, rel_to_human=h, reply=True)
    assert not has_reason(p, None, c0, reply=True,  # the same warmth, never lived
                          rel_to_human=Relationship(a=101, b=1, affinity=45.0))
    # a romance stage is an attitude, never a reason (ruling final I1)
    assert not has_reason(p, None, c0, reply=True,
                          rel_to_human=Relationship(a=101, b=1, romance_stage="notices"))


def test_allowed_predicate_and_combat_lead(clock):
    from conftest import HUMAN, OFFICERS, mem
    from leeroy_mind.protocol import Boss, Cast, Group, Hello
    from leeroy_mind.talk.raid import RaidState
    from leeroy_mind.talk.world import WorldView
    w = WorldView(is_officer=lambda g: g in OFFICERS)
    w.apply(Hello(ts=0, server="s", version="1", guild_id=1, human_guid=HUMAN), 0.0)
    w.apply(Cast(ts=0, members=tuple(mem(g) for g in NAMES)), 0.0)
    r = RaidState(world=w, clock=clock, personas=lambda g: None)
    r.on_group(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, *NAMES)))
    t = clock.now()
    r.on_boss(Boss(ts=0, map=631, instance_id=5, boss_id=0, boss_name="", state="engaged",
                   members=(101, 102)), t)

    def ok(g):
        return r.allows(g, TurnKind.REPLY_HUMAN, t + 5)
    rng = random.Random(6)
    for _ in range(100):
        got = plan_replies("anyone know the fix?", channel="raid", to_guid=0, weights=W,
                           names=NAMES, afk=set(), rng=rng, allowed=ok)
        assert got and set(got) <= {102, 104}


def test_whisper_needs_a_valid_target():
    rng = random.Random(1)
    for tg in (0, 999):
        assert plan_replies("hi", channel="whisper", to_guid=tg, weights=W, names=NAMES,
                            afk=set(), rng=rng) == []


def test_whisper_reply_goes_to_target():
    rng = random.Random(8)
    n = Counter(tuple(plan_replies("you around?", channel="whisper", to_guid=102, weights=W,
                                   names=NAMES, afk=set(), rng=rng)) for _ in range(5_000))
    assert set(n) <= {(102,), ()}
    assert abs(n[(102,)] / 5_000 - 0.9) < 0.02


def test_a_woman_with_an_attitude_toward_him_answers_every_whisper():
    """Rehearsal 3, check 4: his third whisper to a drawn woman drew no reply, one in ten by
    design (WHISPER_REPLY_P). A woman fond of him, drawn to him or wanting him answers every
    one; anyone else keeps 0.9."""
    for stance in ("fond", "drawn", "lust"):
        her = make_persona(102, "Belra", player_gender="female", stance=stance)
        assert whisper_reply_p(her) == 1.0, stance
    assert whisper_reply_p(make_persona(102, "Belra", player_gender="female")) == 0.9
    assert whisper_reply_p(make_persona(101, "Arkon", stance="lust")) == 0.9  # a man's stance
    assert whisper_reply_p(None) == 0.9
    rng = random.Random(8)
    assert all(plan_replies("you around?", channel="whisper", to_guid=102, weights=W,
                            names=NAMES, afk=set(), rng=rng, whisper_p=1.0) == [102]
               for _ in range(2_000))
    turns = plan_reply_turns("psst", channel="whisper", to_guid=102, weights=W, names=NAMES,
                             afk=set(), rng=random.Random(8), line_id=78, human_guid=1,
                             whisper_p=1.0)
    assert [(t.speaker, t.to_guid) for t in turns] == [(102, 1)]


def test_reply_turns_carry_ids_and_channels():
    rng = random.Random(9)
    turns = plan_reply_turns("evening all", channel="raid", to_guid=0, weights=W, names=NAMES,
                             afk=set(), rng=rng, line_id=77)
    assert 2 <= len(turns) <= 3
    assert len({t.turn_id for t in turns}) == len(turns)
    assert all(t.kind is TurnKind.REPLY_HUMAN and t.reply_to == "raid"
               and t.opener_channel == "raid" for t in turns)
    for seed in range(20):
        w = plan_reply_turns("psst", channel="whisper", to_guid=102, weights=W, names=NAMES,
                             afk=set(), rng=random.Random(seed), line_id=78, human_guid=1)
        assert all(t.reply_to == "whisper" and t.speaker == 102 and t.to_guid == 1 for t in w)
    g = plan_reply_turns("gn all", channel="guild", to_guid=0, weights=W, names=NAMES,
                         afk=set(), rng=rng, line_id=79)
    assert g and all(t.kind is TurnKind.GOODNIGHT and t.to_guid == 0 for t in g)


def test_typing_delay_never_instant_and_high_is_slower():
    rng = random.Random(0)
    assert typing_delay("k", 8.0, 0.1, rng) == 2.0
    a = typing_delay("x" * 60, 5.0, 1.0, random.Random(1))
    b = typing_delay("x" * 60, 5.0, 1.6, random.Random(1))
    assert b > a >= 2.0


def test_quest_lines_are_spotted():
    for text in ("anyone know where this quest starts?", "doing quests in icecrown",
                 "questing tonight", "this quest chain is endless",
                 "where do i find the argent tournament guy", "how do i turn in this thing",
                 "doing my dailies", "anyone done the daily yet", "daily heroic anyone?",
                 "gonna do my daily then raid", "the daily quest is up",
                 "need |cffffff00|Hquest:13789:80|h[Taking Battle To The Enemy]|h|r",
                 "where do i find thrall?", "where do i go after the icc entrance?",
                 "where do i turn in emblems of frost?"):
        assert is_quest_talk(text), text
    # part E's re-review: "where do i" only about something of the game
    for text in ("pulling in 5", "who wants to run voa", "request a summon", "conquest pts",
                 "turn in for the night", "my daily commute is brutal",
                 "daily standup ran long", "taking my daily vitamins",
                 "where do i get a good pizza around here?", "where do i get my car fixed?"):
        assert not is_quest_talk(text), text


def test_a_seeded_acquaintance_is_not_close_enough_by_rounding():
    p = make_persona(101, "Arkon")
    # 1 + 10/100 + 20/200 is 1.2000000000000002 in floats: not over the 1.2 bar
    assert not has_reason(p, Relationship(a=101, b=2, affinity=10.0, familiarity=20.0), ctx())
    assert has_reason(p, Relationship(a=101, b=2, affinity=10.0, familiarity=21.0), ctx())


def test_quest_talk_gives_every_woman_a_reason():
    w = make_persona(201, "Belra", player_gender="female")
    m = make_persona(101, "Arkon")
    q = ctx(quest=True)
    assert has_reason(w, None, q, reply=True)
    assert not has_reason(m, None, q, reply=True)
    assert not has_reason(w, None, q)  # a reason to answer him, not to open
    assert not has_reason(w, None, ctx(), reply=True)


# -- who answers him (final review spec I1 and its ruling) -------------------------------------

def invented_guild():
    """Twenty invented members in the owner's proportions: 14 women (3 lust, 4 drawn, 6 fond,
    1 none; most healers are women), 6 men (none: the tanks, a raid lead and a PvP lead), each
    with the row a stance seeds toward him (seed.human_seed): nothing lived yet."""
    from leeroy_mind.personas.seed import human_seed
    women = [("lust", "healer", "Holy"), ("lust", "ranged", "Arcane"),
             ("lust", "healer", "Restoration"), ("drawn", "healer", "Discipline"),
             ("drawn", "ranged", "Fire"), ("drawn", "melee", "Combat"), ("drawn", "healer", "Holy"),
             ("fond", "ranged", "Balance"), ("fond", "healer", "Restoration"),
             ("fond", "ranged", "Frost"), ("fond", "melee", "Assassination"),
             ("fond", "healer", "Discipline"), ("fond", "ranged", "Affliction"),
             ("none", "ranged", "Shadow")]
    men = [("tank", "Protection", "", "achiever"), ("tank", "Blood", "Raid Lead", "achiever"),
           ("melee", "Arms", "PvP Lead", "competitor"), ("ranged", "Marksmanship", "", "social"),
           ("melee", "Retribution", "", "explorer"), ("tank", "Feral Combat", "", "social")]
    cast, rels = [], {}
    for i, (stance, role, spec) in enumerate(women):
        g = 300 + i
        cast.append(make_persona(g, f"W{i}", player_gender="female", stance=stance,
                                 raid_role=role, main_spec=spec, off_spec="", likes=["gardening"],
                                 motivation="social", thinks_as="social"))
    for i, (role, spec, title, motivation) in enumerate(men):
        g = 400 + i
        cast.append(make_persona(g, f"M{i}", raid_role=role, main_spec=spec, off_spec="",
                                 officer_title=title, likes=["fishing"], motivation=motivation,
                                 thinks_as="competitor" if title == "PvP Lead" else "social"))
    for p in cast:
        rels[p.guid] = human_seed(p.guid, 1, p.romance.stance)
    return cast, rels


def his_answers(text, cast, rels, rng, *, thread=frozenset()):
    """Who answers his line, weighed as the engine's _weigh does for a reply to him."""
    c = ctx(topic=topic_words([text]), quest=is_quest_talk(text), to_human=True, thread=thread)
    weights = {p.guid: speaker_weight(p, PersonaStateRow(guid=p.guid), rels[p.guid], c, None, 1.0)
               for p in cast}
    reasons = {p.guid for p in cast
               if has_reason(p, rels[p.guid], c, rel_to_human=rels[p.guid], reply=True)}
    return plan_replies(text, channel="guild", to_guid=0, weights=weights, names={}, afk=set(),
                        rng=rng, reasons=reasons), reasons


GENERIC = ("evening all", "lol", "long day at work, finally home", "anyone around?",
           "brb grabbing food", "that was a fun night", "ugh my internet")


def test_men_answer_a_fair_share_of_his_generic_lines():
    """Seeded attitudes made every woman a "reason" to answer: 90-95% of his replies came from
    women. A seeded row is no reason, and attitude only weights the draw a little."""
    cast, rels = invented_guild()
    men = {p.guid for p in cast if p.identity.player_gender == "male"}
    rng, said = random.Random(31), Counter()
    for _ in range(300):
        for text in GENERIC:
            got, reasons = his_answers(text, cast, rels, rng)
            assert not reasons, text  # nobody has a real reason to answer "lol"
            said.update("man" if g in men else "woman" for g in got)
    share = said["man"] / (said["man"] + said["woman"])
    assert 0.25 <= share <= 0.4, share
    # and the women's attitude still shows: lust above drawn above fond, all modestly
    by = {p.guid: p.romance.stance for p in cast}
    picks = Counter()
    for _ in range(3000):
        got, _ = his_answers("evening all", cast, rels, rng)
        picks.update(by[g] for g in got)
    per = {s: picks[s] / sum(1 for p in cast if p.romance.stance == s
                             and p.identity.player_gender == "female")
           for s in ("lust", "drawn", "fond")}
    assert per["lust"] > per["drawn"] > per["fond"] > 0
    assert per["lust"] < 1.6 * per["fond"]


def test_topic_fit_men_answer_his_line_far_more():
    cast, rels = invented_guild()
    by = {p.character_name: p.guid for p in cast}
    rng = random.Random(32)

    def rate(text, guids):
        hits = 0
        for _ in range(500):
            got, _ = his_answers(text, cast, rels, rng)
            hits += any(g in guids for g in got)
        return hits / 500
    pvp, lead = by["M2"], by["M1"]
    tanks = {by["M0"], by["M1"], by["M5"]}
    assert rate("anyone up for arena tonight?", {pvp}) > 0.9
    assert rate("anyone up for arena tonight?", {pvp}) > 3 * rate("evening all", {pvp})
    assert rate("who is tanking icc tonight?", tanks) > 0.9
    assert rate("what's the strat for the next boss in icc?", {lead}) > 0.6
    healers = {p.guid for p in cast if p.play.raid_role == "healer"}
    got, reasons = his_answers("need a healer for heroics", cast, rels, rng)
    assert reasons == healers and set(got) <= healers


def test_real_history_and_an_open_thread_are_reasons_attitude_is_not():
    p = make_persona(101, "Arkon")
    lust = make_persona(201, "Belra", player_gender="female", stance="lust")
    c = ctx(to_human=True)
    from leeroy_mind.personas.seed import human_seed
    seeded = human_seed(201, 1, "lust")
    assert not has_reason(lust, seeded, c, rel_to_human=seeded, reply=True)
    noticing = Relationship(a=201, b=1, affinity=30.0, familiarity=30.0, romance_stage="banter")
    assert not has_reason(lust, noticing, c, rel_to_human=noticing, reply=True)
    # history counts only when lived: a relinked friend row (0 interactions) is no reason
    relinked = Relationship(a=101, b=1, affinity=40.0, trust=40.0, familiarity=60.0)
    assert not has_reason(p, relinked, c, rel_to_human=relinked, reply=True)
    friend = Relationship(a=101, b=1, affinity=45.0, trust=40.0, familiarity=60.0,
                          interactions=12)
    assert has_reason(p, friend, c, rel_to_human=friend, reply=True)
    known = Relationship(a=101, b=1, affinity=20.0, familiarity=65.0, interactions=3)
    assert has_reason(p, known, c, rel_to_human=known, reply=True)
    assert not has_reason(p, Relationship(a=101, b=1, affinity=20.0, familiarity=40.0,
                                          interactions=30), c, reply=True,
                          rel_to_human=Relationship(a=101, b=1, affinity=20.0,
                                                    familiarity=40.0, interactions=30))
    assert has_reason(p, None, ctx(to_human=True, thread=frozenset({101})), reply=True)
    assert not has_reason(p, None, ctx(to_human=True, thread=frozenset({102})), reply=True)
    # a seeded row weighs no more than no row at all; an attitude weighs a little
    plain = weight(p, rel=human_seed(101, 1, "none"), to_human=True)
    assert plain == weight(p, rel=None, to_human=True) == weight(p, rel=relinked, to_human=True)
    assert weight(p, rel=friend, to_human=True) > plain
    w = weight(lust, rel=seeded, to_human=True)
    assert plain < w < 1.5 * plain


def test_unlived_friend_rows_change_nothing():
    """Part C's relink makes 61 of 106 members friends of his on paper (affinity 40, trust 40,
    familiarity 60, no interactions). Unlived, they change neither who has a reason nor the
    draw: the same lines get the same answers as with the seeded rows alone."""
    cast, seeded = invented_guild()
    relinked = dict(seeded)
    for p in cast[: round(len(cast) * 61 / 106)]:
        relinked[p.guid] = Relationship(a=p.guid, b=1, affinity=40.0, trust=40.0,
                                        familiarity=60.0, attraction=seeded[p.guid].attraction,
                                        romance_stage=seeded[p.guid].romance_stage,
                                        label="friend", notes="an old raiding friend")
    for text in (*GENERIC, "anyone up for arena tonight?", "who is tanking icc tonight?"):
        a, b = random.Random(text), random.Random(text)
        for _ in range(200):
            assert his_answers(text, cast, seeded, a) == his_answers(text, cast, relinked, b)


# -- fix wave G3: his questions about the guild's past (rehearsal 3, check 3) -------------------

from leeroy_mind.talk.select import asks_the_past  # noqa: E402

PAST_QUESTIONS = [  # the rehearsal's two shapes, with an invented guild matter, and their kin
    "who gets the even weeks for bank duty again?", "how long has the potion feud been going?",
    "since when do we have a bank keeper?", "when did the potion feud start?",
    "who started the potion feud?", "how did the potion feud start?",
    "what year did we found the guild?", "who remembers the first time we cleared the vault?",
    "remember when we wiped on the drake all night?", "how long have we been raiding together?",
    "what's the bank duty rule again?", "wait who won the potion feud again?",
    "when was the last time we did a guild photo?", "how long's the feud been going on now?",
    "who founded the guild?", "who remembers when we killed marrowgar?"]
NOT_PAST = [  # a question to the members about themselves is theirs to answer
    "who wants to go again?", "who's ready to pull again?", "anyone up for icc tonight?",
    "how long is the raid tonight?", "how long have you been playing?", "when did you get on?",
    "how long have you guys been playing?", "when did u start raiding?",
    "who's tanking tonight?", "what are you up to tonight?", "who has a spare flask?",
    "can we go again?", "who can tank again?", "how's everyone doing?", "what time is it?",
    "when do we pull again?", "what are we doing again?", "did anyone get the bracers?",
    "how did the attempts go?", "you guys remember to bring flasks?",
    "since when do you play a priest?", "who won the roll?"]


@pytest.mark.parametrize("line", PAST_QUESTIONS)
def test_his_question_about_the_guilds_past_is_read_as_one(line):
    assert asks_the_past(line)


@pytest.mark.parametrize("line", NOT_PAST)
def test_his_plans_invitations_and_questions_to_them_are_not_about_the_past(line):
    assert not asks_the_past(line)


@pytest.mark.parametrize("line, past", [
    ("who's tanking again?", False), ("what's the plan again?", False),
    ("what time is raid again?", False), ("when's the raid again?", False),
    ("who's on interrupts again?", False), ("what's your name again?", False),
    ("who gets the even weeks for bank duty again?", True),
    ("what was the bank pact again?", True), ("when was the pact?", True)])
def test_a_bare_again_is_no_question_about_the_past(line, past):
    """Fix round 1 (review C1): every "...again?" read as a question about the past. An again
    with one subject word or none asks to be reminded of tonight, not of the guild's past."""
    assert asks_the_past(line) is past


# -- fix wave G5: a line that names one member gets that member's answer only ------------------
# Seen live: "whatever you need <name>, just link it in chat" planned a second replier from the
# reason pool (a remark's count is 0-2, and a named line was held to at least 1), who riffed on
# the named member's answer with the referents tangled.

from leeroy_mind.talk.select import aimed_at_one, speaks_to_guild  # noqa: E402

TO_ONE = [  # the live shape, then one member named in a question, a greeting and a goodbye
    "whatever you need arkon, just link it in chat", "arkon can you link it in chat?",
    "what do you need arkon?", "arkon you up?", "is the vault run still on arkon",
    "thanks arkon, that was a great pull", "lol arkon", "hey arkon", "evening arkon",
    "gn arkon", "night arkon"]
TO_GUILD_TOO = [  # one member named, and the guild spoken to as well
    "arkon, anyone know where the argent dailies start?", "who has the link arkon?",
    "arkon who's tanking tonight", "gn all, thanks arkon", "evening all, arkon welcome back",
    "guys, arkon is a legend", "thanks everyone, especially arkon",
    "you guys are great, arkon especially", "any1 up for voa? arkon you in?"]


def test_the_lines_cover_every_kind_of_line():
    assert {classify_human_line(t) for t in TO_ONE} == {"remark", "question_all", "greeting",
                                                       "goodbye"}
    assert {classify_human_line(t) for t in TO_GUILD_TOO} == {"remark", "question_all",
                                                             "greeting", "goodbye"}


@pytest.mark.parametrize("line", TO_ONE)
def test_a_line_naming_one_member_is_answered_by_that_member_only(line):
    """Whoever else has a reason to answer him: the reason pool adds no one."""
    for seed in range(200):
        got = plan_replies(line, channel="guild", to_guid=0, weights=W, names=NAMES, afk=set(),
                           rng=random.Random(seed), reasons={102, 103, 104})
        assert got == [101], (line, seed, got)
        turns = plan_reply_turns(line, channel="guild", to_guid=0, weights=W, names=NAMES,
                                 afk=set(), rng=random.Random(seed), line_id=5,
                                 reasons={102, 103, 104})
        assert [t.speaker for t in turns] == [101]


@pytest.mark.parametrize("line", TO_GUILD_TOO)
def test_a_line_that_also_speaks_to_the_guild_keeps_the_pool(line):
    sizes = Counter()
    for seed in range(200):
        got = plan_replies(line, channel="guild", to_guid=0, weights=W, names=NAMES, afk=set(),
                           rng=random.Random(seed), reasons={102, 103})
        assert got[0] == 101 and set(got[1:]) <= {102, 103}, (line, seed, got)
        sizes[len(got)] += 1
    assert max(sizes) >= 2, (line, sizes)


def test_a_question_with_no_name_keeps_the_pool():
    """Nothing named, so the line is the guild's: 2-3 answers, the members with a reason first
    (the rule above changes nothing for it)."""
    sizes = Counter()
    for seed in range(200):
        got = plan_replies("anyone know where the argent dailies start?", channel="guild",
                           to_guid=0, weights=W, names=NAMES, afk=set(),
                           rng=random.Random(seed), reasons={103, 104})
        assert {103, 104} <= set(got), (seed, got)
        sizes[len(got)] += 1
    assert set(sizes) == {2, 3}, sizes


def test_a_line_naming_two_members_plans_both_and_may_add_the_pool():
    """Two names leave the line to the usual count: a remark is answered by the two, a question
    by the two and, half the time, one from the reason pool."""
    for line, want in (("arkon and belra, just link it in chat", {2}),
                       ("arkon and belra, can you link it in chat?", {2, 3})):
        sizes = Counter()
        for seed in range(200):
            got = plan_replies(line, channel="guild", to_guid=0, weights=W, names=NAMES,
                               afk=set(), rng=random.Random(seed), reasons={103, 104})
            assert got[:2] == [101, 102] and set(got[2:]) <= {103, 104}, got
            sizes[len(got)] += 1
        assert set(sizes) == want, (line, sizes)
    for seed in range(50):
        got = plan_replies("arkon belra coros link it", channel="guild", to_guid=0, weights=W,
                           names=NAMES, afk=set(), rng=random.Random(seed))
        assert got[:3] == [101, 102, 103]


def test_the_one_he_named_is_the_only_one_unless_she_cannot_answer():
    """The rule counts the members named who can answer: an AFK or gated one is not named, so
    the line is no one's and the guild answers it as before (the mention test above)."""
    rng = random.Random(7)
    sizes = {len(plan_replies("arkon you up?", channel="guild", to_guid=0, weights=W,
                              names=NAMES, afk={101}, rng=rng)) for _ in range(200)}
    assert sizes == {2, 3}
    no_arkon = {g: w for g, w in W.items() if g != 101}
    sizes = {len(plan_replies("arkon you up?", channel="guild", to_guid=0, weights=no_arkon,
                              names=NAMES, afk=set(), rng=rng)) for _ in range(200)}
    assert sizes == {2, 3}
    # two named, one of them away: the one who can answers, alone, as with any one name
    assert plan_replies("arkon belra you up?", channel="guild", to_guid=0, weights=W,
                        names=NAMES, afk={102}, rng=rng) == [101]


def test_a_whisper_is_still_the_one_he_whispered():
    got = {tuple(plan_replies("arkon, you around?", channel="whisper", to_guid=102, weights=W,
                              names=NAMES, afk=set(), rng=random.Random(s)))
           for s in range(100)}
    assert got <= {(102,), ()}


def test_aimed_at_one_wants_one_name_and_no_word_to_the_guild():
    assert aimed_at_one("link it in chat arkon", [101])
    assert not aimed_at_one("link it in chat", [])
    assert not aimed_at_one("link it in chat arkon belra", [101, 102])
    assert not aimed_at_one("anyone link it in chat arkon", [101])


@pytest.mark.parametrize("line", [
    "anyone know where it starts?", "any1 up for voa", "anybody around", "who has the link",
    "who's got it", "whos tanking", "guys listen up", "thanks everyone", "everyone's here",
    "you all rock", "you guys rock", "u all ready", "hey all", "evening all", "gn team",
    "thx guild", "sup peeps", "yall ready", "y'all ready?", "hello people", "night all",
    "ok folks", "whoever has it, link it"])
def test_the_words_that_speak_to_the_whole_guild(line):
    assert speaks_to_guild(line)


@pytest.mark.parametrize("line", [
    "whatever you need, just link it in chat", "thats all i need", "thanks for all the help",
    "the guild bank is full", "people are weird", "he is a team player", "what do you need?",
    "can you link it?", "hey", "gn", "all good", "i have the whole guild to thank",
    "link |cff0070dd|Hitem:1234|h[Everyone's Friend]|h|r please",
    "somebody told me you had it", "are you in"])
def test_ordinary_words_speak_to_no_one_in_particular(line):
    assert not speaks_to_guild(line)
