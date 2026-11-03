import json

import pytest

from leeroy_mind.library.core import Library
from leeroy_mind.library.serverref import ServerRefSource
from library_snapshot_helpers import command, server_snapshot, teleport, update_export


@pytest.fixture
def lib(tmp_path, clock):
    library = Library(tmp_path / "library.db", clock=clock)
    yield library
    library.close()


def test_exact_teleport_coordinates_syntax_permissions_and_static_coverage(tmp_path, lib):
    root = tmp_path / "server"
    server_snapshot(root, commands=[command(), command(
        name="debug things", syntax=".debug things <value> [flag]", permission={"kind": "rbac", "value": "RBAC_DEBUG"},
        metadata={"target": "selected player", "effects": "Print diagnostic state.", "declared_security": 3})],
        unresolved=[{"source_path": "module/commands.cpp", "expression": "DYNAMIC_COMMANDS"}],
        unmatched=[{"name": "tele", "help": "SQL alone does not register this abbreviation."}])
    lib.build([ServerRefSource(root)])
    row = lib.conn.execute("SELECT * FROM game_tele WHERE id=7").fetchone()
    assert (row["position_x"], row["position_y"], row["position_z"], row["orientation"]) == (
        "-1234.5000", "+42.125000", "0.00000001", "6.283185")
    assert row["map_name"] == "Eastern Kingdoms"
    commands = lib.conn.execute("SELECT * FROM server_command ORDER BY name").fetchall()
    assert len(commands) == 2 and commands[1]["name"] == "teleport"
    assert commands[1]["syntax"] == ".teleport $location"
    assert commands[1]["help"] == "Go to a named location.\nExact help."
    assert json.loads(commands[0]["permission"]) == {"kind": "rbac", "value": "RBAC_DEBUG"}
    assert json.loads(commands[0]["metadata"])["target"] == "selected player"
    coverage = json.loads(lib.conn.execute("SELECT coverage FROM source_snapshot").fetchone()[0])
    assert coverage["verification"] == "source-observed" and len(coverage["unresolved"]) == 1
    assert len(coverage["unmatched_overrides"]) == 1
    text = lib.conn.execute("SELECT text FROM chunks WHERE kind='teleport'").fetchone()[0]
    assert "x=-1234.5000" in text and "not zone-map percentages" in text
    assert "Zone:" not in text
    command_chunk = lib.conn.execute("SELECT * FROM chunks WHERE key='command:teleport'").fetchone()
    command_meta = json.loads(command_chunk["metadata"])
    assert command_meta["name"] == "teleport" and command_meta["syntax"] == ".teleport $location"
    assert command_meta["help"] == commands[1]["help"] and command_meta["handler"] == "HandleTeleCommand"
    assert command_meta["source_path"] == "src/cs_tele.cpp" and command_meta["source_line"] == 42
    assert command_chunk["source_path"] == "commands.json"
    tele_chunk = lib.conn.execute("SELECT * FROM chunks WHERE kind='teleport'").fetchone()
    tele_meta = json.loads(tele_chunk["metadata"])
    assert tele_meta["id"] == 7 and tele_meta["map"] == 0 and tele_meta["map_name"] == "Eastern Kingdoms"
    assert tele_meta["position_x"] == "-1234.5000" and tele_meta["orientation"] == "6.283185"
    receipt = lib.conn.execute("SELECT manifest_sha256 FROM source_snapshot").fetchone()[0]
    assert command_meta["snapshot_manifest_sha256"] == tele_meta["snapshot_manifest_sha256"] == receipt


def test_namespaces_empty_exports_and_explicit_unknown_permissions_are_honest(tmp_path, lib):
    root = tmp_path / "server"
    server_snapshot(root, commands=[command(name="guild", syntax="", help="", handler="",
        permission={"kind": "unknown", "value": None}, allow_console=None, source_line=None,
        metadata={"namespace": True})], teleports=[])
    lib.build([ServerRefSource(root)])
    row = lib.conn.execute("SELECT * FROM server_command").fetchone()
    assert row["handler"] == "" and row["allow_console"] is None and row["source_line"] is None
    assert lib.conn.execute("SELECT count(*) FROM game_tele").fetchone()[0] == 0
    text = lib.conn.execute("SELECT text FROM chunks").fetchone()[0]
    assert "Permission: unknown" in text and "Syntax: (not resolved)" in text
    assert lib.conn.execute("SELECT status FROM source_inventory WHERE path='teleports.json'").fetchone()[0] == "empty"


