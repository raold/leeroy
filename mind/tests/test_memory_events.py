import pytest

from conftest import make_persona, run, seed_test_cast
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.events import EventMemoryRules
from leeroy_mind.memory.writer import Embedder, MemoryWriter
from leeroy_mind.protocol import Achievement, BgEnd, Boss, Death, LootWon, Zone
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import BOSS_NAMES, boss_name

HUMAN = 1
ICC, SINDRA = 631, 11


def jump(clock, seconds):
    clock._t += seconds


@pytest.fixture
def rig(store, clock):
    personas = seed_test_cast(store)
    personas[103] = make_persona(103, "Coros", wants=["Shadowmourne"])
    llm = FakeLLM()
    w = MemoryWriter(store, Embedder(LLMScheduler(llm, clock=clock), "m"), clock)
    rules = EventMemoryRules(w, store, clock, personas.get, lambda: HUMAN)
    return rules, w


class Feed:
    def __init__(self, rules):
        self.rules, self.n = rules, 0

    def __call__(self, ev):
        self.n += 1
        return run(self.rules.on_event(ev, self.n))


def boss(state, boss_id=SINDRA, map=ICC, name="", members=(HUMAN, 101, 102), inst=77):
    return Boss(ts=0, map=map, instance_id=inst, boss_id=boss_id, boss_name=name, state=state,
                members=members)


def all_memories(store):
    return [m for o in (0, 1, 101, 102, 103)
            for m in store.memories_with_actors(o, [101, 102, 103])]


