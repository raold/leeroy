import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from leeroy_mind.__main__ import main
from leeroy_mind.personas import roster as roster_mod
from leeroy_mind.personas.roster import (DEFAULT_CONF, DEFAULT_MYSQL, ROSTER_SQL, RosterError,
                                         fetch_cast_from_bridge, members_from_json,
                                         parse_roster_tsv, query_roster, read_db_info)
from leeroy_mind.testing.fake_bridge import FakeBridge

FIXTURE_TSV = (Path(__file__).parent / "fixtures" / "roster.tsv").read_text(encoding="utf-8")


def test_read_db_info_masks_password(tmp_path):
    conf = tmp_path / "w.conf"
    conf.write_text('CharacterDatabaseInfo = "127.0.0.1;3306;acore;s3cret;acore_characters"\n')
    db = read_db_info(conf)
    assert (db.port, db.name) == (3306, "acore_characters")
    assert "s3cret" not in repr(db) and "s3cret" not in str(db)


def test_read_db_info_missing_key(tmp_path):
    conf = tmp_path / "w.conf"
    conf.write_text("Other = 1\n")
    with pytest.raises(RosterError):
        read_db_info(conf)


def test_parse_roster_tsv():
    ms = parse_roster_tsv(FIXTURE_TSV)
    assert ms[0].class_ == 8 and ms[0].spec_tab == -1 and ms[0].online is True
    with pytest.raises(RosterError):
        parse_roster_tsv("1\tonly\tthree")


def test_parse_roster_tsv_fields_and_order_kept():
    text = ("9\tZed\t2\t1\t1\t77\t4\t0\t530\t3703\n"
            "3\tAmy\t10\t8\t0\t80\t0\t1\t0\t12\n")
    z, a = parse_roster_tsv(text)
    assert [m.guid for m in (z, a)] == [9, 3]  # the SQL orders; parsing keeps the order
    assert (z.online, z.level, z.rank, z.gender, z.map, z.zone) == (False, 77, 4, 1, 530, 3703)
    assert (a.online, a.level, a.rank) == (True, 80, 0)


def test_roster_sql_formats_guild_id():
    assert "gm.guildid = 21 " in ROSTER_SQL.format(guild_id=21)


def test_query_roster_keeps_password_off_the_command_line(tmp_path, monkeypatch):
    conf = tmp_path / "w.conf"
    conf.write_text('CharacterDatabaseInfo = "127.0.0.1;3306;acore;hunter2;acore_characters"\n')
    db = read_db_info(conf)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        cnf = Path(argv[1].split("=", 1)[1])
        seen["cnf"] = cnf
        seen["text"] = cnf.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, FIXTURE_TSV, "")

    monkeypatch.setattr(roster_mod.subprocess, "run", fake_run)
    members = query_roster(db, 21, Path("mysql.exe"))
    assert len(members) == len(FIXTURE_TSV.splitlines())
    assert not any("hunter2" in a for a in seen["argv"])
    assert "hunter2" in seen["text"] and "acore_characters" in seen["argv"]
    assert not seen["cnf"].exists() and not seen["cnf"].parent.exists()


@pytest.fixture
def tmpdirs(monkeypatch):
    made = []
    real = tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        made.append(Path(d))
        return d
    monkeypatch.setattr(roster_mod.tempfile, "mkdtemp", spy)
    return made


def _db(tmp_path):
    conf = tmp_path / "w.conf"
    conf.write_text('CharacterDatabaseInfo = "h;3306;u;hunter2;d"\n')
    return read_db_info(conf)


def test_query_roster_error_paths_clean_up_and_hide_password(tmp_path, monkeypatch, tmpdirs):
    db = _db(tmp_path)

    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "access denied using hunter2")

    def timing_out(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60, output=b"hunter2")

    def missing(argv, **kw):
        raise FileNotFoundError("hunter2 " + argv[0])

    for fake in (failing, timing_out, missing):
        monkeypatch.setattr(roster_mod.subprocess, "run", fake)
        with pytest.raises(RosterError) as e:
            query_roster(db, 21, Path("mysql.exe"))
        assert "hunter2" not in str(e.value) and "hunter2" not in repr(e.value)
    assert len(tmpdirs) == 3 and not any(d.exists() for d in tmpdirs)


def test_query_roster_real_missing_exe(tmp_path, tmpdirs):
    with pytest.raises(RosterError) as e:
        query_roster(_db(tmp_path), 21, tmp_path / "no-such-mysql.exe")
    assert "hunter2" not in str(e.value)
    assert len(tmpdirs) == 1 and not tmpdirs[0].exists()


def test_query_roster_empty_password_error(tmp_path, monkeypatch):
    conf = tmp_path / "w.conf"
    conf.write_text('CharacterDatabaseInfo = "h;3306;u;;d"\n')
    monkeypatch.setattr(roster_mod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "boom"))
    with pytest.raises(RosterError) as e:
        query_roster(read_db_info(conf), 21, Path("mysql.exe"))
    assert "boom" in str(e.value)


def test_members_from_json_format():
    text = json.dumps([{"guid": 5, "name": "Arkon", "race": 2, "class": 1, "gender": 0,
                        "level": 80, "rank": 3, "online": True, "map": 0, "zone": 1}])
    m = members_from_json(text)[0]
    assert (m.guid, m.class_, m.spec_tab) == (5, 1, -1)
    for bad in ("{}", "[1]", '[{"guid": 1}]', "nope"):
        with pytest.raises(RosterError):
            members_from_json(bad)


def test_members_from_json_online_is_strict():
    base = {"guid": 5, "name": "Arkon", "race": 2, "class": 1, "gender": 0, "level": 80,
            "rank": 3, "map": 0, "zone": 1}
    for good, want in ((True, True), (False, False), (1, True), (0, False)):
        assert members_from_json(json.dumps([{**base, "online": good}]))[0].online is want
    for bad in ("false", "0", 2, None, "yes"):
        with pytest.raises(RosterError, match="online"):
            members_from_json(json.dumps([{**base, "online": bad}]))


def test_cli_roster_file_stores_cast(tmp_path, capsys):
    f = tmp_path / "roster.json"
    f.write_text(json.dumps([{"guid": 5, "name": "Arkon", "race": 2, "class": 1, "gender": 0,
                              "level": 80, "rank": 3, "online": False, "map": 0, "zone": 1}]))
    assert main(["init", "--data-dir", str(tmp_path / "d")]) == 0
    assert main(["roster", "--data-dir", str(tmp_path / "d"), "--file", str(f)]) == 0
    assert "stored 1 guild members" in capsys.readouterr().out


def test_fetch_cast_from_fake_bridge():
    async def go():
        bridge = FakeBridge()
        port = await bridge.start()
        try:
            return await fetch_cast_from_bridge("127.0.0.1", port, timeout=5.0)
        finally:
            await bridge.stop()
    assert [m.name for m in asyncio.run(go())] == ["Arkon", "Belra"]


@pytest.mark.mysql
def test_query_roster_live():
    assert len(query_roster(read_db_info(DEFAULT_CONF), 21, DEFAULT_MYSQL)) > 50
