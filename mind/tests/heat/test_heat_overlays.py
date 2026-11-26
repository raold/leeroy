"""The owner's files for the heat tiers: the lookup order and its fallbacks, the listing and the
CLI that never show text, the console's view, and that nothing ever writes into prompts/.
Every file here is an invented stand-in; the repo holds no content for them."""
import hashlib
import random

import pytest

from conftest import HUMAN, make_persona, seed_test_cast
from leeroy_mind.console.heatview import OFF_NOTE, UNSET_NOTE, heat_view
from leeroy_mind.heat import overlays, photos
from leeroy_mind.heat import explain_row, what_changes
from leeroy_mind.heat.config import HeatConfig
from leeroy_mind.heat.session import SessionBook
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.talk import romance as R
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning.cli import tuning_main

SECRET = "ZQ-OWNER-WORDS"  # stands in for the owner's wording: must never be printed or listed


def put(root, name, text, folder="heat"):
    path = root / "prompts" / folder if folder else root / "prompts"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.txt").write_bytes(text.encode("utf-8"))


def digest(root):
    files = sorted((root / "prompts").rglob("*")) if (root / "prompts").exists() else []
    return [(str(f.relative_to(root)), hashlib.sha256(f.read_bytes()).hexdigest())
            for f in files if f.is_file()]


def test_with_no_heat_files_every_tier_reads_what_it_read_before(tmp_path):
    for tier in ("warm", "flirty", "suggestive"):
        assert overlays.band_text(tier, tmp_path) == R.BUILTIN_ROMANCE[tier]
    assert overlays.band_text("frank", tmp_path) == R.BUILTIN_ROMANCE["suggestive"]
    assert overlays.band_text("explicit", tmp_path) == R._band_text("explicit", tmp_path)
    put(tmp_path, "romance_explicit", "LEGACY ONE\n---\nLEGACY TWO", folder="")
    put(tmp_path, "romance_explicit_whisper", "LEGACY TAIL", folder="")
    for seed in range(6):  # the same draw as before the dial
        assert overlays.band_text("explicit", tmp_path, random.Random(seed)) == R._band_text(
            "explicit", tmp_path, random.Random(seed))
    assert overlays.tail_variants("explicit", tmp_path) == ("LEGACY TAIL",)
    assert overlays.tail_variants("frank", tmp_path) == ()
    assert overlays.tail_variants("suggestive", tmp_path) == ()
    assert overlays.has_explicit(tmp_path)


def test_the_lookup_order_heat_file_then_legacy_then_the_tier_below(tmp_path):
    put(tmp_path, "romance_explicit", "LEGACY EXPLICIT", folder="")
    put(tmp_path, "romance_suggestive", "LEGACY SUGGESTIVE", folder="")
    assert overlays.band_text("suggestive", tmp_path) == "LEGACY SUGGESTIVE"
    assert overlays.band_text("frank", tmp_path) == "LEGACY SUGGESTIVE"  # the tier below
    assert overlays.band_text("explicit", tmp_path) == "LEGACY EXPLICIT"
    put(tmp_path, "suggestive", "HEAT SUGGESTIVE")
    assert overlays.band_text("suggestive", tmp_path) == "HEAT SUGGESTIVE"
    assert overlays.band_text("frank", tmp_path) == "HEAT SUGGESTIVE"
    put(tmp_path, "frank", "HEAT FRANK\n---\nHEAT FRANK TWO")
    assert overlays.band_text("frank", tmp_path) == "HEAT FRANK"
    assert {overlays.band_text("frank", tmp_path, random.Random(n)) for n in range(20)} == {
        "HEAT FRANK", "HEAT FRANK TWO"}
    put(tmp_path, "explicit", "HEAT EXPLICIT")
    assert overlays.band_text("explicit", tmp_path) == "HEAT EXPLICIT"  # wins over the legacy
    put(tmp_path, "explicit_whisper", "HEAT TAIL")
    put(tmp_path, "romance_explicit_whisper", "LEGACY TAIL", folder="")
    assert overlays.tail_variants("explicit", tmp_path) == ("HEAT TAIL",)
    put(tmp_path, "frank_whisper", "FRANK TAIL")
    assert overlays.tail_variants("frank", tmp_path) == ("FRANK TAIL",)


