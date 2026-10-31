import gzip
import json
import urllib.parse
from pathlib import Path

import pytest

from leeroy_mind.library.wiki import (
    BATCH, LICENSE, QUOTE_WEIGHT, USER_AGENT, PlanEntry, WikiClient, WikiError, WikiSource,
    _merge_small, build_plan, cache_name, normalize_title, page_categories, page_chunks,
    page_content, page_url)

FIXTURES = Path(__file__).parent / "fixtures" / "library"


def _page(title, text, cats=()):
    return {"pageid": abs(hash(title)) % 10**6, "ns": 0, "title": title,
            "revisions": [{"revid": 1, "timestamp": "2026-01-01T00:00:00Z",
                           "slots": {"main": {"contentmodel": "wikitext", "content": text}}}],
            "categories": [{"ns": 14, "title": c} for c in cats]}


class FakeWiki:
    """Answers query requests from a dict of title -> wikitext, like api.php would."""

    def __init__(self, pages=None, redirects=None, categories=None, fail=()):
        self.pages = dict(pages or {})
        self.redirects = dict(redirects or {})
        self.categories = dict(categories or {})
        self.fail = list(fail)
        self.calls: list[dict] = []
        self.headers: list[dict] = []

    def __call__(self, url, headers, timeout):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.calls.append(params)
        self.headers.append(headers)
        if self.fail:
            return self.fail.pop(0)
        if params.get("list") == "categorymembers":
            members = self.categories.get(params["cmtitle"], [])
            ns = {int(n) for n in params["cmnamespace"].split("|")}
            body = {"query": {"categorymembers": [m for m in members if m["ns"] in ns]}}
            return 200, {}, json.dumps(body).encode()
        titles = params["titles"].split("|")
        q: dict = {"pages": []}
        redirects = []
        for t in titles:
            target = self.redirects.get(t)
            if target:
                redirects.append({"from": t, "to": target})
                t = target
            if t in self.pages:
                text, cats = self.pages[t] if isinstance(self.pages[t], tuple) else                     (self.pages[t], ())
                q["pages"].append(_page(t, text, cats))
            else:
                q["pages"].append({"ns": 0, "title": t, "missing": True})
        if redirects:
            q["redirects"] = redirects
        return 200, {}, json.dumps({"batchcomplete": True, "query": q}).encode()


class Clock:
    def __init__(self):
        self.t = 100.0
        self.sleeps: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def client(tmp_path, http, **kw):
    c = Clock()
    return WikiClient(tmp_path / "wiki", http=http, sleep=c.sleep, clock=c.now, **kw), c


def test_titles_and_urls():
    assert normalize_title("  icecrown_citadel  (instance) ") == "Icecrown citadel (instance)"
    assert page_url("Kel'Thuzad (tactics)") == "https://warcraft.wiki.gg/wiki/Kel'Thuzad_(tactics)"
    assert cache_name("A/B") != cache_name("A B") and cache_name("A/B").endswith(".json")


def test_pages_are_batched_cached_and_never_refetched(tmp_path):
    titles = [f"Page {i}" for i in range(BATCH + 3)]
    http = FakeWiki({t: f"text of {t}" for t in titles if t != "Page 2"})
    c, _ = client(tmp_path, http)
    got = c.pages(titles)
    assert len(http.calls) == 2 and c.requests == 2
    assert got["Page 2"] is None and page_content(got["Page 5"]) == "text of Page 5"
    assert http.headers[0]["User-Agent"] == USER_AGENT
    assert "(+https://github.com/raold/leeroy)" in USER_AGENT and "@" not in USER_AGENT
    assert http.calls[0]["maxlag"] == "5" and http.calls[0]["formatversion"] == "2"
    again = WikiClient(tmp_path / "wiki", http=http, offline=True).pages(titles)
    assert len(http.calls) == 2 and again.keys() == got.keys() and again["Page 2"] is None
    cached = json.loads(next((tmp_path / "wiki" / "pages").glob("Page_5-*.json")).read_text())
    assert cached["query"]["pages"][0]["title"] == "Page 5"


