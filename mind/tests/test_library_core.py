import logging
import sqlite3
from dataclasses import replace

import pytest
import sqlite_vec

from conftest import run
from leeroy_mind.library.chunks import ChunkDraft, ItemRow, QuestRow, SourceResult
from leeroy_mind.library.core import QUERY_PREFIX, Library, loot_target
from leeroy_mind.library.db import MIGRATIONS_DIR, latest_version
from leeroy_mind.llm.scheduler import LLMScheduler, Priority
from leeroy_mind.storage.db import EmbeddingMismatch
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_llm import FakeLLM

LICENSE = "CC BY-SA 4.0"


class ListSource:
    def __init__(self, name, chunks, items=None, quests=None):
        self.name = name
        self.chunks = list(chunks)
        self.items = items
        self.quests = quests

    def load(self, library):
        return SourceResult(self.name, list(self.chunks), items=self.items, quests=self.quests,
                            log=[f"{self.name} loaded"])


def world_source(**over):
    chunks = [
        ChunkDraft("item:50415", "item", "Bryntroll, the Bone Arbiter",
                   "Bryntroll, the Bone Arbiter: epic two-hand axe, item level 264. Drops from: "
                   "Lord Marrowgar (Icecrown Citadel, 25-player).", section="epic two-hand axe",
                   ref_ids=("item:50415",)),
        ChunkDraft("item:50709", "item", "Bryntroll, the Bone Arbiter",
                   "Bryntroll, the Bone Arbiter: epic two-hand axe, item level 277. Drops from: "
                   "Lord Marrowgar (Icecrown Citadel, 25-player heroic).",
                   section="epic two-hand axe", ref_ids=("item:50709",)),
        ChunkDraft("item:49426", "item", "Emblem of Frost",
                   "Emblem of Frost: epic item. Drops from every boss in Icecrown Citadel.",
                   ref_ids=("item:49426",)),
        ChunkDraft("loot:npc:36853:0", "loot", "Sindragosa",
                   "Sindragosa (Icecrown Citadel, 10-player) loot. Always: 2 x Emblem of Frost. "
                   "2 items from: Rimetooth Pendant (251 neck), Splintershard (251 fist weapon).",
                   section="10-player", map_id=631, instance="Icecrown Citadel",
                   ref_ids=("npc:36853", "item:49426")),
        ChunkDraft("loot:npc:36612:1", "loot", "Lord Marrowgar",
                   "Lord Marrowgar (Icecrown Citadel, 25-player) loot. One item from: "
                   "Bryntroll, the Bone Arbiter (264 two-hand axe).", section="25-player",
                   map_id=631, instance="Icecrown Citadel",
                   ref_ids=("npc:36612", "item:50415")),
        ChunkDraft("quest:1", "quest", "A Grim Start", "Quest A Grim Start (level 80). "
                   "Leads to: The Middle Road.", ref_ids=("quest:1",)),
        ChunkDraft("quest:2", "quest", "The Middle Road", "Quest The Middle Road (level 80). "
                   "Follows: A Grim Start.", ref_ids=("quest:2",)),
        ChunkDraft("quest:3", "quest", "The Frozen End", "Quest The Frozen End (level 80).",
                   ref_ids=("quest:3",)),
        ChunkDraft("quest:4", "quest", "A Side Errand", "Quest A Side Errand (level 80).",
                   ref_ids=("quest:4",)),
    ]
    items = [ItemRow(50415, "Bryntroll, the Bone Arbiter", 4, 264, 80, "two-hand axe"),
             ItemRow(50709, "Bryntroll, the Bone Arbiter", 4, 277, 80, "two-hand axe"),
             ItemRow(49426, "Emblem of Frost", 4, 80, 80, "")]
    quests = [QuestRow(1, "A Grim Start", 80), QuestRow(2, "The Middle Road", 80, prev_id=1,
                                                         next_id=3),
              QuestRow(3, "The Frozen End", 80, prev_id=2), QuestRow(4, "A Side Errand", 80,
                                                                      prev_id=2)]
    return ListSource("world", over.get("chunks", chunks), items, quests)


