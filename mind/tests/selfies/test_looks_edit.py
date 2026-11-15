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
from leeroy_mind.selfies import files, kind, varied
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
KEEP = "with her new figure; pick one as her photo to keep it."
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


def _protected(store, guid):
    with store.transaction():
        row = store.conn.execute("SELECT reason, version FROM persona_looks_protection "
                                 "WHERE guid=?", (guid,)).fetchone()
    return None if row is None else tuple(row)


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
    shapes = {"glutes": None, "waist": None, "hips": None}
    assert json.loads(store.get_meta(RECEIPT.format(ledger=made.ledger))) == {
        "before": {"weight_kg": 64, "bust": None, **shapes},
        "after": {"weight_kg": 88, "bust": "large", **shapes},
        "source": "seed", "version": made.version, "protection": None}
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
    assert _protected(store, 102) is None  # the variety pass may re-roll her again, as before
    with store.transaction():
        view = member_view(store.conn, 102, now=7000.0)
    assert [r["undone"] for r in view["ledger"] if r["kind"] == "looks"] == [True]
    assert view["looks_limits"] == {"weight_kg": [40, 180],
                                    "busts": ["small", "medium", "large", "very_large"],
                                    "glutes": ["small", "medium", "large", "very_large"],
                                    "waists": ["very_narrow", "narrow", "average", "wide"],
                                    "hips": ["narrow", "average", "wide", "very_wide"]}
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
                     "message": f"Saved (ledger #{n}). A new set renders tonight {KEEP} The set "
                                "waiting for your choice stays in her gallery."}
    assert [(s.guid, s.set_no, s.reason) for s in queued_sets(store)] == [(102, 2, "more")]
    assert ready_set(store, 102, 1).decided == "passed"  # as "4 more" passes it: still pickable
    spec = build_prompt(app.personas.base(102))  # what tonight's render reads: the new looks
    assert ("curvy hourglass figure, large full bust, wide hips, narrow waist", 0) in spec.phrases
    assert ("chubby, thick arms", 0) in spec.phrases  # 88 kg at 168 cm
    again = run(LooksService(app).handle(looks(weight_kg=90, bust="medium")))
    assert again["set"] == 2 and again["message"].endswith(
        f"A set is already queued: it renders tonight {KEEP}")
    assert len(queued_sets(store)) == 1
    same = run(LooksService(app).handle(looks(weight_kg=90, bust="medium")))
    assert same == {"ok": True, "guid": 102, "ledger": None, "set": None,
                    "message": "Nothing changed: those are already their looks."}


def test_the_words_name_the_booth_when_it_applies(store, clock, tmp_path):
    from leeroy_mind.selfies.store import pick
    app = app_for(store, clock, tmp_path, SelfieCfg(enabled=True, booth=True, identity_sets=True))
    save_woman(store, 102)
    ready(store, tmp_path, 102, 1)
    pick(store, 102, 1, 0, source="console", now=2.0)
    reply = run(LooksService(app).handle(looks()))
    assert reply["message"].endswith("A new set renders tonight or at the photo booth's next "
                                     f"pass {KEEP}")


def test_a_lust_style_woman_gets_a_varied_set_even_after_one_today(store, clock, tmp_path):
    app = app_for(store, clock, tmp_path, SelfieCfg(enabled=True, varied_sets=True))
    save_woman(store, 102, lust=True, build="voluptuous")
    day = clock.local().date().isoformat()
    store.set_meta(varied.DAY_KEY.format(guid=102), day)  # "selfies vary" ran for her today
    reply = run(LooksService(app).handle(looks(weight_kg=100, bust="very_large")))
    assert reply["ok"] and reply["set"] == 1
    assert reply["message"].endswith(f"A new varied set renders tonight {KEEP}")
    plan = json.loads(store.get_meta(VARIED_SET_KEY.format(guid=102, set_no=1)))
    assert len(plan["looks"]) == 4


def test_with_varied_sets_off_a_lust_woman_gets_an_ordinary_set(store, clock, tmp_path):
    app = app_for(store, clock, tmp_path)
    save_woman(store, 102, lust=True)
    reply = run(LooksService(app).handle(looks()))
    assert reply["set"] == 1 and reply["message"].endswith(f"A new set renders tonight {KEEP}")
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
    assert back["message"].endswith(f"A set is already queued: it renders tonight {KEEP}")
    assert run(LooksService(app).handle({"action": "undo", "ledger": made["ledger"]}))["ok"] is False
    note = ledger_add(store, guid=102, kind="note_added", text="likes boats", evidence=[], now=1.0)
    assert run(LooksService(app).handle({"action": "undo", "ledger": note})) == {
        "ok": False, "error": f"Ledger #{note} is not a weight or figure change."}


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


