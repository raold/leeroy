"""Synthetic WDBC fixtures; live/DB checks require explicit environment paths."""
import os
import struct
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from leeroy_mind.library.world import WorldSource, dbc_dir_of
from leeroy_mind.personas.roster import DbInfo, read_db_info
from leeroy_mind.training import spells as S


def write_dbc(path, fields, records, strings=b"\0"):
    path.write_bytes(struct.pack("<4s4I", b"WDBC", len(records), fields, fields * 4,
                                 len(strings)) + b"".join(
        struct.pack(f"<{fields}I", *(v & 0xFFFFFFFF for v in row)) for row in records) + strings)


def spell(id, name="Frost Strike", rank="", **values):
    return id, name, rank, values


def fixture_book(tmp_path, specs, *, casts=(), skills=(), runes=(), world=None):
    strings = bytearray(b"\0")
    rows = []
    for id, name, rank, values in specs:
        row = [0] * 234
        row[0] = id
        for col, value in ((136, name), (153, rank)):
            row[col] = len(strings)
            strings.extend(value.encode() + b"\0")
        for field, value in values.items():
            row[int(field.removeprefix("c"))] = value
        rows.append(row)
    write_dbc(tmp_path / "Spell.dbc", 234, rows, bytes(strings))
    write_dbc(tmp_path / "SpellCastTimes.dbc", 4, casts)
    write_dbc(tmp_path / "SkillLineAbility.dbc", 14, skills)
    write_dbc(tmp_path / "SpellRuneCost.dbc", 5, runes)
    return S.load_spellbook(tmp_path, world=world)


def ability(id, spell_id, mask, next_id=0):
    row = [0] * 14
    row[0], row[1], row[2], row[4], row[8] = id, 8, spell_id, mask, next_id
    return row


def test_reads_a_spell_with_its_cast_time_and_names(tmp_path):
    book = fixture_book(tmp_path, [spell(49143, rank="Rank 1", c28=7, c29=8000,
        c30=10000, c39=70, c41=6, c42=40, c204=5, c206=1500, c225=16, c226=2)],
        casts=[(7, 1500, 0, 0)], runes=[(2, 0, 1, 1, 10)])
    info = book.info(49143)
    assert (info.id, info.name, info.rank, info.cast_ms) == (49143, "Frost Strike", "Rank 1", 1500)
    assert (info.cooldown_ms, info.gcd_ms, info.power_type, info.cost, info.cost_pct) == (
        10000, 1500, 6, 40, 5)
    assert (info.rune_cost, info.school, info.level) == ((0, 1, 1), 16, 70)
    assert len(info.effects) == 3
    assert book.loaded == frozenset({"Spell.dbc", "SpellCastTimes.dbc",
                                     "SpellRuneCost.dbc", "SkillLineAbility.dbc"})
    assert book.info(999) is None
    assert book.name(999) == "spell 999"
    assert book.ids("missing") == frozenset()


def test_ids_by_name_cover_ranks_and_variants(tmp_path):
    book = fixture_book(tmp_path, [spell(10, "Coldflame"), spell(11, "Coldflame", "Rank 2"),
                                   spell(12, "COLDFLAME")])
    assert book.ids("coldflame") == frozenset({10, 11, 12})


def test_class_spells_take_the_highest_rank_at_level(tmp_path):
    book = fixture_book(tmp_path, [spell(10, "Frostbolt", "Rank 1", c39=70),
        spell(11, "Frostbolt", "Rank 2", c39=80), spell(12, "Other", c39=1)],
        skills=[ability(1, 10, 128, 11), ability(2, 11, 128), ability(3, 12, 1)])
    assert [s.id for s in book.class_spells(8)] == [11]
    assert [s.id for s in book.class_spells(8, 79)] == [10]
    assert book.class_spells(8, 69) == []
    assert book.class_spells(0) == []
    assert book.class_spells(1000000) == []


