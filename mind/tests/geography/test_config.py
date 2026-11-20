"""[geography] and [owner] in mind.toml (geography/config.py, attach.py): on by default, an
invalid table off, his home never logged, the switch written to meta for the console. Invented
homes only."""
import logging
from types import SimpleNamespace

import pytest

from leeroy_mind.geography import attach, config
from leeroy_mind.geography.config import (META_KEY, GeographyCfg, load_geography_config,
                                          load_owner_home, parse_owner)


def write(tmp_path, text):
    path = tmp_path / "mind.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_on_by_default_and_with_no_file(tmp_path):
    assert load_geography_config(tmp_path / "mind.toml") == GeographyCfg(enabled=True)
    assert load_geography_config(write(tmp_path, "[talk]\nchattiness = 4\n")).enabled


@pytest.mark.parametrize("text", ["[geography]\nenabled = 1\n", "[geography]\nsurprise = true\n",
                                  "geography = 3\n", "[geography\n"])
def test_an_invalid_table_is_off(tmp_path, text, caplog):
    caplog.set_level(logging.WARNING)
    assert load_geography_config(write(tmp_path, text)).enabled is False
    assert "geography_config_invalid" in caplog.text


def test_off_when_said(tmp_path):
    assert not load_geography_config(write(tmp_path, "[geography]\nenabled = false\n")).enabled


@pytest.mark.parametrize("table,key,detail", [
    ({"home": "Montana"}, "Montana", ""),
    ({"home": "NV", "home_detail": "  Lake   View "}, "Nevada", "Lake View"),
    ({"home": "Scotland", "home_detail": "St. Andrew's-on-Sea"}, "uk.scotland",
     "St. Andrew's-on-Sea"),
])
def test_his_home(table, key, detail):
    home = parse_owner(table)
    assert home.place.key == key and home.detail == detail


@pytest.mark.parametrize("table", [{}, {"home": ""}, None, "Montana", {"home_detail": "Lakeview"}])
def test_no_home(table):
    assert parse_owner(table) is None


@pytest.mark.parametrize("table,warned", [
    ({"home": "Atlantis"}, "key=home"), ({"home": 7}, "key=home"),
    ({"home": "x" * 61}, "key=home")])
def test_an_unknown_home_is_none_and_logged_without_its_value(table, warned, caplog):
    caplog.set_level(logging.WARNING)
    assert parse_owner(table) is None
    assert f"owner_home_invalid {warned}" in caplog.text
    assert "Atlantis" not in caplog.text


@pytest.mark.parametrize("detail", ["x" * 41, "12 Main St", "<script>", "a|b", 5, "\u0000x"])
def test_a_bad_detail_is_dropped_and_the_home_kept(detail, caplog):
    caplog.set_level(logging.WARNING)
    home = parse_owner({"home": "Wyoming", "home_detail": detail})
    assert home.place.key == "Wyoming" and home.detail == ""
    assert "owner_home_invalid key=home_detail" in caplog.text
    assert "Main St" not in caplog.text and "Wyoming" not in caplog.text


def test_load_owner_home(tmp_path):
    assert load_owner_home(tmp_path / "mind.toml") is None
    path = write(tmp_path, '[owner]\nhome = "Idaho"\nhome_detail = "Pinecrest"\n')
    assert load_owner_home(path) == config.OwnerHome(config.lookup("Idaho"), "Pinecrest")


def test_the_pin_keeps_his_home_only_while_on():
    home = parse_owner({"home": "Montana"})
    config.pin(True, home)
    assert config.on() and config.owner() == home
    config.pin(False, home)
    assert not config.on() and config.owner() is None
    config.pin("yes", home)  # only True is on
    assert not config.on()


class _Store:
    def __init__(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def test_attach_pins_and_tells_the_console_never_where(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    write(tmp_path, '[owner]\nhome = "Montana"\nhome_detail = "Pinecrest"\n')
    app = SimpleNamespace(data_dir=tmp_path, store=_Store())
    assert attach.attach(app).enabled
    assert config.on() and config.owner().place.key == "Montana"
    assert app.store.get_meta(META_KEY) == "1" and config.stored(app.store.conn)
    assert "geography on owner_home=set" in caplog.text
    assert "Montana" not in caplog.text and "Pinecrest" not in caplog.text


def test_attach_off_and_a_broken_attach(tmp_path):
    write(tmp_path, '[geography]\nenabled = false\n[owner]\nhome = "Montana"\n')
    app = SimpleNamespace(data_dir=tmp_path, store=_Store())
    assert not attach.attach(app).enabled
    assert not config.on() and config.owner() is None
    assert app.store.get_meta(META_KEY) == "0" and not config.stored(app.store.conn)
    config.pin(True)
    assert not attach.off(app).enabled and not config.on()


def test_stored_reads_on_without_a_row_or_a_table():
    import sqlite3
    assert config.stored(sqlite3.connect(":memory:"))  # no meta table: the default
    s = _Store()
    assert config.stored(s.conn)
    s.set_meta(META_KEY, "0")
    assert not config.stored(s.conn)
