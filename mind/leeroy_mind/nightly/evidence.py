"""What the night shows a model (P1c Task 6): evidence items under aliases, the facts the
grounding rules check against, game events as text, retrieval that touches nothing, and the
content words several stages compare. Every date shown is in the mind clock's zone (ctx.tz),
never the host's, and no item text carries the owner's real name."""
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from functools import cached_property
from typing import TYPE_CHECKING, Iterable, Mapping

from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.recall import fts_query
from leeroy_mind.nightly.audience import cast_names
from leeroy_mind.nightly.knowledge import could_have_seen, knowers, parties, roots
from leeroy_mind.personas.screens import has_word, redact_real_name
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.storage.store import Store
from leeroy_mind.text import strip_wow_markup
from leeroy_mind.wow import BOSS_NAMES, boss_name

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_REF = re.compile(r"(memory|chat|event|session):([0-9]+)")
RRF_K = 60  # reciprocal rank fusion: a candidate scores sum(1 / (RRF_K + rank))

# Content words (moved here from Task 12, so Task 11's backstop needs no later task; re-review M7).
CONTENT_RE = re.compile(r"[a-z']{5,}")
STOPWORDS = frozenset(
    "about after again there their these those where which would could should think thing "
    "things really maybe gonna wanna today tonight night yeah thanks still never always raid "
    "raiding guild boss".split())


def content_words(text: str, names: Iterable[str]) -> set[str]:
    """The CONTENT_RE words of `text`, case-folded, less STOPWORDS and every word of `names`
    (the cast's and his names)."""
    named = {w for n in names for w in CONTENT_RE.findall(n.casefold())}
    return set(CONTENT_RE.findall(text.casefold())) - STOPWORDS - named


def _date(t: float, tz: tzinfo) -> str:
    """"Mon 26 Oct", in English whatever the host's locale."""
    d = datetime.fromtimestamp(t, tz)
    return f"{_DAYS[d.weekday()]} {d.day:02d} {_MONTHS[d.month - 1]}"


@dataclass(frozen=True)
class Item:
    """One piece of evidence. `actors` are the people it involves (grounding rules 5 and 6),
    `knowers` who knows what it rests on (rule 12), and `tier` what trim drops first: 0 for the
    who-knows-what items (never dropped), 1 for a digest, 2 for retrieved memories."""
    ref: str
    text: str
    actors: frozenset[int]
    t: float
    importance: int = 0
    valence: float = 0.0
    knowers: frozenset[int] = frozenset()
    tier: int = 1


class Evidence:
    """An ordered map from ref to Item, each under an alias 1..n, the number a model cites.
    Aliases are never reused, so one that trim dropped stays unknown and a claim citing it
    fails grounding rule 1."""

    def __init__(self) -> None:
        self._items: dict[str, Item] = {}
        self._aliases: dict[str, int] = {}
        self._refs: dict[int, str] = {}
        self._next = 1

    def add(self, item: Item) -> int:
        """The item's alias. A ref already here keeps its alias and its item, at the lower of
        the two tiers."""
        if item.ref in self._items:
            old = self._items[item.ref]
            if item.tier < old.tier:
                self._items[item.ref] = replace(old, tier=item.tier)
            return self._aliases[item.ref]
        alias, self._next = self._next, self._next + 1
        self._items[item.ref], self._aliases[item.ref], self._refs[alias] = item, alias, item.ref
        return alias

    def ref(self, alias: int) -> str:
        """The ref under `alias`, or "?{alias}" for an alias that is not (or no longer) here."""
        return self._refs.get(alias, f"?{alias}")

    def alias(self, ref: str) -> int | None:
        return self._aliases.get(ref)

    def items(self) -> list[Item]:
        """The items, in the order they were added."""
        return list(self._items.values())

    def __contains__(self, ref: object) -> bool:
        return ref in self._items

    def __getitem__(self, ref: str) -> Item:
        return self._items[ref]

    def __len__(self) -> int:
        return len(self._items)

    def line(self, ref: str, tz: tzinfo) -> str:
        """One item as a model sees it: "[n] (Mon 26 Oct) text"."""
        item = self._items[ref]
        return f"[{self._aliases[ref]}] ({_date(item.t, tz)}) {item.text}"

    def render(self, tz: tzinfo) -> str:
        return "\n".join(self.line(ref, tz) for ref in self._items)

    def trim(self, max_chars: int, tz: tzinfo) -> list[str]:
        """Drop items until render(tz) fits in `max_chars`: the highest tier first and, within a
        tier, the last added first; tier 0 is never dropped. Returns the dropped refs."""
        size = {ref: len(self.line(ref, tz)) for ref in self._items}
        total = sum(size.values()) + max(0, len(size) - 1)
        order = sorted((r for r, i in self._items.items() if i.tier > 0),
                       key=lambda r: (-self._items[r].tier, -self._aliases[r]))
        dropped = []
        for ref in order:
            if total <= max_chars:
                break
            total -= size[ref] + (1 if len(self._items) > 1 else 0)
            del self._items[ref]
            del self._refs[self._aliases.pop(ref)]
            dropped.append(ref)
        return dropped


