"""Stock callouts: which lines are stock, and the cache on disk (tmp dirs, the fake engine and
the fake bank), keyed by the voice (I6)."""
import dataclasses

import numpy as np
import pytest
import soundfile as sf

from conftest import BANK_SHA
from leeroy_voice import stock
from leeroy_voice.attitude import RAID_CALL_LEVERS, Levers
from leeroy_voice.decision import Decision
from leeroy_voice.engines import EngineError
from leeroy_voice.engines.fake import FakeEngine
from leeroy_voice.stock import STOCK, StockCache, stock_key
from leeroy_voice.voices import FIELDS, SpecError, spec_from_dict


def spec(**kw):
    return spec_from_dict({"version": 1, "guid": 101, "name": "Arkon", "engine": "kokoro",
                           "lang": "a", "timbre": {"am_michael": 0.7, "am_fenrir": 0.3},
                           "prosody": {}, "speed": 1.0, "pitch_st": -1.0, "mic": "desk_mic",
                           "gain_db": 0.0, "locked": False, "voices_sha256": BANK_SHA, "ref": "",
                           **kw})


def her(**kw):
    return spec(**{"guid": 102, "name": "Belra", "timbre": {"af_kore": 0.5, "af_sarah": 0.5},
                   **kw})


class RecordingEngine(FakeEngine):
    """FakeEngine that records each call and fails, with EngineError, on the texts in fail_on."""

    def __init__(self, fail_on=()):
        super().__init__()
        self.calls: list[tuple] = []
        self.fail_on = set(fail_on)

    def synth(self, text, spec, style, speed):
        self.calls.append((text, spec, style, speed))
        if text in self.fail_on:
            raise EngineError(f"fake: failed on a line of {len(text)} characters")
        return super().synth(text, spec, style, speed)


def clips(root) -> list:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_stock_keys():
    assert stock_key("Pulling!") == "pulling" and stock_key("pull") == "pulling"
    assert stock_key("OOM") == "oom" and stock_key("OOM in 10") is None


def test_stock_keys_aliases_and_texts():
    for key, text in STOCK.items():                     # each key, and each line as it is said
        assert stock_key(key) == key and stock_key(text) == key, key
    for said, key in [("pulling now", "pulling"), ("Pulling... now!", "pulling"),
                      ("Incoming!!", "inc"), ("inc", "inc"), ("out of mana...", "oom"),
                      ("I’m out of mana", "oom"), ("im out of mana", "oom"),
                      ("BL", "lust"), ("hero", "lust"), ("Heroism!", "lust"),
                      ("  heal   ME  ", "heal me"), ("stack up", "stack"), ("go go go", "go")]:
        assert stock_key(said) == key, said
    for said in ["", "!!!", "lol", "pull my target", "heal", "rez pls", "adds incoming",
                 "ready?! no", "oom :( brb"]:
        assert stock_key(said) is None, said


def test_stock_cache(tmp_path, fake_engine, fake_bank):
    root = tmp_path / "stock"
    cache, s = StockCache(root, fake_engine, fake_bank, Decision()), spec()
    assert cache.get(s, "oom") is None
    assert cache.render_missing(s) == len(STOCK)
    folder = root / "101" / cache.voice_hash(s)
    assert len(list(folder.glob("*.wav"))) == len(STOCK) == len(clips(root))
    assert cache.render_missing(s) == 0
    oom = cache.get(s, "oom")
    want = fake_engine.synth(STOCK["oom"], s, None, s.speed * RAID_CALL_LEVERS.speed_mult)
    assert oom.dtype == np.float32 and np.array_equal(oom, want)
    assert cache.get(s, "lol") is None and cache.get(s, "Pulling.") is None
    assert len(clips(root)) == len(STOCK)               # a key outside STOCK is never written
    assert {p.name.rsplit("-", 1)[0].replace("_", " ") for p in clips(root)} == set(STOCK)


def test_stock_is_the_neutral_raid_call_reading(tmp_path, fake_bank):
    engine, s, decision = RecordingEngine(), her(speed=1.1), Decision(prosody_swap=True)
    StockCache(tmp_path / "stock", engine, fake_bank, decision).render_missing(s)
    assert sorted(text for text, *_ in engine.calls) == sorted(STOCK.values())
    neutral = fake_bank.style(s, "none", decision)      # no attitude, so no prosody swap
    for _, got_spec, style, speed in engine.calls:
        assert got_spec == s and style is neutral
        assert speed == pytest.approx(1.1 * RAID_CALL_LEVERS.speed_mult)