def test_explicit_without_his_explicit_file_reads_the_frank_one(tmp_path):
    put(tmp_path, "frank", "HEAT FRANK")
    assert overlays.band_text("explicit", tmp_path) == "HEAT FRANK"
    assert not overlays.has_explicit(tmp_path)


def test_a_variant_is_capped_like_the_band_overlays(tmp_path):
    put(tmp_path, "frank", "word " * 2000)
    text = overlays.band_text("frank", tmp_path)
    assert len(text) <= R.OVERLAY_CAP_TOKENS * 4 + 8


def test_unknown_names_and_blank_files(tmp_path):
    with pytest.raises(ValueError):
        overlays.variants("romance_explicit", tmp_path)
    put(tmp_path, "frank", "  \n\n")
    assert overlays.variants("frank", tmp_path) == ()
    assert overlays.one_line("session_open", tmp_path) == ""
    put(tmp_path, "session_open", "OPEN LINE")
    assert overlays.one_line("session_open", tmp_path) == "OPEN LINE"


def test_the_listing_has_names_counts_and_fallbacks_never_text(tmp_path):
    put(tmp_path, "romance_explicit", SECRET + " legacy", folder="")
    put(tmp_path, "frank", f"{SECRET} one\n---\n{SECRET} two")
    put(tmp_path, "public_block", SECRET)
    before = digest(tmp_path)
    rows = {r["name"]: r for r in overlays.listing(tmp_path)}
    assert set(rows) == set(overlays.FILES)
    assert (rows["frank"]["present"], rows["frank"]["variants"]) == (True, 2)
    assert rows["frank"]["tokens"] > 0 and rows["frank"]["reads"] == "heat/frank.txt"
    assert rows["explicit"]["reads"] == "romance_explicit.txt" and not rows["explicit"]["present"]
    assert rows["explicit"]["legacy_variants"] == 1
    assert rows["suggestive"]["reads"] == "the built-in suggestive wording"
    assert rows["voice"]["reads"] == "nothing"
    assert SECRET not in repr(rows)
    assert digest(tmp_path) == before  # nothing writes into prompts/


@pytest.fixture
def data(store):
    from pathlib import Path
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def cast(store, data):
    seed_test_cast(store)
    store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0, level=80,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
    store.set_meta("human_guid", str(HUMAN))
    save_persona(store, make_persona(102, "Belra", player_gender="female", stance="lust"),
                 source="seed", ts=1.0, race="Night Elf", class_name="Druid")
    save_persona(store, make_persona(104, "Dunna", player_gender="female", stance="none"),
                 source="seed", ts=1.0, race="Dwarf", class_name="Paladin")
    (data / "mind.toml").write_bytes(b"[romance]\ndial = 9\n[heat]\nenabled = true\n")
    put(data, "romance_explicit", SECRET, folder="")
    put(data, "frank", SECRET)
    return store


def _sets(store, *sets):
    pic = C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})
    C.apply_sets(store, [C.Set(s, t, "heat", v) for s, t, v in sets], pic, source="console",
                 now=2.0)