def test_redirects_resolve_and_cache(tmp_path):
    http = FakeWiki({"Storm Peaks": "peaks"}, redirects={"The Storm Peaks": "Storm Peaks"})
    c, _ = client(tmp_path, http)
    assert c.pages(["The Storm Peaks"])["The Storm Peaks"]["title"] == "Storm Peaks"
    assert c.pages(["The Storm Peaks", "Storm Peaks"]) and len(http.calls) == 2
    assert c.pages(["Storm Peaks"])["Storm Peaks"]["title"] == "Storm Peaks"
    assert len(http.calls) == 2


def test_requests_are_at_least_one_second_apart(tmp_path):
    http = FakeWiki({f"P{i}": "x" for i in range(3 * BATCH)})
    c, clock = client(tmp_path, http)
    c.pages([f"P{i}" for i in range(3 * BATCH)])
    assert len(http.calls) == 3
    assert clock.sleeps == [pytest.approx(1.0)] * 2


def test_maxlag_and_server_errors_back_off_then_succeed(tmp_path):
    maxlag = (200, {"retry-after": "7"},
              json.dumps({"error": {"code": "maxlag", "lag": 9, "info": "lagged"}}).encode())
    busy = (503, {}, b"<html>busy</html>")
    gz = (200, {"content-encoding": "gzip"}, gzip.compress(json.dumps(
        {"query": {"pages": [_page("A", "alpha")]}}).encode()))
    http = FakeWiki(fail=[maxlag, busy, gz])
    c, clock = client(tmp_path, http)
    assert page_content(c.pages(["A"])["A"]) == "alpha"
    assert clock.sleeps == [7.0, 10.0]  # Retry-After for maxlag, then the doubled backoff


def test_gives_up_and_raises_on_api_errors(tmp_path):
    c, _ = client(tmp_path, FakeWiki(fail=[(503, {}, b"")] * 3), retries=2)
    with pytest.raises(WikiError, match="gave up"):
        c.pages(["A"])
    bad = (200, {}, json.dumps({"error": {"code": "badvalue", "info": "nope"}}).encode())
    c, _ = client(tmp_path, FakeWiki(fail=[bad]))
    with pytest.raises(WikiError, match="badvalue"):
        c.pages(["A"])


def test_offline_never_calls_http(tmp_path):
    def boom(*a):
        raise AssertionError("no network offline")
    c = WikiClient(tmp_path / "wiki", http=boom, offline=True)
    assert c.pages(["Anything"]) == {} and c.category_members("Category:X") == []


def test_fixture_page_has_the_api_shape():
    data = json.loads((FIXTURES / "wiki_page.json").read_text(encoding="utf-8"))
    page = data["query"]["pages"][0]
    assert "Sindragosa" in page_content(page)
    assert "Category:Bosses" in page_categories(page)