def wiki_source(extra=()):
    return ListSource("wiki", [
        ChunkDraft("wiki:1:0", "lore", "Sindragosa", "Sindragosa was the first consort of "
                   "Malygos. The Lich King raised her as a frost wyrm to guard his citadel.",
                   section="Biography", url="https://warcraft.wiki.gg/wiki/Sindragosa",
                   license=LICENSE),
        ChunkDraft("wiki:2:0", "strategy", "Sindragosa (tactics)", "Spread out for Blistering "
                   "Cold and hide behind Ice Tombs from Frost Bombs in the air phase.",
                   section="Phase Two", url="https://warcraft.wiki.gg/wiki/Sindragosa_(tactics)",
                   license=LICENSE),
        ChunkDraft("wiki:3:0", "lore", "Battle of Angrathar the Wrathgate", "At the Wrathgate "
                   "the Forsaken plague killed Bolvar Fordragon and Dranosh Saurfang.",
                   url="https://warcraft.wiki.gg/wiki/Wrathgate", license=LICENSE),
        *extra])


def atlas_source():
    return ListSource("atlasloot", [
        ChunkDraft("atlas:ICCSindragosa25Man:0", "loot", "Sindragosa", "Sindragosa (Icecrown "
                   "Citadel, 25-player) loot in AtlasLoot: Sindragosa's Cruel Claw (Neck).",
                   section="25-player", instance="Icecrown Citadel",
                   ref_ids=("atlas:ICCSindragosa25Man",))])


@pytest.fixture
def lib(tmp_path, clock):
    lib = Library(tmp_path / "library.db", clock=clock)
    lib.build([wiki_source(), atlas_source(), world_source()])
    yield lib
    lib.close()


def test_build_orders_sources_and_counts(tmp_path, clock):
    lib = Library(tmp_path / "library.db", clock=clock)
    try:
        seen = []
        reports = lib.build([wiki_source(), atlas_source(), world_source()], progress=seen.append)
        assert [r.source for r in reports] == ["world", "atlasloot", "wiki"]
        assert [r.inserted for r in reports] == [9, 1, 3] and all(r.log for r in reports)
        st = lib.stats()
        assert st["chunks"]["world/item"] == 3 and st["chunks"]["wiki/lore"] == 2
        assert (st["items"], st["quests"], st["embedded"], st["pending"]) == (3, 4, 0, 13)
        assert st["meta"]["count:world"] == "9"
        assert any("world: 9 chunks" in s for s in seen)
    finally:
        lib.close()


def test_text_search_ranks_and_filters(lib):
    hits = lib.search_text("what does Sindragosa drop", k=3, kinds=["loot"])
    assert hits and all(h.kind == "loot" for h in hits) and hits[0].title == "Sindragosa"
    assert {h.source for h in hits} == {"world", "atlasloot"}
    lore = lib.search_text("who raised Sindragosa as a frost wyrm", k=1)
    assert lore[0].title == "Sindragosa"
    assert lore[0].url.endswith("/Sindragosa") and lore[0].license == LICENSE
    assert lib.search_text('he said "NEAR" * -x OR (y) wrathgate', k=1)[0].title.startswith(
        "Battle of Angrathar")
    assert lib.search_text("!!! ??", k=3) == []
    with pytest.raises(ValueError):
        lib.search_text("x", kinds=["gossip"])


def test_search_without_vectors_never_calls_the_embedder(lib):
    llm = FakeLLM()
    hits = run(lib.search("Wrathgate Bolvar", k=2, llm=llm))
    assert hits[0].title == "Battle of Angrathar the Wrathgate"
    assert llm.calls == []


def test_embed_pending_is_batched_resumable_and_enables_vectors(lib, clock):
    llm = FakeLLM()
    assert run(lib.embed_pending(llm, batch=5, limit=7)) == 7
    assert lib.pending_embeddings() == 6
    assert run(lib.embed_pending(llm, batch=5)) == 6
    assert run(lib.embed_pending(llm, batch=5)) == 0
    assert len([c for c in llm.calls if "input" in c]) == 4  # 5 + 2, then 5 + 1
    assert lib.get_meta("embed_model") == "qwen3-embedding:0.6b"
    assert lib.conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 13
    llm.calls.clear()
    hits = run(lib.search("frost wyrm consort of Malygos", k=2, llm=llm))
    assert llm.calls and llm.calls[0]["input"][0].startswith(QUERY_PREFIX)
    assert hits[0].title == "Sindragosa" and hits[0].kind == "lore"
    only = run(lib.search("Sindragosa", k=5, kinds=["strategy"], llm=llm))
    assert [h.title for h in only] == ["Sindragosa (tactics)"]


def test_embed_through_the_scheduler_at_background_priority(lib, clock):
    llm = FakeLLM()
    sched = LLMScheduler(llm, clock=clock)

    async def go():
        return await lib.embed_pending(sched, batch=50, priority=Priority.BACKGROUND)
    assert run(go()) == 13 and lib.pending_embeddings() == 0


