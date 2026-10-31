import asyncio
import copy
import dataclasses
import typing
from datetime import datetime, timezone

import pytest

import live_guard as _live_guard
from leeroy_mind.personas.io import save_persona
from leeroy_mind.personas.schema import Persona, persona_from_dict
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock


def run(coro):
    return asyncio.run(coro)


_live_guard.install()


@pytest.fixture(autouse=True)
def live_guard(request, tmp_path, monkeypatch):
    """Every test runs with the guard armed (tests/live_guard.py) and with LEEROY_MIND_DATA at a
    tmp dir of its own, so the default data dir is never the owner's mind-data, subprocesses
    included. A test can take this fixture to read the guard."""
    guard = _live_guard.GUARD
    guard.data_dir = tmp_path / "default-mind-data"
    monkeypatch.setenv("LEEROY_MIND_DATA", str(guard.data_dir))
    guard.allowed = frozenset(m.name for m in request.node.iter_markers())
    guard.hits.clear()
    guard.active = True
    try:
        yield guard
    finally:
        guard.active = False
        guard.allowed = frozenset()
    guard.verify()


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc))


def unit(i: int) -> list[float]:
    v = [0.0] * 512
    v[i] = 1.0
    return v


def make_mem(**kw) -> MemoryRow:
    base = dict(
        id=None, owner=7, kind="observation", text="Coros pulled early again.",
        t_event=1000, t_created=1000, t_accessed=1000, access_n=0, actors=(), place=None,
        boss_id=None, raid_id=None, importance=5, valence=0.0, source="game",
        evidence=("event:1",))
    base.update(kw)
    return MemoryRow(**base)


@pytest.fixture
def store(tmp_path, clock):
    s = Store(open_db(tmp_path / "mind.db"), clock=clock)
    yield s
    s.close()


_REAL_NAMES = ["Sam Carter", "Jo Mercer", "Chris Dale", "Pat Reeve", "Alex Finch"]


def _base_persona_dict(guid: int, name: str) -> dict:
    trait = lambda score, text: {"score": score, "behaviour": text}  # noqa: E731
    return {
        "schema": 1, "guid": guid, "character_name": name, "core": True,
        "identity": {"real_name": _REAL_NAMES[guid % 5], "age": 34, "player_gender": "male",
                     "region": "UK", "accent": "en-gb", "voice": {"base": [], "speed": 1.0}},
        "big_five": {
            "openness": trait(5, "tries new things when asked"),
            "conscientiousness": trait(6, "turns up on time"),
            "extraversion": trait(7, "chats easily in guild"),
            "agreeableness": trait(5, "goes along with most plans"),
            "neuroticism": trait(2, "rarely rattled")},
        "attachment": "secure", "humour": "dry",
        "typing": {"case": "lower", "end_punct": "none", "emoticons": [":)"],
                   "emoticon_rate": 0.1, "abbreviations": {"you": "u"}, "typo_rate": 0.02,
                   "words_per_line": [3, 12], "split_rate": 0.1, "chars_per_sec": 5.0},
        "real_life": {"job": "warehouse supervisor", "rhythm": "early shifts, free evenings",
                      "household": "lives with a flatmate", "money": "comfortable",
                      "sleep": "in bed by midnight", "life_thread": "learning to cook",
                      "birthday": "03-14", "night_owl": False},
        "habits": [],
        "romance": {"stance": "none", "style": None, "jealousy_mode": None, "dial": None},
        "game": {"motivation": "achiever", "thinks_as": "theorycrafter",
                 "wants": ["clear the instance"], "outside_raids": ["dungeons"],
                 "favourite_spot": "the old harbour town", "years_played": 5},
        "play": {"main_spec": "Protection", "off_spec": "Arms", "raid_role": "tank",
                 "skill": "solid", "guild_role": "raider", "officer_title": ""},
        "opinions": ["pulls should be planned", "patch notes are worth reading",
                     "tea beats coffee"],
        "knowledge_gaps": [], "speech_habits": ["says mate a lot"], "likes": ["quiet runs"],
        "dislikes": ["late starts"], "values": ["reliability"], "nevers": ["mocks new players"],
        "secret": "once lost a bet and never mentioned it",
        "sample_lines": [{"text": "ok pulling in 5", "approved": True}],
        "bits": [{"id": f"bit{guid}", "instruction": "mention the weather once in a while",
                  "min_days_between": 3.0, "voice_bit": False, "contexts": ["any"], "lines": 1,
                  "dates": []}],
        "relationships": [], "mock_interview": "",
    }