# -- the set it queues shows her new figure under a light face ---------------------------------

def _service_on(h, cfg):
    app = SimpleNamespace(store=h.store, clock=h.clock, data_dir=h.settings.data_dir,
                          personas=PersonaCache(h.store), human_guid=1)
    app.selfies = SelfieService(app, cfg)
    return app


def _face_file(h, guid, change):
    ref = files.history(h.settings.data_dir, guid, change)
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_bytes(b"jpeg")
    return str(ref.absolute())


def test_a_looks_edit_set_holds_her_cover_face_lightly_until_he_picks_from_it(h, tmp_path):
    """Controller, measured on the GPU 2026-11-15: at identity_scale the face reference carries
    her slim cover's body into the set. The set a weight or bust edit queues takes it at the
    worker's floor in every slot, front views included; once he picks a new cover from it, the
    next set holds that fuller cover at full strength again."""
    from leeroy_mind.selfies.store import LOOKS_EDIT_SET_KEY, pick, queue_more
    from selfies.test_varied_sets import IDENT, _cover, _logged, lust, make_stage_for
    lust(h, 101, appearance=replace(APPEARANCE, build="voluptuous"))
    old_cover = _cover(h)
    app = _service_on(h, IDENT)
    reply = run(LooksService(app).handle(looks(guid=101, weight_kg=96, bust="very_large")))
    assert reply["set"] == 2 and reply["message"].endswith(
        "A new varied set renders tonight with her new figure; pick one as her photo to keep it.")
    assert h.store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=101, set_no=2)) == "1"
    plan = varied.plan_of(h.store.conn, 101, 2)
    assert {x.view for x in plan} & set(kind.BACK_VIEWS) and {x.view for x in plan} - set(kind.BACK_VIEWS)
    log = tmp_path / "requests.log"
    stage = make_stage_for(IDENT, tmp_path, FAKE_SELFIE_IDENTITY="1", FAKE_SELFIE_LOG=str(log))
    run(stage(h.ctx()))
    rows = [r for r in _logged(log) if r[0].startswith("101:2:")]
    ref = str(files.history(h.settings.data_dir, 101, old_cover).absolute())
    assert len(rows) == 4
    for row in rows:  # her old cover's face, at the floor in every slot, front views included
        assert row[2] == "scale" and float(row[3]) == 0.3 and row[4] == "identity"
        assert row[5] == ref
    front = next(i for i, x in enumerate(plan) if x.view in ("front", "side"))
    new_cover = pick(h.store, 101, 2, front, source="console", now=h.T + 10)
    new_ref = _face_file(h, 101, new_cover)
    queue_more(h.store, 101, now=h.T + 20)  # "4 more" later: no looks edit, no marker
    assert h.store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=101, set_no=3)) is None
    log.unlink()
    run(stage(h.ctx()))
    rows = [r for r in _logged(log) if r[0].startswith("101:3:")]
    assert len(rows) == 4
    assert all(float(r[3]) == 0.6 and r[5] == new_ref for r in rows)  # the fuller cover, full


def test_a_set_already_queued_is_marked_too(store, clock, tmp_path):
    from leeroy_mind.selfies.store import LOOKS_EDIT_SET_KEY, queue_more
    app = app_for(store, clock, tmp_path)
    save_woman(store, 102)
    ready(store, tmp_path, 102, 1)
    queue_more(store, 102, now=5.0)  # his "4 more", before the edit
    assert store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=102, set_no=2)) is None
    reply = run(LooksService(app).handle(looks()))
    assert reply["set"] == 2 and reply["message"].endswith(
        "A set is already queued: it renders tonight with her new figure; pick one as her "
        "photo to keep it.")
    assert store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=102, set_no=2)) == "1"
    assert store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=102, set_no=1)) is None


def test_an_ordinary_set_of_a_member_without_an_edit_keeps_the_full_face(h, tmp_path):
    from leeroy_mind.selfies.store import queue_more
    from selfies.test_varied_sets import _cover, _logged, lust, make_stage_for
    lust(h, 101)
    _cover(h)
    queue_more(h.store, 101, now=h.T)
    cfg = SelfieCfg(enabled=True, identity_sets=True)
    log = tmp_path / "requests.log"
    run(make_stage_for(cfg, tmp_path, FAKE_SELFIE_IDENTITY="1", FAKE_SELFIE_LOG=str(log))(h.ctx()))
    rows = [r for r in _logged(log) if r[0].startswith("101:2:")]
    assert len(rows) == 4 and all(float(r[3]) == 0.6 for r in rows)