@pytest.mark.parametrize("bad", [1.0, float("inf"), "NaN", "Infinity", "1 2", "", None, True])
def test_coordinates_never_round_or_accept_nonfinite_values(tmp_path, lib, bad):
    root = tmp_path / "server"
    server_snapshot(root, teleports=[teleport(position_x=bad)])
    with pytest.raises(ValueError):
        lib.build([ServerRefSource(root)])
    assert lib.conn.execute("SELECT count(*) FROM game_tele").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["partial_commands", "partial_teleports", "count", "duplicates",
                                    "teleport_duplicates", "unproved_runtime", "bad_permission",
                                    "bool_console", "missing_coverage"])
def test_invalid_structured_refresh_leaves_old_tables_chunks_and_provenance(tmp_path, lib, damage):
    root = tmp_path / "server"
    manifest, c, t = server_snapshot(root)
    lib.build([ServerRefSource(root)])
    before = {table: [tuple(r) for r in lib.conn.execute(f"SELECT * FROM {table}")]
              for table in ("game_tele", "server_command", "chunks", "source_snapshot", "source_inventory")}
    name, value = "commands.json", c
    if damage == "partial_commands":
        c["complete"] = False
    elif damage == "partial_teleports":
        name, value = "teleports.json", t
        t["complete"] = False
    elif damage == "count":
        c["expected_records"] = 2
    elif damage == "duplicates":
        c["records"].append(command(name="TELEPORT"))
        c["expected_records"] = c["coverage"]["registered"] = 2
    elif damage == "teleport_duplicates":
        name, value = "teleports.json", t
        t["records"].append(teleport())
        t["expected_records"] = 2
    elif damage == "unproved_runtime":
        c["coverage"]["verification"] = "runtime-verified"
    elif damage == "bad_permission":
        c["records"][0]["permission"] = {"kind": "security", "value": True}
    elif damage == "bool_console":
        c["records"][0]["allow_console"] = 1
    elif damage == "missing_coverage":
        del c["coverage"]
    update_export(root, manifest, name, value)
    with pytest.raises(ValueError):
        lib.build([ServerRefSource(root)])
    for table, rows in before.items():
        assert [tuple(r) for r in lib.conn.execute(f"SELECT * FROM {table}")] == rows


def test_structured_transaction_rolls_back_on_late_failure_and_retries_once(tmp_path, lib):
    root = tmp_path / "server"
    server_snapshot(root)
    lib.build([ServerRefSource(root)])
    before = [tuple(r) for r in lib.conn.execute("SELECT * FROM game_tele")]
    server_snapshot(root, commands=[command(name="lookup", syntax=".lookup <name>")],
                    teleports=[teleport(id=8, name="New Harbor")])
    lib.conn.execute("CREATE TRIGGER fail_chunk BEFORE INSERT ON chunks WHEN new.key='teleport:8' "
                     "BEGIN SELECT RAISE(ABORT,'synthetic late failure'); END")
    lib.conn.commit()
    with pytest.raises(Exception, match="synthetic late failure"):
        lib.build([ServerRefSource(root)])
    assert [tuple(r) for r in lib.conn.execute("SELECT * FROM game_tele")] == before
    assert lib.conn.execute("SELECT name FROM server_command").fetchone()[0] == "teleport"
    lib.conn.execute("DROP TRIGGER fail_chunk")
    lib.conn.commit()
    lib.build([ServerRefSource(root)])
    assert lib.conn.execute("SELECT id FROM game_tele").fetchone()[0] == 8
    assert lib.conn.execute("SELECT name FROM server_command").fetchone()[0] == "lookup"
    assert lib.build([ServerRefSource(root)])[0].unchanged == 2


def test_two_destinations_may_share_names_but_not_ids(tmp_path, lib):
    root = tmp_path / "server"
    server_snapshot(root, teleports=[teleport(), teleport(id=8, map=1)])
    lib.build([ServerRefSource(root)])
    assert lib.conn.execute("SELECT count(*) FROM game_tele WHERE name='Test Harbor'").fetchone()[0] == 2


def test_registered_punctuation_name_is_preserved_without_becoming_an_alias(tmp_path, lib):
    root = tmp_path / "server"
    server_snapshot(root, commands=[command(name="tools ?", syntax=".tools ?")])
    lib.build([ServerRefSource(root)])
    assert lib.conn.execute("SELECT name FROM server_command").fetchone()[0] == "tools ?"
    assert lib.conn.execute("SELECT title FROM chunks WHERE kind='command'").fetchone()[0] == ".tools ?"
