"""console/items.py: item names from a scratch library.db."""
import sqlite3

from leeroy_mind.console import items


def test_item_names_from_a_library_db(tmp_path):
    p = tmp_path / "library.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT, quality INTEGER, ilvl INTEGER, "
              "req_level INTEGER, slot INTEGER)")
    c.executemany("INSERT INTO item VALUES(?,?,?,?,?,?)",
                  [(10, "Sword of Tests", 4, 200, 80, 1), (11, "Plain Cloth", 1, 5, 1, 2)])
    c.commit()
    c.close()
    assert items.item_names(p, [10, 11, 12, 10]) == {10: {"name": "Sword of Tests", "quality": 4},
                                                     11: {"name": "Plain Cloth", "quality": 1}}
    assert items.item_names(p, []) == {}


def test_missing_unreadable_or_tableless_library_is_empty(tmp_path):
    assert items.item_names(tmp_path / "none.db", [1]) == {}
    (tmp_path / "junk.db").write_bytes(b"not a database at all" * 20)
    assert items.item_names(tmp_path / "junk.db", [1]) == {}
    sqlite3.connect(tmp_path / "bare.db").close()
    assert items.item_names(tmp_path / "bare.db", [1]) == {}
