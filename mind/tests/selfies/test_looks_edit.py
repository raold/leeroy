"""His weight and bust edit (owner, 2026-11-15: "Adjusting them triggers a new selfie render"): the
looks op's strict arguments, an owner persona version with its ledger row and receipt, the
persona ledger's undo, a fresh profile set queued as "4 more" queues one (a varied one for the
lust style), and the endpoint's gates and content-free logs. Every person here is invented."""
import asyncio
import contextlib
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import TEST_CAST, make_persona, run
from leeroy_mind import endpoint
from leeroy_mind.console.personaview import member_view
from leeroy_mind.endpoint import BadRequest, ControlCfg, ControlEndpoint, looks_args, parse_request
from leeroy_mind.nightly.growth import undo
from leeroy_mind.personas.io import PersonaCache, load_persona, save_persona
from leeroy_mind.personas.looks_edit import RECEIPT, LooksError, LooksService, set_looks
from leeroy_mind.selfies import kind, varied
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.prompt import build_prompt
from leeroy_mind.selfies.service import SelfieService
from leeroy_mind.selfies.store import VARIED_SET_KEY, queued_sets, ready_set
from leeroy_mind.selfies.style import LUST
from leeroy_mind.wow import CLASSES, RACES
from nightly.test_night_enrich import APPEARANCE

from .fakes import OWNER, PHOTO, mind_app, ready, renderable_cast
from .test_selfie_prompt import LUST_PHOTO

KEY = "K" * 43
ON = "[selfies]\nenabled = true\n"


def save_woman(store, guid: int, *, lust: bool = False, **looks) -> None:
    """`guid` of the test cast stored again as a woman with looks and a photo kind; `lust`: of
    the lust style, her kind one the lust rules chose."""
    name, race, cls = TEST_CAST[guid]
    p = make_persona(guid, name, player_gender="female", age=34,
                     stance="lust" if lust else "none")
    p = replace(p, appearance=replace(APPEARANCE, **looks),
                photo=LUST_PHOTO if lust else PHOTO)
    save_persona(store, p, source="seed", ts=1000.0, race=RACES[race], class_name=CLASSES[cls])
    if lust:
        kind._record(store, guid, LUST, p.photo)