def test_plan_tiers_dedupe_filter_and_cap(tmp_path):
    cats = {
        "Category:Wrath of the Lich King instances": [
            {"ns": 14, "title": "Category:Icecrown Citadel"}],
        "Category:Icecrown Citadel": [
            {"ns": 0, "title": "Icecrown Citadel (instance)"},
            {"ns": 0, "title": "Icecrown Gunship Battle"},
            {"ns": 0, "title": "Icecrown Citadel loot"},
            {"ns": 14, "title": "Category:Icecrown Citadel NPCs"}],
        "Category:Icecrown Citadel NPCs": [
            {"ns": 0, "title": "Sindragosa"}, {"ns": 0, "title": "Frostwarden Handler"},
            {"ns": 0, "title": "Rotface"}],
        "Category:Bosses": [{"ns": 0, "title": t} for t in (
            "Sindragosa (tactics)", "Rotface", "Ragnaros", "Lord Marrowgar", "Blood Prince Council",
            "Lich King (tactics)", "Onyxia (Classic)", "Kel'Thuzad (Sanctum of Domination tactics)",
            "Fel Lord Betrug", "Frost King Malakk")],
        "Category:Holidays": [{"ns": 0, "title": "Brewfest"}, {"ns": 0, "title": "Moonkin Day"}],
        "Category:Borean Tundra": [{"ns": 0, "title": "Valiance Keep"},
                                   {"ns": 0, "title": "Taunka'le Village"}],
    }
    icc = ("Intro.\n===Bosses and encounters===\n;[[Lower Spire|The Lower Spire]]\n"
           "*{{RaceIcon|BoneWraith}} [[Lord Marrowgar]]\n*{{NPC||Blood Prince Council}}\n"
           "*[[Lich King (tactics)|The Lich King]], the last boss.\n"
           "====Boss support====\n*[[Frostwarden Sentry]]\n"
           "===NPCs, bosses and monsters===\n*[[Alchemist Finklestein]] [[Onyxia (Classic)]] "
           "[[Kel'Thuzad (Sanctum of Domination tactics)]]\n"
           "===Legion===\n*[[Fel Lord Betrug]] came later.\n==In the RPG==\n[[Frost King Malakk]]\n")
    c, _ = client(tmp_path, FakeWiki({"Icecrown Citadel (instance)": icc}, categories=cats))
    plan, skipped = build_plan(c, cap=100000)
    by = {e.title: e for e in plan}
    assert by["Icecrown Citadel (instance)"].tier == 0
    assert by["Icecrown Gunship Battle"] == PlanEntry(
        "Icecrown Gunship Battle", "lore", 1, "in Category:Icecrown Citadel", "Icecrown Citadel")
    assert by["Icecrown Citadel (instance)"].instance == "Icecrown Citadel"
    assert by["Lord Marrowgar"].instance == "Icecrown Citadel" and by["Brewfest"].instance is None
    assert by["Sindragosa"].tier == 0 and by["Sindragosa (tactics)"].kind == "strategy"
    assert by["Lord Marrowgar"].tier == 2 and "Lord Marrowgar (tactics)" not in by
    assert by["Lich King (tactics)"].kind == "strategy" and by["Lich King"].tier == 0
    assert by["Blood Prince Council"].tier == 2 and by["Rotface"].kind == "lore"
    assert "Frostwarden Handler" not in by and "Ragnaros" not in by
    assert "Alchemist Finklestein" not in by and "Frostwarden Sentry" not in by
    assert "Onyxia (Classic)" not in by and not any("Sanctum of Domination" in t for t in by)
    # bosses linked only from a later-era or off-topic section are not this instance's
    assert "Fel Lord Betrug" not in by and "Frost King Malakk" not in by
    assert by["Brewfest"].tier == 0 and by["Moonkin Day"].kind == "event"
    assert by["Taunka'le Village"].tier == 4 and by["Valiance Keep"].tier == 0
    assert by["Lord Marrowgar"].boss and by["Lich King"].boss and by["Sindragosa"].boss
    # a boss's tactics page is the boss too, linked (Lich King) or in the NPCs category
    assert by["Lich King (tactics)"].boss and by["Sindragosa (tactics)"].boss
    assert not by["Brewfest"].boss and not by["Icecrown Gunship Battle"].boss
    assert any("Icecrown Citadel loot" in s for s in skipped)
    small, skipped = build_plan(c, cap=10)
    assert len(small) == 10 and sum("over the cap" in s for s in skipped) == len(plan) - 10