def test_class_spells_are_deterministic_for_unordered_duplicates_and_cycles(tmp_path):
    specs = [spell(10, "Test", "Rank 9", c39=70), spell(11, "Test", "Rank 10", c39=70),
             spell(12, "Alpha", c39=1)]
    skills = [ability(1, 11, 128, 10), ability(2, 10, 128, 11),
              ability(3, 11, 128), ability(4, 12, 128)]
    first = fixture_book(tmp_path, specs, skills=skills)
    second = fixture_book(tmp_path, list(reversed(specs)), skills=list(reversed(skills)))
    assert [s.id for s in first.class_spells(8)] == [12, 11]
    assert first.class_spells(8) == second.class_spells(8)


def test_superseding_spell_wins_even_when_rank_label_is_absent(tmp_path):
    book = fixture_book(tmp_path, [spell(20, "Test", c39=1), spell(10, "Test", c39=1)],
                        skills=[ability(1, 20, 128, 10), ability(2, 10, 128)])
    assert [s.id for s in book.class_spells(8)] == [10]


def test_signed_base_points_and_die_sides(tmp_path):
    book = fixture_book(tmp_path, [spell(10, c71=2, c74=-2, c80=-1, c95=3, c116=15)])
    assert book.info(10).effects[0] == S.Effect(2, 3, -1, -2, 15)


def test_interrupt_flag(tmp_path):
    book = fixture_book(tmp_path, [spell(10, c73=68), spell(11)])
    assert book.info(10).interrupt is True
    assert book.info(11).interrupt is False


def test_unread_columns_are_never_decoded(tmp_path, monkeypatch):
    original = S.Dbc.__init__
    def small_only(self, data, name="dbc"):
        assert name != "Spell.dbc", "large table must use the column reader"
        return original(self, data, name)
    monkeypatch.setattr(S.Dbc, "__init__", small_only)
    book = fixture_book(tmp_path, [spell(10, c137=0xFFFFFFFF, c170=0xFFFFFFFF,
                                        c187=0xFFFFFFFF, c209=0xFFFFFFFF)])
    assert book.name(10) == "Frost Strike"


@pytest.mark.parametrize("values", [{"c136": 0xFFFFFFFF}, {"c153": 0xFFFFFFFF}])
def test_invalid_selected_string_offsets_skip_the_table(tmp_path, caplog, values):
    book = fixture_book(tmp_path, [spell(10, **values)])
    assert book.info(10) is None
    assert "name offset" in caplog.text


def test_oversized_names_are_bounded(tmp_path, caplog):
    book = fixture_book(tmp_path, [spell(10, "x" * 4097)])
    assert book.info(10) is None
    assert "oversized name" in caplog.text


@pytest.mark.parametrize("data", [b"WDBC", b"not a dbc file at all!",
    struct.pack("<4s4I", b"WDBC", 0xFFFFFFFF, 234, 936, 1) + b"\0",
    struct.pack("<4s4I", b"WDBC", 0, 233, 932, 1) + b"\0"])
def test_bad_file_is_skipped_not_fatal(tmp_path, caplog, data):
    (tmp_path / "Spell.dbc").write_bytes(data)
    book = S.load_spellbook(tmp_path)
    assert book.info(10) is None
    assert "Spell.dbc" in caplog.text


def test_bad_small_table_is_skipped_and_signed_cast_time_is_kept(tmp_path, caplog):
    book = fixture_book(tmp_path, [spell(10, c28=2)], casts=[(2, -1, 0, 0)])
    assert book.info(10).cast_ms == -1
    write_dbc(tmp_path / "SpellCastTimes.dbc", 1, [(2,)])
    book = S.load_spellbook(tmp_path)
    assert book.info(10).cast_ms == 0
    assert "SpellCastTimes.dbc" not in book.loaded
    assert "SpellCastTimes.dbc" in caplog.text


def test_uint32_values_do_not_overflow_compact_signed_arrays(tmp_path):
    book = fixture_book(tmp_path, [spell(0xFFFFFFFF, c41=0xFFFFFFFE, c42=0xFFFFFFFF)])
    assert book.info(0xFFFFFFFF).cost == 0xFFFFFFFF
    assert book.info(0xFFFFFFFF).power_type == -2


def test_duplicate_spell_ids_reject_the_table(tmp_path, caplog):
    book = fixture_book(tmp_path, [spell(10), spell(10, "Different")])
    assert book.info(10) is None
    assert "duplicate" in caplog.text


