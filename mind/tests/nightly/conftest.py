"""The nightly tests' harness (P1c Task 1): the fixture `h`, a migrated mind.db on a step clock
with the test cast and the human, plus a fake Ollama admin, scripted model replies and the
real-name scan. Every name and line here is invented: "Quillon" is the fixture real name, never
the owner's."""
import asyncio
import json
import re
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

import pytest

from conftest import TEST_CAST, make_mem, make_persona, seed_test_cast
from leeroy_mind.config import load_config
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import PersonaCache, save_persona
from leeroy_mind.personas.schema import Appearance, Disclosure, Persona
from leeroy_mind.personas.screens import names_real_name
from leeroy_mind.protocol import (BgEnd, Boss, Chat, Group, Hello, Login, Logout, LootWon,
                                  Member, TextEmote)
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.rows import MemoryRow, pack_vec
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_llm import FakeLLM, fake_embed
from leeroy_mind.wow import CLASSES, RACES

UTC = timezone.utc
START = datetime(2026, 10, 29, 21, 0, tzinfo=UTC)
IDENT = HumanIdentity(1, "Testmage", "Testy", "Quillon", True, "he")
# The tables no_real_name scans: every TEXT column of each.
SCANNED = ("memory", "relationship", "persona_state", "persona_core", "rel_delta",
           "grounding_log", "review_queue", "story", "life_thread", "life_beat", "away",
           "encounter", "wow_life", "feud", "lexicon", "vocab", "chronicle", "award",
           "persona_ledger", "fact", "rumor", "rumor_version", "emotion", "lands_profile",
           "probe_run")


class StepClock:
    """The mind clock for nightly tests: it moves only when a test moves it (advance, set),
    and sleep returns at once. local() is UTC."""

    def __init__(self, start: datetime):
        self._t = start.timestamp()

    def now(self) -> float:
        return self._t

    def local(self) -> datetime:
        return datetime.fromtimestamp(self._t, UTC)

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(0)

    def advance(self, s: float) -> None:
        self._t += s

    def set(self, t: float) -> None:
        self._t = t


class FakeAdmin:
    """Ollama's model admin as the GPU arbiter uses it (Task 3's HttpOllamaAdmin): `loaded` is
    what /api/ps lists, and each unload and preload is recorded in `calls` as ("unload", model)
    and ("preload", model, keep_alive). A preload's `options` go to `preload_options`, one entry
    per preload (None when it had none), so the pinned `calls` tuples stay as they are."""

    def __init__(self, loaded: list[str]):
        self.loaded = list(loaded)
        self.calls: list[tuple] = []
        self.preload_options: list[dict | None] = []

    async def loaded_models(self) -> list[str]:
        return list(self.loaded)

    async def unload(self, model: str) -> None:
        self.calls.append(("unload", model))
        if model in self.loaded:
            self.loaded.remove(model)

    async def preload(self, model: str, keep_alive, *, options: dict | None = None) -> None:
        self.calls.append(("preload", model, keep_alive))
        self.preload_options.append(None if options is None else dict(options))
        if model not in self.loaded:
            self.loaded.append(model)


_TITLE = re.compile(r'"title":\s*"([^"]+)"')
_ITEM = re.compile(r"\bITEM (\d+)\b")


def generic(prompt: str) -> dict:
    """Schema-valid quiet replies for full-pipeline synthetic integration tests.

    Explicit judge/scene replies keep those tests about persistence and orchestration;
    individual stage tests exercise the content decisions with their scripted answers.
    """
    shape, _ = json.JSONDecoder().raw_decode(prompt.rsplit("matching this schema:", 1)[-1].lstrip())

    def smallest(part):
        if "enum" in part:
            return next((v for v in (None, "none", "neutral") if v in part["enum"]), part["enum"][0])
        kind = part.get("type")
        if isinstance(kind, list):
            if "null" in kind:
                return None
            kind = kind[0]
        if kind == "object":
            return {key: smallest(part["properties"][key]) for key in part.get("required", [])}
        if kind == "array":
            return [smallest(part["items"]) for _ in range(part.get("minItems", 0))]
        if kind in ("integer", "number"):
            return max(0, part.get("minimum", 0))
        if kind == "boolean":
            return False
        if kind == "null":
            return None
        return "Taking a quiet moment."[:part.get("maxLength", 200)]

    title = shape["title"]
    result = smallest(shape)
    if title == "judge":
        return {"verdicts": [{"i": int(i), "support": "yes", "invariants_ok": True,
                               "why": "Supported by the supplied evidence."}
                              for i in dict.fromkeys(_ITEM.findall(prompt.split("matching this schema:")[0]))]}
    if title == "reflection":
        return {"outputs": [], "emotions": [], "diary": {"text": "Taking a quiet moment.", "evidence": [1]}}
    if title == "encounter":
        return {"scene": "We took a quiet moment.", "memories": [
            {"who": who, "text": "We took a quiet moment.", "importance": 1, "valence": 0}
            for who in ("a", "b")], "deltas": [], "bystanders": [], "gossip": None}
    if title == "critic":
        indices = re.findall(r"^(\d+): ", prompt, re.MULTILINE)
        return {"scores": [{"i": int(i), "score": 5, "why": "Consistent."} for i in indices]}
    return result