def test_search_degrades_to_text_when_the_embed_call_fails(lib, caplog):
    llm = FakeLLM()
    run(lib.embed_pending(llm))
    llm.fail_next(1)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        hits = run(lib.search("Wrathgate", k=1, llm=llm))
    assert hits[0].title == "Battle of Angrathar the Wrathgate"
    assert "using text search" in caplog.text


def test_other_embedding_model_is_refused_and_ignored(lib, tmp_path, clock):
    run(lib.embed_pending(FakeLLM(), model="model-a"))
    other = Library(tmp_path / "library.db", clock=clock, embed_model="model-b")
    try:
        llm = FakeLLM()
        assert run(other.search("Wrathgate", k=1, llm=llm))[0].kind == "lore"
        assert llm.calls == []  # vectors from model-a are never compared with model-b
        with pytest.raises(EmbeddingMismatch):
            run(other.embed_pending(llm, model="model-b"))
    finally:
        other.close()


def test_rebuild_keeps_embeddings_of_unchanged_chunks(lib):
    run(lib.embed_pending(FakeLLM()))
    changed = ChunkDraft("wiki:3:0", "lore", "Battle of Angrathar the Wrathgate",
                         "Rewritten text about the Wrathgate.", license=LICENSE)
    new = ChunkDraft("wiki:9:0", "event", "Brewfest", "Brewfest is an autumn festival.",
                     license=LICENSE)
    src = wiki_source()
    src.chunks = [src.chunks[0], changed, new]  # wiki:2:0 is gone
    (rep,) = lib.build([src])
    assert (rep.unchanged, rep.updated, rep.inserted, rep.deleted) == (1, 1, 1, 1)
    rows = {r["key"]: r for r in lib.conn.execute("SELECT key, embedding FROM chunks")}
    assert rows["wiki:1:0"]["embedding"] is not None
    assert rows["wiki:3:0"]["embedding"] is None and "wiki:2:0" not in rows
    assert lib.pending_embeddings() == 2
    vec_ids = {r[0] for r in lib.conn.execute("SELECT chunk_id FROM chunks_vec")}
    ids = {r[0] for r in lib.conn.execute("SELECT id FROM chunks WHERE embedding IS NOT NULL")}
    assert vec_ids == ids


def test_lookup_item_by_id_name_link_and_words(lib):
    assert [c.text.split("item level ")[1][:3] for c in lib.lookup_item("bryntroll, the bone "
                                                                         "arbiter")] == ["277",
                                                                                         "264"]
    assert lib.lookup_item(50415)[0].section == "epic two-hand axe"
    assert lib.lookup_item("50709")[0].text.count("277") == 1
    link = "|cffa335ee|Hitem:50415:0:0:0:0:0:0:0:80|h[Bryntroll, the Bone Arbiter]|h|r"
    assert lib.lookup_item(link)[0].ref_ids == ("item:50415",)
    assert lib.lookup_item("[Emblem of Frost]")[0].title == "Emblem of Frost"
    assert {c.title for c in lib.lookup_item("bryntroll arbiter")} == {
        "Bryntroll, the Bone Arbiter"}
    assert lib.lookup_item("no such thing") == [] and lib.lookup_item(1) == []


def test_drops_from_world_first_then_atlasloot(lib):
    hits = lib.drops_from("sindragosa")
    assert [(h.source, h.section) for h in hits] == [("world", "10-player"),
                                                     ("atlasloot", "25-player")]
    assert lib.drops_from("Marrowgar")[0].title == "Lord Marrowgar"
    assert lib.drops_from("Nobody") == [] and lib.drops_from("  ") == []


def test_quest_chain_walks_back_and_forward(lib):
    chain = lib.quest_chain("the middle road")
    assert [c.title for c in chain] == ["A Grim Start", "The Middle Road", "The Frozen End"]
    assert [c.title for c in lib.quest_chain("Frozen End")][0] == "A Grim Start"
    assert lib.quest_chain("Unknown quest") == []


@pytest.mark.parametrize("query, who", [
    ("what drops from Sindragosa", "Sindragosa"),
    ("What does Sindragosa drop?", "Sindragosa"),
    ("which items does the Lich King drop in 25 heroic", "the Lich King"),
    ("loot from Lord Marrowgar", "Lord Marrowgar"),
    ("Sindragosa's loot table", "Sindragosa"),
    ("Sindragosa drops", "Sindragosa"),
    ("what drops from Sindragosa in ICC 25", "Sindragosa"),
    ("who is Sindragosa", None),
    ("Wrathgate", None),
    ("what drops from me", None),
])
def test_loot_questions(query, who):
    assert loot_target(query) == who