def test_returned_info_and_book_are_immutable_and_world_is_copied(tmp_path):
    world = {10: (25, -100)}
    book = fixture_book(tmp_path, [spell(10)], world=world)
    world[10] = (900, 900)
    assert book.coefficient(10) == (25, -100)
    assert book.coefficient(99) == (0, 0)
    assert "spell_bonus_data" in book.loaded
    with pytest.raises((FrozenInstanceError, AttributeError)):
        book.info(10).name = "changed"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        book.extra = "changed"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        book.loaded = frozenset()


def test_empty_bonus_data_has_explicit_coverage_and_missing_tables_do_not(tmp_path):
    book = fixture_book(tmp_path, [spell(10)], world={})
    assert "spell_bonus_data" in book.loaded
    (tmp_path / "SpellRuneCost.dbc").unlink()
    book = S.load_spellbook(tmp_path)
    assert "SpellRuneCost.dbc" not in book.loaded
    assert "spell_bonus_data" not in book.loaded
    assert book.info(10).rune_cost == (0, 0, 0)


def test_read_bonus_data_uses_the_read_only_query(monkeypatch):
    db = DbInfo("localhost", 3306, "test", "synthetic", "world")
    exe = Path("mysql.exe")
    seen = []
    def fake_query(actual_db, sql, actual_exe):
        seen.append((actual_db, sql, actual_exe))
        return [["10", "25", "-100"], ["11", "0", "0"]]
    monkeypatch.setattr(S, "query", fake_query)
    assert S.read_bonus_data(db, exe) == {10: (25, -100), 11: (0, 0)}
    assert seen == [(db, "SELECT entry, ROUND(direct_bonus*100), ROUND(dot_bonus*100) "
                         "FROM spell_bonus_data", exe)]


@pytest.mark.parametrize("row", [["bad", "2", "3"], ["1", None, "3"], ["1", "2"]])
def test_bonus_data_rejects_malformed_rows_without_echoing_them(monkeypatch, row):
    monkeypatch.setattr(S, "query", lambda *args: [row])
    with pytest.raises(ValueError, match="invalid spell bonus data"):
        S.read_bonus_data(DbInfo("host", 3306, "user", "private", "world"), Path("mysql"))


def test_dbc_dir_of_conf(tmp_path):
    conf = tmp_path / "worldserver.conf"
    conf.write_text('# DataDir = "ignored"\nDataDir = "F:/x/Data"\n')
    assert dbc_dir_of(conf) == Path("F:/x/Data/dbc")
    conf.write_text("# no data directory\n")
    assert dbc_dir_of(conf) is None


def test_library_client_names_uses_shared_parse(tmp_path, monkeypatch):
    expected = tmp_path / "data" / "dbc"
    seen = []
    monkeypatch.setattr("leeroy_mind.library.world.dbc_dir_of", lambda conf: expected)
    monkeypatch.setattr("leeroy_mind.library.world.load_client_names", lambda path: seen.append(path))
    WorldSource(conf=tmp_path / "unused.conf")._client_names()
    assert seen == [expected]


@pytest.mark.live
def test_real_spell_book_has_the_mechanics():
    path = os.environ.get("LEEROY_TEST_DBC_DIR")
    if not path:
        pytest.skip("set LEEROY_TEST_DBC_DIR explicitly to read installed client data")
    book = S.load_spellbook(Path(path))
    assert book.ids("Defile") and book.ids("Coldflame")
    assert "Obliterate" in {s.name for s in book.class_spells(6)}


@pytest.mark.mysql
def test_real_bonus_data():
    conf, exe = os.environ.get("LEEROY_TEST_WORLD_CONF"), os.environ.get("LEEROY_TEST_MYSQL_EXE")
    if not conf or not exe:
        pytest.skip("set LEEROY_TEST_WORLD_CONF and LEEROY_TEST_MYSQL_EXE explicitly")
    assert len(S.read_bonus_data(read_db_info(Path(conf), "WorldDatabaseInfo"), Path(exe))) > 1000