def test_undo_puts_back_a_protection_that_was_there_and_keeps_a_later_ones(store, clock, tmp_path):
    from leeroy_mind.personas.io import import_personas  # noqa: F401  (an owner save, as an import)
    app_for(store, clock, tmp_path)
    save_woman(store, 102)
    with store.transaction():  # an earlier owner change of her looks protected them
        store.conn.execute("INSERT INTO persona_looks_protection VALUES (102, 'owner', 1)")
    made = set_looks(store, 102, weight_kg=88, bust=None, now=5000.0)
    assert _protected(store, 102) == ("owner", made.version)
    assert undo(store, made.ledger, now=6000.0) is True
    assert _protected(store, 102) == ("owner", 1)  # as it was before the edit
    second = set_looks(store, 102, weight_kg=90, bust=None, now=7000.0)
    p = load_persona(store, 102)  # a later owner change of her hair holds the protection
    name, race, cls = TEST_CAST[102]
    later = save_persona(store, replace(p, appearance=replace(p.appearance, hair_colour="auburn")),
                         source="owner", ts=7100.0, race=RACES[race], class_name=CLASSES[cls])
    assert _protected(store, 102) == ("owner", later)
    assert undo(store, second.ledger, now=7200.0) is True
    assert _protected(store, 102)[1] != 1  # not taken back: a later owner change holds it


def test_an_edit_while_her_set_renders_says_so_and_a_set_with_her_new_figure_follows(store, clock,
                                                                                       tmp_path):
    from leeroy_mind.selfies.store import queue_more
    app = app_for(store, clock, tmp_path)
    save_woman(store, 102)
    ready(store, tmp_path, 102, 1)
    queue_more(store, 102, now=5.0)
    files.staging_dir(tmp_path, 102, 2).mkdir(parents=True)  # the night is rendering set 2
    reply = run(LooksService(app).handle(looks()))
    assert reply["set"] == 2 and reply["message"].endswith(
        "Her set is rendering now from her previous looks; a set with her new figure follows "
        "it. Pick one as her photo to keep it.")
    # marked even while it renders (controller, round 4): if that render fails or is cancelled,
    # the set renders again from the new looks and must take the light face then
    from leeroy_mind.selfies.store import LOOKS_EDIT_SET_KEY
    assert store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=102, set_no=2)) == "1"


def test_a_set_whose_render_an_edit_overtook_is_stamped_old_and_a_fresh_one_queued(h, tmp_path,
                                                                                   monkeypatch):
    """Review of #316: a set stamped with the version in force at publish read as current when
    an edit landed mid-render. It is stamped with the version its prompts were built from, and a
    fresh looks-edit set is queued that renders from the persona after the edit."""
    from leeroy_mind.selfies import render
    from leeroy_mind.selfies.store import LOOKS_EDIT_SET_KEY, queue_more, queued_sets
    from selfies.test_varied_sets import _cover, lust, make_stage_for
    lust(h, 101, appearance=replace(APPEARANCE, build="voluptuous"))
    _cover(h)
    queue_more(h.store, 101, now=h.T)  # set 2, his "4 more"
    built = h.store.persona_versions()[101]
    real = render.render_candidates
    edits = []

    async def edit_midway(*a, **kw):  # the owner saves her looks while set 2 renders
        if not edits:
            edits.append(set_looks(h.store, 101, weight_kg=96, bust="very_large", now=h.T + 5))
        return await real(*a, **kw)
    monkeypatch.setattr(render, "render_candidates", edit_midway)
    cfg = SelfieCfg(enabled=True, identity_sets=True)
    run(make_stage_for(cfg, tmp_path, FAKE_SELFIE_IDENTITY="1")(h.ctx()))
    assert edits and edits[0].version == built + 1
    stamped = h.store.conn.execute("SELECT persona_version FROM selfie_set WHERE guid=101 AND "
                                   "set_no=2").fetchone()[0]
    assert stamped == built  # what its prompts were built from, not the version at publish
    fresh = h.store.conn.execute("SELECT set_no, status, persona_version FROM selfie_set WHERE "
                                 "guid=101 AND set_no=3").fetchone()
    assert fresh is not None and h.store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=101, set_no=3))
    if fresh[1] == "ready":  # the same stage rendered it, from the persona after the edit
        assert fresh[2] == built + 1
    else:
        assert [s.set_no for s in queued_sets(h.store)] == [3]