def scripted(replies: dict[str, list[dict | Callable[[str], dict]]],
             default: Callable[[str], dict] | None = None) -> Callable[[str, list[dict]], str]:
    """A FakeLLM script. A call's schema title (`"title": "<t>"` in the schema P1b's chat_json
    appends to the last message) pops that title's next reply: a dict, or a callable given the
    last message's text. A call with no schema (a think pass) gets "notes". An unscripted or
    used-up title asks `default(text)`, and fails the test when there is no default."""
    queues = {title: list(r) for title, r in replies.items()}

    def script(model: str, messages: list[dict]) -> str:
        text = messages[-1]["content"]
        found = _TITLE.search(text.rsplit("matching this schema:", 1)[-1]) or _TITLE.search(text)
        if found is None:
            return "notes"
        title = found.group(1)
        if queues.get(title):
            reply = queues[title].pop(0)
        elif default is not None:
            reply = default
        else:
            raise AssertionError(f"no scripted reply for {title!r}")
        return json.dumps(reply(text) if callable(reply) else reply)
    return script


def yes(prompt: str) -> dict:
    """The judge's reply (Task 6's JUDGE_SCHEMA) that supports every ITEM n in the prompt and
    finds each one within the rules."""
    return {"verdicts": [{"i": int(n), "support": "yes", "invariants_ok": True, "why": ""}
                         for n in dict.fromkeys(_ITEM.findall(prompt))]}


def no_real_name(store: Store) -> list[tuple[str, str]]:
    """(table, column) for every TEXT column of SCANNED holding the fixture real name in a form
    P1b's matcher takes ("Quillon", "QUILLON"; controller ruling I4), and for a vocab.word or
    lexicon.term that is the name in any case (the vocabulary stores lower-case words). Empty
    when nothing does."""
    conn = store.conn
    hits = []
    for table in SCANNED:
        for col in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")
                    if r[2].upper() == "TEXT"]:
            if any(isinstance(v, str) and names_real_name(v, IDENT)
                   for (v,) in conn.execute(f"SELECT {col} FROM {table}")):
                hits.append((table, col))
    name = IDENT.real_name.casefold()
    for table, col in (("vocab", "word"), ("lexicon", "term")):
        if (table, col) not in hits and any(
                isinstance(v, str) and v.casefold() == name
                for (v,) in conn.execute(f"SELECT {col} FROM {table}")):
            hits.append((table, col))
    return hits


def put_wow(h, guid, **cols):
    """A WoW-vs-life state row; shared with the later breaks stage's tests."""
    from leeroy_mind.nightly.wowlife import get_wow_life, put_wow_life
    with h.store.transaction():
        put_wow_life(h.store, replace(get_wow_life(h.store, guid), **cols))


