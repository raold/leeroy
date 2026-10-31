"""A test rig for the talk engine: the five-member test cast online, the human (guid 1,
"Testmage", called Zan, he/him, the guild master, with an invented real name), a FakeLLM that
answers like the live model (OrionLLM) behind a real LLMScheduler and Recall, and a recording
`send`. Tests drive time with the FakeClock."""
import itertools
import random
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from conftest import HUMAN, OFFICERS, TEST_CAST, make_persona, seed_test_cast
from leeroy_mind.config import HumanCfg, RomanceCfg, TalkCfg, load_config
from leeroy_mind.library.chunks import Chunk
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.recall import Recall
from leeroy_mind.memory.writer import Embedder
from leeroy_mind.personas.io import PersonaCache, save_persona
from leeroy_mind.protocol import Ack, Boss, Cast, Chat, Emote, Group, Hello, Member, Say
from leeroy_mind.talk.engine import TalkEngine
from leeroy_mind.talk.raid import RaidState
from leeroy_mind.talk.world import WorldView
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import CLASSES, RACES

HUMAN_NAME = "Testmage"
NICK = "Zan"
REAL = "Quillon"  # invented: the owner's real name lives only in his local mind.toml
OVERLAY = "LOCAL-OVERLAY"  # stands in for the owner's local romance_explicit.txt
# The owner's own dial, which the engine tests were written at; the public default (3) is the
# template's, and a test of it passes dial=3.
ROMANCE_DIAL = 9
RAID = (HUMAN, 101, 102, 103, 104, 105)


_CHUNK_IDS = itertools.count(1)


def chunk(title: str, text: str, *, kind: str = "lore", source: str = "wiki", section: str = "",
          instance: str | None = None, url: str | None = None) -> Chunk:
    """A library chunk as Library.search returns one, with invented text."""
    return Chunk(id=next(_CHUNK_IDS), source=source, kind=kind, title=title, section=section,
                 text=text, url=url, license="CC BY-SA 4.0" if source == "wiki" else None,
                 map_id=None, instance=instance, ref_ids=(), t_created=0)


class FakeLibrary:
    """The library as the talk engine uses it (search, search_text, titled, strategy_for), with
    canned chunks and a record of every call. search waits for `gate` when given; `fail` and
    `text_fail` make search and search_text raise a database error. `named`: the chunks titled
    finds (by title, "The" and a "(...)" qualifier aside). `strategy`: the page strategy_for
    gives any boss, or a {boss: page} dict."""

    def __init__(self, chunks=(), *, strategy=(), named=(), gate=None, fail=False,
                 text_fail=False):
        self.chunks, self.named = list(chunks), list(named)
        self.strategy = dict(strategy) if isinstance(strategy, dict) else list(strategy)
        self.gate, self.fail, self.text_fail = gate, fail, text_fail
        self.searches: list[dict] = []
        self.text_searches: list[dict] = []
        self.titled_calls: list[tuple[list[str], list[str] | None]] = []
        self.strategy_calls: list[tuple[str, str | None]] = []
        self.closed = False

    async def search(self, query, k=5, kinds=None, *, llm=None, priority=None):
        self.searches.append({"query": query, "k": k, "kinds": kinds, "llm": llm,
                              "priority": priority})
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise sqlite3.OperationalError("database disk image is malformed")
        return self.chunks[:k]

    def search_text(self, query, k=5, kinds=None):
        self.text_searches.append({"query": query, "k": k, "kinds": kinds})
        if self.text_fail:
            raise sqlite3.OperationalError("database disk image is malformed")
        return self.chunks[:k]

    def titled(self, names, kinds=None, *, limit=60):
        names = [" ".join(n.casefold().split()) for n in names]
        self.titled_calls.append((names, None if kinds is None else list(kinds)))

        def bare(title: str) -> str:
            t = re.sub(r"\s*\([^()]*\)$", "", title).casefold()
            return t[4:] if t.startswith("the ") else t
        return [c for c in self.named if bare(c.title) in names
                and (kinds is None or c.kind in kinds)][:limit]

    def strategy_for(self, boss_name, instance=None):
        self.strategy_calls.append((boss_name, instance))
        if isinstance(self.strategy, dict):
            return list(self.strategy.get(boss_name, ()))
        return list(self.strategy)

    def close(self):
        self.closed = True


class OrionLLM(FakeLLM):
    """FakeLLM as the live model answers (CLAUDE.md): a request that does not turn thinking
    off gets empty content, so a call site that forgets think=False leaves the guild silent
    and the engine tests fail."""

    async def chat(self, model, messages, *, options, think=None, **kw):
        result = await super().chat(model, messages, options=options, think=think, **kw)
        return result if think is False else replace(result, text="")


def engine_clock() -> FakeClock:
    """The conftest clock's start, settling each step by yielding: the engine's tasks do no
    real I/O (FakeLLM, the recording send), so no real time has to pass."""
    return FakeClock(datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc), yields=64)


class Sent:
    """The recording `send`: every command with the FakeClock time it went out. `ack(cmd)`
    returns (ok, error) or raises a bridge error."""

    def __init__(self, clock, ack=None):
        self.clock = clock
        self.ack = ack
        self.log: list[tuple[float, object]] = []

    async def __call__(self, cmd):
        self.log.append((self.clock.now(), cmd))
        ok, error = self.ack(cmd) if self.ack is not None else (True, "")
        return Ack(ts=0, id=cmd.id, ok=ok, error=error)

    def timed_says(self) -> list[tuple[float, Say]]:
        return [(t, c) for t, c in self.log if isinstance(c, Say)]

    def says(self) -> list[Say]:
        return [c for _, c in self.timed_says()]

    def emotes(self) -> list[Emote]:
        return [c for _, c in self.log if isinstance(c, Emote)]


