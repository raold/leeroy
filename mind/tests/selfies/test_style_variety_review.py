"""Review regressions over invented members, fake models and scratch databases."""
from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from conftest import run
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.selfies import cli, kind, photo_extra, restyle, style_variety as sv
from leeroy_mind.selfies.prompt import build_prompt, looks_phrases
from leeroy_mind.selfies.style import LUST, PLAIN
from nightly.conftest import IDENT
from nightly.test_night_enrich import APPEARANCE
from nightly.test_night_photo_kind import KIND
from selfies.test_style_variety import prepared


def save(h, p, style=PLAIN):
    save_persona(h.store, p, source="generated", ts=h.T, race="Human", class_name="Warrior")
    if p.photo:
        kind._record(h.store, p.guid, style, p.photo)
    return p


@pytest.mark.parametrize("recipient", ["man", "under25", "young_looks"])
def test_lust_history_never_enters_plain_prompt(h, recipient):
    prior = h.woman(102, "lust", appearance=APPEARANCE)
    prior = save(h, replace(prior, photo=ProfilePhoto(**{**KIND,
        "outfit": "a red low-cut cocktail dress"})), LUST)
    photo_extra.record(h.store, prior, None, h.T)
    p = h.persona(101, appearance=APPEARANCE)
    if recipient == "under25":
        p = replace(p, identity=replace(p.identity, age=24))
    elif recipient == "young_looks":
        p = h.woman(101, "lust", appearance=replace(APPEARANCE, face=("baby-faced",)))
    save(h, p)
    (h.settings.data_dir / "mind.toml").write_text("[selfies]\nvariety=true")
    ctx = h.ctx({"photo": [KIND]})
    run(kind.stage_photo(ctx, variety=True))
    assert ctx.reasoner.calls == 1  # selection matches the original plain pass
    assert "low-cut" not in json.dumps(ctx.reasoner.llm.calls)


@pytest.mark.parametrize("field,text", [("outfit", "lace lingerie"),
    ("setting", "a school classroom"), ("outfit", "a retro dress"),
    ("setting", "a cafe named Saffron")])
def test_plain_hints_are_screened_and_include_other_real_names(h, field, text):
    p = h.persona(101, appearance=APPEARANCE)
    other = h.persona(102, appearance=APPEARANCE)
    save(h, replace(other, identity=replace(other.identity, real_name="Saffron")))
    photo_extra.record(h.store, replace(p, photo=ProfilePhoto(**{**KIND, field:text})), None, h.T)
    (h.settings.data_dir / "mind.toml").write_text("[selfies]\nvariety=true")
    ctx = h.ctx({"photo": [KIND, KIND]})
    run(kind.stage_photo(ctx, variety=True))
    assert text not in json.dumps(ctx.reasoner.llm.calls)


def test_real_worker_fit_keeps_identity_outfit_setting_before_light(h):
    path = Path(__file__).parents[3] / "selfie/leeroy_selfie/fit.py"
    spec = importlib.util.spec_from_file_location("actual_photo_fit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    p = h.persona(101, appearance=replace(APPEARANCE, face=(
        "small freckles across the cheeks", "thin round glasses", "a neatly trimmed beard")))
    p = replace(p, photo=ProfilePhoto(**{**KIND, "setting": "a garden party"}))
    base = build_prompt(p)
    changed = build_prompt(p, light="golden hour")
    count = lambda s: len(s.split())
    limit = count(", ".join(t for t, _ in base.phrases))
    kept, dropped = module.fit(changed.phrases, count, limit)
    assert "golden hour" in dropped
    assert all(t in kept for t, _ in looks_phrases(p, None))
    assert p.photo.outfit in kept and p.photo.setting in kept
    assert changed.phrases[-1] == ("golden hour", 9)


def test_plain_restyle_defers_to_model_and_records_old_kind(h):
    p = save(h, replace(h.persona(101, appearance=APPEARANCE), photo=ProfilePhoto(**KIND)))
    restyle.run(h.store, IDENT, h.clock, go=True)
    assert load_persona(h.store, 101).photo is None
    assert h.store.get_meta(kind.STYLE_KEY.format(guid=101)) is None
    receipt = json.loads(h.store.get_meta("selfies:restyle:last"))
    assert receipt["old_kinds"]["101"] == asdict(p.photo)


