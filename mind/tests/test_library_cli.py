import json

import pytest

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


@pytest.mark.parametrize("command,flag,value", [
    (command, "--batch", value) for command in ("build", "embed")
    for value in ("-1", "0", "1.5", "true")
] + [("embed", "--limit", value) for value in ("-1", "1.5", "true")])
def test_invalid_embedding_counts_fail_before_opening_database(monkeypatch, command, flag, value):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid arguments must not open or migrate a database")
    monkeypatch.setattr(cli, "_library", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main([command, flag, value])
    assert caught.value.code == 2


def test_build_reports_embedding_failure_and_retains_imported_chunks(tmp_path, monkeypatch, capsys):
    from leeroy_mind.llm.client import LLMError
    from leeroy_mind.testing.fake_llm import FakeLLM
    class FailedEmbed(FakeLLM):
        async def embed(self, *args, **kwargs):
            raise LLMError("embedding interrupted")
    monkeypatch.setattr(cli, "make_sources", lambda args, root: [world_source()])
    monkeypatch.setattr(cli, "_ollama", lambda root: FailedEmbed())
    assert cli.main(["--data-dir", str(tmp_path), "build"]) == 1
    assert "embed stopped: embedding interrupted (9 still pending)" in capsys.readouterr().err
    assert cli.main(["--data-dir", str(tmp_path), "stats"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == 9