def test_plan_takes_a_boss_from_its_own_category_or_the_bosses_section(tmp_path):
    """A prison page's inmates and an instance's history link bosses of other raids; only a
    boss whose own page names the instance, or one listed under Bosses, is that instance's."""
    vh = ("== History ==\n[[Argaloth]] and [[Kel'Thuzad (tactics)|Kel'Thuzad]] were held here.\n"
          "{{legion-section}}\n*{{NPC|mob|Shivermaw}}\n"
          "== Dungeon denizens ==\n=== Bosses ===\n*{{mob|Erekem}}\n*{{mob|Cyanigosa}}\n"
          "*[[Hogger (tactics)|Hogger]], whose memory appears\n")
    nexus = "== Bosses ==\n*[[Keristrasza]]\n== Background ==\n[[Malygos (tactics)|Malygos]]\n"
    eoe = "== Dungeon denizens ==\n*[[Malygos (tactics)|Malygos]]\n"
    naxx = "== Encounters ==\n*[[Kel'Thuzad (tactics)|Kel'Thuzad]]\n"
    pages = {
        "Violet Hold (instance)": vh, "Nexus (instance)": nexus, "Eye of Eternity": eoe,
        "Naxxramas": naxx,
        "Argaloth": ("A pit lord.", ["Category:Baradin Hold mobs", "Category:Bosses"]),
        "Shivermaw": ("A frost wyrm.", ["Category:Assault on Violet Hold mobs"]),
        "Cyanigosa": ("A blue dragon.", ["Category:Violet Hold mobs"]),
        "Kel'Thuzad (tactics)": ("Frost.", ["Category:Naxxramas mobs"]),
        "Malygos (tactics)": ("Arcane.", ["Category:Eye of Eternity mobs"]),
        "Hogger (tactics)": ("A gnoll.", ["Category:Stormwind Stockade mobs"]),
        "Keristrasza": ("A red dragon.", ["Category:Borean Tundra NPCs"]),
    }
    cats = {"Category:Bosses": [{"ns": 0, "title": t} for t in (
        "Argaloth", "Shivermaw", "Erekem", "Cyanigosa", "Kel'Thuzad (tactics)",
        "Malygos (tactics)", "Hogger (tactics)", "Keristrasza")]}
    c, _ = client(tmp_path, FakeWiki(pages, categories=cats))
    plan, skipped = build_plan(c, cap=100000)
    by = {e.title: e for e in plan}
    assert "Argaloth" not in by and "Shivermaw" not in by and "Hogger" not in by
    assert any("not a boss of Violet Hold: Argaloth (its categories name no WotLK instance)" == s
               for s in skipped)
    assert by["Erekem"].instance == "Violet Hold" and by["Erekem"].boss  # Bosses, not cached
    assert by["Cyanigosa"].instance == "Violet Hold"
    assert by["Keristrasza"].instance == "Nexus" and by["Keristrasza"].boss
    # the instance comes from the page's own category, not from whoever linked it first
    assert by["Malygos (tactics)"].instance == "Eye of Eternity"
    assert by["Kel'Thuzad (tactics)"].instance == "Naxxramas"
    assert by["Kel'Thuzad (tactics)"].why == "tactics for a boss linked from Naxxramas"


def _fixture_page():
    return json.loads((FIXTURES / "wiki_page.json").read_text(encoding="utf-8"))["query"]["pages"][0]


def test_page_chunks_carry_source_license_and_kinds():
    log: list[str] = []
    chunks = page_chunks(_fixture_page(), "lore", log, instance="Icecrown Citadel")
    assert [c.key for c in chunks] == [f"wiki:4242:{i}" for i in range(len(chunks))]
    assert all(c.url == page_url("Sindragosa") and c.license == LICENSE for c in chunks)
    assert all(c.instance == "Icecrown Citadel" and c.title == "Sindragosa" for c in chunks)
    strategy = [c for c in chunks if c.kind == "strategy"]
    assert len(strategy) == 1 and strategy[0].section == "Strategy"
    assert "Spread out" in strategy[0].text
    assert any("first consort of Malygos" in c.text for c in chunks if c.kind == "lore")
    assert not any("later-era" in c.text or "level 85" in c.text for c in chunks)
    assert any("later era" in line for line in log)