class NightHarness:
    """What a nightly test works on. `store` is a migrated mind.db on `clock`, a StepClock at
    T, holding the test cast (101-105, P1b's seed), the human (guid 1, Testmage, level 83, the
    guild master) and meta human_guid 1. `settings` are P1b's defaults with no mind.toml. The
    helpers seed what a night reads; a time `t` is unix seconds."""

    T = START.timestamp()
    night = "2026-10-29"
    human = 1
    ident = IDENT

    def __init__(self, store: Store, settings, clock: StepClock):
        self.store, self.settings, self.clock = store, settings, clock
        self.LIVE = settings.live_model
        self.REFL = settings.reflection_model
        self.EMB = settings.embedding_model

    # -- personas and memories -----------------------------------------------------------------

    def persona(self, guid: int, *, appearance: Appearance | None = None, drawn_to=(),
                turned_off_by=(), disclosures=(), **over) -> Persona:
        """P1b's make_persona with `over`, then Task 7's fields, which P1b's base dict does not
        have: `disclosures` is a sequence of (depth, text) pairs."""
        name, race, cls = TEST_CAST[guid]
        p = make_persona(guid, name, **over)
        p = replace(p, appearance=appearance,
                    disclosures=tuple(Disclosure(depth, text) for depth, text in disclosures),
                    romance=replace(p.romance, drawn_to=tuple(drawn_to),
                                    turned_off_by=tuple(turned_off_by)))
        save_persona(self.store, p, source="seed", ts=self.T, race=RACES[race],
                     class_name=CLASSES[cls])
        return p

    def woman(self, guid: int, stance: str, **over) -> Persona:
        return self.persona(guid, stance=stance, player_gender="female", **over)

    def mem(self, owner: int, text: str, *, t: float | None = None, **kw) -> int:
        at = self.T if t is None else t
        return self.store.insert_memory(make_mem(**{
            "owner": owner, "text": text, "t_event": at, "t_created": at, "t_accessed": at,
            "embedding": pack_vec(fake_embed(text)), **kw}))

    def rows(self, owner: int) -> list[MemoryRow]:
        """Every memory of `owner`, of any status, by id."""
        ids = [r[0] for r in self.store.conn.execute(
            "SELECT id FROM memory WHERE owner = ? ORDER BY id", (owner,))]
        return self.store.get_memories(ids)

    def count(self, table: str) -> int:
        return self.store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def rel(self, a: int, b: int, **cols) -> None:
        self.store.put_relationship(replace(self.store.get_relationship(a, b), **cols))

    def live(self):
        from leeroy_mind.nightly.attraction import LiveAttraction
        return LiveAttraction(self.store, PersonaCache(self.store), self.clock, lambda: self.ident)

    def seen(self, *guids):
        from leeroy_mind.personas.io import load_persona
        with self.store.transaction():
            for guid in guids:
                p = load_persona(self.store, guid)
                self.store.conn.execute(
                    "INSERT INTO attraction_state(guid,stance,version,updated) VALUES (?,?,?,?) "
                    "ON CONFLICT(guid) DO UPDATE SET stance=excluded.stance, "
                    "version=excluded.version, updated=excluded.updated",
                    (guid, p.romance.stance, self.store.persona_versions()[guid], self.clock.now()))

    # -- game events: stamped at t, the clock left where it was --------------------------------

    @contextmanager
    def _at(self, t: float):
        was = self.clock.now()
        self.clock.set(t)
        try:
            yield
        finally:
            self.clock.set(was)

    def _event(self, t: float, ev) -> int:
        with self._at(t):
            return self.store.insert_game_event(ev, session_id=None)

    def boss(self, t: float, map_id: int, boss_id: int, state: str, members, *,
             instance_id: int = 1) -> int:
        """The bridge never sends a boss name (P1b contract note 5)."""
        return self._event(t, Boss(ts=int(t * 1000), map=map_id, instance_id=instance_id,
                                   boss_id=boss_id, boss_name="", state=state,
                                   members=tuple(members)))

    def bg(self, t: float, members, winner: str = "horde") -> int:
        return self._event(t, BgEnd(ts=int(t * 1000), bg_type=2, winner=winner,
                                    members=tuple(members)))

    def loot(self, t: float, guid: int, name: str, item_id: int = 1) -> int:
        return self._event(t, LootWon(ts=int(t * 1000), guid=guid, item_id=item_id, name=name,
                                      method="need"))

    def group(self, t: float, members, *, raid: bool = False, leader: int = 1) -> int:
        return self._event(t, Group(ts=int(t * 1000), leader_guid=leader, raid=raid,
                                    members=tuple(members)))

    def emote(self, t: float, guid: int, emote: str, target: int) -> int:
        return self._event(t, TextEmote(ts=int(t * 1000), guid=guid, emote=emote,
                                        target_guid=target))

    def session(self, start: float, end: float) -> int:
        sid = self.store.start_session(start)
        self.store.end_session(sid, end)
        return sid

    # -- chat: the clock moves to the line's time and stays there ------------------------------

    def _name(self, guid: int) -> str:
        return "Testmage" if guid == self.human else TEST_CAST.get(guid, (f"M{guid}",))[0]

    def _chat(self, t: float, guid: int, text: str, channel: str, to: int) -> Chat:
        return Chat(ts=int(t * 1000), channel=channel, from_guid=guid, from_name=self._name(guid),
                    to_guid=to, text=text, map=0, zone=0)

    def say(self, t: float, guid: int, text: str, *, channel: str = "guild", to: int = 0,
            kind: str = "open", origin: str | None = None) -> int:
        """A stored chat line, and for a member's line (origin "mind") the utterance P1b's
        _record pairs with it: same guid, channel, kind, text and time. Returns the line id."""
        self.clock.set(t)
        origin = origin or ("human" if guid == self.human else "mind")
        sid = self.store.open_session()
        line = self.store.insert_chat_line(self._chat(t, guid, text, channel, to),
                                           session_id=sid, origin=origin)
        if origin == "mind":
            self.store.log_utterance(t, guid, channel, kind, text, [], None, sid, to_guid=to)
        return line

    def his(self, t: float, text: str, *, channel: str = "guild", to: int = 0) -> Chat:
        """The human's line stored as `say` stores it, and the event the app hands a registered
        handler after storing it."""
        self.say(t, self.human, text, channel=channel, to=to)
        return self._chat(t, self.human, text, channel, to)

    def utter(self, t: float, guid: int, text: str, memory_ids=(), dropped: str | None = None
              ) -> int:
        return self.store.log_utterance(t, guid, "guild", "open", text, list(memory_ids), dropped,
                                        self.store.open_session())

    # -- the human's presence ------------------------------------------------------------------

    def hello(self, human_guid: int) -> Hello:
        return Hello(ts=0, server="mod-leeroy", version="1", guild_id=21, human_guid=human_guid)

    def login(self) -> Login:
        return Login(ts=0, guid=self.human, human=True)

    def logout(self) -> Logout:
        return Logout(ts=0, guid=self.human, human=True)

    # -- the night's context (Task 5) ----------------------------------------------------------

    def ctx(self, replies=None, *, default=None, night: str = "2026-10-29", window=None,
            admin=None, gate=None):
        """A NightContext on this harness. One FakeLLM, which lists every model, answers from
        `scripted(replies, default)` and holds each request until `gate` is set, serves the
        Reasoner and, through an LLMScheduler, the Embedder. The writer is P1b's MemoryWriter
        over that Embedder, the arbiter a GpuArbiter over `admin` (by default a FakeAdmin with
        only the embedding model loaded), and the window the night's 06:00 to 06:00 in UTC."""
        from leeroy_mind.llm.scheduler import LLMScheduler
        from leeroy_mind.memory.writer import Embedder, MemoryWriter
        from leeroy_mind.nightly.gpu import GpuArbiter
        from leeroy_mind.nightly.llm import Reasoner
        from leeroy_mind.nightly.runner import NightContext
        from leeroy_mind.nightly.settings import night_bounds
        llm = FakeLLM(script=scripted(replies or {}, default),
                      models=frozenset({self.REFL, self.LIVE, self.EMB}), gate=gate)
        embedder = Embedder(LLMScheduler(llm, clock=self.clock), self.EMB)
        return NightContext(
            store=self.store, night=night,
            window=night_bounds(night, UTC) if window is None else window,
            settings=self.settings, reasoner=Reasoner(llm, self.REFL), embedder=embedder,
            writer=MemoryWriter(self.store, embedder, self.clock, human=lambda: self.ident),
            arbiter=GpuArbiter(FakeAdmin([self.EMB]) if admin is None else admin, self.settings),
            clock=self.clock, ident=self.ident, personas=PersonaCache(self.store))


@pytest.fixture
def h(tmp_path):
    # Imported here, not at the top: a test file that needs no nightly module still collects
    # (and fails on its own imports) while one is missing.
    from leeroy_mind.nightly.settings import load_settings
    store = Store(open_db(tmp_path / "mind.db"), clock=StepClock(START))
    seed_test_cast(store)
    store.upsert_cast([Member(guid=1, name="Testmage", race=10, class_=8, gender=0, level=83,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)],
                      START.timestamp())
    store.set_meta("human_guid", "1")
    settings = load_settings(load_config(tmp_path / "missing.toml"), tmp_path)
    yield NightHarness(store, settings, store.clock)
    store.close()
