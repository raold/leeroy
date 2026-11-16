from conftest import seed_test_cast


def test_life_day_prints_codes_only(tmp_path, store, capsys):   # a seeded test cast on disk
    seed_test_cast(store)
    from leeroy_mind.life.cli import life_main
    assert life_main(["day", "--data-dir", str(tmp_path), "--guid", "101", "--date", "2026-11-19"]) == 0
    out = capsys.readouterr().out
    assert "101 job=early event=" in out and "05:30-06:00 commute" in out
    assert "warehouse" not in out and "Arkon" not in out


def test_life_now_counts_where_and_attention(tmp_path, store, capsys):
    from leeroy_mind.__main__ import main
    seed_test_cast(store)
    before = store.conn.execute("SELECT count(*) FROM meta").fetchone()[0]
    # Thursday noon in Britain: all five test members (early shifts, UK) are at work
    assert main(["life", "now", "--data-dir", str(tmp_path), "--at", "2026-11-19T12:00+00:00"]) == 0
    out = capsys.readouterr().out
    assert ": 5 members" in out
    assert "where: asleep=0 at_work=5 busy=0 free=0 gaming=0" in out
    assert "game attention: none=0 glancing=5 half=0 full=0" in out
    assert "discord attention: none=0 glancing=5 half=0 full=0" in out
    assert "Arkon" not in out and "warehouse" not in out
    assert store.conn.execute("SELECT count(*) FROM meta").fetchone()[0] == before


def test_life_day_lists_every_member_and_refuses_unknown(tmp_path, store, capsys):
    from leeroy_mind.life.cli import life_main
    seed_test_cast(store)
    assert life_main(["day", "--data-dir", str(tmp_path), "--date", "2026-11-21"]) == 0
    out = capsys.readouterr().out
    assert [line.split()[0] for line in out.splitlines() if " job=" in line] == [
        "101", "102", "103", "104", "105"]
    assert life_main(["day", "--data-dir", str(tmp_path), "--guid", "999"]) == 1
    assert "no persona for guid 999" in capsys.readouterr().err


def test_life_without_mind_db(tmp_path, capsys):
    from leeroy_mind.life.cli import life_main
    assert life_main(["now", "--data-dir", str(tmp_path / "empty")]) == 2
    assert "no mind.db" in capsys.readouterr().err