def test_later_era_and_disambiguation_pages_are_left_out():
    page = _fixture_page()
    later = dict(page, categories=[{"ns": 14, "title": "Category:Added in patch 7.0.3"}])
    log: list[str] = []
    assert page_chunks(later, "lore", log) == [] and "later era" in log[0]
    classic = dict(page, categories=[{"ns": 14, "title": "Category:Added in patch 3.4.1"},
                                     {"ns": 14, "title": "Category:Unique to Classic"}])
    assert page_chunks(classic, "lore", log) == [] and "Unique to Classic" in log[-1]
    scenario = _page("Deaths of Someone", "{{Infobox zone\n|level={{level|legion-max+2}}\n}}\n"
                     "A scenario.\n== Story ==\nIt happens later.\n")
    assert page_chunks(scenario, "zone", log) == [] and "levelled by a later" in log[-1]
    dab = dict(page, categories=[{"ns": 14, "title": "Category:Disambiguations"}])
    assert page_chunks(dab, "lore", log) == [] and "disambiguation" in log[-1]
    faire = _page("Darkmoon Faire", "The faire is on an island now.")
    assert page_chunks(faire, "event", log) == [] and "Darkmoon Island" in log[-1]
    # the Dungeon Finder and Heirloom pages describe later systems; curated summaries stand in
    for title, kind in (("Dungeon Finder", "event"), ("Heirloom", "item")):
        assert page_chunks(_page(title, "A system."), kind, log) == []
        assert log[-1].startswith(f"later era, left out: {title} (") and "curated" in log[-1]
    sod = _page("Naxxramas (Season of Discovery)", "A Classic season raid.\n== Bosses ==\nx\n")
    assert page_chunks(sod, "zone", log) == [] and "by its title" in log[-1]


def test_holiday_catalogs_are_left_out():
    """A holiday page's food, collectables and holiday items list what it sells today."""
    text = ("'''Fair''' is a holiday. " + _words(60, "fair") + "\n== Food and drink ==\n"
            "=== Alliance ===\n* Admiralty Ale\n== Collectables ==\n=== Pets ===\n* A pet\n"
            "== Holiday-only items ==\n* A hat\n== Quests ==\n* Pick up the keg\n")
    log: list[str] = []
    plain = " ".join(c.text for c in page_chunks(_page("Fair", text), "event", log))
    assert "Admiralty Ale" not in plain and "A pet" not in plain and "A hat" not in plain
    assert "Pick up the keg" in plain
    assert "Fair: dropped section Food and drink: off-topic" in log
    lore = " ".join(c.text for c in page_chunks(_page("Fair", text), "lore", []))
    assert "A pet" in lore  # only holiday pages drop them


def _words(n: int, word: str) -> str:
    return " ".join(f"{word}{i}." if i % 10 == 9 else word for i in range(n))


def test_character_pages_drop_abilities_and_quests_unless_a_wrath_boss():
    text = ("{{Npcbox|name=Hero}}\n'''Hero''' is a knight of the Silver Hand. " + _words(60, "bio")
            + "\n== Abilities ==\n* Divine Storm: hits nearby foes.\n== Quests ==\n* A Quest\n"
            "== Objective of ==\n* Another Quest\n== Tactics ==\nHis guards hit for 60k.\n"
            "== Enemies ==\n=== A rival ===\nThey met much later.\n== Allies ==\n* A friend\n")
    log: list[str] = []
    plain = " ".join(c.text for c in page_chunks(_page("Hero", text), "lore", log))
    assert "Divine Storm" not in plain and "A Quest" not in plain and "Another" not in plain
    assert "60k" not in plain and "much later" not in plain and "A friend" not in plain
    assert any("Hero: dropped section Abilities: off-topic" == line for line in log)
    assert any("Hero: dropped section Tactics: off-topic" == line for line in log)
    boss = " ".join(c.text for c in page_chunks(_page("Hero", text), "lore", [], boss=True))
    assert "Divine Storm" in boss and "A Quest" in boss and "60k" in boss
    zone = "'''Keep''' is a fortress. " + _words(60, "z") + "\n== Quests ==\n* A Quest\n"
    assert "A Quest" in " ".join(c.text for c in page_chunks(_page("Keep", zone), "zone", []))


