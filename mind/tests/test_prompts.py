"""prompts.load_prompt reads the owner's local overlays (privacy minor 11): an editor may save
one with a byte order mark, in a Windows code page or as UTF-16 (Notepad's "Unicode"), and a
romance turn must not fail on it."""
import codecs
import logging

from leeroy_mind.prompts import load_prompt, read_overlay

BOM, LQ, RQ, REPLACED = chr(0xFEFF), chr(0x201C), chr(0x201D), chr(0xFFFD)


def _overlay(tmp_path, name: str, data: bytes) -> None:
    folder = tmp_path / "prompts"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.txt").write_bytes(data)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "prompt_overlay_not_utf8" in r.getMessage()]


def test_an_overlay_saved_with_a_byte_order_mark_is_read_without_it(tmp_path, caplog):
    _overlay(tmp_path, "romance_flirty", (BOM + "Keep it playful.\n").encode("utf-8"))
    with caplog.at_level(logging.WARNING):
        assert load_prompt("romance_flirty", tmp_path, "built-in") == "Keep it playful."
    assert _warnings(caplog) == []


def test_an_overlay_in_a_windows_code_page_is_read_and_warned_about_once(tmp_path, caplog):
    """An ANSI-saved overlay: cp1252's curly quotes are not UTF-8."""
    _overlay(tmp_path, "romance_explicit", f"say {LQ}hi{RQ} to him".encode("cp1252"))
    with caplog.at_level(logging.WARNING):
        first = load_prompt("romance_explicit", tmp_path, "built-in")
        again = load_prompt("romance_explicit", tmp_path, "built-in")
    assert first == again == f"say {REPLACED}hi{REPLACED} to him"
    warned = _warnings(caplog)
    assert len(warned) == 1 and "romance_explicit.txt" in warned[0]
    assert "replaced=2" in warned[0] and "to him" not in warned[0]  # never the overlay's text
    _overlay(tmp_path, "romance_flirty", f"un caf{chr(0xE9)}".encode("cp1252"))  # its own
    with caplog.at_level(logging.WARNING):
        assert load_prompt("romance_flirty", tmp_path, "built-in") == f"un caf{REPLACED}"
    assert len(_warnings(caplog)) == 2


def test_an_overlay_saved_as_utf16_is_read_as_utf16(tmp_path, caplog):
    """Part F review, Minor 7: Notepad's "Unicode" is UTF-16 with a byte order mark. Read as
    UTF-8 it became replacement characters with a NUL between the letters."""
    text = f"say {LQ}hi{RQ} to him"
    _overlay(tmp_path, "romance_flirty", text.encode("utf-16"))  # with its BOM
    _overlay(tmp_path, "romance_warm", codecs.BOM_UTF16_BE + text.encode("utf-16-be"))
    with caplog.at_level(logging.WARNING):
        assert load_prompt("romance_flirty", tmp_path, "built-in") == text
        assert load_prompt("romance_warm", tmp_path, "built-in") == text
    assert _warnings(caplog) == []  # read as written: nothing to warn about
    flirty = read_overlay("romance_flirty", tmp_path)
    assert (flirty.text, flirty.encoding, flirty.replaced) == (text, "utf-16", 0)


def test_read_overlay_says_how_a_file_was_read(tmp_path):
    """What doctor reports: the encoding, and how many characters could not be read."""
    _overlay(tmp_path, "romance_explicit", f"say {LQ}hi{RQ}".encode("cp1252"))
    _overlay(tmp_path, "romance_flirty", (BOM + "  playful \n").encode("utf-8"))
    explicit = read_overlay("romance_explicit", tmp_path)
    assert (explicit.encoding, explicit.replaced) == ("utf-8", 2)
    flirty = read_overlay("romance_flirty", tmp_path)
    assert (flirty.text, flirty.encoding, flirty.replaced) == ("playful", "utf-8", 0)
    assert read_overlay("romance_warm", tmp_path) is None


def test_a_utf8_overlay_is_read_as_written(tmp_path, caplog):
    text = f"a kind word, {LQ}no rush{RQ}"
    _overlay(tmp_path, "romance_warm", text.encode("utf-8"))
    with caplog.at_level(logging.WARNING):
        assert load_prompt("romance_warm", tmp_path, "built-in") == text
        assert load_prompt("romance_suggestive", tmp_path, "built-in") == "built-in"  # none
    assert _warnings(caplog) == []
