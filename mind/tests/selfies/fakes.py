"""Test doubles for the selfie worker (P2): the fake worker's command and a factory for it, a
render request, a ready set with its files, members the night can render, and a whole mind on a
scratch data dir. Every person here is invented."""
import asyncio
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from conftest import TEST_CAST, seed_test_cast
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.selfies import files
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.store import (Candidate, ensure_first_sets, mark_ready, queue_more,
                                       queued_sets)
from leeroy_mind.protocol import Member
from leeroy_mind.selfies.worker import RenderRequest, SelfieWorker
from dbtemplate import open_migrated
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import CLASSES, RACES
from nightly.test_night_enrich import APPEARANCE  # Task 6's invented looks

from .fake_worker import JPEG  # the bytes the fake writes; the tests take it from here

FAKE_WORKER = Path(__file__).with_name("fake_worker.py")
SELFIE_CFG = SelfieCfg(enabled=True)
OWNER = 100
CLOCK = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
PHOTO = ProfilePhoto(kind="snapshot", framing="upper_body", setting="on the deck of a small boat",
                     outfit="orange rain bib over a grey hoodie",
                     expression="squinting into the wind")


def tagged_jpeg(tag: str) -> bytes:
    """JPEG with a comment segment naming `tag`: still a valid JPEG, but no two tags give the
    same bytes, so a test can tell which candidate a file was copied from."""
    text = tag.encode("ascii")
    return JPEG[:2] + b"\xff\xfe" + (len(text) + 2).to_bytes(2, "big") + text + JPEG[2:]


def ready(store, data_dir: Path, guid: int, set_no: int, *, now: float = 1.0,
          night: str = "2026-10-29") -> list[Candidate]:
    """Set `set_no` of `guid` queued (a first set when the member has none, else "4 more"),
    then marked ready with four invented candidates, their JPEG files and thumbnails, as a
    night's render leaves it."""
    if (guid, set_no) not in {(s.guid, s.set_no) for s in queued_sets(store)}:
        with store.transaction():
            had = store.conn.execute("SELECT count(*) FROM selfie_set WHERE guid=?",
                                     (guid,)).fetchone()[0]
        if had:
            queue_more(store, guid, now=now)
        else:
            ensure_first_sets(store, [guid], now=now)
    assert (guid, set_no) in {(s.guid, s.set_no) for s in queued_sets(store)}, \
        f"set {set_no} is not the one queued next for {guid}"
    d = files.set_dir(data_dir, guid, set_no)
    d.mkdir(parents=True)
    for i in range(4):
        files.candidate(data_dir, guid, set_no, i).write_bytes(
            tagged_jpeg(f"{guid}/{set_no}/c{i}"))
        files.candidate(data_dir, guid, set_no, i, thumb=True).write_bytes(
            tagged_jpeg(f"{guid}/{set_no}/c{i}.thumb"))
    cands = [Candidate(idx=i, seed=1000 * set_no + i, tries=1, nsfw=0.01,
                       prompt=f"photo of an invented test person, set {set_no}, slot {i}")
             for i in range(4)]
    mark_ready(store, guid, set_no, cands, now=now, night=night,
               persona_version=store.persona_versions().get(guid))
    return cands


def fake_command(tmp_path: Path, **env: str) -> tuple[list[str], dict[str, str]]:
    """The fake worker under this interpreter, isolated as the real one runs (on Windows a venv
    launcher that starts the base interpreter as its child), and its environment: this
    process's, plus the FAKE_SELFIE_* behaviour given."""
    return ([sys.executable, "-I", str(FAKE_WORKER)],
            {**os.environ, "LEEROY_SELFIE_DATA": str(tmp_path / "selfie-data"), **env})


def request(tmp_path: Path, **over) -> RenderRequest:
    req = RenderRequest(
        phrases=(("photo of a woman in her thirties", 0), ("short auburn hair", 1),
                 ("standing in a sunlit kitchen", 3)),
        negative=(("child, teenager", 0), ("blurry", 1)),
        seed=1234, steps=30, cfg=5.0, size=1024, out=tmp_path / "c0.jpg",
        thumb=tmp_path / "c0.thumb.jpg", thumb_size=512, nsfw_max=0.2)
    return replace(req, **over)


def renderable_cast(store, *guids: int) -> None:
    """Each of `guids` (TEST_CAST, seeded) stored again with invented looks and a photo kind,
    so the night can build its prompt (prompt.renderable)."""
    for guid in guids:
        _, race, cls = TEST_CAST[guid]
        p = load_persona(store, guid)
        save_persona(store, replace(p, appearance=APPEARANCE, photo=PHOTO), source="seed",
                     ts=1000.0, race=RACES[race], class_name=CLASSES[cls])


