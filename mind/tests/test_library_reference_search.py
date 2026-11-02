import pytest

from leeroy_mind.library.azeroth import AzerothWikiSource
from leeroy_mind.library.core import Library
from library_snapshot_helpers import snapshot


@pytest.fixture
def library(tmp_path, clock):
    root = tmp_path / "docs"
    snapshot(root, files={
        "installation.md": "# Core Installation\n## Building\nConfigure the build with CMake before compiling.\n",
        "cn/installation.md": "# Core Installation\nCMake build CMake build CMake build.\n",
        "settings.md": "# Settings\nPlayerLimit controls the player limit.\n",
        "archive.md": "# Older Settings\nPlayerLimit changed in an older revision.\n"},
        entries={"cn/installation.md": {"language": "zh-CN"}, "archive.md": {"archived": True}})
    lib = Library(tmp_path / "library.db", clock=clock)
    lib.build([AzerothWikiSource(root)])
    yield lib
    lib.close()


def test_reference_search_never_calls_lore_rank_mmr_or_models(library, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("reference search must not run lore ranking")
    monkeypatch.setattr(library, "_rank", forbidden)
    monkeypatch.setattr(library, "_knn", forbidden)
    hits = library.search_reference_text("How do I build AzerothCore with CMake?",
                                         kinds=["documentation"], language="en")
    assert hits and hits[0].source_path == "installation.md"
    assert all(c.metadata["language"] == "en" for c in hits)


def test_missing_exact_identifier_never_falls_back_to_generic_settings(library):
    assert library.search_reference_text("What does UnknownSetting mean in AzerothCore?",
                                          kinds=["documentation"], language="en") == []
    hits = library.search_reference_text("What does PlayerLimit mean in AzerothCore?",
                                          kinds=["documentation"], language="en")
    assert hits and hits[0].source_path == "settings.md"
    assert all("PlayerLimit" in c.text for c in hits)


def test_reference_kind_scope_and_nonpositive_count_are_explicit(library):
    assert library.search_reference_text("PlayerLimit") == []
    assert library.search_reference_text("PlayerLimit", kinds=["lore"]) == []
    assert library.search_reference_text("PlayerLimit", kinds=["documentation"], k=0) == []
    with pytest.raises(ValueError, match="unknown chunk kinds"):
        library.search_reference_text("PlayerLimit", kinds=["invented"])


def test_language_choice_precedes_candidate_limit_and_current_version_precedes_archive(library):
    chinese = library.search_reference_text("CMake build", kinds=["documentation"], language="zh-CN", k=1)
    assert len(chinese) == 1 and chinese[0].metadata["language"] == "zh-CN"
    current = library.search_reference_text("PlayerLimit", kinds=["documentation"], language="en", k=1)
    assert len(current) == 1 and current[0].metadata["archived"] is False


def test_query_operators_remain_text_not_fts_syntax(library):
    assert isinstance(library.search_reference_text('CMake OR NOT " * {title}: / NEAR',
                                                   kinds=["documentation"], language="en"), list)
