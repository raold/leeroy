"""The voice rule (owner's ruling 5): a voice note speaks frank at most at heat 9 and the
explicit tier only at 10 or on a no-limits evening; a line above the tier a note may speak is
typed, never dropped. With heat off voice notes have no such rule, as before."""
from leeroy_mind import heat as H
from voice_notes.test_service import fixture

LUST = H.Member(stance="lust", lust_style=True)
TEXT = "An invented short message"


def test_with_heat_off_a_note_is_chosen_as_before(store, clock, monkeypatch):
    app, p, turn, meta, _ = fixture(store, clock, monkeypatch)
    assert meta.heat is None and meta.heat_band is None
    assert app.voice_notes.eligible(turn, meta, p, TEXT)


def test_a_line_above_the_voice_tier_is_typed(store, clock, monkeypatch):
    app, p, turn, meta, _ = fixture(store, clock, monkeypatch)
    notes = app.voice_notes
    meta.heat = H.resolve(9, LUST)  # heat 9: frank at most
    for band, spoken in ((None, True), ("warm", True), ("suggestive", True), ("frank", True),
                         ("explicit", False)):
        meta.heat_band = band
        assert notes.eligible(turn, meta, p, TEXT) is spoken, band
    meta.heat = H.resolve(10, LUST)  # heat 10: as the text tier
    meta.heat_band = "explicit"
    assert notes.eligible(turn, meta, p, TEXT)
    meta.heat = H.resolve(9, LUST, session="open")  # a no-limits evening
    assert notes.eligible(turn, meta, p, TEXT)
    meta.heat = H.resolve(8, LUST, his_heat=9)  # heat 8: a frank line is typed
    meta.heat_band = "frank"
    assert not notes.eligible(turn, meta, p, TEXT)
    meta.heat_band = "suggestive"
    assert notes.eligible(turn, meta, p, TEXT)
    meta.heat = H.resolve(0, LUST)  # heat 0: plain lines only
    meta.heat_band = None
    assert notes.eligible(turn, meta, p, TEXT)


def test_typed_never_dropped_the_rule_only_answers_whether_to_speak(store, clock, monkeypatch):
    """eligible False is the path every other gate takes: the line goes out as text."""
    import inspect

    from leeroy_mind.voice_notes import service
    source = inspect.getsource(service.VoiceNotes.eligible) if hasattr(service, "VoiceNotes") \
        else inspect.getsource(service)
    assert "hr.speaks(" in source and "heat_band" in source
    app, p, turn, meta, _ = fixture(store, clock, monkeypatch)
    meta.heat, meta.heat_band = H.resolve(9, LUST), "explicit"
    assert app.voice_notes.eligible(turn, meta, p, TEXT) is False