def test_setting_cap_uses_floor_above_one(h):
    p = h.woman(101, "lust", appearance=APPEARANCE)
    people = {g:replace(p, guid=g) for g in range(101, 126)}
    chosen, rejected, cap, _ = sv.assign(people, people, ident=IDENT)
    assert cap == 3 and len(chosen) == 25 and not rejected
    assert max(sum(c.photo.setting == x.photo.setting for c in chosen.values())
               for x in chosen.values()) <= 3


@pytest.mark.parametrize("setting,allowed", [("in an empty gym", {None}),
    ("at a hobby workbench", {None}), ("a city street at night", {1,3,4,5}),
    ("a garden party", {0,4})])
def test_light_fits_actual_setting(h, setting, allowed):
    p = replace(h.persona(101, appearance=APPEARANCE), photo=ProfilePhoto(**{**KIND,"setting":setting}))
    for seed in range(30):
        light = photo_extra.choose_light(p, seed, ident=IDENT)
        assert light in allowed
        if light is not None and light != 0:
            prompt = build_prompt(p, light=photo_extra.LIGHTS[light])
            assert "natural light" not in ", ".join(t for t, _ in prompt.phrases)


def test_date_outfits_exclude_work_clothes():
    assert not any(word in s for s in sv.DATE_OUTFITS for word in ("blazer", "pencil skirt", "silk blouse"))