def test_loot_questions_go_to_drops_from_first(lib):
    hits = lib.search_text("what drops from Sindragosa", k=5)
    assert [(h.kind, h.source) for h in hits[:2]] == [("loot", "world"), ("loot", "atlasloot")]
    assert [h.score for h in hits[:2]] == [1.0, 1.0] and len(hits) == 5  # then the ranked rest
    assert len({h.id for h in hits}) == 5
    lore = lib.search_text("what drops from Sindragosa", kinds=["lore"])
    assert lore and all(h.kind == "lore" for h in lore)
    llm = FakeLLM()
    top = run(lib.search("Sindragosa loot table", k=1, llm=llm))
    assert (top[0].kind, top[0].source) == ("loot", "world") and llm.calls == []


def test_weights_scale_scores(tmp_path, clock):
    same = "Gift of Arthas: a potion that brews the plague."
    lib = Library(tmp_path / "library.db", clock=clock)
    try:
        lib.build([ListSource("world", [
            ChunkDraft("item:1", "item", "Gift of Arthas", same, weight=0.4),
            ChunkDraft("item:2", "item", "Gift of Arthas", same)])])
        hits = lib.search_text("Gift of Arthas", k=2)
        assert [h.score for h in hits] == [1.0, 0.4]
        assert hits[0].id > hits[1].id  # the later, heavier chunk wins the tie
    finally:
        lib.close()


def test_light_trade_goods_stay_below_the_biography(tmp_path, clock):
    bio = ("Arthas Menethil was the crown prince of Lordaeron, trained by Uther. He took up "
           "Frostmourne in Northrend, lost his soul and returned as a death knight. Later he "
           "merged with the Lich King on the Frozen Throne and led the Scourge against the "
           "living. Arthas fell at last to Tirion Fordring and the Ashen Verdict.")
    filler = [ChunkDraft(f"item:{i}", "item", f"Frost Lotus {i}", "Frost Lotus: a herb.",
                         weight=0.4) for i in range(10, 40)]

    def build(weight):
        lib = Library(tmp_path / f"w{weight}.db", clock=clock)
        lib.build([ListSource("world", [ChunkDraft("item:1", "item", "Arthas' Tears",
                                                   "Arthas' Tears: common trade good.",
                                                   weight=weight), *filler]),
                   ListSource("wiki", [ChunkDraft("wiki:1:0", "lore", "Arthas Menethil", bio,
                                                  license=LICENSE)])])
        return lib
    heavy, light = build(1.0), build(0.4)
    try:
        assert heavy.search_text("Arthas", k=1)[0].kind == "item"  # BM25 favours short chunks
        assert [c.kind for c in light.search_text("Arthas", k=2)] == ["lore", "item"]
    finally:
        heavy.close()
        light.close()


def test_a_page_lead_answers_its_own_name(tmp_path, clock, monkeypatch):
    from leeroy_mind.library import core
    monkeypatch.setattr(core, "_CANDIDATES", 3)  # the lead is outside the plain top 3
    body = [ChunkDraft(f"wiki:7:{n}", "lore", "Arthas Menethil",
                       f"Arthas rode north in chapter {n}. Arthas burned the town and left.",
                       license=LICENSE) for n in range(1, 6)]
    lead = ChunkDraft("wiki:7:0", "lore", "Arthas Menethil",
                      "Arthas Menethil was the crown prince of Lordaeron, a paladin who took up "
                      "Frostmourne. Arthas became the Lich King.", license=LICENSE)
    other = ChunkDraft("wiki:8:0", "lore", "Frostmourne", "Arthas took up Frostmourne in "
                       "Northrend. Arthas lost his soul to it.", license=LICENSE)
    lib = Library(tmp_path / "library.db", clock=clock)
    try:
        lib.build([ListSource("wiki", [*body, other, lead])])
        assert lib.search_text("Arthas", k=1)[0].text.startswith("Arthas Menethil was the crown")
        # the bonus needs every query word in the title: "Frostmourne" is not about "Arthas"
        assert lib.search_text("Frostmourne", k=1)[0].title == "Frostmourne"
    finally:
        lib.close()