def test_stock_follows_the_voice(tmp_path, fake_engine, fake_bank):   # I6
    root = tmp_path / "stock"
    cache = StockCache(root, fake_engine, fake_bank, Decision())
    old = spec(speed=1.0)
    new = dataclasses.replace(old, speed=1.1)
    cache.render_missing(old)
    old_folder = root / "101" / cache.voice_hash(old)
    assert cache.voice_hash(new) != cache.voice_hash(old)
    assert cache.get(new, "oom") is None
    assert cache.render_missing(new) == len(STOCK)
    got = cache.get(new, "oom")
    assert np.array_equal(got, fake_engine.synth(STOCK["oom"], new, None,
                                                 1.1 * RAID_CALL_LEVERS.speed_mult))
    assert len(got) < len(cache.get(old, "oom"))       # the faster voice's render
    assert cache.drop_stale(new) == 1
    assert not old_folder.exists() and cache.get(old, "oom") is None
    assert np.array_equal(cache.get(new, "oom"), got) and cache.drop_stale(new) == 0
    for flipped in (Decision(prosody_swap=True), Decision(uk_timbre_mix=True),
                    Decision(engine="chatterbox")):
        assert StockCache(root, fake_engine, fake_bank, flipped).voice_hash(new) != \
            cache.voice_hash(new)


NOT_RENDERED = {"version", "name", "mic", "pitch_st", "gain_db", "locked"}   # play time, or never


def test_voice_hash_is_what_the_render_hears(tmp_path, fake_engine, fake_bank):
    assert set(stock.RENDER_FIELDS) | NOT_RENDERED == set(FIELDS)        # a new field is a choice
    assert not set(stock.RENDER_FIELDS) & NOT_RENDERED
    cache = StockCache(tmp_path / "stock", fake_engine, fake_bank, Decision())
    a = spec(timbre={"am_michael": 0.7, "am_fenrir": 0.3})
    b = spec(timbre={"am_fenrir": 0.3, "am_michael": 0.7})
    h = cache.voice_hash(a)
    assert h == cache.voice_hash(b) and len(h) == 12 and int(h, 16) >= 0
    for change in ({"name": "Arko"}, {"mic": "laptop"}, {"pitch_st": 0.5}, {"gain_db": 2.0},
                   {"locked": True}):
        assert cache.voice_hash(dataclasses.replace(a, **change)) == h, change
    uk = dataclasses.replace(a, lang="b", prosody={"bm_george": 1.0})    # lang never moves alone
    cloned = dataclasses.replace(a, engine="chatterbox", ref="101.ref.wav")
    for other, base in [(spec(guid=105, name="Ebbo"), a), (spec(speed=1.1), a),
                        (spec(timbre={"am_michael": 0.6, "am_fenrir": 0.4}), a), (uk, a),
                        (dataclasses.replace(uk, prosody={"bm_fable": 1.0}), uk),
                        (dataclasses.replace(a, voices_sha256="0" * 64), a), (cloned, a),
                        (dataclasses.replace(cloned, ref="101b.ref.wav"), cloned),
                        (dataclasses.replace(cloned, engine="chatterbox-turbo"), cloned)]:
        assert cache.voice_hash(other) != cache.voice_hash(base), other


def test_locking_or_a_new_mic_keeps_the_clips(tmp_path, fake_engine, fake_bank):   # lock all
    cache, s = StockCache(tmp_path / "stock", fake_engine, fake_bank, Decision()), spec()
    cache.render_missing(s)
    oom = cache.get(s, "oom")
    for later in (dataclasses.replace(s, locked=True),
                  dataclasses.replace(s, mic="laptop", pitch_st=0.5, gain_db=-2.0, name="Arko")):
        assert np.array_equal(cache.get(later, "oom"), oom)
        assert cache.render_missing(later) == 0 and cache.drop_stale(later) == 0


def test_drop_stale_keeps_everyone_else(tmp_path, fake_engine, fake_bank):
    root = tmp_path / "stock"
    cache = StockCache(root, fake_engine, fake_bank, Decision())
    she = her()
    cache.render_missing(spec())
    cache.render_missing(she)
    note = root / "101" / "notes.txt"
    note.write_text("kept")
    other = root / "101" / "not-a-hash"
    other.mkdir()
    assert cache.drop_stale(spec(speed=1.05)) == 1
    assert note.exists() and other.exists()
    assert cache.get(she, "oom") is not None and cache.render_missing(she) == 0
    assert cache.drop_stale(her()) == 0 and cache.drop_stale(spec(guid=105, name="Ebbo")) == 0