def _set_leaf(d: dict, leaf: str, value) -> bool:
    hits = [x for x in _walk_dicts(d) if leaf in x]
    if len(hits) != 1:
        raise KeyError(f"{leaf!r} matches {len(hits)} persona fields")
    hits[0][leaf] = value
    return True


def _walk_dicts(d: dict):
    yield d
    for v in d.values():
        if isinstance(v, dict) and v is not d:
            yield from _walk_dicts(v)


_KEEP = {"guid", "character_name", "birthday", "dates", "id", "words_per_line", "schema"}


def _pad(s: str, cap: int) -> str:
    filler = "steady and quietly stubborn about it "
    return (s + " " + filler * (cap // len(filler) + 1))[:cap].rstrip()


def _maximize(cls, d: dict) -> dict:
    hints = typing.get_type_hints(cls)
    for f in dataclasses.fields(cls):
        if f.name in _KEEP or f.name not in d:
            continue
        t, v, m = hints[f.name], d[f.name], f.metadata
        d[f.name] = _max_value(t, v, m)
    return d


def _max_value(t, v, m):
    if typing.get_origin(t) in (typing.Union,) or str(type(t)) == "<class 'types.UnionType'>":
        return v
    origin = typing.get_origin(t)
    if t is str:
        return _pad(v, m.get("max_len") or 300)
    if origin is tuple:
        item_t = typing.get_args(t)[0]
        if not v:
            if dataclasses.is_dataclass(item_t) or typing.get_origin(item_t) is typing.Literal:
                return v
            v = [""]
        n = m.get("max_items") or len(v)
        items = [copy.deepcopy(v[i % len(v)]) for i in range(n)]
        return [_max_value(item_t, x, m) if not dataclasses.is_dataclass(item_t)
                else _maximize(item_t, x) for x in items]
    if origin is dict:
        n = m.get("max_items") or len(v)
        return {f"{'k' * 4}{i}": "v" * 4 for i in range(n)}
    if dataclasses.is_dataclass(t):
        return _maximize(t, v)
    return v


def make_persona(guid: int, name: str, *, maximal: bool = False, **over) -> Persona:
    d = _base_persona_dict(guid, name)
    for leaf, value in over.items():
        _set_leaf(d, leaf, value)
    if maximal:
        _maximize(Persona, d)
        d["identity"]["real_name"] = "Alex" + "q" * 30
        d["bits"][0]["instruction"] = ("BITINSTR " + d["bits"][0]["instruction"])[:300]
        d["sample_lines"][0]["text"] = "SAMPLELINE " + d["sample_lines"][0]["text"][:200]
        d["secret"] = "SECRETTEXT " + d["secret"][:200]
        d["bits"] = [dict(d["bits"][0], id=f"bit{i}") for i in range(len(d["bits"]))]
    return persona_from_dict(d)


TEST_CAST = {101: ("Arkon", 2, 1), 102: ("Belra", 4, 11), 103: ("Coros", 10, 8),
             104: ("Dunna", 3, 2), 105: ("Ebbo", 7, 9)}


def seed_test_cast(store: Store) -> dict[int, Persona]:
    """Cast rows and one stored persona (source "seed", version 1) for guids 101-105."""
    from leeroy_mind.wow import CLASSES, RACES
    store.upsert_cast([Member(guid=g, name=n, race=r, class_=c, gender=0, level=80, rank=3,
                              online=False, map=0, zone=0, spec_tab=-1)
                       for g, (n, r, c) in TEST_CAST.items()], 1000.0)
    out = {}
    for g, (n, r, c) in TEST_CAST.items():
        out[g] = make_persona(g, n)
        save_persona(store, out[g], source="seed", ts=1000.0, race=RACES[r], class_name=CLASSES[c])
    return out


HUMAN = 1
OFFICERS = {102, 104}


def mem(guid, *, online=True, zone=10, level=80, rank=3):
    return Member(guid=guid, name=f"M{guid}", race=1, class_=1, gender=0, level=level, rank=rank,
                  online=online, map=0, zone=zone, spec_tab=-1)