def test_queue_looks_set_marks_a_queued_set_whether_or_not_it_renders(store, clock, tmp_path):
    from leeroy_mind.selfies.service import queue_looks_set
    from leeroy_mind.selfies.store import LOOKS_EDIT_SET_KEY, queue_more
    app_for(store, clock, tmp_path)
    save_woman(store, 102)
    p = load_persona(store, 102)
    args = dict(human=OWNER, now=6.0, day="2026-10-29", data_dir=tmp_path)
    queue_more(store, 102, now=5.0)
    staging = files.staging_dir(tmp_path, 102, 1)
    staging.mkdir(parents=True)
    assert queue_looks_set(store, SelfieCfg(enabled=True), p, 102, **args) == (1, False,
                                                                               "rendering")
    assert store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=102, set_no=1)) == "1"
    store.conn.execute("DELETE FROM meta WHERE key=?", (LOOKS_EDIT_SET_KEY.format(guid=102,
                                                                                  set_no=1),))
    staging.rmdir()  # a set not started yet takes the mark the same way
    assert queue_looks_set(store, SelfieCfg(enabled=True), p, 102, **args) == (1, False, "queued")
    assert store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=102, set_no=1)) == "1"


def test_a_marked_set_whose_render_was_cancelled_renders_again_under_the_light_face(h, tmp_path):
    """Controller, round 4: an edit while her set renders marks it; the render is cut short
    (his login cancels the booth: only its <set>.part is left), and when the set renders again
    from her new looks every slot takes her cover's face at MIN_SCALE."""
    from leeroy_mind.selfies.service import queue_looks_set
    from leeroy_mind.selfies.store import queue_more
    from selfies.test_varied_sets import _cover, _logged, lust, make_stage_for
    lust(h, 101)
    _cover(h)
    queue_more(h.store, 101, now=h.T)  # set 2
    files.staging_dir(h.settings.data_dir, 101, 2).mkdir(parents=True)  # rendering when he edits
    set_looks(h.store, 101, weight_kg=96, bust="very_large", now=h.T + 1)
    state = queue_looks_set(h.store, SelfieCfg(enabled=True), load_persona(h.store, 101), 101,
                            human=1, now=h.T + 1, day="2026-10-29",
                            data_dir=h.settings.data_dir)
    assert state == (2, False, "rendering")
    log = tmp_path / "requests.log"
    cfg = SelfieCfg(enabled=True, identity_sets=True)
    run(make_stage_for(cfg, tmp_path, FAKE_SELFIE_IDENTITY="1", FAKE_SELFIE_LOG=str(log))(h.ctx()))
    rows = [r for r in _logged(log) if r[0].startswith("101:2:")]
    assert len(rows) == 4 and all(float(r[3]) == 0.3 for r in rows)


def test_a_failed_stale_check_is_logged_and_the_stage_goes_on(h, tmp_path, monkeypatch, caplog):
    """Review of #316: render._stale runs after the set is saved ready; an error there is logged
    and the stage renders the next set."""
    from leeroy_mind.selfies import render
    from leeroy_mind.selfies.store import queue_more, ready_set
    from selfies.test_varied_sets import _cover, lust, make_stage_for
    lust(h, 101)
    lust(h, 102)
    _cover(h, 101)
    _cover(h, 102)
    queue_more(h.store, 101, now=h.T)
    queue_more(h.store, 102, now=h.T)

    def broken(*a, **kw):
        raise RuntimeError("a stale check that fails")
    monkeypatch.setattr(render, "_stale", broken)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_stage_for(SelfieCfg(enabled=True), tmp_path)(h.ctx()))
    assert ready_set(h.store, 101, 2) is not None and ready_set(h.store, 102, 2) is not None
    failed = [r for r in caplog.records
              if getattr(r, "leeroy_event", None) == "selfies_stale_check_failed"]
    assert [(r.leeroy_fields["guid"], r.leeroy_fields["set"], r.leeroy_fields["error"])
            for r in failed] == [(101, 2, "RuntimeError"), (102, 2, "RuntimeError")]
    assert "a stale check that fails" not in caplog.text



# -- glutes, waist and hips (owner, 2026-11-15) --------------------------------------------------

