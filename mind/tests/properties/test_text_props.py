"""Text screens and trims over generated chat: the say cleaning (protocol.py), Discord's sentence
trim, the game length cuts and line splits (talk/length.py), item-link
placeholders (talk/items.py) and the real-name redaction (personas/screens.py)."""
import re

import pytest

pytest.importorskip("hypothesis", reason='Hypothesis comes with the test extra: pip install -e ".[test]"')

from hypothesis import assume, given, strategies as st

from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.screens import names_real_name, redact_real_name
from leeroy_mind.protocol import MAX_TEXT_BYTES, clean_say_text, sanitize_say_text
from leeroy_mind.talk.discord_length import _SENTENCE_END, trim_to_sentence
from leeroy_mind.talk.items import MAX_LINKS, ItemLinker, fold, link_bytes
from leeroy_mind.talk.length import KEEP_SLACK, MIN_WORDS, asks, fit_answer, split_lines, tail_lines
from leeroy_mind.talk.style import _DANGLING, _bare

WORDS = ("the", "boss", "but", "and", "i", "so", "we", "wiped", "on", "sindragosa", "lol", "ok", "you're",
         "really", "a", "deathbringer's", "will", "tonight", "8pm", "because", "it's", "that", "fine",
         ":)", "<3", "\U0001F602", "naxx", "gz", "heals", "x", "3.5", "example.com")
PUNCT = ("", "", "", ",", ".", "!", "?", "...", ";", ":", " -", '"', "”", "?!", ")")
chat_words = st.tuples(st.sampled_from(WORDS), st.sampled_from(PUNCT)).map("".join)
chat = st.lists(st.one_of(chat_words, chat_words, chat_words, st.sampled_from(("\n", "  ", "\t"))),
                max_size=60).map(" ".join)
any_text = st.one_of(chat, st.text(max_size=300))


@given(any_text)
def test_say_cleaning_and_the_sentence_trim_are_stable_prefixes(text):
    """clean_say_text leaves no "|", control character or doubled/edge whitespace and is
    idempotent; sanitize_say_text is that cleaning cut on a character boundary to at most
    MAX_TEXT_BYTES bytes of UTF-8, a prefix of it, and idempotent too. trim_to_sentence keeps a
    prefix of a reply: the whole reply when it has no sentence end, else up to its last one
    (never mid-sentence), and trimming again changes nothing."""
    clean = clean_say_text(text)
    assert clean_say_text(clean) == clean
    assert "|" not in clean and clean == clean.strip() and "  " not in clean
    assert not any(c in clean for c in "\r\n\t")
    cut = sanitize_say_text(text)
    assert len(cut.encode("utf-8")) <= MAX_TEXT_BYTES
    assert clean.startswith(cut) and sanitize_say_text(cut) == cut
    out = trim_to_sentence(text)
    assert text.startswith(out) and trim_to_sentence(out) == out
    ends = list(_SENTENCE_END.finditer(text))
    assert out == (text[:ends[-1].end()].rstrip() if ends else text)


def words_prefix(cut, text):
    """`cut`'s words lead `text`'s (its last word may have lost a trailing , ; : or -)."""
    a, b = cut.split(), text.split()
    return len(a) <= len(b) and a[:-1] == b[:len(a) - 1] and b[len(a) - 1].startswith(a[-1])


@given(chat, st.integers(0, 40), st.sampled_from(("", "how was the raid?", "nice", "what's the plan for icc",
                                                    "explain the lich king fight", "gz on the bow")),
       st.integers(0, 8))
def test_a_game_reply_is_cut_only_at_a_whole_clause_and_never_when_he_asked(text, limit, his, floor):
    """fit_answer: an answer to a line of his that asks anything is never cut; any other reply is
    either left exactly as written or cut to its leading whole clauses, at least MIN_WORDS (and
    her floor) and at most KEEP_SLACK times the turn's words, never ending on a dangling word
    outside a sentence's end and never inside a quotation."""
    fit = fit_answer(text, limit, his, floor=floor)
    assert fit.action in ("fits", "cut", "kept", "retry")
    if asks(his):
        assert fit.text == text and fit.action in ("fits", "kept")
    if fit.text == text:
        return
    words = fit.text.split()
    assert fit.action in ("cut", "kept") and words_prefix(fit.text, text)
    assert max(MIN_WORDS, floor) <= len(words) <= int(KEEP_SLACK * limit)
    last = words[-1].rstrip("\"')”’")
    assert last.endswith((".", "!", "?")) or _bare(words[-1]) not in _DANGLING
    assert len(re.findall(r'["“”]', fit.text)) % 2 == 0


