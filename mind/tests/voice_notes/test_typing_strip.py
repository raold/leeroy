"""The "is recording a voice message" strip, from the talk pipeline to the console's payload: the
pipeline's recording phase is in the control endpoint's status `typing`, the console keeps it in
the strip it sends the page, and that payload is the shared fixture the page's own test reads
(tests/console/js/voice-notes.test.mjs). Invented text only."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from conftest import run
from console.rig import http, make_app, running
from console.test_console_typing import LINKED, FakeLink, served, status, typing_seen
from leeroy_mind.console.app import typing_entries
from leeroy_mind.discord.presence import Presence
from leeroy_mind.endpoint import ControlCfg, ControlEndpoint
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.tuning.effective import MemberTuning, effective
from leeroy_mind.voice import wire
from leeroy_mind.voice_notes import service
from selfies.fakes import mind_app

FIXTURE = Path(__file__).parent.parent / "fixtures" / "typing_recording.json"


def recording_status(root: Path, monkeypatch) -> list:
    """The mind's status `typing` while a member's DM line is being rendered as a voice note."""
    async def go():
        app = mind_app(root, toml="[voice_notes]\nenabled=true\nshare=0.5")
        app.arbiter.hold("discord")
        app.voice = SimpleNamespace(specs={101: {}}, client=SimpleNamespace(
            greeted=True, send=lambda msg: None,
            status=wire.Status(False, "closed", "none", "parakeet", False, 0, (), True)))
        monkeypatch.setattr(service, "presence_of", lambda *a, **k: Presence("online", False, 0))
        app.engine._blocked = lambda *a, **k: None
        app.engine.live_review.valid = lambda *a, **k: True
        entered, release = asyncio.Event(), asyncio.Event()

        async def request(msg):
            entered.set()
            await release.wait()
            return None  # no note: the line goes out as text

        app.voice_notes.request = request
        turn = Turn(TurnKind.REPLY_HUMAN, 101, "dm:101", to_guid=app.human_guid)
        p = effective(app.personas.get(101), MemberTuning({"talkative": 10}))
        try:
            task = asyncio.create_task(app.engine._send_line(
                turn, TurnMeta("note-strip"), p, "Meet beside the invented old tree.", [],
                first=True))
            await entered.wait()
            typing = ControlEndpoint(app, ControlCfg(), key="K" * 43).status()["typing"]
            release.set()
            assert await task is None
            [after] = app.engine.typing_now()
            assert after["phase"] == "typing"  # the render is over: back to the typing dots
            return json.loads(json.dumps(typing))
        finally:
            await app.stop()
    return run(go())


def test_recording_reaches_the_console_strip_as_the_page_fixture(tmp_path, store, monkeypatch):
    (tmp_path / "mind").mkdir()
    typing = recording_status(tmp_path / "mind", monkeypatch)
    [entry] = typing
    assert entry["phase"] == "recording" and entry["channel"] == "dm:101"
    assert typing_entries(typing) == typing  # the console keeps the phase

    data = Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent
    store.start_session(1000.0)  # he is in game: the console asks the mind who is typing
    link = FakeLink(status(typing))

    async def console():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED,
                                   link=link)) as app:
            seen = await typing_seen(app)
            state = (await http(app, "GET", "/api/state")).json()["status"]["typing"]
            return seen, state
    seen, state = asyncio.run(console())
    assert seen == state == typing

    # The page's test reads this payload (its times fixed): it must be what the console sends.
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    [fixed] = fixture["typing"]
    assert fixture["human"] == entry["to"]
    assert {**entry, "since": fixed["since"], "age": fixed["age"]} == fixed
    assert 0 <= entry["age"] < fixed["age"] + 30


def test_the_console_keeps_only_the_minds_own_phases():
    base = {"guid": 101, "name": "Arkon", "channel": "dm:101", "to": 1, "since": 5.0, "age": 1.0}
    for phase in ("composing", "typing", "recording"):
        assert typing_entries([{**base, "phase": phase}]) == [{**base, "phase": phase}]
    for phase in ("singing", "", 7, None, ["recording"], {"recording": 1}):
        assert typing_entries([{**base, "phase": phase}]) == [base]
    assert typing_entries([base]) == [base]  # an older mind's entry: no phase