def test_the_console_view_is_built_from_the_codes_own_words(cast, data, clock):
    unset = heat_view(cast.conn, config=HeatConfig(enabled=True), data_dir=data, romance_file=10,
                      now=clock.now())
    assert (unset["enabled"], unset["world"], unset["set"], unset["follows"], unset["suggest"],
            unset["members"], unset["changes"]) == (True, None, False, 10, 9, [], [])
    assert unset["note"] == UNSET_NOTE.format(dial=10)
    _sets(cast, ("world", "", 9))
    view = heat_view(cast.conn, config=HeatConfig(enabled=True), data_dir=data, romance_file=10,
                     now=clock.now())
    assert (view["enabled"], view["world"], view["set"], view["follows"]) == (True, 9, True, None)
    assert view["rows"] == [explain_row(h) for h in range(11)]
    assert view["changes"] == [what_changes(9, h) for h in range(11)]
    assert view["photos"] == photos.listing(4)
    by = {m["guid"]: m for m in view["members"]}
    assert set(by) == {102, 104}  # the women; men have no heat card
    assert by[102]["title"] == "Heat 9 (world 9, hers +0)" and by[102]["applies"]
    assert by[104] == {**by[104], "applies": False,
                       "title": "stance none: heat does not apply"}
    assert SECRET not in repr(view) and "learning to cook" not in repr(view)  # no persona text

    _sets(cast, ("world", "", 6), ("member", "102", -2), ("group", "everyone", 1))
    SessionBook(cast, clock).set(102, "clean", medium="discord")
    view = heat_view(cast.conn, config=HeatConfig(enabled=True, public_cap=True), data_dir=data,
                     romance_file=9, now=clock.now())
    assert (view["world"], view["set"], view["follows"], view["public_cap"]) == (6, True, None,
                                                                                 True)
    assert view["groups"] == [{"target": "everyone", "name": view["groups"][0]["name"],
                               "describe": view["groups"][0]["describe"], "offset": 1}]
    member = {m["guid"]: m for m in view["members"]}[102]
    assert (member["heat"], member["offset"], member["groups"], member["pinned"],
            member["session"]) == (2, -2, 1, -2, "clean")
    assert "clean for this session" in member["caps"]
    assert view["sessions"]["members"] == {"102": {"mode": "clean", "ends": "morning"}}


def test_the_console_view_with_heat_off_says_so_and_still_lists_the_files(cast, data, clock):
    view = heat_view(cast.conn, config=HeatConfig(enabled=False), data_dir=data, romance_file=9,
                     now=clock.now())
    assert not view["enabled"] and view["note"] == OFF_NOTE
    assert {o["name"] for o in view["overlays"]} == set(overlays.FILES)
    unread = heat_view(cast.conn, config=HeatConfig(enabled=True), data_dir=data,
                       romance_file=None, now=clock.now())
    assert unread["world"] is None and unread["members"] == []


def test_the_cli_reads_status_explain_and_overlays_and_never_text(cast, data, capsys):
    _sets(cast, ("world", "", 7), ("member", "102", 1))
    cast.conn.commit()
    before = digest(data)
    assert tuning_main(["--data-dir", str(data), "heat", "status"]) == 0
    out = capsys.readouterr().out
    assert "[heat] enabled = true, public_cap = false" in out and "world heat 7 (set)" in out
    assert "Belra (102): heat +1" in out
    assert tuning_main(["--data-dir", str(data), "heat", "explain"]) == 0
    assert capsys.readouterr().out.strip() == "heat 7: " + explain_row(7)
    assert tuning_main(["--data-dir", str(data), "heat", "explain", "--heat", "3"]) == 0
    assert capsys.readouterr().out.strip() == "heat 3: " + explain_row(3)
    assert tuning_main(["--data-dir", str(data), "heat", "explain", "--guid", "102"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Belra (102): Heat 8 (world 7, hers +1)") and "Wording: frank" in out
    assert tuning_main(["--data-dir", str(data), "heat", "explain", "--guid", "999"]) == 1
    assert tuning_main(["--data-dir", str(data), "heat", "explain", "--heat", "12"]) == 1
    capsys.readouterr()
    assert tuning_main(["--data-dir", str(data), "heat", "overlays"]) == 0
    out = capsys.readouterr().out
    assert "heat/frank.txt: 1 variant" in out and "heat/explicit.txt: not there" in out
    assert SECRET not in out
    assert digest(data) == before  # read-only: nothing in prompts/ moved


def test_no_tool_writes_into_the_prompts_folder():
    """By construction: the heat package opens the owner's files for reading only."""
    import inspect

    from leeroy_mind.heat import config, guard, live, resolve, session, table
    for module in (overlays, config, guard, live, resolve, session, table, photos):
        source = inspect.getsource(module)
        for call in ("write_text", "write_bytes", ".unlink(", "open(", "rename(", "mkdir("):
            assert call not in source, (module.__name__, call)