def render_event(type: str, payload: Mapping, *, names: Mapping[int, str] | None = None) -> str:
    """A game event as text. A boss is "{name}: killed" or "{name}: wiped", named by
    wow.boss_name (the bridge sends boss_name ""); loot "{who} won {item}"; an achievement
    "{who} earned {achievement}"; a battleground "battleground won by {winner}". `names` maps a
    guid to who it is ("someone" when it does not)."""
    names = names or {}

    def who() -> str:
        return names.get(payload.get("guid"), "someone")
    if type == "boss":
        name = boss_name(payload.get("map", 0), payload.get("boss_id", -1),
                         payload.get("boss_name", ""))
        return f"{name}: {payload.get('state', '')}"
    if type == "loot_won":
        return f"{who()} won {strip_wow_markup(payload.get('name', ''))}"
    if type == "achievement":
        return f"{who()} earned {strip_wow_markup(payload.get('name', ''))}"
    if type == "bg_end":
        return f"battleground won by {payload.get('winner', '')}"
    if type == "death":
        return f"{who()} died"
    if type == "zone":
        return f"{who()} went to {strip_wow_markup(payload.get('zone_name', ''))}"
    if type == "login":
        return f"{who()} logged in"
    if type == "logout":
        return f"{who()} logged out"
    return type.replace("_", " ")


def _event_actors(payload: Mapping) -> frozenset[int]:
    """Its members, or the one guid of a loot, an achievement, a login and the like."""
    members = payload.get("members")
    if isinstance(members, (list, tuple)):
        return frozenset(g for g in members if g)
    guid = payload.get("guid")
    return frozenset({guid}) if guid else frozenset()


def _knowers_of(store: Store, ref: str) -> frozenset[int]:
    """Who knows, or may know, what `ref` rests on (grounding rule 12 reads it): the ledger's
    knowers of each of its roots, each root's own parties (who said a line, or was in a scene,
    knows it without a ledger row) and whoever could have seen it (knowledge.could_have_seen:
    a line's audience, an event's members, and him). A ref that rests on no root still has him."""
    with store.transaction():
        return frozenset(g for r in roots(store, [ref]) or {ref}
                         for g in knowers(store, r) | parties(store, r) | could_have_seen(store, r))