def fake_worker(tmp_path: Path, **env: str) -> Callable[[], SelfieWorker]:
    """The stage's worker factory, for the fake worker with the FAKE_SELFIE_* behaviour given."""
    command, environ = fake_command(tmp_path, **env)
    return lambda: SelfieWorker(command, env=environ)


async def until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    """Wait on the loop until `condition()` holds; AssertionError after `timeout` seconds."""
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            raise AssertionError(f"not reached within {timeout} s")
        await asyncio.sleep(0.02)


def mind_app(root: Path, *, toml: str = "", prepare: Callable[[Store], None] | None = None
             ) -> MindApp:
    """A MindApp on the data dir `root`, built as tests/tuning/test_tuning_endpoint.py builds one
    (a FakeLLM and a fake clock, no bridge, nothing started): mind.toml holding `toml`; the test
    cast seeded, 101-103 with looks and a photo kind; the owner (OWNER) on the roster as guild
    master and named; and `prepare(store)` run on its mind.db before the app opens it. The
    caller closes app.store."""
    (root / "mind.toml").write_text(toml, encoding="utf-8")
    s = Store(open_migrated(root / "mind.db"), clock=FakeClock(CLOCK))
    try:
        seed_test_cast(s)
        renderable_cast(s, 101, 102, 103)
        s.upsert_cast([Member(guid=OWNER, name="Ownerly", race=2, class_=1, gender=0, level=83,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
        s.set_meta("human_guid", str(OWNER))
        if prepare is not None:
            prepare(s)
    finally:
        s.close()
    return MindApp(load_config(root / "mind.toml"), root, llm=FakeLLM(), clock=FakeClock(CLOCK))


def known_offline(app: MindApp) -> MindApp:
    """His WoW presence known and offline, as a connected bridge's hello naming nobody leaves
    it (nothing is started: only the state the GPU pause reads)."""
    from leeroy_mind.protocol import Hello
    app.hello = Hello(ts=int(app.clock.now() * 1000), server="mod-leeroy", version="0.1.0",
                      guild_id=0, human_guid=0)
    app.bridge._connected = True
    app.night_scheduler.presence.online = False
    return app


LOOKS = "0" * 64  # an invented looks hash


def planned_snap(store, guid: int, **over) -> int:
    """A situational photo planned for `guid` (pick 1, looks LOOKS, all five phases, a window from
    0 to far ahead); `over` replaces any plan_snap argument. The ref is unique per call."""
    from leeroy_mind.selfies.snaps import plan_snap
    n = store.conn.execute("SELECT count(*) FROM selfie_snap").fetchone()[0]
    args = dict(kind="situation", guid=guid, source="life", ref=f"beat:{n + 1}",
                night="2026-10-29", scene={"shows": "an invented scene"}, audience="anyone",
                phases=("morning", "day", "evening", "late", "dead"), not_before=0.0,
                expires=1e12, now=1.0, pick=1, looks=LOOKS)
    args.update(over)
    sid = plan_snap(store, **args)
    assert sid is not None
    return sid


def ready_snap(store, data_dir, guid: int, **over) -> int:
    """planned_snap, then rendered, with its photo and thumbnail files (two JPEGs)."""
    from leeroy_mind.selfies.snaps import mark_rendered
    sid = planned_snap(store, guid, **over)
    guild = over.get("kind") == "guild"
    mark_rendered(store, sid, width=832, height=1216, seed=7, tries=1, nsfw=0.01,
                  prompt="an invented prompt", now=2.0)
    for thumb in (False, True):
        path = (files.guild_photo(data_dir, sid, thumb=thumb) if guild
                else files.snap(data_dir, guid, sid, thumb=thumb))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(JPEG)
    return sid


def sent_snap(store, data_dir, guid: int, *, line_id: int | None = None, **over) -> int:
    """ready_snap, then sent by `guid` in a whisper."""
    from leeroy_mind.selfies.snaps import mark_sent
    sid = ready_snap(store, data_dir, guid, **over)
    assert mark_sent(store, sid, sender=guid, channel="whisper",
                     line_id=line_id if line_id is not None else 1000 + sid, now=3.0)
    return sid


def expired_snap(store, data_dir, guid: int, ended: str = "passed", **over) -> int:
    """ready_snap, then expired for `ended`."""
    from leeroy_mind.selfies.snaps import expire
    sid = ready_snap(store, data_dir, guid, **over)
    expire(store, sid, ended, now=4.0)
    return sid