@dataclass
class Rig:
    engine: TalkEngine
    store: object
    world: WorldView
    raid: RaidState
    llm: FakeLLM
    sent: Sent
    clock: object
    cfg: object

    async def advance(self, seconds: float, step: float = 1.0) -> None:
        await self.clock.advance(seconds, step)

    async def session(self) -> int:
        """What the app does at the human's login: a stored session, then the engine's."""
        sid = self.store.start_session(self.clock.now())
        await self.engine.start_session(sid)
        return sid

    async def human(self, text: str, channel: str = "guild", to_guid: int = 0, zone: int = 10):
        """What the app does with the human's line: store it, add it to the world, then the
        engine."""
        ev = Chat(ts=0, channel=channel, from_guid=HUMAN, from_name=HUMAN_NAME, to_guid=to_guid,
                  text=text, map=0, zone=zone)
        line_id = self.store.insert_chat_line(ev, session_id=None, origin="human")
        row = self.store.chat_lines([line_id])[0]
        self.world.add_line(row, self.clock.now())
        await self.engine.on_human_chat(row)
        return row

    async def event(self, ev) -> None:
        """What the app does with a game event: the world, the raid state, then the engine."""
        now = self.clock.now()
        self.world.apply(ev, now)
        if isinstance(ev, Group):
            self.raid.on_group(ev)
        if isinstance(ev, Boss):
            self.raid.on_boss(ev, now)
        await self.engine.on_event(ev, now)

    def chat_calls(self) -> list[dict]:
        return [c for c in self.llm.calls if "messages" in c]

    def prompts(self) -> list[str]:
        """Each live prompt as one text: the system message, then the user message."""
        return ["\n\n".join(m["content"] for m in c["messages"]) for c in self.chat_calls()]

    def utterances(self) -> list:
        return self.store.conn.execute(
            "SELECT guid, channel, kind, text, dropped FROM utterance ORDER BY id").fetchall()

    def sent_kinds(self) -> list[tuple[int, str]]:
        return [(u["guid"], u["kind"]) for u in self.utterances() if u["dropped"] is None]


def numbered(template: str = "line {n} from me"):
    """A FakeLLM script whose replies differ (no repeat tells when one member talks a lot)."""
    count = [0]

    def script(model, messages):
        count[0] += 1
        return template.format(n=count[0])
    return script


def save_member(store, guid: int, **over) -> None:
    """Store a test-cast persona with fields changed, e.g. player_gender="female",
    stance="drawn", jealousy_mode="teaser"."""
    name, race, cls = TEST_CAST[guid]
    save_persona(store, make_persona(guid, name, **over), source="seed", ts=1000.0,
                 race=RACES[race], class_name=CLASSES[cls])


def make_rig(store, clock, data_dir, *, reply: str = "fair enough", script=None, seed: int = 1,
             ack=None, gate=None, online=tuple(TEST_CAST), chattiness: int = 4,
             jealousy: int = 5, real_name: str = REAL, send=None, library=None,
             dial: int = ROMANCE_DIAL) -> Rig:
    """A fresh engine over `store` (the test cast is seeded if the store has no personas).
    The FakeLLM answers `reply` unless a `script` is given; `send` replaces the recording
    send; `library` (a FakeLibrary) gives the engine the library's facts; `dial` is the
    owner's romance dial (ROMANCE_DIAL unless a test says otherwise)."""
    if not store.persona_versions():
        seed_test_cast(store)
    store.upsert_cast([Member(guid=HUMAN, name=HUMAN_NAME, race=1, class_=8, gender=0, level=80,
                              rank=0, online=True, map=0, zone=10, spec_tab=-1)], 1000.0)
    prompts = data_dir / "prompts"  # the owner's explicit wording lives in a local overlay
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "romance_explicit.txt").write_text(OVERLAY + "\n", encoding="utf-8")
    base = load_config(data_dir / "absent.toml")
    cfg = replace(base, talk=TalkCfg(chattiness, base.talk.raid_rule),
                  romance=RomanceCfg(dial, jealousy),
                  human=HumanCfg(NICK, real_name, "he"))
    world = WorldView(is_officer=lambda g: g in OFFICERS)
    now = clock.now()
    world.apply(Hello(ts=0, server="mod-leeroy", version="0.1.0", guild_id=21,
                      human_guid=HUMAN), now)
    world.apply(Cast(ts=0, members=tuple(
        Member(guid=g, name=n, race=r, class_=c, gender=0, level=80, rank=3, online=g in online,
               map=0, zone=10, spec_tab=-1) for g, (n, r, c) in TEST_CAST.items())), now)
    personas = PersonaCache(store)
    raid = RaidState(world=world, personas=personas.get, clock=clock)
    llm = OrionLLM(script or (lambda model, messages: reply), gate=gate)
    scheduler = LLMScheduler(llm, clock=clock)
    recall = Recall(store, Embedder(scheduler, cfg.models.embedding), clock,
                    tau_days=cfg.memory.depth_days)
    sent = Sent(clock, ack)
    engine = TalkEngine(store=store, world=world, raid=raid, recall=recall, scheduler=scheduler,
                        personas=personas, cfg=cfg, data_dir=data_dir, clock=clock,
                        rng=random.Random(seed), send=send or sent, human_guid=HUMAN,
                        library=library)
    return Rig(engine, store, world, raid, llm, sent, clock, cfg)