def test_a_weight_change_keeps_the_embedding(lib):
    run(lib.embed_pending(FakeLLM()))
    src = wiki_source()
    src.chunks = [replace(src.chunks[0], weight=0.5), *src.chunks[1:]]
    (rep,) = lib.build([src])
    assert (rep.updated, rep.unchanged) == (1, 2)
    row = lib.conn.execute("SELECT weight, embedding FROM chunks WHERE key = 'wiki:1:0'").fetchone()
    assert row["weight"] == 0.5 and row["embedding"] is not None


def test_a_v1_library_gains_weights(tmp_path, clock):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.executescript((MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO chunks(source, key, kind, title, text, t_created) "
                 "VALUES ('wiki', 'wiki:1:0', 'lore', 'Old Page', 'Some old text.', 0)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    lib = Library(path, clock=clock)
    try:
        assert lib.conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() == 3
        assert lib.search_text("old text")[0].title == "Old Page"
        with pytest.raises(sqlite3.IntegrityError):
            lib.conn.execute("UPDATE chunks SET weight = 0")
        lib.conn.rollback()
    finally:
        lib.close()


def test_a_v2_library_takes_curated_chunks_and_keeps_its_rows(tmp_path, clock):
    """v3 rebuilds the chunks table to let source 'curated' in: ids, weights, embeddings, refs
    and the FTS index all survive."""
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    for name in ("0001_init.sql", "0002_weight.sql"):
        conn.executescript((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
    vec = pack_vec([0.5] * 512)
    conn.execute("INSERT INTO chunks(id, source, key, kind, title, text, t_created, embedding, "
                 "weight) VALUES (7, 'wiki', 'wiki:1:0', 'lore', 'Old Page', 'Some old text.', 0, "
                 "?, 0.5)", (vec,))
    conn.execute("INSERT INTO chunk_ref(chunk_id, ref) VALUES (7, 'item:1')")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    lib = Library(path, clock=clock)
    try:
        assert lib.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        row = lib.conn.execute("SELECT id, weight, embedding FROM chunks").fetchone()
        assert (row["id"], row["weight"], row["embedding"]) == (7, 0.5, vec)
        assert [tuple(r) for r in lib.conn.execute("SELECT * FROM chunk_ref")] == [(7, "item:1")]
        assert lib.search_text("old text")[0].id == 7
        (rep,) = lib.build([ListSource("curated", [ChunkDraft(
            "curated:x", "event", "Darkmoon Faire", "The faire travels between three sites.")])])
        assert rep.inserted == 1
        hit = lib.search_text("faire travels")[0]
        assert hit.source == "curated" and "hand-written" in hit.cite()
        # it stands in for the page, so it gets a page lead's bonus over chunks that name it
        lib.build([ListSource("world", [ChunkDraft(
            "quest:9", "quest", "Darkmoon Cards", "Darkmoon Faire cards: Darkmoon Faire deck.",
            section="Darkmoon Faire")])])
        assert lib.search_text("Darkmoon Faire", k=1)[0].source == "curated"
        lib.conn.execute("DELETE FROM chunks WHERE id = 7")
        assert lib.search_text("old text") == []
        assert lib.conn.execute("SELECT count(*) FROM chunk_ref").fetchone()[0] == 0
        lib.conn.rollback()
    finally:
        lib.close()


def test_the_curated_source_checks_its_items_and_quests_against_the_world(tmp_path, clock):
    from leeroy_mind.library.curated import SUMMARIES, CuratedSource
    lib = Library(tmp_path / "library.db", clock=clock)
    try:
        res = CuratedSource().load(lib)
        assert [c.title for c in res.chunks] == ["Darkmoon Faire", "Dungeon Finder", "Heirloom"]
        assert all(len(c.text.split()) <= 250 for c in res.chunks)
        assert "not checked" in res.log[0]  # no world build yet
        cited = sorted({r for c in SUMMARIES for r in c.ref_ids
                        if r.startswith(("item:", "quest:"))})
        world = ListSource("world", [], items=[ItemRow(int(r[5:]), "x", 1, 1, 1)
                                               for r in cited if r.startswith("item:")
                                               and r != "item:19182"],
                           quests=[QuestRow(int(r[6:]), "q", 1) for r in cited
                                   if r.startswith("quest:")])
        lib.build([world])
        res = CuratedSource().load(lib)
        assert "cited but not in the world tables: item:19182" in res.log
        assert f"{len(cited) - 1} of {len(cited)} cited items and quests are in the world " \
               "tables" in res.log
        assert lib.known_refs(["npc:1", "item:19182", "quest:7905"]) == {"quest:7905"}
    finally:
        lib.close()
