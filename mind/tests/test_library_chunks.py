import pytest

from leeroy_mind.library.chunks import (MAX_WORDS, MIN_WORDS, Chunk, ChunkDraft, chunk_sections,
                                        join_labels, split_sentences, word_count)


def para(n: int, word: str = "word") -> str:
    """n words in sentences of ten."""
    words = [word] * n
    return " ".join(" ".join(words[i:i + 10]).capitalize() + "." for i in range(0, n, 10))


def test_sentences_split_at_ends():
    assert split_sentences("One two. Three four! Five? six") == ["One two.", "Three four!",
                                                                 "Five? six"]


def test_small_sections_merge_into_one_chunk():
    out = chunk_sections([("A", [para(40)]), ("B", [para(40)]), ("C", [para(30)])])
    assert len(out) == 1
    label, text = out[0]
    assert label == "A; B; C" and word_count(text) == 110


def test_chunks_stay_within_bounds():
    sections = [(f"S{i}", [para(70), para(60)]) for i in range(6)]
    out = chunk_sections(sections)
    sizes = [word_count(t) for _, t in out]
    assert all(s <= MAX_WORDS for s in sizes)
    assert all(s >= MIN_WORDS for s in sizes[:-1])
    assert sum(sizes) == 6 * 130


def test_long_paragraph_splits_at_sentences():
    out = chunk_sections([("Long", [para(600)])])
    sizes = [word_count(t) for _, t in out]
    assert len(out) == 3 and all(s <= MAX_WORDS for s in sizes) and sum(sizes) == 600
    assert all(t.endswith(".") for _, t in out)
    one_sentence = " ".join(["x"] * 700)
    assert all(word_count(t) <= MAX_WORDS for _, t in chunk_sections([("S", [one_sentence])]))


def test_new_section_starts_a_chunk_once_full():
    out = chunk_sections([("A", [para(130)]), ("B", [para(100)])])
    assert [label for label, _ in out] == ["A", "B"]


def test_short_tail_folds_back():
    out = chunk_sections([("A", [para(200)]), ("B", [para(30)])])
    assert len(out) == 1 and out[0][0] == "A; B"


def test_drafts_validate():
    with pytest.raises(ValueError):
        ChunkDraft(key="k", kind="gossip", title="t", text="x")
    with pytest.raises(ValueError):
        ChunkDraft(key="k", kind="lore", title="t", text="  ")
    assert ChunkDraft(key="k", kind="lore", title="t", text="x").weight == 1.0
    for bad in (0, -0.5, 1.5):
        with pytest.raises(ValueError):
            ChunkDraft(key="k", kind="lore", title="t", text="x", weight=bad)


def test_join_labels():
    assert join_labels("A", "B") == "A; B" and join_labels("A; B", "B; C") == "A; B; C"
    assert join_labels("A; B; C; ...", "D") == "A; B; C; ..."


def test_chunk_formatting():
    c = Chunk(1, "wiki", "lore", "Sindragosa", "Biography", "Text.", "https://w/x",
              "CC BY-SA 4.0", None, None, (), 0)
    assert c.format() == "[Sindragosa - Biography] Text."
    assert "CC BY-SA 4.0" in c.cite() and "https://w/x" in c.cite()
    w = Chunk(2, "world", "item", "Bryntroll", "", "Axe.", None, None, None, None, (), 0)
    assert w.heading() == "Bryntroll" and "world database" in w.cite()