def test_boss_night_counter(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    for _ in range(6):
        feed(boss("engaged", name="Sindragosa"))
        feed(boss("wiped", name="Sindragosa"))
    feed(boss("engaged", name="Sindragosa"))
    feed(boss("killed", name="Sindragosa"))
    m = store.memories_with_actors(101, [101])[0]
    assert m.text == "We wiped 6\u00d7 on Sindragosa, then killed it."  # server firsts are old news
    assert m.importance == 7 and m.valence == pytest.approx(0.6)
    assert m.raid_id == ICC and m.actors == (HUMAN, 101, 102)
    assert m.key == "boss:631:11:2026-10-29" and m.boss_id == 11
    mems = all_memories(store)
    assert any(x.owner == 0 for x in mems) and not any(x.owner == 1 for x in mems)
    assert sum(1 for x in mems if x.owner == 101 and x.key) == 1
    assert rules.wipes_tonight(ICC, SINDRA) == 6


def test_boss_name_from_table_never_empty(rig, store):
    rules, _ = rig
    Feed(rules)(boss("killed"))  # the bridge sends no name
    m = store.memories_with_actors(101, [101])[0]
    assert "Sindragosa" in m.text and " on ," not in m.text
    Feed(rules)(boss("killed", boss_id=40))  # a raid index the table does not know
    Feed(rules)(boss("killed", boss_id=99, map=1))  # not a raid map: no boss memories at all
    texts = [x.text for x in store.memories_with_actors(101, [101])]
    assert "We killed the boss." in texts and len(texts) == 2


def test_same_boss_id_in_two_raids_is_two_bosses(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    feed(boss("killed", boss_id=0, map=631))
    feed(boss("killed", boss_id=0, map=533))
    texts = sorted(x.text for x in store.memories_with_actors(101, [101]))
    assert texts == ["We killed Lord Marrowgar.", "We killed Patchwerk."]


def test_boss_night_crosses_midnight(rig, store, clock):
    rules, _ = rig
    feed = Feed(rules)
    jump(clock, 3 * 3600 + 50 * 60)  # 23:50
    feed(boss("engaged"))
    feed(boss("wiped"))
    jump(clock, 30 * 60)  # 00:20 next day, same night
    feed(boss("engaged"))
    feed(boss("killed"))
    mine = [m for m in store.memories_with_actors(101, [101]) if m.key]
    assert len(mine) == 1 and mine[0].text.startswith("We wiped once on Sindragosa, then killed")
    assert len([m for m in store.memories_with_actors(0, [101]) if m.key]) == 1


def test_no_night_gets_a_first_kill_note(rig, store, clock):
    rules, _ = rig
    feed = Feed(rules)
    feed(boss("killed"))
    assert "First kill" not in store.memories_with_actors(101, [101])[0].text
    jump(clock, 24 * 3600)
    for _ in range(2):
        feed(boss("engaged"))
        feed(boss("wiped"))
    feed(boss("killed"))
    m = [x for x in store.memories_with_actors(101, [101]) if x.key.endswith("2026-10-30")][0]
    assert m.text == "We wiped 2\u00d7 on Sindragosa, then killed it."
    assert m.importance == 5 and m.valence == pytest.approx(0.6)


def test_wipes_only_night(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    for _ in range(4):
        feed(boss("engaged"))
        feed(boss("wiped"))
    m = store.memories_with_actors(101, [101])[0]
    assert m.text == "We wiped 4\u00d7 on Sindragosa tonight."
    assert m.valence == pytest.approx(-0.6) and m.importance == 6


def test_wipe_after_kill_keeps_the_kill_memory(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    feed(boss("killed"))
    feed(boss("engaged"))
    feed(boss("wiped"))
    assert store.memories_with_actors(101, [101])[0].text.startswith("We killed Sindragosa")


def test_first_death_in_wiped_encounter_only(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    feed(boss("engaged"))
    feed(Death(ts=0, guid=101, map=ICC, killer_entry=1))
    feed(Death(ts=0, guid=102, map=ICC, killer_entry=1))
    feed(Death(ts=0, guid=101, map=ICC, killer_entry=1))
    feed(boss("wiped"))
    texts101 = [m.text for m in store.memories_with_actors(101, [101])]
    assert "I died first on Sindragosa and we wiped." in texts101
    assert not any("died first" in m.text for m in store.memories_with_actors(102, [102]))
    died = [m for m in store.memories_with_actors(101, [101]) if "died first" in m.text][0]
    assert len(died.evidence) == 2 and all(e.startswith("event:") for e in died.evidence)
    feed(boss("engaged"))
    feed(Death(ts=0, guid=102, map=ICC, killer_entry=1))
    feed(boss("killed"))
    assert not any("died first" in m.text for m in store.memories_with_actors(102, [102]))


def test_death_on_another_map_or_outside_encounter_is_ignored(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    feed(Death(ts=0, guid=101, map=ICC, killer_entry=1))  # nothing engaged
    feed(boss("engaged"))
    feed(Death(ts=0, guid=101, map=533, killer_entry=1))  # other map
    feed(Death(ts=0, guid=555, map=ICC, killer_entry=1))  # not a member
    feed(boss("wiped"))
    assert not any("died first" in m.text for m in all_memories(store))


def test_loot_want_importance(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    feed(LootWon(ts=0, guid=103, item_id=49623, name="Shadowmourne", method="need"))
    feed(LootWon(ts=0, guid=103, item_id=1, name="|cff0070dd|Hitem:1:0|h[Bracers of Dusk]|h|r",
                 method="greed"))
    feed(LootWon(ts=0, guid=103, item_id=2, name="Shadowmourne Hilt", method="greed"))
    by_text = {m.text: m for m in store.memories_with_actors(103, [103])}
    assert by_text["Finally got Shadowmourne."].importance == 6
    assert by_text["Finally got Shadowmourne."].valence == pytest.approx(0.7)
    assert by_text["I won [Bracers of Dusk]."].importance == 3
    assert by_text["I won [Bracers of Dusk]."].valence == pytest.approx(0.3)
    assert by_text["Finally got Shadowmourne Hilt."].importance == 6  # substring either way


def test_loot_lost_and_achievement(rig, store):
    rules, _ = rig
    r = run(rules.on_loot_lost(101, "Shadowmourne", ("event:9",)))
    assert r.action == "inserted"
    m = store.get_memories([r.id])[0]
    assert (m.importance, m.valence, m.evidence) == (4, pytest.approx(-0.4), ("event:9",))
    Feed(rules)(Achievement(ts=0, guid=102, achievement_id=4530, name="Glory of the Raider"))
    a = [m for m in store.memories_with_actors(102, [102]) if "Glory" in m.text][0]
    assert (a.importance, a.valence) == (5, pytest.approx(0.5))
    assert run(rules.on_loot_lost(HUMAN, "x", ("event:1",))).action == "rejected"


def test_activity_flush_one_memory_per_member(rig, store, clock):
    rules, w = rig
    feed = Feed(rules)
    # 101 is race 2 (orc, horde); 102 is race 4 (night elf, alliance)
    feed(BgEnd(ts=0, bg_type=2, winner="horde", members=(101, 102, HUMAN)))
    feed(BgEnd(ts=0, bg_type=2, winner="horde", members=(101, 102)))
    feed(BgEnd(ts=0, bg_type=2, winner="alliance", members=(101, 102)))
    for z in ("Orgrimmar", "Orgrimmar", "Durotar", "Barrens", "Tirisfal"):
        feed(Zone(ts=0, guid=101, zone_id=1, zone_name=z))
    res = run(rules.activity.flush(w, clock.now()))
    assert len(res) == 2 and all(r.action == "inserted" for r in res)
    m101 = [m for m in store.memories_with_actors(101, [101]) if m.text.startswith("Tonight")][0]
    assert m101.text.startswith("Tonight: 3 battlegrounds (2 won); spent time in Orgrimmar, ")
    assert m101.text.count(",") == 2 and m101.importance == 2
    m102 = [m for m in store.memories_with_actors(102, [102]) if m.text.startswith("Tonight")][0]
    assert m102.text == "Tonight: 3 battlegrounds (1 won)." and m102.importance == 2
    assert run(rules.activity.flush(w, clock.now())) == []


def test_activity_three_wins_is_importance_three_and_evidence_capped(rig, store, clock):
    rules, w = rig
    feed = Feed(rules)
    for _ in range(25):
        feed(BgEnd(ts=0, bg_type=2, winner="horde", members=(101,)))
    run(rules.activity.flush(w, clock.now()))
    m = store.memories_with_actors(101, [101])[0]
    assert m.importance == 3 and len(m.evidence) == 20


def test_human_never_gets_memories(rig, store):
    from leeroy_mind.personas.io import save_persona
    from leeroy_mind.protocol import Member
    from leeroy_mind.wow import CLASSES, RACES
    store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=2, class_=8, gender=0, level=80,
                              rank=0, online=True, map=0, zone=0, spec_tab=-1)], 1000.0)
    save_persona(store, make_persona(HUMAN, "Testmage"), source="seed", ts=1000.0,
                 race=RACES[2], class_name=CLASSES[8])
    rules, _ = rig
    rules._personas = lambda g: make_persona(g, "Xerxes", wants=["Shadowmourne"])
    feed = Feed(rules)
    feed(boss("engaged"))
    feed(Death(ts=0, guid=HUMAN, map=ICC, killer_entry=1))
    feed(boss("wiped"))
    feed(boss("killed"))
    feed(LootWon(ts=0, guid=HUMAN, item_id=1, name="Shadowmourne", method="need"))
    feed(Achievement(ts=0, guid=HUMAN, achievement_id=1, name="Something"))
    feed(BgEnd(ts=0, bg_type=2, winner="horde", members=(HUMAN,)))
    feed(Zone(ts=0, guid=HUMAN, zone_id=1, zone_name="Dalaran"))
    assert run(rules.activity.flush(rules._writer, 0)) == []
    assert run(rules.on_loot_lost(HUMAN, "x", ("event:1",))).action == "rejected"
    assert store.count_inserted_since(HUMAN, 0) == 0


def test_boss_memories_list_the_human_as_an_actor(rig, store):
    rules, _ = rig
    Feed(rules)(boss("killed"))
    assert store.memories_with_actors(101, [HUMAN])[0].text.startswith("We killed Sindragosa")
    assert store.memories_with_actors(0, [HUMAN])


@pytest.mark.parametrize("m,b", [(631, 13), (631, 14), (724, 3), (724, 4), (724, 5)])
def test_non_fight_steps_write_nothing(rig, store, m, b):
    rules, _ = rig
    feed = Feed(rules)
    feed(boss("engaged", boss_id=b, map=m))
    feed(Death(ts=0, guid=101, map=m, killer_entry=1))
    feed(boss("wiped", boss_id=b, map=m))
    feed(boss("killed", boss_id=b, map=m))
    assert all_memories(store) == [] and rules.wipes_tonight(m, b) == 0


def test_instances_are_tracked_apart(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    feed(boss("engaged", inst=1, members=(HUMAN, 101)))
    feed(boss("engaged", inst=2, members=(HUMAN, 102)))
    feed(Death(ts=0, guid=102, map=ICC, killer_entry=1))
    feed(boss("wiped", inst=1, members=(HUMAN, 101)))
    assert not any("died first" in m.text for m in all_memories(store))
    feed(boss("wiped", inst=2, members=(HUMAN, 102)))
    assert any("died first" in m.text for m in store.memories_with_actors(102, [102]))


def test_owners_are_the_union_of_engaged_and_final_members(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    feed(boss("engaged", members=(HUMAN, 101, 102)))
    feed(boss("killed", members=(HUMAN, 101)))
    assert store.memories_with_actors(102, [102])


def test_activity_wording(rig, store, clock):
    rules, w = rig
    feed = Feed(rules)
    feed(BgEnd(ts=0, bg_type=2, winner="horde", members=(101,)))
    feed(Zone(ts=0, guid=102, zone_id=1, zone_name="Dalaran"))
    run(rules.activity.flush(w, clock.now()))
    assert store.memories_with_actors(101, [101])[0].text == "Tonight: 1 battleground (1 won)."
    assert store.memories_with_actors(102, [102])[0].text == "Tonight: spent time in Dalaran."


def test_loot_lost_accepts_a_bare_string_as_one_ref(rig, store):
    rules, _ = rig
    r = run(rules.on_loot_lost(101, "Shadowmourne", "event:9"))
    assert store.get_memories([r.id])[0].evidence == ("event:9",)


def test_night_evidence_is_pruned(rig, clock):
    rules, _ = rig
    feed = Feed(rules)
    feed(boss("killed"))
    jump(clock, 24 * 3600)
    feed(boss("killed", boss_id=0))
    assert len(rules._night_evidence) == 1


def test_game_templates_do_not_merge_by_cosine(rig, store):
    rules, _ = rig
    feed = Feed(rules)
    for item in ("Shadowmourne", "Shadowmourne Hilt", "Shadowmourne Hil"):
        feed(LootWon(ts=0, guid=101, item_id=1, name=f"Frostmourne Fragment {item}",
                     method="greed"))
    assert len(store.memories_with_actors(101, [101])) == 3


def test_boss_table_known_indices():
    assert boss_name(631, 11) == "Sindragosa" and boss_name(631, 12) == "the Lich King"
    assert boss_name(631, 0) == "Lord Marrowgar" and boss_name(533, 0) == "Patchwerk"
    assert boss_name(533, 14) == "Kel'Thuzad" and boss_name(603, 12) == "Yogg-Saron"
    assert boss_name(603, 0) == "Flame Leviathan" and boss_name(649, 4) == "Anub'arak"
    assert boss_name(615, 0) == "Sartharion" and boss_name(616, 0) == "Malygos"
    assert boss_name(624, 3) == "Toravon the Ice Watcher" and boss_name(724, 6) == "Halion"
    assert boss_name(249, 0) == "Onyxia"
    assert boss_name(631, 0, "Marrow") == "Marrow"  # a bridge name wins
    assert boss_name(631, 13) == boss_name(724, 4) == boss_name(1, 1) == "the boss"
    assert boss_name(631, 11, "  ") == "Sindragosa"


def test_boss_table_covers_the_wotlk_raids():
    counts = {}
    for (m, _), name in BOSS_NAMES.items():
        counts[m] = counts.get(m, 0) + 1
        assert name and name == name.strip()
    assert counts == {631: 13, 649: 5, 603: 14, 533: 15, 615: 4, 616: 1, 624: 4, 724: 4, 249: 1}


def test_a_replayed_event_is_remembered_at_its_own_time(rig, store, clock):
    """Correctness M2: the app hands an event the bridge replayed after a restart its own
    server time, so its wipe counts, boss memory and night keys are that night's, and so are
    the times of its loot and achievement memories. A live event (no time) is now."""
    rules, _ = rig
    yesterday = clock.now() - 24 * 3600  # 2026-10-28, 20:00
    run(rules.on_event(boss("engaged"), 1, yesterday - 120))
    run(rules.on_event(boss("wiped"), 2, yesterday - 60))
    run(rules.on_event(boss("engaged"), 3, yesterday - 30))
    run(rules.on_event(boss("wiped"), 4, yesterday))
    assert store.get_meta("wipes:631:11:2026-10-28") == "2"
    assert rules.wipes_tonight(ICC, SINDRA) == 0
    m = store.memories_with_actors(101, [101])[0]
    assert (m.key, m.t_event, m.text) == ("boss:631:11:2026-10-28", int(yesterday),
                                          "We wiped 2× on Sindragosa tonight.")
    run(rules.on_event(Achievement(ts=0, guid=102, achievement_id=4530,
                                   name="Glory of the Raider"), 5, yesterday + 10))
    run(rules.on_event(LootWon(ts=0, guid=103, item_id=1, name="Shadowmourne", method="need"),
                       6, yesterday + 20))
    assert [x.t_event for x in store.memories_with_actors(102, [102]) if "Glory" in x.text] == [
        int(yesterday) + 10]
    assert [x.t_event for x in store.memories_with_actors(103, [103]) if "Shadowmourne" in x.text
            ] == [int(yesterday) + 20]
    run(rules.on_event(boss("engaged"), 7))  # tonight's live pull starts tonight's count
    run(rules.on_event(boss("wiped"), 8))
    assert rules.wipes_tonight(ICC, SINDRA) == 1
    tonight = [x for x in store.memories_with_actors(101, [101]) if x.key.endswith("2026-10-29")]
    assert len(tonight) == 1 and tonight[0].t_event == int(clock.now())
    assert tonight[0].text == "We wiped on Sindragosa tonight."