def test_a_failed_line_waits_for_the_next_pass(tmp_path, fake_bank):
    engine = RecordingEngine(fail_on={STOCK["rez"]})
    cache, s = StockCache(tmp_path / "stock", engine, fake_bank, Decision()), spec()
    assert cache.render_missing(s) == len(STOCK) - 1
    assert cache.get(s, "rez") is None and len(clips(tmp_path / "stock")) == len(STOCK) - 1
    engine.fail_on.clear()
    assert cache.render_missing(s) == 1 and cache.get(s, "rez") is not None


def test_a_broken_clip_is_rendered_again(tmp_path, fake_engine, fake_bank):
    root = tmp_path / "stock"
    cache, s = StockCache(root, fake_engine, fake_bank, Decision()), spec()
    cache.render_missing(s)
    (path,) = [p for p in clips(root) if p.name.startswith("oom-")]
    path.write_bytes(b"RIFF\x10\x00\x00\x00WAVEjunk")
    assert cache.get(s, "oom") is None
    assert cache.render_missing(s) == 1 and cache.get(s, "oom") is not None
    sf.write(path, cache.get(s, "oom"), 16000, subtype="FLOAT")     # whole, at another rate
    assert cache.get(s, "oom") is None
    assert cache.render_missing(s) == 1 and cache.get(s, "oom") is not None
    other_rate = StockCache(root, FakeEngine(sample_rate=16000), fake_bank, Decision())
    assert other_rate.get(s, "oom") is None             # never another engine's rate


def test_a_changed_line_renders_afresh(tmp_path, fake_engine, fake_bank, monkeypatch):
    root = tmp_path / "stock"
    cache, s = StockCache(root, fake_engine, fake_bank, Decision()), spec()
    cache.render_missing(s)
    monkeypatch.setitem(stock.STOCK, "oom", "Out of mana!")
    assert cache.get(s, "oom") is None
    assert cache.render_missing(s) == 1
    assert len(clips(root)) == len(STOCK)               # the old line's clip is gone
    assert np.array_equal(cache.get(s, "oom"), fake_engine.synth(
        "Out of mana!", s, None, s.speed * RAID_CALL_LEVERS.speed_mult))


def test_a_new_raid_call_speed_renders_afresh(tmp_path, fake_engine, fake_bank, monkeypatch):
    root = tmp_path / "stock"
    cache, s = StockCache(root, fake_engine, fake_bank, Decision()), spec()
    cache.render_missing(s)
    old = set(clips(root))
    monkeypatch.setattr(stock, "RAID_CALL_LEVERS", Levers(1.2, 0, 0, 0.0))   # the name stock uses
    assert cache.get(s, "oom") is None
    assert cache.render_missing(s) == len(STOCK)
    assert not old & set(clips(root)) and len(clips(root)) == len(STOCK)
    assert np.array_equal(cache.get(s, "oom"),
                          fake_engine.synth(STOCK["oom"], s, None, 1.2 * s.speed))


def test_another_engine_at_the_same_rate_renders_afresh(tmp_path, fake_bank):
    class OtherEngine(FakeEngine):
        name = "other"
    root, s = tmp_path / "stock", spec()
    StockCache(root, FakeEngine(), fake_bank, Decision()).render_missing(s)
    other = StockCache(root, OtherEngine(), fake_bank, Decision())
    assert other.engine.sample_rate == FakeEngine().sample_rate
    assert other.get(s, "oom") is None
    assert other.render_missing(s) == len(STOCK) and other.get(s, "oom") is not None


def test_a_new_clip_format_renders_afresh(tmp_path, fake_engine, fake_bank, monkeypatch):
    root = tmp_path / "stock"
    cache, s = StockCache(root, fake_engine, fake_bank, Decision()), spec()
    cache.render_missing(s)
    monkeypatch.setattr(stock, "CLIP_FORMAT", stock.CLIP_FORMAT + 1)
    assert cache.get(s, "oom") is None
    assert cache.render_missing(s) == len(STOCK) and len(clips(root)) == len(STOCK)


def test_an_unspeakable_voice_writes_nothing(tmp_path, fake_engine, fake_bank):
    recipe = her(lang="b", timbre={"af_kore": 1.0}, prosody={"bf_emma": 1.0})   # the UK recipe
    cache = StockCache(tmp_path / "stock", fake_engine, fake_bank, Decision())
    with pytest.raises(SpecError):
        cache.render_missing(recipe)
    assert not (tmp_path / "stock").exists()
    allowed = StockCache(tmp_path / "stock", fake_engine, fake_bank, Decision(uk_timbre_mix=True))
    assert allowed.render_missing(recipe) == len(STOCK)