@given(chat, st.integers(1, 3), st.integers(10, 240))
def test_game_lines_split_and_tail_by_whole_clauses_without_losing_words(text, lines, max_bytes):
    """split_lines lays the whole text (whitespace collapsed) on at most `lines` lines of at most
    `max_bytes` bytes, nothing dropped or reordered, or says None; tail_lines does the same for
    the text's end (a suffix of it)."""
    flat = " ".join(text.split())
    parts = split_lines(text, lines, max_bytes)
    if parts is not None:
        assert 1 <= len(parts) <= lines and " ".join(parts) == flat
        assert all(len(p.encode("utf-8")) <= max_bytes for p in parts)
    elif flat:
        assert len(flat.encode("utf-8")) > max_bytes
    tail = tail_lines(text, lines, max_bytes)
    if tail is not None:
        assert 1 <= len(tail) <= lines and flat.endswith(" ".join(tail))
        assert all(len(p.encode("utf-8")) <= max_bytes for p in tail)


ITEMS = ("Shadowmourne", "Deathbringer's Will", "Frozen Orb", "Bryntroll, the Bone Arbiter",
         "Tiny Abomination in a Jar", "Sharpened Twilight Scale", "Ashen Band of Endless Vengeance")
# an item id is the item table's primary key; names may repeat (a heroic copy)
item_rows = st.lists(st.tuples(st.sampled_from(ITEMS), st.integers(1, 99999), st.integers(2, 5),
                               st.integers(200, 284)), min_size=1, max_size=6, unique_by=lambda r: r[1])
pieces = st.one_of(
    chat_words,
    st.sampled_from(ITEMS).flatmap(lambda n: st.sampled_from((n, n.lower(), n.upper()))).map(lambda n: f"[{n}]"),
    st.sampled_from(ITEMS).map(lambda n: n.lower()),
    st.sampled_from(("{", "{{", "{{item:5}}", "}}", "|", "{|{", "[", "]", "[nothing]", "\u200b", "\x07")),
    st.text(max_size=12),
)
TOKEN = re.compile(r"\{\{item:([0-9]{1,10})\}\}")


@given(item_rows, st.lists(pieces, max_size=40).map(" ".join), st.integers(1, 80))
def test_item_placeholders_are_only_the_linkers_and_fit_the_line_once_expanded(rows, line, level):
    """ItemLinker.wire with native links on: every {{item:<id>}} placeholder in the say text is
    one it placed, for an id the item table holds, at most MAX_LINKS of them, and once native
    expands them into links the line still fits MAX_TEXT_BYTES (or none was placed); the words
    themselves never make one. Without native links the text is the folded line."""
    table = [{"id": i, "name": n, "quality": q, "ilvl": lvl, "req_level": 80}
             for n, i, q, lvl in rows]
    linker = ItemLinker(lambda: table, now=lambda: 0.0)
    folded = fold(line)
    assert linker.wire(line, level) == folded  # native links off
    linker.native = True
    out = linker.wire(line, level)
    tokens = list(TOKEN.finditer(out))
    assert len(tokens) <= MAX_LINKS
    starts = {m.start() for m in tokens}
    assert {i for i in range(len(out)) if out.startswith("{{item:", i)} == starts
    if not tokens:
        assert out == folded
        return
    by_id = {ref.id: ref for refs in linker._index().values() for ref in refs}
    assert all(int(m.group(1)) in by_id for m in tokens)
    size = len(out.encode("utf-8")) - sum(len(m.group(0)) for m in tokens)
    size += sum(link_bytes(by_id[int(m.group(1))], level) for m in tokens)
    assert size <= MAX_TEXT_BYTES
    # the words around the placeholders are the folded line's own
    outside = TOKEN.split(out)[::2]
    rest = folded
    for part in outside:
        at = rest.find(part)
        assert at >= 0
        rest = rest[at + len(part):]


names = st.lists(st.from_regex(r"[A-Za-z][a-z]{1,8}(?:'[a-z]{1,4})?", fullmatch=True), min_size=1,
                 max_size=2).map(" ".join)


@given(names, st.lists(st.one_of(chat_words, st.just("NAME"), st.just("NAME's"), st.just("(NAME)")),
                       max_size=20), st.sampled_from(("Testy", "the guild master", "")))
def test_a_redacted_line_never_names_him_and_redacting_again_changes_nothing(real, words, replacement):
    """redact_real_name: after redaction no form of the real name the screens catch (its spelling,
    Title, UPPER, first letter raised; whole words) is left, whatever case or punctuation it
    came with, and a second pass changes nothing."""
    human = HumanIdentity(1, "Testmage", "Testy", real, True, "he")
    assume(not names_real_name(replacement, human))
    forms = (real, real.title(), real.upper(), real[:1].upper() + real[1:])
    text = " ".join(w.replace("NAME", forms[i % len(forms)]) for i, w in enumerate(words))
    out = redact_real_name(text, human, replacement)
    assert not names_real_name(out, human)
    assert redact_real_name(out, human, replacement) == out
