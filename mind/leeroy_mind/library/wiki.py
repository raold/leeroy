"""warcraft.wiki.gg through its MediaWiki API: a polite cached client and the plan of WotLK-era
pages the library reads.

Every request carries a descriptive User-Agent and maxlag, starts at least `min_interval` seconds
after the previous one and backs off on maxlag, 429 and 5xx. Pages and category listings are
cached as files, so a rerun never fetches the same thing twice. Wiki text is CC BY-SA and lives
only in that cache and in library.db, never in the repo."""
import gzip
import hashlib
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from leeroy_mind.library.chunks import (ChunkDraft, SourceResult, chunk_sections, join_labels,
                                        word_count)
from leeroy_mind.library.wikitext import (is_disambiguation, is_later_era, kept_wikitext,
                                          page_is_later_era, wikitext_to_sections)

log = logging.getLogger("leeroy_mind")

API_URL = "https://warcraft.wiki.gg/api.php"
PAGE_URL = "https://warcraft.wiki.gg/wiki/"
USER_AGENT = "leeroy-local-library/0.1 (+https://github.com/raold/leeroy)"
LICENSE = "CC BY-SA 4.0"  # the wiki's rightsinfo
BATCH = 20                # titles per content request
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_UNCACHED = object()

HttpGet = Callable[[str, dict, float], tuple[int, dict, bytes]]


class WikiError(Exception):
    pass


