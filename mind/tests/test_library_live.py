"""Opt-in checks against the real world database and Ollama (deselected by default:
pytest -m mysql / pytest -m ollama)."""
import pytest

from conftest import run
from leeroy_mind.library.core import Library
from leeroy_mind.library.world import WorldSource
from leeroy_mind.personas.roster import DEFAULT_CONF, DEFAULT_MYSQL
from test_library_core import wiki_source


@pytest.mark.mysql
def test_world_source_reads_the_real_world_db():
    if not DEFAULT_CONF.exists() or not DEFAULT_MYSQL.exists():
        pytest.skip("no worldserver.conf or mysql.exe")
    res = WorldSource().load(None)
    by = {c.key: c for c in res.chunks}
    assert "Emblem of Frost" in by["loot:npc:36853:0"].text  # Sindragosa, 10-player
    assert "Icecrown Citadel" in by["instance:631"].text
    assert len(res.items) > 40000 and len(res.quests) > 5000
    # mysql.exe ends rows with CRLF: no CR and no "NULL" string may reach a chunk
    assert not any("\r" in c.text or "<NULL>" in c.text for c in res.chunks)


@pytest.mark.ollama
def test_embed_and_hybrid_search_with_ollama(tmp_path, clock):
    from leeroy_mind.llm.client import OllamaClient
    lib = Library(tmp_path / "library.db", clock=clock)
    try:
        lib.build([wiki_source()])
        llm = OllamaClient("http://localhost:11434", timeout=120.0)
        assert run(lib.embed_pending(llm)) == 3
        hits = run(lib.search("who died at the gate when the plague was released", k=1, llm=llm))
        assert hits[0].title == "Battle of Angrathar the Wrathgate"
    finally:
        lib.close()