def load_items(store: Store, refs: Iterable[str], *, ident: HumanIdentity) -> list[Item]:
    """The items for `refs` of kinds memory:, chat:, event: and session:, in their order; an
    unknown ref is left out. A chat item reads "{from_name}: {text}" with markup stripped, the
    human's name shown as what the guild calls him, and his real name redacted to it (P1b's
    matcher), so no model is shown it."""
    wanted: list[tuple[str, int, str]] = []
    for ref in dict.fromkeys(refs):
        m = _REF.fullmatch(ref) if isinstance(ref, str) else None
        if m:
            wanted.append((m.group(1), int(m.group(2)), ref))
    ids = {kind: [i for k, i, _ in wanted if k == kind] for kind in ("memory", "chat", "event",
                                                                     "session")}
    cast = cast_names(store)
    names = {g: m.name for g, m in store.cast().items()} | {ident.guid: ident.called}

    def screen(text: str) -> str:
        return redact_real_name(strip_wow_markup(text), ident, ident.called)

    built: dict[str, Item] = {}
    for m in store.get_memories(ids["memory"]):
        built[f"memory:{m.id}"] = Item(f"memory:{m.id}", screen(m.text),
                                       frozenset({m.owner, *m.actors}) - {0}, m.t_event,
                                       m.importance, m.valence)
    for ln in store.chat_lines(ids["chat"]):
        who = ident.called if ln.from_guid == ident.guid else ln.from_name
        named = {g for g, n in cast.items() if has_word(ln.text, n)}
        ends = {ln.from_guid, ln.to_guid if ln.channel == "whisper" else 0}
        built[f"chat:{ln.id}"] = Item(f"chat:{ln.id}", screen(f"{who}: {ln.text}"),
                                      frozenset(ends | named) - {0}, ln.ts)
    with store.transaction():
        events = store.conn.execute(
            "SELECT id, ts, type, payload FROM game_event WHERE id IN "
            "(SELECT value FROM json_each(?))", (json.dumps(ids["event"]),)).fetchall()
        sessions = store.conn.execute(
            "SELECT id, started, ended FROM session WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids["session"]),)).fetchall()
    for e in events:
        payload = json.loads(e["payload"])
        built[f"event:{e['id']}"] = Item(f"event:{e['id']}",
                                         screen(render_event(e["type"], payload, names=names)),
                                         _event_actors(payload), e["ts"])
    for s in sessions:
        ended = s["ended"] if s["ended"] is not None else store.clock.now()
        hours = (ended - s["started"]) / 3600
        built[f"session:{s['id']}"] = Item(f"session:{s['id']}",
                                           f"{ident.called} was online {hours:.1f} hours",
                                           frozenset({ident.guid}), s["started"])
    return [replace(built[ref], knowers=_knowers_of(store, ref)) for _, _, ref in wanted
            if ref in built]


@dataclass(frozen=True)
class Facts:
    """What the grounding rules check names against. `names` maps the lower-case character
    names of the cast, his character name and what the guild calls him to their guids;
    `bosses` and `items` are lower case; `member_names` are the cast's character names."""
    names: dict[str, int]
    bosses: frozenset[str]
    items: frozenset[str]
    human: int
    member_names: tuple[str, ...]
    ident: HumanIdentity

    @cached_property
    def terms(self) -> frozenset[str]:
        """Every boss and item name: built once, since grounding rule 10 reads it for every
        claim and the item list grows with every loot (review M8)."""
        return self.bosses | self.items


def load_facts(ctx: "NightContext") -> Facts:
    """The night's Facts: the cast (away members too), every BOSS_NAMES value and every item
    name a loot_won event has carried."""
    members = ctx.members(include_away=True)
    names = {ctx.name(g).lower(): g for g in members}
    for n in (ctx.ident.character_name, ctx.ident.called):
        if n:
            names[n.lower()] = ctx.human
    with ctx.store.transaction():
        loot = [json.loads(r["payload"]).get("name") or "" for r in ctx.store.conn.execute(
            "SELECT payload FROM game_event WHERE type = 'loot_won'")]
    items = frozenset(n for n in (strip_wow_markup(x).strip(" []").lower() for x in loot) if n)
    return Facts(names=names, bosses=frozenset(n.lower() for n in BOSS_NAMES.values()),
                 items=items, human=ctx.human, member_names=tuple(ctx.name(g) for g in members),
                 ident=ctx.ident)


async def retrieve(ctx: "NightContext", owner: int, query: str, k: int) -> list[MemoryRow]:
    """The owner's `k` memories that best answer `query`: store.knn over 2k and, when the query
    has anything searchable, store.fts over k, fused by reciprocal rank; only active rows. It
    touches nothing (reflection must not bump access_n or t_accessed, or nothing would ever
    qualify for forgetting)."""
    vec = (await ctx.embed([query]))[0]
    ranked = [ctx.store.knn(owner, vec, k=2 * k)]
    searchable = fts_query(query)
    if searchable is not None:
        ranked.append(ctx.store.fts(owner, searchable, k))
    score: dict[int, float] = {}
    for hits in ranked:
        for rank, (mid, _) in enumerate(hits, 1):
            score[mid] = score.get(mid, 0.0) + 1.0 / (RRF_K + rank)
    best = sorted(score, key=lambda i: (-score[i], i))
    return [m for m in ctx.store.get_memories(best) if m.status == "active"][:k]