def app_for(store, clock, data, cfg=SelfieCfg(enabled=True)):
    from conftest import seed_test_cast
    from leeroy_mind.protocol import Member
    seed_test_cast(store)
    renderable_cast(store, 101, 102, 103)
    store.upsert_cast([Member(guid=OWNER, name="Ownerly", race=2, class_=1, gender=0, level=83,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1000.0)
    app = SimpleNamespace(store=store, clock=clock, data_dir=data, personas=PersonaCache(store),
                          human_guid=OWNER)
    app.selfies = SelfieService(app, cfg)
    return app


def looks(guid=102, weight_kg=88, bust="large"):
    return {"action": "set", "guid": guid, "weight_kg": weight_kg, "bust": bust}


def _ledger(store, guid):
    with store.transaction():
        return [dict(r) for r in store.conn.execute(
            "SELECT id, kind, text, ref FROM persona_ledger WHERE guid=? ORDER BY id", (guid,))]


def _source(store, guid):
    with store.transaction():
        return store.conn.execute("SELECT source, version FROM persona_core WHERE guid=?",
                                  (guid,)).fetchone()


# -- the op's arguments -------------------------------------------------------------------------

def test_looks_args_take_each_action_in_its_one_strict_form():
    assert looks_args(looks()) == looks()
    assert looks_args(looks(bust=None)) == looks(bust=None)
    assert looks_args(looks(weight_kg=40, bust="very_large"))["weight_kg"] == 40
    assert looks_args(looks(weight_kg=180, bust="small"))["weight_kg"] == 180
    assert looks_args({"action": "undo", "ledger": 7}) == {"action": "undo", "ledger": 7}


@pytest.mark.parametrize("args,words", [
    (None, "an action"), ({"action": "erase"}, "an action"),
    ({"action": "set", "guid": 102, "weight_kg": 80}, "exactly bust, guid, weight_kg"),
    ({**looks(), "height_cm": 170}, "exactly"),
    (looks(guid=0), "guid"), (looks(guid="102"), "guid"), (looks(guid=True), "guid"),
    (looks(weight_kg=39), "40 to 180"), (looks(weight_kg=181), "40 to 180"),
    (looks(weight_kg=80.5), "whole number"), (looks(weight_kg="80"), "whole number"),
    (looks(weight_kg=True), "whole number"),
    (looks(bust="huge"), "Bust is one of small, medium, large, very large"),
    (looks(bust=3), "Bust is one of"), (looks(bust=""), "Bust is one of"),
    ({"action": "undo"}, "exactly ledger"), ({"action": "undo", "ledger": 0}, "ledger"),
    ({"action": "undo", "ledger": "4"}, "ledger"), ({"action": "undo", "ledger": 1 << 63}, "ledger"),
])
def test_looks_args_refuse_every_other_shape_and_value(args, words):
    with pytest.raises(BadRequest, match=words):
        looks_args(args)


def test_parse_request_routes_the_looks_op():
    line = (json.dumps({"v": 1, "key": KEY, "op": "looks", "args": looks()}) + "\n").encode()
    assert parse_request(line, KEY) == ("looks", looks())
    with pytest.raises(BadRequest, match="looks needs args"):
        parse_request((json.dumps({"v": 1, "key": KEY, "op": "looks"}) + "\n").encode(), KEY)
    assert "looks" in endpoint.OPS


# -- the persona write and its undo -------------------------------------------------------------

def test_a_change_is_an_owner_version_with_a_ledger_row_and_a_receipt(store, clock, tmp_path):
    app_for(store, clock, tmp_path)
    save_woman(store, 102)
    was = _source(store, 102)
    made = set_looks(store, 102, weight_kg=88, bust="large", now=5000.0)
    p = load_persona(store, 102)
    assert (p.appearance.weight_kg, p.appearance.bust) == (88, "large")
    assert p.appearance.height_cm == APPEARANCE.height_cm and p.appearance.build == APPEARANCE.build
    now = _source(store, 102)
    assert now["source"] == "owner" and now["version"] == was["version"] + 1 == made.version
    assert _ledger(store, 102) == [{"id": made.ledger, "kind": "looks", "ref": None,
                                    "text": "weight 64 -> 88 kg, bust the build's own -> large"}]
    assert json.loads(store.get_meta(RECEIPT.format(ledger=made.ledger))) == {
        "before": {"weight_kg": 64, "bust": None}, "after": {"weight_kg": 88, "bust": "large"},
        "source": "seed"}
    with store.transaction():  # the variety pass now leaves her looks alone
        assert store.conn.execute("SELECT reason FROM persona_looks_protection WHERE guid=102"
                                  ).fetchone()[0] == "owner"
    assert set_looks(store, 102, weight_kg=88, bust="large", now=5001.0) is None  # the same again
    assert _source(store, 102)["version"] == made.version and len(_ledger(store, 102)) == 1


def test_the_mind_refuses_in_words_and_writes_nothing(store, clock, tmp_path):
    app_for(store, clock, tmp_path)
    save_woman(store, 102)
    before = _source(store, 102)
    with pytest.raises(LooksError, match="women only"):
        set_looks(store, 101, weight_kg=80, bust="large", now=1.0)   # 101 is a man
    with pytest.raises(LooksError, match="No looks yet"):
        set_looks(store, 104, weight_kg=80, bust=None, now=1.0)     # no appearance yet
    with pytest.raises(LooksError, match="40 to 180"):
        set_looks(store, 102, weight_kg=200, bust=None, now=1.0)
    with pytest.raises(LooksError, match="No persona"):
        set_looks(store, 999, weight_kg=80, bust=None, now=1.0)
    assert _source(store, 102) == before and _ledger(store, 102) == []
    made = set_looks(store, 101, weight_kg=95, bust=None, now=1.0)  # a man's weight is fine
    assert load_persona(store, 101).appearance.weight_kg == 95 and made is not None


def test_undo_restores_both_values_and_the_source_and_is_recorded(store, clock, tmp_path):
    app_for(store, clock, tmp_path)
    save_woman(store, 102)
    made = set_looks(store, 102, weight_kg=88, bust="large", now=5000.0)
    assert undo(store, made.ledger, now=6000.0) is True
    p = load_persona(store, 102)
    assert (p.appearance.weight_kg, p.appearance.bust) == (64, None)
    assert _source(store, 102)["source"] == "seed"
    rows = _ledger(store, 102)
    assert rows[-1]["kind"] == "undo" and rows[-1]["ref"] == made.ledger
    assert rows[-1]["text"].startswith("undid looks: weight 64 -> 88 kg")
    assert undo(store, made.ledger, now=6001.0) is False  # once
    with store.transaction():
        view = member_view(store.conn, 102, now=7000.0)
    assert [r["undone"] for r in view["ledger"] if r["kind"] == "looks"] == [True]
    assert view["looks_limits"] == {"weight_kg": [40, 180],
                                    "busts": ["small", "medium", "large", "very_large"]}
    assert "bust" not in view["card"]["appearance"]  # stored as before the field existed


def test_a_later_change_wins_over_an_undo_of_an_earlier_one(store, clock, tmp_path):
    app_for(store, clock, tmp_path)
    save_woman(store, 102)
    first = set_looks(store, 102, weight_kg=88, bust=None, now=5000.0)
    second = set_looks(store, 102, weight_kg=100, bust="small", now=5100.0)
    assert undo(store, first.ledger, now=6000.0) is False
    assert load_persona(store, 102).appearance.weight_kg == 100
    assert undo(store, second.ledger, now=6001.0) is True
    assert undo(store, first.ledger, now=6002.0) is True
    p = load_persona(store, 102)
    assert (p.appearance.weight_kg, p.appearance.bust) == (64, None)


# -- the service: a fresh set ---------------------------------------------------------------------

def test_a_change_queues_a_fresh_set_and_its_render_says_the_new_looks(store, clock, tmp_path):
    app = app_for(store, clock, tmp_path)
    save_woman(store, 102, build="curvy")
    ready(store, tmp_path, 102, 1)  # a set waiting for his choice
    reply = run(LooksService(app).handle(looks(weight_kg=88)))
    n = reply["ledger"]
    assert reply == {"ok": True, "guid": 102, "ledger": n, "set": 2,
                     "message": f"Saved (ledger #{n}). A new set renders tonight. The set waiting "
                                "for your choice stays in her gallery."}
    assert [(s.guid, s.set_no, s.reason) for s in queued_sets(store)] == [(102, 2, "more")]
    assert ready_set(store, 102, 1).decided == "passed"  # as "4 more" passes it: still pickable
    spec = build_prompt(app.personas.base(102))  # what tonight's render reads: the new looks
    assert ("curvy hourglass figure, large full bust, wide hips, narrow waist", 0) in spec.phrases
    assert ("chubby", 0) in spec.phrases  # 88 kg at 168 cm
    again = run(LooksService(app).handle(looks(weight_kg=90, bust="medium")))
    assert again["set"] == 2 and again["message"].endswith(
        "A set is already queued; it renders tonight with the new looks.")
    assert len(queued_sets(store)) == 1
    same = run(LooksService(app).handle(looks(weight_kg=90, bust="medium")))
    assert same == {"ok": True, "guid": 102, "ledger": None, "set": None,
                    "message": "Nothing changed: those are already their looks."}


def test_the_words_name_the_booth_and_her_covers_face_when_they_apply(store, clock, tmp_path):
    from leeroy_mind.selfies.store import pick
    app = app_for(store, clock, tmp_path, SelfieCfg(enabled=True, booth=True, identity_sets=True))
    save_woman(store, 102)
    ready(store, tmp_path, 102, 1)
    pick(store, 102, 1, 0, source="console", now=2.0)
    reply = run(LooksService(app).handle(looks()))
    assert reply["message"].endswith("A new set renders tonight or at the photo booth's next "
                                     "pass, with her cover's face.")


def test_a_lust_style_woman_gets_a_varied_set_even_after_one_today(store, clock, tmp_path):
    app = app_for(store, clock, tmp_path, SelfieCfg(enabled=True, varied_sets=True))
    save_woman(store, 102, lust=True, build="voluptuous")
    day = clock.local().date().isoformat()
    store.set_meta(varied.DAY_KEY.format(guid=102), day)  # "selfies vary" ran for her today
    reply = run(LooksService(app).handle(looks(weight_kg=100, bust="very_large")))
    assert reply["ok"] and reply["set"] == 1
    assert reply["message"].endswith("A new varied set renders tonight.")
    plan = json.loads(store.get_meta(VARIED_SET_KEY.format(guid=102, set_no=1)))
    assert len(plan["looks"]) == 4


def test_with_varied_sets_off_a_lust_woman_gets_an_ordinary_set(store, clock, tmp_path):
    app = app_for(store, clock, tmp_path)
    save_woman(store, 102, lust=True)
    reply = run(LooksService(app).handle(looks()))
    assert reply["set"] == 1 and reply["message"].endswith("A new set renders tonight.")
    assert store.get_meta(VARIED_SET_KEY.format(guid=102, set_no=1)) is None


def test_with_rendering_off_the_looks_save_and_no_set_is_queued(store, clock, tmp_path):
    app = app_for(store, clock, tmp_path, SelfieCfg(enabled=False))
    save_woman(store, 102)
    reply = run(LooksService(app).handle(looks()))
    assert reply["ok"] and reply["set"] is None and load_persona(store, 102).appearance.bust == "large"
    assert "Rendering is off in mind.toml" in reply["message"] and queued_sets(store) == []


def test_only_cast_members_and_never_the_owner(store, clock, tmp_path):
    app = app_for(store, clock, tmp_path)
    for guid in (OWNER, 999):
        assert run(LooksService(app).handle(looks(guid=guid, bust=None))) == {
            "ok": False, "error": "No such member in the cast."}
    assert run(LooksService(app).handle(looks(guid=101))) == {
        "ok": False, "error": "Bust is for women only."}


def test_undo_through_the_service_queues_a_set_and_refuses_other_ledger_rows(store, clock,
                                                                                 tmp_path):
    from leeroy_mind.nightly.store import ledger_add
    app = app_for(store, clock, tmp_path)
    save_woman(store, 102)
    made = run(LooksService(app).handle(looks()))
    assert made["set"] == 1
    app.personas.base(102)  # cached: the undo must drop it
    back = run(LooksService(app).handle({"action": "undo", "ledger": made["ledger"]}))
    assert back["ok"] and back["ledger"] == made["ledger"] and back["message"].startswith("Undone.")
    assert app.personas.base(102).appearance.bust is None
    assert back["message"].endswith("A set is already queued; it renders tonight with the new looks.")
    assert run(LooksService(app).handle({"action": "undo", "ledger": made["ledger"]}))["ok"] is False
    note = ledger_add(store, guid=102, kind="note_added", text="likes boats", evidence=[], now=1.0)
    assert run(LooksService(app).handle({"action": "undo", "ledger": note})) == {
        "ok": False, "error": f"Ledger #{note} is not a weight or bust change."}


# -- the endpoint -------------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def served(app):
    ep = ControlEndpoint(app, ControlCfg(), key=KEY)
    task = asyncio.create_task(ep.serve())
    await asyncio.wait_for(ep.listening.wait(), 5)
    try:
        yield ep
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def ask(ep, args, op="looks"):
    return await asyncio.to_thread(endpoint.request, ep.port, KEY, op, args, timeout=10)


def test_a_change_through_the_endpoint_answers_and_logs_content_free(tmp_path, caplog):
    async def go():
        app = mind_app(tmp_path, toml=ON, prepare=lambda s: save_woman(s, 102))
        try:
            async with served(app) as ep:
                got = await ask(ep, looks())
                assert got["ok"] and got["guid"] == 102 and got["set"] == 1
                assert "renders tonight" in got["message"]
                async with ep._selfie:  # a photo change being written holds looks off too
                    assert await ask(ep, looks(weight_kg=70)) == {
                        "ok": False, "error": "another photo change is still being written"}
                back = await ask(ep, {"action": "undo", "ledger": got["ledger"]})
                assert back["ok"] is True
                assert app.personas.get(102).appearance.bust is None
                app._stopping = True
                assert await ask(ep, looks()) == {"ok": False, "error": "the mind is stopping"}
            names = [n for p in (app.personas.base(g) for g in TEST_CAST)
                     for n in (p.character_name, p.identity.real_name)]
        finally:
            app.store.close()
        return names
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        names = run(go())
    text = caplog.text
    assert "control_request: op=looks action=set outcome=ok" in text
    assert "control_request: op=looks action=undo outcome=ok" in text
    assert "looks_change: action=set guid=102" in text
    for name in names:
        assert name not in text
    assert "weight" not in text and "bust" not in text  # ids and outcomes only