def test_mechanics_pages_lose_the_parts_that_describe_the_live_game():
    """The Talent page opens with Dragonflight's trees; a class page's Demon hunter section is
    Legion's class."""
    talent = ("One may swap talents whenever one rests at an inn. " + _words(60, "lead")
              + "\n== Gaining talents ==\nOne point a level.\n== History ==\nThree trees each, "
              "with points from level 10. " + _words(60, "h") + "\n")
    log: list[str] = []
    plain = " ".join(c.text for c in page_chunks(_page("Talent", talent), "class", log))
    assert "whenever one rests" not in plain and "One point" not in plain and "Three trees" in plain
    assert "Talent: dropped the lead: describes the live game" in log
    hero = ("A hero class starts higher. " + _words(60, "lead") + "\n== Hero classes ==\n"
            "=== Death knight ===\nRunes and runic power.\n=== Demon hunter ===\nFury.\n")
    plain = " ".join(c.text for c in page_chunks(_page("Hero class", hero), "class", []))
    assert "Runes and runic power" in plain and "Fury" not in plain
    # the paladin's stat priorities are today's; the warrior's ratings are 3.3.5's
    stats = ("'''X''' fights. " + _words(60, "lead") + "\n== Stats ==\nStrength is key. "
             + _words(60, "s") + "\n")
    assert "Strength is key" in " ".join(
        c.text for c in page_chunks(_page("Warrior", stats), "class", []))
    assert "Strength is key" not in " ".join(
        c.text for c in page_chunks(_page("Paladin", stats), "class", []))


def test_a_wrath_boss_keeps_its_tactics_page_and_a_later_raid_boss_does_not(tmp_path):
    """Through the plan and the load, as built: a Wrath boss's tactics page opens with an
    Npcbox and keeps its fight; a character whose tactics page is another era's raid loses the
    Tactics section of his own page."""
    icc = "== Bosses ==\n*[[Lich King (tactics)|The Lich King]]\n*[[Garrosh Hellscream]]\n"
    lk = ("{{Npcbox|name=The Lich King}}\nArthas waits atop the citadel as its last boss. "
          + _words(60, "lead") + "\n== Abilities ==\n* Defile: a growing pool of shadow.\n"
          "== Tactics ==\n=== Phase One ===\nTank him facing away from the raid. "
          + _words(60, "p") + "\n")
    garrosh = ("{{Npcbox|name=Garrosh Hellscream}}\nGarrosh Hellscream led the Warsong "
               "Offensive. " + _words(60, "bio") + "\n== Tactics ==\nHe is fought much later.\n")
    pages = {
        "Icecrown Citadel (instance)": (icc, ["Category:Icecrown Citadel"]),
        "Lich King (tactics)": (lk, ["Category:Icecrown Citadel mobs"]),
        "Lich King": ("A title worn by Ner'zhul and then Arthas. " + _words(60, "t"), []),
        "Garrosh Hellscream": (garrosh, ["Category:Icecrown Citadel NPCs"]),
        "Garrosh Hellscream (tactics)": ("== Abilities ==\nA later raid.",
                                         ["Category:Siege of Orgrimmar mobs"]),
    }
    cats = {"Category:Bosses": [{"ns": 0, "title": "Lich King (tactics)"},
                                {"ns": 0, "title": "Garrosh Hellscream (tactics)"}]}
    c, _ = client(tmp_path, FakeWiki(pages, categories=cats))
    res = WikiSource(c, cap=100000).load(None)
    text = {t: " ".join(ch.text for ch in res.chunks if ch.title == t)
            for t in {ch.title for ch in res.chunks}}
    assert "Tank him facing away" in text["Lich King (tactics)"]
    assert "Defile" in text["Lich King (tactics)"]
    assert not any(x.startswith("Lich King (tactics): dropped section") for x in res.log)
    assert "Warsong" in text["Garrosh Hellscream"] and "much later" not in text["Garrosh Hellscream"]
    assert "Garrosh Hellscream: dropped section Tactics: off-topic" in res.log
    assert "Garrosh Hellscream (tactics)" not in text
    assert "not a WotLK encounter, left out: Garrosh Hellscream (tactics)" in res.log