def urllib_get(url: str, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return e.code, hdrs, e.read() or b""


def normalize_title(title: str) -> str:
    t = " ".join(title.replace("_", " ").split())
    return t[:1].upper() + t[1:]


def page_url(title: str) -> str:
    return PAGE_URL + urllib.parse.quote(title.replace(" ", "_"), safe="():',!-.")


def cache_name(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60] or "page"
    return f"{slug}-{hashlib.sha1(title.encode('utf-8')).hexdigest()[:10]}.json"


def _retry_after(headers: dict) -> float | None:
    try:
        return float(headers.get("retry-after", ""))
    except ValueError:
        return None


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class WikiClient:
    """Cache layout under `cache_dir`: pages/<name>.json holds one page in the API's own JSON
    shape ({"query": {"pages": [page]}}), titles/<name>.json maps a requested title to the page
    it resolved to (null when missing), categories/<name>.json holds a full member listing."""

    def __init__(self, cache_dir: Path, *, http: HttpGet = urllib_get, min_interval: float = 1.0,
                 maxlag: int = 5, retries: int = 6, timeout: float = 60.0, offline: bool = False,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.cache = Path(cache_dir)
        self.offline = offline
        self.requests = 0
        self._http = http
        self._min_interval = min_interval
        self._maxlag = maxlag
        self._retries = retries
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    # -- HTTP --------------------------------------------------------------------------

    def _wait_turn(self) -> None:
        if self._last is not None:
            gap = self._last + self._min_interval - self._clock()
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()

    def api(self, params: dict) -> dict:
        if self.offline:
            raise WikiError("offline: the request is not in the cache")
        query = dict(params, format="json", formatversion="2", maxlag=str(self._maxlag))
        url = API_URL + "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip",
                   "Accept": "application/json"}
        backoff = 5.0
        for attempt in range(self._retries + 1):
            self._wait_turn()
            self.requests += 1
            try:
                status, hdrs, body = self._http(url, headers, self._timeout)
                if hdrs.get("content-encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
            except (OSError, http.client.HTTPException, EOFError) as e:
                wait, why = backoff, e.__class__.__name__
            else:
                data = None
                try:
                    data = json.loads(body.decode("utf-8"))
                except ValueError:
                    if status == 200:
                        raise WikiError("reply is not JSON") from None
                err = data.get("error") if isinstance(data, dict) else None
                if status == 200 and err is None and isinstance(data, dict):
                    return data
                if isinstance(err, dict) and err.get("code") == "maxlag":
                    wait = max(_retry_after(hdrs) or 0.0, float(self._maxlag))
                    why = f"maxlag ({err.get('lag')} s)"
                elif status in _RETRY_STATUS:
                    wait, why = max(_retry_after(hdrs) or 0.0, backoff), f"HTTP {status}"
                elif isinstance(err, dict):
                    raise WikiError(f"API error {err.get('code')}: {str(err.get('info'))[:200]}")
                else:
                    raise WikiError(f"HTTP {status}")
            if attempt == self._retries:
                raise WikiError(f"gave up after {attempt + 1} tries: {why}")
            log.warning("wiki: %s, retrying in %.0f s", why, wait)
            self._sleep(wait)
            backoff = min(backoff * 2, 120.0)
        raise AssertionError("unreachable")

    # -- pages -------------------------------------------------------------------------

    def _title_file(self, title: str) -> Path:
        return self.cache / "titles" / cache_name(title)

    def _page_file(self, title: str) -> Path:
        return self.cache / "pages" / cache_name(title)

    def cached(self, title: str):
        """The cached page for `title`, None if it is cached as missing, _UNCACHED otherwise."""
        tf = self._title_file(normalize_title(title))
        if not tf.exists():
            return _UNCACHED
        resolved = json.loads(tf.read_text(encoding="utf-8")).get("title")
        if resolved is None:
            return None
        pf = self._page_file(resolved)
        if not pf.exists():
            return _UNCACHED
        return json.loads(pf.read_text(encoding="utf-8"))["query"]["pages"][0]

    def pages(self, titles, *, progress: Callable[[str], None] | None = None
              ) -> dict[str, dict | None]:
        """Requested title (normalized) -> page object with content, or None when the wiki has
        no such page. Uncached titles are fetched BATCH at a time; offline, they are left out."""
        out: dict[str, dict | None] = {}
        todo = []
        for t in dict.fromkeys(normalize_title(t) for t in titles if t.strip()):
            hit = self.cached(t)
            if hit is _UNCACHED:
                todo.append(t)
            else:
                out[t] = hit
        if self.offline:
            for t in todo:
                log.info("wiki: offline, %s is not cached", t)
            return out
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            out.update(self._fetch_batch(batch))
            if progress is not None:
                progress(f"fetched {min(i + BATCH, len(todo))}/{len(todo)} uncached titles "
                         f"({self.requests} requests)")
        return out

    def _fetch_batch(self, batch: list[str]) -> dict[str, dict | None]:
        params = {"action": "query", "prop": "revisions|categories",
                  "rvprop": "content|ids|timestamp", "rvslots": "main", "cllimit": "max",
                  "redirects": "1", "titles": "|".join(batch)}
        by_title: dict[str, dict] = {}
        norm: dict[str, str] = {}
        redir: dict[str, tuple[str, str | None]] = {}
        cont: dict = {}
        for _ in range(50):
            data = self.api({**params, **cont})
            q = data.get("query", {})
            norm.update({n["from"]: n["to"] for n in q.get("normalized", [])})
            redir.update({r["from"]: (r["to"], r.get("tofragment")) for r in q.get("redirects", [])})
            for p in q.get("pages", []):
                have = by_title.setdefault(p["title"], {k: v for k, v in p.items()
                                                        if k not in ("categories", "revisions")})
                if p.get("revisions") and not have.get("revisions"):
                    have["revisions"] = p["revisions"]
                if p.get("categories"):
                    have.setdefault("categories", []).extend(p["categories"])
            if "continue" not in data:
                break
            cont = data["continue"]
        out: dict[str, dict | None] = {}
        for t in batch:
            cur, frag, seen = norm.get(t, t), None, set()
            while cur in redir and cur not in seen:
                seen.add(cur)
                cur, frag = redir[cur]
            page = by_title.get(cur)
            if page is None or page.get("missing") or page.get("invalid") or not page.get("revisions"):
                _write_json(self._title_file(t), {"requested": t, "title": None})
                out[t] = None
                continue
            _write_json(self._page_file(page["title"]), {"query": {"pages": [page]}})
            _write_json(self._title_file(t), {"requested": t, "title": page["title"],
                                              "fragment": frag})
            out[t] = page
        return out

    # -- categories --------------------------------------------------------------------

    def category_members(self, category: str, namespaces=(0, 14)) -> list[dict]:
        """Every member of a category as {"ns", "title", "pageid"}; [] for an unknown one."""
        cat = normalize_title(category if category.startswith("Category:")
                              else "Category:" + category)
        path = self.cache / "categories" / cache_name(cat)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["members"]
        if self.offline:
            log.info("wiki: offline, %s is not cached", cat)
            return []
        members: list[dict] = []
        cont: dict = {}
        for _ in range(200):
            data = self.api({"action": "query", "list": "categorymembers", "cmtitle": cat,
                             "cmlimit": "max", "cmprop": "ids|title",
                             "cmnamespace": "|".join(str(n) for n in namespaces), **cont})
            members += data.get("query", {}).get("categorymembers", [])
            if "continue" not in data:
                break
            cont = data["continue"]
        _write_json(path, {"category": cat, "members": members})
        return members


def page_content(page: dict) -> str:
    rev = (page.get("revisions") or [{}])[0]
    slot = rev.get("slots", {}).get("main", rev)
    return slot.get("content", "") or ""


def page_categories(page: dict) -> list[str]:
    return [c["title"] for c in page.get("categories", []) if isinstance(c, dict) and "title" in c]


# -- the plan --------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanEntry:
    title: str
    kind: str
    tier: int
    why: str
    instance: str | None = None
    boss: bool = False   # a WotLK instance's boss, or its tactics page


# The dungeon pages themselves: "Violet Hold" is the Dalaran prison (its inmates span every
# expansion) and "Culling of Stratholme" the Third War event; both stay as seeds below.
INSTANCE_PAGES = (
    "Utgarde Keep (instance)", "Nexus (instance)", "Azjol-Nerub (instance)",
    "Ahn'kahet: The Old Kingdom", "Drak'Tharon Keep", "Violet Hold (instance)", "Gundrak",
    "Halls of Stone", "Halls of Lightning", "Culling of Stratholme (instance)", "Oculus",
    "Utgarde Pinnacle", "Trial of the Champion", "Forge of Souls", "Pit of Saron",
    "Halls of Reflection", "Naxxramas", "Obsidian Sanctum", "Eye of Eternity",
    "Vault of Archavon", "Ulduar (instance)", "Trial of the Crusader", "Onyxia's Lair",
    "Icecrown Citadel (instance)", "Ruby Sanctum")

SEEDS: dict[str, tuple[str, ...]] = {
    "zone": INSTANCE_PAGES + (
        "Northrend", "Borean Tundra", "Howling Fjord", "Dragonblight", "Grizzly Hills",
        "Zul'Drak", "Sholazar Basin", "Crystalsong Forest", "Storm Peaks", "Icecrown",
        "Wintergrasp", "Hrothgar's Landing", "Dalaran", "Coldarra", "Wyrmrest Temple",
        "Ulduar", "Azjol-Nerub", "Icecrown Citadel", "Argent Tournament Grounds",
        "Acherus: The Ebon Hold", "Plaguelands: The Scarlet Enclave", "Light's Hope Chapel",
        "Angrathar the Wrathgate", "Undercity", "Valiance Keep", "Warsong Hold",
        "Vengeance Landing", "Valgarde", "Wintergarde Keep", "Agmar's Hammer", "K3",
        "Dun Niffelem", "Crusaders' Coliseum", "Frozen Halls", "Strand of the Ancients",
        "Isle of Conquest", "Violet Hold"),
    "lore": (
        "World of Warcraft: Wrath of the Lich King", "Culling of Stratholme", "Arthas Menethil",
        "Lich King",
        "Ner'zhul", "Bolvar Fordragon", "Tirion Fordring", "Darion Mograine",
        "Jaina Proudmoore", "Thrall", "Sylvanas Windrunner", "Varian Wrynn",
        "Garrosh Hellscream", "Varimathras", "Grand Apothecary Putress", "Varok Saurfang",
        "Dranosh Saurfang", "Muradin Bronzebeard", "Brann Bronzebeard", "Alexstrasza",
        "Korialstrasz", "Malygos", "Kalecgos", "Ysera", "Nozdormu", "Rhonin",
        "Vereesa Windrunner", "Yogg-Saron", "Algalon the Observer", "Loken", "Thorim",
        "Hodir", "Freya", "Mimiron", "Kel'Thuzad", "Anub'arak", "Sindragosa",
        "Lady Deathwhisper", "Professor Putricide", "Blood-Queen Lana'thel", "Prince Valanar",
        "Prince Keleseth", "Prince Taldaram", "Halion", "Onyxia", "Uther the Lightbringer",
        "Falric", "Marwyn", "Koltira Deathweaver", "Thassarian", "King Ymiron",
        "Ingvar the Plunderer", "Mal'Ganis", "Crok Scourgebane", "Alexandros Mograine",
        "Sartharion", "Highlord Bolvar Fordragon", "Frostmourne", "Helm of Domination",
        "Frozen Throne", "Scourge", "Cult of the Damned", "San'layn", "Vrykul", "Taunka",
        "Tuskarr", "Nerubian", "Iron dwarf", "Titan", "Keepers", "Old Gods",
        "Blue dragonflight", "Red dragonflight", "Nexus War", "Ashbringer", "Argent Dawn",
        "Scarlet Onslaught", "Death knight (lore)", "Battle of Angrathar the Wrathgate",
        "Battle for the Undercity", "Argent Tournament", "Zombie Infestation",
        "Scourge Invasion", "Assault on Icecrown Citadel", "War against the Lich King",
        "Patch 3.0.2", "Patch 3.1.0", "Patch 3.2.0", "Patch 3.3.0", "Patch 3.3.5",
        "Alliance Vanguard", "Valiance Expedition", "Explorers' League", "Silver Covenant",
        "Frostborn", "Horde Expedition", "Warsong Offensive", "Hand of Vengeance",
        "Sunreavers", "Argent Crusade", "Kirin Tor", "Knights of the Ebon Blade", "Kalu'ak",
        "Frenzyheart Tribe", "Oracles", "Sons of Hodir", "Wyrmrest Accord", "Ashen Verdict",
        "Winterfin Retreat", "Kael'thas Sunstrider", "Lady Vashj", "Nefarian", "Anduin Wrynn"),
    "class": (
        "Death knight", "Druid", "Hunter", "Mage", "Paladin", "Priest", "Rogue", "Shaman",
        "Warlock", "Warrior", "Talent", "Dual Talent Specialization", "Hero class",
        "Class role", "Glyph", "Rune", "Runic power"),
    "event": (
        "Brewfest", "Hallow's End", "Feast of Winter Veil", "Love is in the Air", "Noblegarden",
        "Children's Week", "Midsummer Fire Festival", "Pilgrim's Bounty", "Lunar Festival",
        "Harvest Festival", "Day of the Dead", "Pirates' Day", "Darkmoon Faire",
        "Stranglethorn Fishing Extravaganza", "Kalu'ak Fishing Derby", "Lake Wintergrasp",
        "Dungeon Finder", "Glory of the Hero", "Glory of the Raider",
        "Glory of the Ulduar Raider", "Glory of the Icecrown Raider"),
    "item": (
        "Shadowmourne", "Val'anyr, Hammer of Ancient Kings", "Emblem of Frost",
        "Emblem of Triumph", "Emblem of Conquest", "Emblem of Valor", "Emblem of Heroism",
        "Tier 7", "Tier 8", "Tier 9", "Tier 10", "Heirloom"),
}

INSTANCE_CATEGORY = "Category:Wrath of the Lich King instances"
BOSS_CATEGORY = "Category:Bosses"
HOLIDAY_CATEGORY = "Category:Holidays"
ZONE_CATEGORIES = (
    "Borean Tundra", "Howling Fjord", "Dragonblight", "Grizzly Hills", "Zul'Drak",
    "Sholazar Basin", "Crystalsong Forest", "Storm Peaks", "Icecrown", "Wintergrasp",
    "Hrothgar's Landing", "Azjol-Nerub")
_NOT_CONTENT = re.compile(
    r"^(?:Category|File|Template|User|Help|Talk|Module|Warcraft Wiki|Image):|"
    r" (?:NPCs|quests|objects|loot|items|mobs|achievements|image requests)$")
_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_NPC_TEMPLATE = re.compile(r"\{\{NPC\|[^|}]*\|([^|}]+)")
_MOB_TEMPLATE = re.compile(r"\{\{\s*[Mm]ob\s*\|([^|}]+)")
_QUALIFIED = re.compile(r"^(.*?) \(([^()]*)\)$")
_HEADING_LINE = re.compile(r"^(={1,6})\s*(.*?)\s*\1\s*$")
_BOSS_SECTION = re.compile(r"\b(?:boss(?:es)?|encounters?)\b", re.IGNORECASE)
_OWN_CATEGORY = re.compile(r"^Category:(.+) (?:mobs|NPCs)$")


def wotlk_instance_names(client: WikiClient) -> frozenset[str]:
    """Instance names as the wiki's "<instance> mobs" and "<instance> NPCs" categories spell
    them: the instance pages and Category:Wrath of the Lich King instances, without
    " (instance)", with and without a leading "The"."""
    names = {t.removesuffix(" (instance)") for t in INSTANCE_PAGES}
    names |= {m["title"].removeprefix("Category:").removesuffix(" (instance)")
              for m in client.category_members(INSTANCE_CATEGORY, (14,))}
    return frozenset(names | {n.removeprefix("The ") for n in names})


def own_instances(page: dict | None, wotlk: frozenset[str]) -> list[str]:
    """The WotLK instances a page's own "<instance> mobs/NPCs" categories name."""
    if not page:
        return []
    found = [m.group(1) for c in page_categories(page) if (m := _OWN_CATEGORY.match(c))]
    return list(dict.fromkeys(x for x in found if x in wotlk))


def boss_pages(wikitext: str, instance: str, bosses: set[str]
               ) -> list[tuple[str, list[str], bool]]:
    """Every boss an instance page links, as (boss page, its tactics pages, linked from the
    page's Bosses or Encounters section): a link counts when Category:Bosses (`bosses`) lists
    it, its "(tactics)" page or its "(<instance> tactics)" page. Pages qualified for another
    raid or release ("(Classic)", "(X tactics)") are not this instance's boss and are left
    out."""
    name = instance.removesuffix(" (instance)")
    found: list[tuple[str, bool]] = []
    path: list[tuple[int, str]] = []
    for line in wikitext.split("\n"):
        m = _HEADING_LINE.match(line.strip())
        if m:
            level = len(m.group(1))
            path = [(lv, h) for lv, h in path if lv < level] + [(level, m.group(2))]
        in_section = any(_BOSS_SECTION.search(h) for _, h in path)
        for rx in (_LINK, _NPC_TEMPLATE, _MOB_TEMPLATE):
            found += [(x.group(1), in_section) for x in rx.finditer(line)]
    out: dict[str, tuple[list[str], list[bool]]] = {}
    for raw, in_section in found:
        t = normalize_title(raw)
        if not t or ":" in t.split(" ")[0]:
            continue
        m = _QUALIFIED.match(t)
        base, qual = (m.group(1), m.group(2)) if m else (t, None)
        if qual not in (None, "tactics", f"{name} tactics"):
            continue
        tactics = [x for x in (f"{base} (tactics)", f"{base} ({name} tactics)") if x in bosses]
        if base in bosses or tactics:
            have, sect = out.setdefault(base, ([], [False]))
            have += [x for x in tactics if x not in have]
            sect[0] = sect[0] or in_section
    return [(base, have, sect[0]) for base, (have, sect) in out.items()]


def build_plan(client: WikiClient, *, cap: int = 1500) -> tuple[list[PlanEntry], list[str]]:
    """The pages to read, most important first, and a log line for each one left out. Category
    listings come from the client (cached), so this costs only a few dozen requests once.

    A boss an instance page links joins the plan when its own page (or its tactics page) carries
    that instance's "mobs" or "NPCs" category, or when the link sits in the page's Bosses or
    Encounters section and its own categories name no other WotLK instance and do not place its
    fight elsewhere. A boss page's instance comes from its own category, and from the linking
    page only when it has none."""
    entries: list[PlanEntry] = []
    seen: set[str] = set()
    skipped: list[str] = []
    boss_titles: set[str] = set()

    def add(title: str, kind: str, tier: int, why: str, instance: str | None = None) -> None:
        t = normalize_title(title)
        if t in seen:
            return
        if _NOT_CONTENT.search(t):
            skipped.append(f"not content: {t} ({why})")
            seen.add(t)
            return
        seen.add(t)
        entries.append(PlanEntry(t, kind, tier, why, instance))

    for kind, titles in SEEDS.items():
        for t in titles:
            add(t, kind, 0, f"seed {kind}",
                t.removesuffix(" (instance)") if t in INSTANCE_PAGES else None)

    instance_cats = [m["title"] for m in client.category_members(INSTANCE_CATEGORY, (14,))]
    npc_cats: list[str] = []
    for cat in instance_cats:
        name = cat.removeprefix("Category:")
        for m in client.category_members(cat):
            if m["ns"] == 0:
                kind = "zone" if m["title"].endswith("(instance)") or m["title"] == name else "lore"
                add(m["title"], kind, 1, f"in {cat}", name.removesuffix(" (instance)"))
            elif m["title"].endswith(" NPCs"):
                npc_cats.append(m["title"])

    # Category:Bosses lists a boss's "(tactics)" page when it has one, else the boss page.
    bosses = {m["title"] for m in client.category_members(BOSS_CATEGORY, (0,))}
    wotlk = wotlk_instance_names(client)
    instance_pages = client.pages(INSTANCE_PAGES)
    linked: list[tuple[str, str, list[str], bool]] = []
    for inst in INSTANCE_PAGES:
        page = instance_pages.get(normalize_title(inst))
        if page is not None:
            linked += [(inst, base, tactics, in_section) for base, tactics, in_section
                       in boss_pages(kept_wikitext(page_content(page)), inst, bosses)]
    # The boss pages' own categories say where the fight is (they are planned anyway).
    own_pages = client.pages(list(dict.fromkeys(t for _, base, tactics, _ in linked
                                                for t in (base, *tactics))))

    def own(title: str) -> list[str]:
        return own_instances(own_pages.get(normalize_title(title)), wotlk)

    def fought_elsewhere(tactics: list[str]) -> bool:
        """Every tactics page puts the fight in a "<X> mobs" category that is not WotLK's."""
        cats = [page_categories(p) for t in tactics if (p := own_pages.get(normalize_title(t)))]
        return bool(cats) and all(any(c.endswith(" mobs") for c in cs) for cs in cats)

    for inst, base, tactics, in_section in linked:
        name = inst.removesuffix(" (instance)")
        mine = list(dict.fromkeys(x for t in (base, *tactics) for x in own(t)))
        if name not in mine and (not in_section or mine or fought_elsewhere(tactics)):
            where = ", ".join(mine) if mine else "no WotLK instance"
            skipped.append(f"not a boss of {name}: {base} (its categories name {where})")
            continue

        def instance_of(title: str) -> str:
            o = own(title)
            return name if name in o or not o else o[0]

        boss_titles.add(base)
        add(base, "lore", 2, f"boss linked from {inst}", instance_of(base))
        for t in tactics:
            boss_titles.add(normalize_title(t))
            add(t, "strategy", 2, f"tactics for a boss linked from {inst}", instance_of(t))
    for cat in npc_cats:
        name = cat.removeprefix("Category:").removesuffix(" NPCs").removesuffix(" (instance)")
        for m in client.category_members(cat, (0,)):
            t = m["title"]
            if f"{t} (tactics)" in bosses:
                boss_titles |= {normalize_title(t), normalize_title(f"{t} (tactics)")}
                add(t, "lore", 2, f"boss in {cat}", name)
                add(f"{t} (tactics)", "strategy", 2, f"tactics for a boss in {cat}", name)
            elif t in bosses:
                boss_titles.add(normalize_title(t))
                add(t, "lore", 2, f"boss in {cat}", name)

    for m in client.category_members(HOLIDAY_CATEGORY, (0,)):
        add(m["title"], "event", 3, f"in {HOLIDAY_CATEGORY}")

    for zone in ZONE_CATEGORIES:
        for m in client.category_members("Category:" + zone, (0,)):
            add(m["title"], "zone", 4, f"in Category:{zone}")
    for zone in ZONE_CATEGORIES:
        for m in client.category_members(f"Category:{zone} subzones", (0,)):
            add(m["title"], "zone", 5, f"in Category:{zone} subzones")

    entries = [replace(e, boss=True) if e.title in boss_titles else e for e in entries]
    for e in entries[cap:]:
        skipped.append(f"over the cap of {cap}: {e.title} (tier {e.tier}, {e.why})")
    return entries[:cap], skipped


# -- pages to chunks -----------------------------------------------------------------------

_STRATEGY_HEADING = re.compile(
    r"\b(?:abilities|strateg(?:y|ies)|tactics?|phases?|positioning|hard modes?|heroic mode|"
    r"heroic difficulty|tips|preparation|tanking|healing|raid setup|trash)\b", re.IGNORECASE)
_QUOTE_HEADING = re.compile(r"\b(?:quotes?|quotations?|dialogue|gossip|on-click|yells?)\b",
                            re.IGNORECASE)
_NPCBOX = re.compile(r"\{\{\s*Npcbox", re.IGNORECASE)
# A character who is not a Wrath boss lists abilities and quests from every expansion he
# appears in, with nothing to tell the eras apart (the world database has the quests anyway),
# fights from later raids (Sylvanas and Garrosh as Cataclysm's faction-leader raids) and
# enemies and allies from every era.
NPC_ONLY_HEADINGS = frozenset({"abilities", "quests", "objective of", "tactics", "enemies",
                               "allies"})
# A class page describes the class as it plays today (races, specs, gear, endgame): wrong for
# 3.3.5, so only its lore is kept. The patch pages carry the 3.x class changes.
CLASS_ONLY_HEADINGS = frozenset({
    "races", "racial abilities", "racial traits", "racial advantages", "overview", "abilities",
    "specializations", "pet specializations", "gear", "attribute conversions",
    "end-game expectations", "endgame expectations", "endgame", "pvp", "class overview",
    "creation screen description", "classic guide", "game guide", "organizations",
    "runeforging", "talents", "rotation", "stat priority", "demon hunter"})
# Pages written for the live game: these parts describe today's system, not 3.3.5's
# (Dragonflight's talent trees, Legion's cosmetic glyphs and class books, dual spec after its
# 4.0 price cut, the paladin's stat priorities with Mastery and Versatility). "(lead)" is the
# text before the first heading. The Warrior's "Stats" section is 3.3.5's (ratings at level 80).
LIVE_PARTS = {
    "Talent": frozenset({"(lead)", "gaining talents"}),
    "Glyph": frozenset({"(lead)", "glyphs by class", "class books"}),
    "Dual Talent Specialization": frozenset({"(lead)", "initial purchase", "talents and glyphs"}),
    "Paladin": frozenset({"stats"}),
}
# A holiday page's catalogs (food and drink, collectables, holiday-only items) list what the
# holiday sells today, later expansions' additions included; the world database has the 3.3.5
# vendors and items.
EVENT_ONLY_HEADINGS = frozenset({
    "food and drink", "collectables", "collectibles", "collectables and rewards",
    "holiday-only items", "garrison decorations", "cosmetics", "toys", "heirlooms"})
QUOTE_WEIGHT = 0.6   # quotes match a name often and say little about it
SMALL_WORDS = 40     # a chunk shorter than this joins a neighbour
# Pages that describe only the later rebuild of something Wrath had (a curated 3.3.5 summary,
# curated.py, stands in for each), or something later that no era category marks.
LATER_PAGES = {
    "Tishamaat": "a draenei holiday first held after the Dragon Isles (Dragonflight)",
    "Darkmoon Faire": "the faire as rebuilt on Darkmoon Island in patch 4.3; a curated 3.3.5 "
                      "summary stands in",
    "Dungeon Finder": "the Cataclysm-to-Pandaria finder (Justice and Valor Points, Scarlet Halls, "
                      "level-90 queues); a curated 3.3.5 summary stands in",
    "Heirloom": "heirlooms after their Cataclysm-to-Pandaria rework (guild vendors, 5.x "
                "heirlooms, level 85+ scaling); a curated 3.3.5 summary stands in",
}


def _merge_small(parts: list[list], min_words: int = SMALL_WORDS) -> list[list]:
    """parts are [kind, weight, label, text]. A part under min_words joins a neighbour of its
    own kind, one of its own weight too when there is one; with no such neighbour it stays."""
    parts = [list(p) for p in parts]
    i = 0
    while i < len(parts):
        prev = parts[i - 1] if i > 0 else None
        nxt = parts[i + 1] if i + 1 < len(parts) else None
        cur = parts[i]
        kin = [p for p in (prev, nxt) if p is not None and p[0] == cur[0]]
        if word_count(cur[3]) >= min_words or not kin:
            i += 1
            continue
        into = next((p for p in kin if p[1] == cur[1]), kin[0])
        parts.pop(i)
        if into is prev:
            into[2], into[3] = join_labels(into[2], cur[2]), into[3] + "\n" + cur[3]
        else:
            into[2], into[3] = join_labels(cur[2], into[2]), cur[3] + "\n" + into[3]
        i = max(0, i - 1)
    return parts


def page_chunks(page: dict, kind: str, log: list[str], *, instance: str | None = None,
                boss: bool = False) -> list[ChunkDraft]:
    """One wiki page as chunks of about 120-250 words, era-filtered. Boss and instance pages
    switch to kind "strategy" for their abilities, phases and tactics sections. On a character
    page that is not a Wrath boss or a Wrath boss's tactics page (`boss`), abilities, quests,
    tactics, enemies and allies are left out (NPC_ONLY_HEADINGS), and LIVE_PARTS go from the
    pages written for the live game. Quote sections make their own chunks at QUOTE_WEIGHT, and
    chunks under SMALL_WORDS join a neighbour."""
    title = page["title"]
    text = page_content(page)
    cats = page_categories(page)
    if title in LATER_PAGES:
        log.append(f"later era, left out: {title} ({LATER_PAGES[title]})")
        return []
    if is_later_era(title):  # "Naxxramas (Season of Discovery)" has no patch category
        log.append(f"later era, left out: {title} (by its title)")
        return []
    if page_is_later_era(cats, text):
        why = [c.removeprefix("Category:") for c in cats
               if c.startswith("Category:Added in") or c in ("Category:Unique to Classic",
                                                             "Category:Scenarios")]
        why_text = ", ".join(why) or "levelled by a later expansion"
        log.append(f"later era, left out: {title} ({why_text})")
        return []
    if is_disambiguation(text, cats):
        log.append(f"disambiguation page, left out: {title}")
        return []
    drop: set[str] = set()
    if _NPCBOX.search(re.split(r"\n==", text, maxsplit=1)[0]) and not boss:
        drop |= NPC_ONLY_HEADINGS
    if kind == "class":
        drop |= CLASS_ONLY_HEADINGS | {f"{title.lower()} variants"}
    if kind == "event":
        drop |= EVENT_ONLY_HEADINGS
    drop |= LIVE_PARTS.get(title, frozenset())
    if "(lead)" in drop:
        log.append(f"{title}: dropped the lead: describes the live game")
    dropped: list[str] = []
    sections = wikitext_to_sections(text, log=dropped, drop_headings=frozenset(drop),
                                    drop_lead="(lead)" in drop)
    log += [f"{title}: {d}" for d in dropped]
    runs: list[tuple[tuple[str, float], list]] = []
    for s in sections:
        k = kind
        if kind in ("lore", "zone") and any(_STRATEGY_HEADING.search(h) for h in s.path):
            k = "strategy"
        w = QUOTE_WEIGHT if any(_QUOTE_HEADING.search(h) for h in s.path) else 1.0
        if runs and runs[-1][0] == (k, w):
            runs[-1][1].append(s)
        else:
            runs.append(((k, w), [s]))
    parts = [[k, w, label, body] for (k, w), secs in runs
             for label, body in chunk_sections([(s.label, s.paragraphs) for s in secs])]
    pid = page.get("pageid") or cache_name(title).removesuffix(".json")
    out = [ChunkDraft(key=f"wiki:{pid}:{n}", kind=k, title=title, section=label, text=body,
                      url=page_url(title), license=LICENSE, instance=instance,
                      ref_ids=(f"wiki:{pid}",), weight=w)
           for n, (k, w, label, body) in enumerate(_merge_small(parts))]
    if not out:
        log.append(f"nothing left after cleanup: {title}")
    return out


class WikiSource:
    name = "wiki"

    def __init__(self, client: WikiClient, *, cap: int = 1500,
                 progress: Callable[[str], None] | None = None):
        self.client = client
        self.cap = cap
        self.progress = progress

    def load(self, library=None) -> SourceResult:
        plan, skipped = build_plan(self.client, cap=self.cap)
        pages = self.client.pages([e.title for e in plan], progress=self.progress)
        res = SourceResult(self.name, log=list(skipped))
        wotlk = wotlk_instance_names(self.client)
        # A bare "(tactics)" link can be another raid's fight (Sunwell's Kalecgos, Shadowlands'
        # Sylvanas): its "<instance> mobs" category tells. The character is then no Wrath boss.
        elsewhere = {pages[e.title]["title"] for e in plan
                     if e.tier == 2 and e.kind == "strategy" and pages.get(e.title)
                     and not own_instances(pages[e.title], wotlk)}
        seen: dict[str, str] = {}
        used = 0
        for e in plan:
            if e.title not in pages:
                res.log.append(f"not in the cache (offline): {e.title}")
                continue
            page = pages[e.title]
            if page is None:
                res.log.append(f"missing on the wiki: {e.title} ({e.why})")
                continue
            if page["title"] in seen:
                res.log.append(f"same page as {seen[page['title']]}: {e.title}")
                continue
            if page["title"] in elsewhere:
                res.log.append(f"not a WotLK encounter, left out: {page['title']}")
                continue
            seen[page["title"]] = e.title
            boss = e.boss and f"{page['title']} (tactics)" not in elsewhere
            chunks = page_chunks(page, e.kind, res.log, instance=e.instance, boss=boss)
            used += bool(chunks)
            res.chunks += chunks
        res.log.append(f"{used} wiki pages gave chunks ({len(plan)} planned, "
                       f"{self.client.requests} requests this run)")
        try:
            (self.client.cache / "skipped.log").write_text("\n".join(res.log) + "\n",
                                                           encoding="utf-8")
        except OSError:
            pass
        return res
