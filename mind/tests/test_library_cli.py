import json

from leeroy_mind.library import __main__ as cli
from test_library_core import wiki_source, world_source


def test_build_stats_and_search(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_sources", lambda args, root: [world_source(), wiki_source()])
    data = ["--data-dir", str(tmp_path)]
    assert cli.main([*data, "build", "--no-embed"]) == 0
    out = capsys.readouterr().out
    assert "world: 9 chunks" in out and "wiki: 3 chunks" in out and "not embedding" in out
    assert (tmp_path / "library.db").exists()
    assert "world loaded" in (tmp_path / "library-cache" / "build.log").read_text(encoding="utf-8")

    assert cli.main([*data, "stats"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["chunks"]["world/item"] == 3 and stats["pending"] == 12

    assert cli.main([*data, "search", "Wrathgate", "--text-only", "-k", "1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[lore] Battle of Angrathar the Wrathgate") and "warcraft.wiki.gg" in out

    assert cli.main([*data, "search", "Sindragosa", "--kind", "loot", "--text-only"]) == 0
    assert capsys.readouterr().out.startswith("[loot] Sindragosa - 10-player")

    assert cli.main([*data, "search", "zzzzqqq", "--text-only"]) == 1
    assert "no matches" in capsys.readouterr().out


def test_unknown_source_is_refused(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["--data-dir", str(tmp_path), "build", "--sources", "world,forum", "--no-embed"])
