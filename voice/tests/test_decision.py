from pathlib import Path

import pytest

from leeroy_voice.decision import (CLONING, ENGINES, SOURCES, Decision, DecisionError,
                                   load_decision)


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "decision.toml"
    p.write_text(text, encoding="utf-8")
    return p


def refused(tmp_path, text: str, match: str | None = None) -> None:
    with pytest.raises(DecisionError, match=match):
        load_decision(write(tmp_path, text))


def test_the_choices():
    assert ENGINES == ("kokoro", "chatterbox", "chatterbox-turbo", "indextts2")
    assert CLONING == ("chatterbox", "chatterbox-turbo", "indextts2")
    assert SOURCES == ("kokoro", "qwen3-voicedesign", "voxcpm2")


def test_missing_decision_means_kokoro(tmp_path):
    d = load_decision(tmp_path / "decision.toml")
    assert (d.decided, d.engine, d.prosody_swap, d.uk_timbre_mix) == ("", "kokoro", False, False)
    assert (d.voice_source, d.uk_share, d.vram_trade, d.featured) == ("kokoro", "keep", "none", {})
    assert d == Decision() and d.uk_lower_to == 0.0


def test_unknown_engine_refused(tmp_path):
    refused(tmp_path, 'engine = "orpheus"\n', "engine")


def test_expressive_needs_a_trade(tmp_path):
    refused(tmp_path, 'engine = "chatterbox"\n', "vram_trade")
    refused(tmp_path, '[featured]\n"101" = "chatterbox"\n', "vram_trade")
    d = load_decision(write(tmp_path, 'engine = "chatterbox"\nvram_trade = "trade-a"\n'))
    assert (d.engine, d.vram_trade) == ("chatterbox", "trade-a")


def test_featured_keys_are_guids(tmp_path):
    d = load_decision(write(tmp_path, 'vram_trade = "trade-a"\n[featured]\n'
                                      '"101" = "chatterbox-turbo"\n"105" = "indextts2"\n'))
    assert d.engine == "kokoro" and d.featured == {101: "chatterbox-turbo", 105: "indextts2"}
    refused(tmp_path, 'vram_trade = "trade-a"\n[featured]\nArkon = "chatterbox"\n', "featured")
    refused(tmp_path, 'vram_trade = "trade-a"\n[featured]\n"0" = "chatterbox"\n', "featured")
    refused(tmp_path, 'vram_trade = "trade-a"\n[featured]\n"101" = "kokoro"\n', "featured")
    refused(tmp_path, 'vram_trade = "trade-a"\nfeatured = ["101"]\n', "featured")


def test_lowered_share_needs_a_value(tmp_path):
    refused(tmp_path, 'uk_share = "lower"\n', "uk_lower_to")
    refused(tmp_path, 'uk_share = "lower"\nuk_lower_to = 0.0\n', "uk_lower_to")
    refused(tmp_path, 'uk_share = "lower"\nuk_lower_to = 1.0\n', "uk_lower_to")
    refused(tmp_path, 'uk_share = "raise"\n', "uk_share")
    d = load_decision(write(tmp_path, 'uk_share = "lower"\nuk_lower_to = 0.15\n'))
    assert (d.uk_share, d.uk_lower_to) == ("lower", 0.15)


def test_designed_voices_need_a_cloning_engine(tmp_path):   # I11
    refused(tmp_path, 'voice_source = "qwen3-voicedesign"\nvram_trade = "trade-a"\n', "engine")
    refused(tmp_path, 'voice_source = "qwen3-voicedesign"\nengine = "indextts2"\n', "vram_trade")
    refused(tmp_path, 'voice_source = "elevenlabs"\n', "voice_source")
    d = load_decision(write(tmp_path, 'voice_source = "qwen3-voicedesign"\nengine = "indextts2"\n'
                                      'vram_trade = "trade-b"\n'))
    assert (d.voice_source, d.engine, d.vram_trade) == ("qwen3-voicedesign", "indextts2", "trade-b")


def test_a_whole_decision(tmp_path):
    d = load_decision(write(tmp_path, 'decided = "2026-11-01"\nengine = "kokoro"\n'
                                      'prosody_swap = true\nuk_timbre_mix = true\n'
                                      'voice_source = "kokoro"\nuk_share = "keep"\n'
                                      'uk_lower_to = 0\nvram_trade = "none"\nfeatured = {}\n'))
    assert d == Decision(decided="2026-11-01", prosody_swap=True, uk_timbre_mix=True)
    assert type(d.uk_lower_to) is float


def test_bad_values(tmp_path):
    refused(tmp_path, 'prosody_swap = "yes"\n', "prosody_swap")
    refused(tmp_path, 'uk_timbre_mix = 1\n', "uk_timbre_mix")
    refused(tmp_path, 'decided = "yesterday"\n', "decided")
    refused(tmp_path, 'vram_trade = ""\n', "vram_trade")
    refused(tmp_path, 'uk_lower_to = true\n', "uk_lower_to")
    refused(tmp_path, 'engine = \n')
    p = tmp_path / "decision.toml"
    p.write_bytes(b'engine = "\xff"\n')
    with pytest.raises(DecisionError, match="UTF-8"):
        load_decision(p)