def test_restyle_snapshot_precedes_any_migration_or_write(h, capsys, monkeypatch):
    import re
    from leeroy_mind import __main__ as main
    from leeroy_mind.storage.db import MIGRATIONS_DIR
    root = prepared(h)
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    feature = next(int(p.name[:4]) for p in files if p.name[5:] == "photo_extra.sql")
    previous_schema = feature - 1
    # Recreate the pre-feature schema: this migration and every later one (each creates only
    # its own tables, with their indexes and triggers) are pending again.
    with h.store.transaction():
        for p in files:
            if int(p.name[:4]) >= feature:
                for table in re.findall(r"^CREATE TABLE (\w+)", p.read_text(encoding="utf-8"), re.M):
                    h.store.conn.execute(f"DROP TABLE {table}")
        h.store.conn.execute(f"PRAGMA user_version={previous_schema}")
    calls = []
    original = main._snapshot_file
    def snapshot(db, dest, label):
        calls.append(label)
        return original(db, dest, label)
    monkeypatch.setattr(main, "_snapshot_file", snapshot)
    assert cli.selfies_main(["restyle", "--data-dir", str(root), "--go"]) == 0
    capsys.readouterr()
    assert calls == ["pre-restyle"]
    snapshots = list((root / "snapshots").glob("*.db"))
    assert len(snapshots) == 1
    with sqlite3.connect(snapshots[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == previous_schema
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='photo_extra'").fetchone() is None


def test_restyle_reports_adult_floor(h):
    p = h.persona(101, appearance=APPEARANCE)
    save(h, replace(p, identity=replace(p.identity, age=24)))
    report = restyle.run(h.store, IDENT, h.clock)
    assert report["skipped"] == [{"guid":101, "code":"adult_floor"}]


def test_seeded_history_normalizes_articles_spacing_and_colour(h):
    for g, outfit in [(102, "A blue jumper"), (103, "the   BLUE jumper"), (104, "a red jumper")]:
        save(h, replace(h.persona(g, appearance=APPEARANCE), photo=ProfilePhoto(**{**KIND,"outfit":outfit})))
    h.persona(101, appearance=APPEARANCE)
    (h.settings.data_dir / "mind.toml").write_text("[selfies]\nvariety=true")
    ctx = h.ctx({"photo":[KIND]})
    run(kind.stage_photo(ctx, variety=True))
    user = ctx.reasoner.llm.calls[0]["messages"][-1]["content"]
    assert '"value": "jumper", "count": 3' in user


def test_attach_freezes_variety_and_does_not_read_config_per_member(h, tmp_path, monkeypatch):
    from selfies.fakes import mind_app
    from selfies.test_style_variety import population
    from leeroy_mind.selfies import config
    app = mind_app(tmp_path, toml="[selfies]\nenabled=true\nvariety=true")
    try:
        stage = next(s for s in app.night_runner.stages if s.name == "photo")
        (tmp_path / "mind.toml").write_text("[selfies]\nvariety=false")
        population(h)
        def unexpected(*args):
            pytest.fail("per-member configuration read")
        monkeypatch.setattr(config, "load_selfie_config", unexpected)
        ctx = h.ctx()
        run(stage.fn(ctx))
        assert ctx.reasoner.calls == 0
        assert all(load_persona(h.store, g).photo for g in (101,102,103,104,105))
    finally:
        app.store.close()


@pytest.mark.parametrize("fields", [
    {"outfit":"a loose tee and full-length leggings", "setting":"in a living room"},
    {"kind":"mirror_selfie", "framing":"face"},
    {"kind":"selfie", "framing":"three_quarter"},
    {"kind":"headshot", "framing":"upper_body"},
    {"kind":"snapshot", "framing":"three_quarter"},
])
def test_plain_rechoice_preserves_pairings(h, fields):
    h.woman(101, "lust", appearance=replace(APPEARANCE, face=("baby-faced",)))
    ctx = h.ctx({"photo":[{**KIND, **fields}]})
    run(kind.stage_photo(ctx, variety=True))
    assert (load_persona(h.store,101).photo is None) == (fields.get("framing") == "three_quarter")


@pytest.mark.parametrize("variety", [False,True])
def test_plain_restyle_rechooses_personal_kind_and_queues_new_photos(h,variety):
    from leeroy_mind.selfies.store import queued_sets
    p = h.persona(101, appearance=APPEARANCE)
    save(h, replace(p, photo=ProfilePhoto(**KIND)))
    restyle.run(h.store, IDENT, h.clock, go=True)
    assert not queued_sets(h.store)
    personal = {**KIND, "outfit":"a green rain jacket", "setting":"at a quiet lake"}
    ctx = h.ctx({"photo":[personal]})
    run(kind.stage_photo(ctx, variety=variety))
    assert load_persona(h.store,101).photo == ProfilePhoto(**personal)
    assert {s.guid for s in queued_sets(h.store)} == {101}
    assert h.store.get_meta("selfies:restyle:pending:101") is None


def test_snapshot_failure_stops_restyle_and_reports_fixed_code(h, capsys, monkeypatch):
    from leeroy_mind import __main__ as main
    root = prepared(h)
    before = h.store.persona_versions()
    def broken(*args):
        raise OSError("private path should not print")
    monkeypatch.setattr(main, "_snapshot_file", broken)
    assert cli.selfies_main(["restyle","--data-dir",str(root),"--go"]) == 1
    assert capsys.readouterr().err == "selfies restyle failed: filesystem_error\n"
    assert h.store.persona_versions() == before


def test_migration_does_not_freeze_light_list_and_reader_rejects_unknown(h):
    p = save(h, replace(h.persona(101, appearance=APPEARANCE),photo=ProfilePhoto(**KIND)))
    h.store.conn.execute("INSERT INTO photo_extra VALUES(?,?,?,?)",(101,kind._digest(p.photo),500,h.T))
    assert photo_extra.light_for(h.store,p) == ""


@pytest.mark.parametrize("error,code", [("personas_changed","personas_changed"),
    ("too_many_members","too_many_members"),
    ("cannot identify the human from the stored roster","human_unidentified"),
    ("stored human is absent from the roster","human_not_on_roster"),
    ("untrusted private detail","restyle_failed")])
def test_restyle_reports_fixed_value_error_codes(h, capsys, monkeypatch, error, code):
    root = prepared(h)
    def broken(*args):
        raise ValueError(error)
    monkeypatch.setattr(cli,"_identity",broken)
    assert cli.selfies_main(["restyle","--data-dir",str(root),"--dry-run"]) == 1
    assert capsys.readouterr().err == f"selfies restyle failed: {code}\n"