def test_glutes_waist_and_hips_save_with_the_bust_in_one_edit_and_undo(store, clock, tmp_path):
    app_for(store, clock, tmp_path)
    save_woman(store, 102)
    made = set_looks(store, 102, weight_kg=64, bust="large", glutes="very_large", waist="narrow",
                     hips="very_wide", now=5000.0)
    a = load_persona(store, 102).appearance
    assert (a.bust, a.glutes, a.waist, a.hips) == ("large", "very_large", "narrow", "very_wide")
    assert _ledger(store, 102)[-1]["text"] == (
        "bust the build's own -> large, glutes not said -> very large, "
        "waist the build's own -> narrow, hips the build's own -> very wide")
    kept = set_looks(store, 102, weight_kg=70, bust="large", now=5100.0)  # shapes not given: kept
    a = load_persona(store, 102).appearance
    assert (a.weight_kg, a.glutes, a.waist, a.hips) == (70, "very_large", "narrow", "very_wide")
    assert undo(store, kept.ledger, now=5200.0) and undo(store, made.ledger, now=5300.0)
    a = load_persona(store, 102).appearance
    assert (a.weight_kg, a.bust, a.glutes, a.waist, a.hips) == (64, None, None, None, None)
    from leeroy_mind.personas.schema import persona_to_dict
    assert not {"bust", "glutes", "waist", "hips"} & set(persona_to_dict(
        load_persona(store, 102))["appearance"])  # stored as before the fields existed


def test_a_receipt_from_before_the_shapes_still_undoes(store, clock, tmp_path):
    """A #316 receipt holds weight and bust only: its undo compares and restores those."""
    app_for(store, clock, tmp_path)
    save_woman(store, 102)
    made = set_looks(store, 102, weight_kg=88, bust="large", now=5000.0)
    key = RECEIPT.format(ledger=made.ledger)
    receipt = json.loads(store.get_meta(key))
    for side in ("before", "after"):
        receipt[side] = {k: receipt[side][k] for k in ("weight_kg", "bust")}
    store.set_meta(key, json.dumps(receipt))
    assert undo(store, made.ledger, now=6000.0) is True
    a = load_persona(store, 102).appearance
    assert (a.weight_kg, a.bust) == (64, None)


@pytest.mark.parametrize("over,words", [
    ({"glutes": "huge"}, "Glutes are one of small, medium, large, very large"),
    ({"waist": "tiny"}, "Waist is one of very narrow, narrow, average, wide"),
    ({"hips": 3}, "Hips are one of narrow, average, wide, very wide")])
def test_a_shape_off_its_list_is_refused(store, clock, tmp_path, over, words):
    app_for(store, clock, tmp_path)
    save_woman(store, 102)
    with pytest.raises(LooksError, match=words):
        set_looks(store, 102, weight_kg=64, bust=None, now=1.0, **over)
    with pytest.raises(BadRequest, match=words):
        looks_args({**looks(), **over})


@pytest.mark.parametrize("name,words", [("glutes", "Glutes are"), ("waist", "Waist is"),
                                        ("hips", "Hips are")])
def test_a_man_has_no_glutes_waist_or_hips(store, clock, tmp_path, name, words):
    app = app_for(store, clock, tmp_path)
    value = {"glutes": "large", "waist": "narrow", "hips": "wide"}[name]
    reply = run(LooksService(app).handle({**looks(guid=101, bust=None), name: value}))
    assert reply == {"ok": False, "error": f"{words} for women only."}


def test_looks_args_take_the_shapes_optionally_and_nothing_else():
    full = {**looks(), "glutes": "large", "waist": None, "hips": "very_wide"}
    assert looks_args(full) == full
    assert looks_args({**looks(), "hips": "wide"}) == {**looks(), "hips": "wide"}
    with pytest.raises(BadRequest, match="and any of glutes, waist, hips"):
        looks_args({**looks(), "thighs": "large"})
    with pytest.raises(BadRequest, match="exactly ledger"):
        looks_args({"action": "undo", "ledger": 3, "hips": "wide"})


def test_a_shape_edit_queues_a_light_face_set_through_the_service(store, clock, tmp_path):
    from leeroy_mind.selfies.store import LOOKS_EDIT_SET_KEY
    app = app_for(store, clock, tmp_path)
    save_woman(store, 102, build="curvy")
    reply = run(LooksService(app).handle({**looks(weight_kg=64, bust=None), "glutes": "large",
                                          "waist": "very_narrow", "hips": "very_wide"}))
    assert reply["ok"] and reply["set"] == 1
    assert store.get_meta(LOOKS_EDIT_SET_KEY.format(guid=102, set_no=1)) == "1"
    spec = build_prompt(app.personas.base(102))
    assert ("curvy hourglass figure, full bust, very wide hips, very narrow waist", 0) \
        in spec.phrases
