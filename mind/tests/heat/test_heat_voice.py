"""The voice rule (ruling 5, as narrowed in review): below heat 10, and off a no-limits evening,
a line that is itself plainly explicit is typed instead of voiced. The line is judged (the
public guard's screen, heat.guard.public_hit), never the tier its prompt was worded at, so every
other line of a romance turn is spoken as before. Typed, never dropped. With heat off, or on and
not set, voice notes have no such rule."""
from leeroy_mind import heat as H
from voice_notes.test_service import fixture

LUST = H.Member(stance="lust", lust_style=True)
PLAIN = "An invented short message"
EXPLICIT = "An invented message about being naked"  # trips heat.guard's built-in screen


def test_with_no_heat_on_the_turn_a_note_is_chosen_as_before(store, clock, monkeypatch):
    app, p, turn, meta, _ = fixture(store, clock, monkeypatch)
    assert meta.heat is None  # heat off, or on and unset: the pipeline leaves it None
    assert app.voice_notes.eligible(turn, meta, p, PLAIN)
    assert app.voice_notes.eligible(turn, meta, p, EXPLICIT)


def test_only_a_line_that_is_itself_explicit_is_typed(store, clock, monkeypatch):
    app, p, turn, meta, _ = fixture(store, clock, monkeypatch)
    notes = app.voice_notes
    for heat in range(10):  # 0-9: an explicit line is typed, whatever tier the prompt was at
        meta.heat = H.resolve(heat, LUST, his_heat=9)
        for band in (None, "warm", "suggestive", "frank", "explicit"):
            meta.heat_band = band
            assert notes.eligible(turn, meta, p, PLAIN), (heat, band)
            assert not notes.eligible(turn, meta, p, EXPLICIT), (heat, band)
    meta.heat = H.resolve(10, LUST)  # heat 10: any line may be spoken
    assert notes.eligible(turn, meta, p, EXPLICIT) and notes.eligible(turn, meta, p, PLAIN)
    meta.heat = H.resolve(4, LUST, session="open")  # a no-limits evening
    assert notes.eligible(turn, meta, p, EXPLICIT)
    meta.heat = H.resolve(10, LUST, session="clean")
    assert not notes.eligible(turn, meta, p, EXPLICIT)


def test_the_owners_block_words_count_as_explicit_for_the_voice_too(store, clock, monkeypatch):
    app, p, turn, meta, _ = fixture(store, clock, monkeypatch)
    folder = app.data_dir / "prompts" / "heat"
    folder.mkdir(parents=True)
    (folder / "public_block.txt").write_bytes(b"invented phrase\n")
    meta.heat = H.resolve(9, LUST)
    assert not app.voice_notes.eligible(turn, meta, p, "what an invented phrase that was")
    assert app.voice_notes.eligible(turn, meta, p, "what an invented thing that was")


def test_which_heats_may_speak_an_explicit_line():
    assert [h for h in range(11) if H.resolve(h, LUST).speaks_explicit] == [10]
    assert H.resolve(0, LUST, session="open").speaks_explicit
    assert not H.resolve(9, H.Member(stance="lust", own_dial=9)).speaks_explicit
    assert H.resolve(9, H.Member(stance="lust", own_dial=10)).speaks_explicit  # her heat is 10
