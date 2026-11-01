"""Audition must play the persisted voice, not silently render its Kokoro fallback."""
import json
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from conftest import BANK_SHA
from leeroy_voice import __main__ as cli
from leeroy_voice import audition, cast, fx
from leeroy_voice.decision import Decision
from leeroy_voice.engines import EngineError
from leeroy_voice.engines.fake import FakeEngine
from leeroy_voice.voices import spec_from_dict


def spec(guid=101, engine="chatterbox-turbo", **kw):
    return spec_from_dict({"version": 1, "guid": guid, "name": "Arkon", "engine": engine,
                           "lang": "a", "timbre": {"am_michael": 1.0}, "prosody": {},
                           "speed": 0.95, "pitch_st": 0.0, "mic": "soft_mic", "gain_db": 0.0,
                           "locked": True, "voices_sha256": BANK_SHA,
                           "ref": "" if engine == "kokoro" else f"{guid}.ref.wav", **kw})


class Recorder(FakeEngine):
    def __init__(self, name, events, *, fail=None):
        super().__init__()
        self.name, self.events, self.fail = name, events, fail
        self.said = []
        self.starts = self.stops = 0

    def start(self):
        self.events.append(("start", self.name))
        self.starts += 1
        if self.fail == "start":
            raise EngineError(f"{self.name}: failed to start")

    def stop(self):
        self.events.append(("stop", self.name))
        self.stops += 1

    def levers(self, attitude):
        assert attitude == "none"
        return {"temperature": 0.7}

    def synth(self, text, voice, style, speed):
        self.said.append((text, voice, style, speed))
        if self.fail == "synth":
            raise EngineError(f"{self.name}: failed to synthesize")
        return super().synth(text, voice, style, speed)


@pytest.fixture
def rig(live_guard, tmp_path, monkeypatch, fake_bank):
    root = live_guard.data_dir
    root.mkdir()
    folder = tmp_path / "specs"
    folder.mkdir()
    (root / "cast").mkdir()
    report = {"closest_pairs": [{"a": 101, "b": 102, "cosine": 0.876}], "threshold": 0.8}
    (root / "cast/report.json").write_text(json.dumps(report), encoding="utf-8")
    (root / "lexicon.toml").write_text('[respell.chatterbox-turbo]\nArkon = "Arrkon"\n',
                                       encoding="utf-8")
    events, loaded, workers = [], [], []
    base = Recorder("kokoro", events)
    monkeypatch.setattr(cli, "load_engine", lambda *args: (base, fake_bank))
    monkeypatch.setattr(cli, "_admission", lambda *args: pytest.fail("audition must stay on CPU"))

    def load(root_, cfg, decision, *, device, refs_dir):
        loaded.append((root_, decision, device, refs_dir))
        events.append(("load", decision.engine))
        made = Recorder(decision.engine, events)
        workers.append(made)
        return made

    monkeypatch.setattr(cli, "load_expressive", load)
    return SimpleNamespace(root=root, folder=folder, events=events, loaded=loaded, workers=workers,
                           base=base, report=report, bank=fake_bank)


def run(rig, *voices):
    for voice in voices:
        cast.write_spec(rig.folder, voice)
    return cli.main(["audition", "--specs", str(rig.folder)])


def test_expressive_audition_uses_spec_engine_reference_and_lexicon_without_kokoro(rig, monkeypatch):
    monkeypatch.setattr(cli, "load_engine", lambda *args: pytest.fail("no Kokoro models needed"))
    voice = spec()
    assert run(rig, voice) == 0
    assert len(rig.workers) == 1
    made = rig.workers[0]
    assert made.starts == made.stops == 1
    text, used_spec, style, speed = made.said[0]
    assert "Arrkon" in text and used_spec.ref == "101.ref.wav"
    assert used_spec == voice and speed == voice.speed and style == {"temperature": 0.7}
    root, selected, device, refs = rig.loaded[0]
    # No decision file means Kokoro by default, but the locked spec still names Turbo.
    assert selected.engine == voice.engine and selected.featured == {}
    assert root == rig.root and device == "cpu" and refs == rig.folder
    clip, rate = sf.read(rig.root / "audition/clips/101.wav", dtype="float32")
    assert rate == fx.OUT_RATE and clip.ndim == 1 and np.isfinite(clip).all()
    assert np.abs(clip).max() > 0.01


def test_mixed_engines_reuse_workers_stop_before_switching_and_keep_closest_pairs(rig):
    voices = [spec(101), spec(102, "kokoro"), spec(103), spec(104, "chatterbox")]
    assert run(rig, *voices) == 0
    assert [s[1].guid for s in rig.base.said] == [102]
    assert rig.events == [("load", "chatterbox"), ("start", "chatterbox"),
                          ("stop", "chatterbox"), ("load", "chatterbox-turbo"),
                          ("start", "chatterbox-turbo"), ("stop", "chatterbox-turbo")]
    by_name = {w.name: w for w in rig.workers}
    assert [s[1].guid for s in by_name["chatterbox-turbo"].said] == [101, 103]
    page = (rig.root / "audition/index.html").read_text(encoding="utf-8")
    assert "0.876" in page and "threshold 0.80" in page
    assert "saved casting report, not from the clips on this page" in page
    assert all(f'clips/{v.guid}.wav' in page for v in voices)


@pytest.mark.parametrize("failure", ["start", "synth"])
def test_failed_expressive_audition_stops_worker_and_does_not_substitute(rig, monkeypatch, capsys, failure):
    made = Recorder("chatterbox-turbo", rig.events, fail=failure)
    monkeypatch.setattr(cli, "load_expressive", lambda *args, **kw: made)
    monkeypatch.setattr(cli, "load_engine", lambda *args: pytest.fail("no fallback"))
    assert run(rig, spec()) == 1
    assert made.stops == 1
    assert not (rig.root / "audition/index.html").exists()
    assert not (rig.root / "audition/clips/101.wav").exists()
    assert "audition: chatterbox-turbo: failed" in capsys.readouterr().err


@pytest.mark.parametrize("wrong", [None, "chatterbox"])
def test_missing_or_mismatched_engine_fails_clearly(rig, monkeypatch, capsys, wrong):
    made = Recorder(wrong, rig.events) if wrong else None
    monkeypatch.setattr(cli, "load_expressive", lambda *args, **kw: made)
    assert run(rig, spec()) == 1
    assert "engine for audition" in capsys.readouterr().err
    if made is not None:
        assert made.stops == 1 and not made.said


def test_builder_cannot_accidentally_audition_a_cloning_spec_on_kokoro(rig):
    with pytest.raises(EngineError, match="matching engine"):
        audition.build_audition([spec()], rig.base, rig.root / "audition",
                                 report=rig.report, bank=rig.bank, decision=Decision())
    assert not rig.base.said