def test_quote_sections_get_their_own_lighter_chunks():
    text = ("'''Hero''' was a knight. " + _words(150, "bio") + "\n== Quotes ==\n"
            + "\n".join("{{Quote|" + _words(12, "q") + f"|Hero {i}}}}}" for i in range(5)))
    chunks = page_chunks(_page("Hero", text), "lore", [])
    assert [(c.kind, c.weight) for c in chunks] == [("lore", 1.0), ("lore", QUOTE_WEIGHT)]
    assert chunks[1].section == "Quotes" and '"q q' in chunks[1].text


def test_small_chunks_join_a_neighbour_of_their_kind():
    parts = [["lore", 1.0, "A", _words(150, "a")], ["lore", 0.6, "Quotes", _words(10, "q")],
             ["strategy", 1.0, "S", _words(10, "s")], ["lore", 1.0, "B", _words(30, "b")]]
    out = _merge_small(parts)
    assert [(k, w, label) for k, w, label, _ in out] == [
        ("lore", 1.0, "A; Quotes"), ("strategy", 1.0, "S"), ("lore", 1.0, "B")]
    assert out[0][3].endswith(_words(10, "q"))
    pair = _merge_small([["lore", 1.0, "X", _words(10, "x")], ["lore", 1.0, "Y", _words(80, "y")]])
    assert len(pair) == 1 and pair[0][2] == "X; Y" and pair[0][3].startswith("x x")


def test_wiki_source_online_then_offline(tmp_path):
    fixture_text = page_content(_fixture_page())
    icc = "===Bosses===\n*[[Sindragosa (tactics)|Sindragosa]]\n*[[Kalecgos (tactics)|Kalecgos]]\n"
    pages = {
        "Icecrown Citadel (instance)": (icc, ["Category:Icecrown Citadel"]),
        "Sindragosa": (fixture_text, ["Category:Bosses"]),
        "Sindragosa (tactics)": ("== Abilities ==\n" + "She breathes frost on the tank. " * 3,
                                 ["Category:Icecrown Citadel mobs"]),
        "Kalecgos (tactics)": ("== Abilities ==\nA Sunwell fight.",
                               ["Category:Sunwell Plateau mobs"]),
        "Kalecgos": ("A blue dragon.", []),
    }
    cats = {"Category:Bosses": [{"ns": 0, "title": "Sindragosa (tactics)"},
                                {"ns": 0, "title": "Kalecgos (tactics)"}]}
    c, _ = client(tmp_path, FakeWiki(pages, categories=cats))
    res = WikiSource(c, cap=100000).load(None)
    titles = {ch.title for ch in res.chunks}
    assert {"Sindragosa", "Sindragosa (tactics)", "Kalecgos"} <= titles
    assert "Kalecgos (tactics)" not in titles
    # its tactics page puts the fight in another raid, so the plan never takes it
    assert any("not a boss of Icecrown Citadel: Kalecgos" in x for x in res.log)
    tactics = [ch for ch in res.chunks if ch.title == "Sindragosa (tactics)"]
    assert tactics[0].kind == "strategy" and tactics[0].instance == "Icecrown Citadel"
    assert (tmp_path / "wiki" / "skipped.log").exists()

    def boom(*a):
        raise AssertionError("offline must not fetch")
    again = WikiSource(WikiClient(tmp_path / "wiki", http=boom, offline=True), cap=100000).load(None)
    assert [ch.key for ch in again.chunks] == [ch.key for ch in res.chunks]
