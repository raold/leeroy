"""A member's voice sheet in the console (Persona.speech, 2026-11-17): the Personas member detail
carries it whole (it is the owner's console) and lists it read-only in a Voice panel; the roster
never carries it; a "voice" ledger row has an Undo, which the running mind's looks op takes.
Every sheet here is invented (tests/sheets.py)."""
import asyncio
import json
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import seed_test_cast
from console.rig import page_source
from leeroy_mind.console import feed, personaview
from leeroy_mind.personas import voices
from leeroy_mind.personas.io import PersonaCache, load_persona
from leeroy_mind.personas.looks_edit import LooksService
from leeroy_mind.protocol import Member
from sheets import GRUFF, sheet

NOW = 1_793_304_000.0


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    store.upsert_cast([Member(guid=1, name="Ownerly", race=2, class_=1, gender=0, level=83,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1000.0)
    store.set_meta("human_guid", "1")
    return store


def test_the_member_detail_carries_her_sheet_and_its_ledger_row(cast, data):
    assert feed.read(data / "mind.db", personaview.member_view, 101,
                     now=NOW)["card"]["speech"] is None
    ledger = voices.set_voice(cast, 101, sheet(), now=NOW - 60)
    view = feed.read(data / "mind.db", personaview.member_view, 101, now=NOW)
    assert view["card"]["speech"] == GRUFF
    assert [(r["id"], r["kind"], r["text"], r["undone"]) for r in view["ledger"]] == [
        (ledger, "voice", "voice sheet set", False)]
    # the ledger row's words never hold the sheet's
    assert "noted" not in json.dumps(view["ledger"])


def test_the_roster_never_carries_a_sheet(cast, data):
    voices.set_voice(cast, 101, sheet(), now=NOW - 60)
    text = json.dumps(feed.read(data / "mind.db", personaview.roster_view, now=NOW))
    for private in ("a bit gruff", "fair enough", "the allotment", "no hurry",
                    "brain is soup"):
        assert private not in text


def test_the_page_has_a_read_only_voice_panel_and_an_undo_for_its_ledger_rows():
    page = resources.files("leeroy_mind.console").joinpath("page.html").read_text("utf-8")
    panel = page[page.index("<h3>Voice</h3>"):page.index("</article>", page.index("<h3>Voice</h3>"))]
    assert 'id="p-voice"' in panel and "<input" not in panel and "<button" not in panel
    source = page_source()
    assert 'voiceRows(c)' in source and "canUndoVoice(r)" in source
    assert 'voice: "voice sheet"' in source
    model = resources.files("leeroy_mind.console").joinpath(
        "static", "js", "voicemodel.mjs").read_text("utf-8")
    assert "\r" not in model and "fetch(" not in model and "post(" not in model


def run(coro):
    return asyncio.run(coro)


def test_the_looks_ops_undo_takes_a_voice_row_and_queues_no_photo(cast, clock):
    queued = []
    app = SimpleNamespace(store=cast, clock=clock, personas=PersonaCache(cast), human_guid=1,
                          selfies=SimpleNamespace(queue_for_looks=lambda g: queued.append(g)))
    ledger = voices.set_voice(cast, 101, sheet(), now=NOW - 60)
    assert app.personas.base(101).speech == sheet()  # cached: the undo must drop it
    back = run(LooksService(app).handle({"action": "undo", "ledger": ledger}))
    assert back == {"ok": True, "guid": 101, "ledger": ledger, "set": None, "message": "Undone."}
    assert load_persona(cast, 101).speech is None and app.personas.base(101).speech is None
    assert queued == []
    again = run(LooksService(app).handle({"action": "undo", "ledger": ledger}))
    assert again == {"ok": False, "error": f"Ledger #{ledger} was undone already, or a later "
                                           "change moved the voice sheet: undo that first."}
