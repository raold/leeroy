"""Casting (Task 19): a stable voice spec for every member of the brief, and the audition page.
The invented cast only (Arkon, Belra, Coros, Dunna, Ebbo); a fake bank, a fake render and a fake
embedding, so nothing needs a model, an audio device or the GPU."""
import json
import re
import zlib

import numpy as np
import pytest

from conftest import BANK_SHA
from leeroy_voice import audition, cast, fx
from leeroy_voice import __main__ as cli
from leeroy_voice.__main__ import main
from leeroy_voice.decision import Decision
from leeroy_voice.engines.fake import FakeEngine
from leeroy_voice.stock import STOCK
from leeroy_voice.voices import BASES, GROUPS, StyleBank, spec_from_dict, spec_to_dict

TODAY = "2026-11-01"
DIM = 64


def entry(guid, name, gender="female", accent="en-us", *, descriptors=(), speed=1.0,
          guild_role="raider", core=False, age=30, raid_role="ranged") -> cast.BriefEntry:
    return cast.BriefEntry(guid=guid, name=name, gender=gender, accent=accent, age=age,
                           descriptors=tuple(descriptors), speed=speed, guild_role=guild_role,
                           raid_role=raid_role, core=core)


CAST = [entry(101, "Arkon", "male"), entry(102, "Belra", "female", guild_role="officer"),
        entry(103, "Coros", "male", core=True), entry(104, "Dunna", "female", "en-gb"),
        entry(105, "Ebbo", "female")]


def fake_render(spec):
    """A deterministic 'audio' per spec: a vector seeded from the whole recipe."""
    blob = json.dumps(spec_to_dict(spec), sort_keys=True).encode()
    return np.random.default_rng(zlib.crc32(blob)).standard_normal(DIM).astype(np.float32)


def fake_embed(audio):
    return np.asarray(audio, dtype=np.float32)


def make(brief, bank, **kw):
    kw.setdefault("decision", Decision())
    kw.setdefault("embed", fake_embed)
    kw.setdefault("render", fake_render)
    kw.setdefault("existing", {})
    kw.setdefault("today", TODAY)
    return cast.cast_voices(brief, bank, **kw)


def by_guid(specs):
    return {s.guid: s for s in specs}


# -- the recipe ---------------------------------------------------------------------------------

def test_gender_and_accent(fake_bank):
    specs = by_guid(make(CAST, fake_bank))
    assert set(specs) == {101, 102, 103, 104, 105}
    for e in CAST:
        s = specs[e.guid]
        want = ("f" if e.gender == "female" else "m", "b" if e.accent == "en-gb" else "a")
        assert s.lang == want[1] and s.name == e.name and not s.locked
        assert s.engine == "kokoro" and s.ref == "" and s.voices_sha256 == BANK_SHA
        for base in (*s.timbre, *s.prosody):
            assert BASES[base] == want
        assert 1 <= len(s.timbre) <= 3
        assert abs(sum(s.timbre.values()) - 1) < 1e-6


def test_british_women_mix_only_when_decided(fake_bank):
    women = [entry(200 + i, f"Uk{i}", "female", "en-gb") for i in range(12)]
    plain = make(women, fake_bank)
    assert all(b in GROUPS[("f", "b")] for s in plain for b in (*s.timbre, *s.prosody))
    mixed = make(women, fake_bank, decision=Decision(uk_timbre_mix=True))
    took_us = [s for s in mixed if any(BASES[b][1] == "a" for b in s.timbre)]
    assert took_us, "twelve women at one in two: some take the US timbre"
    for s in took_us:
        assert s.lang == "b" and s.prosody
        assert all(BASES[b] == ("f", "b") for b in s.prosody)
        assert all(BASES[b][0] == "f" for b in s.timbre)
    men = make([entry(300 + i, f"Us{i}", "male") for i in range(6)], fake_bank,
               decision=Decision(uk_timbre_mix=True))
    assert all(BASES[b] == ("m", "a") for s in men for b in s.timbre)


def test_cleanest_for_officers(fake_bank):
    brief = [entry(400 + i, f"Off{i}", "female", guild_role="officer") for i in range(8)]
    brief += [entry(420 + i, f"Core{i}", "female", core=True) for i in range(8)]
    for s in make(brief, fake_bank):
        top = max(s.timbre, key=s.timbre.get)
        assert cast.GRADES[top] in ("A", "A-", "B+", "B", "B-")
        assert s.timbre[top] >= 0.6


def test_cleanest_falls_back_to_the_best_graded_base(fake_bank):
    # the fake bank's men are both C+: nothing is B- or better, so the best there is
    spec = make([entry(1, "Arkon", "male", core=True)], fake_bank)[0]
    top = max(spec.timbre, key=spec.timbre.get)
    assert cast.GRADES[top] == "C+" and spec.timbre[top] >= 0.6


def test_seeded_and_stable(fake_bank):
    first, again = make(CAST, fake_bank), make(CAST, fake_bank)
    assert first == again
    # a locked existing spec is untouched, an unlocked one is kept unless recast
    locked = spec_from_dict({**spec_to_dict(first[0]), "locked": True})
    kept = first[1]
    out = by_guid(make(CAST, fake_bank, existing={locked.guid: locked, kept.guid: kept},
                       recast={locked.guid}))
    assert out[locked.guid] == locked and out[kept.guid] == kept
    assert out[103] == by_guid(first)[103]  # the others do not move for the kept ones


def test_recast_gives_a_new_voice_and_keeps_moving(fake_bank):
    first = by_guid(make(CAST, fake_bank))
    second = by_guid(make(CAST, fake_bank, existing=first, recast={102}))
    assert second[102] != first[102] and all(second[g] == first[g] for g in first if g != 102)
    third = by_guid(make(CAST, fake_bank, existing=second, recast={102}))
    assert third[102] not in (first[102], second[102])


def test_seed_is_the_documented_crc(fake_bank):
    assert cast.seed(102, 3) == zlib.crc32(b"voice:102:3")


def test_distinctness(fake_bank):
    calls = []

    def render(spec):
        calls.append(spec.guid)
        return np.ones(DIM, dtype=np.float32) + 1e-3 * np.random.default_rng(len(calls)).random(DIM)
    report = {}
    specs = by_guid(make(CAST[:2], fake_bank, render=render, report=report))
    assert calls.count(102) == 1 and calls.count(101) == 20  # the officer is cast first
    assert len(specs) == 2
    flagged = [m for m in report["members"] if m["flagged"]]
    assert [m["guid"] for m in flagged] == [101]
    assert report["flagged"] == [101] and flagged[0]["max_cosine"] > 0.99


def test_distinct_voices_are_not_flagged(fake_bank):
    report = {}
    make(CAST, fake_bank, report=report)
    assert report["flagged"] == []
    assert report["date"] == TODAY and report["threshold"] == 0.80
    pairs = report["closest_pairs"]
    assert len(pairs) == 10 and pairs == sorted(pairs, key=lambda p: -p["cosine"])


def test_the_least_similar_candidate_is_kept(fake_bank):
    n = [0]

    def render(spec):
        n[0] += 1
        v = np.zeros(DIM, dtype=np.float32)
        v[0] = 1.0
        if spec.guid == 101:
            v[1] = [0.1, 0.3, 0.2, 0.4][n[0] % 4]  # always close to 102's, never equal
        return v
    report = {}
    make(CAST[:2], fake_bank, render=render, report=report)
    # 102 first (officer), 101 after against it: all its tries are tried, the best is kept
    m = next(m for m in report["members"] if m["guid"] == 101)
    assert m["flagged"] and m["tries"] == 20
    assert m["max_cosine"] == min(m["cosines"])


@pytest.mark.parametrize("descriptors, check", [
    (["quick"], lambda s: s.speed >= 1.08),
    (["measured"], lambda s: s.speed <= 0.95),
    (["slow", "dry"], lambda s: s.speed <= 0.95),
    (["low"], lambda s: s.pitch_st <= -1.0),
    (["high"], lambda s: s.pitch_st == 1.0),
    (["raspy"], lambda s: s.mic in ("hot_mic", "headset_cheap")),
    (["hoarse"], lambda s: s.mic in ("hot_mic", "headset_cheap")),
    (["quiet"], lambda s: s.gain_db <= -1.5 and s.mic == "soft_mic"),
    (["soft-spoken", "soft"], lambda s: s.gain_db <= -1.5 and s.mic == "soft_mic"),
])
def test_descriptor_levers(fake_bank, descriptors, check):
    for guid in range(10, 16):
        s = make([entry(guid, "Arkon", descriptors=descriptors)], fake_bank)[0]
        assert check(s), (descriptors, s)
        assert s.mic in fx.MIC_PROFILES


def test_speed_comes_from_the_persona_clamped(fake_bank):
    s = {sp: make([entry(1, "Arkon", speed=sp)], fake_bank)[0].speed for sp in (0.5, 1.0, 2.0)}
    assert s == {0.5: 0.9, 1.0: 1.0, 2.0: 1.15}
    plain = make([entry(1, "Arkon", descriptors=["warm"], speed=1.05)], fake_bank)[0]
    assert plain.speed == 1.05 and plain.pitch_st == 0 and plain.gain_db == 0


def test_a_group_the_bank_lacks_is_an_error(fake_bank):
    with pytest.raises(cast.CastError, match="Gru"):
        make([entry(1, "Gru", "male", "en-gb")], fake_bank)  # the fake bank has no British men


def test_brief_round_trip(tmp_path):
    path = tmp_path / "brief.json"
    path.write_text(json.dumps([{
        "guid": 101, "name": "Arkon", "gender": "male", "accent": "en-us", "age": 31,
        "descriptors": ["low"], "speed": 1.0, "guild_role": "raider", "raid_role": "tank",
        "core": True}]), encoding="utf-8")
    [e] = cast.load_brief(path)
    assert e == entry(101, "Arkon", "male", descriptors=["low"], core=True, age=31,
                      raid_role="tank")
    path.write_text(json.dumps([{"guid": 1, "name": "X"}]), encoding="utf-8")
    with pytest.raises(cast.CastError):
        cast.load_brief(path)


# -- the command --------------------------------------------------------------------------------

@pytest.fixture
def rig(live_guard, tmp_path, monkeypatch, fake_voices_bin):
    """The data dir, a brief and the fakes behind the command's three seams."""
    data = live_guard.data_dir
    data.mkdir()
    out = tmp_path / "mind-data" / "voices"
    brief = out.parent / "brief.json"
    out.mkdir(parents=True)
    brief.write_text(json.dumps([{
        "guid": e.guid, "name": e.name, "gender": e.gender, "accent": e.accent, "age": e.age,
        "descriptors": list(e.descriptors), "speed": e.speed, "guild_role": e.guild_role,
        "raid_role": e.raid_role, "core": e.core} for e in CAST]), encoding="utf-8")
    monkeypatch.setattr(cli, "load_engine", lambda root, cfg: (FakeEngine(),
                                                               StyleBank(fake_voices_bin)))
    monkeypatch.setattr(cli, "load_embedder", lambda root: (
        lambda audio: np.random.default_rng(zlib.crc32(np.asarray(audio).tobytes()))
        .standard_normal(DIM)))
    return type("Rig", (), {"data": data, "out": out, "brief": brief})


def run_cast(rig, *extra):
    return main(["cast", "--brief", str(rig.brief), "--out", str(rig.out), *extra])


def test_written_to_mind_data(rig, capsys):
    assert run_cast(rig) == 0
    files = sorted(p.name for p in rig.out.glob("*.json"))
    assert files == [f"{g}.json" for g in (101, 102, 103, 104, 105)]
    for p in rig.out.glob("*.json"):
        spec = spec_from_dict(json.loads(p.read_text(encoding="utf-8")))
        assert spec.guid == int(p.stem) and not spec.locked
    report = json.loads((rig.data / "cast" / "report.json").read_text(encoding="utf-8"))
    assert report["flagged"] == [] and len(report["members"]) == 5
    assert {m["status"] for m in report["members"]} == {"new"}
    stock = rig.data / "cache" / "stock"
    for g in (101, 102, 103, 104, 105):
        [folder] = (stock / str(g)).iterdir()
        assert len(list(folder.glob("*.wav"))) == len(STOCK)
    assert "5 members" in capsys.readouterr().out


def test_a_second_run_changes_nothing(rig):
    assert run_cast(rig) == 0
    before = {p.name: p.read_bytes() for p in rig.out.glob("*.json")}
    stock = rig.data / "cache" / "stock"
    clips = sorted(p.name for p in stock.rglob("*.wav"))
    assert run_cast(rig) == 0
    assert {p.name: p.read_bytes() for p in rig.out.glob("*.json")} == before
    assert sorted(p.name for p in stock.rglob("*.wav")) == clips


def test_recast_rerenders_stock(rig):
    assert run_cast(rig) == 0
    stock = rig.data / "cache" / "stock" / "102"
    [old] = stock.iterdir()
    before = {p.name: p.read_bytes() for p in rig.out.glob("*.json")}
    assert run_cast(rig, "--recast", "102") == 0
    [new] = stock.iterdir()
    assert new.name != old.name and not old.exists()
    assert len(list(new.glob("*.wav"))) == len(STOCK)
    after = {p.name: p.read_bytes() for p in rig.out.glob("*.json")}
    assert after["102.json"] != before["102.json"]
    assert {k: v for k, v in after.items() if k != "102.json"} == \
           {k: v for k, v in before.items() if k != "102.json"}


def test_recast_leaves_a_locked_voice_alone(rig, capsys):
    assert run_cast(rig) == 0
    assert main(["lock", "102", "--specs", str(rig.out)]) == 0
    before = (rig.out / "102.json").read_bytes()
    assert run_cast(rig, "--recast", "102") == 0
    assert (rig.out / "102.json").read_bytes() == before
    report = json.loads((rig.data / "cast" / "report.json").read_text(encoding="utf-8"))
    assert report["skipped_locked"] == [102]


def test_lock_all_and_some(rig, capsys):
    assert run_cast(rig) == 0
    spec = lambda g: json.loads((rig.out / f"{g}.json").read_text(encoding="utf-8"))  # noqa: E731
    assert main(["lock", "101,103", "--specs", str(rig.out)]) == 0
    assert [spec(g)["locked"] for g in (101, 102, 103)] == [True, False, True]
    assert main(["lock", "all", "--specs", str(rig.out)]) == 0
    assert all(spec(g)["locked"] for g in (101, 102, 103, 104, 105))
    assert main(["lock", "999", "--specs", str(rig.out)]) == 1
    assert "999" in capsys.readouterr().err


def test_threshold_option_reaches_the_cast(rig):
    assert run_cast(rig, "--threshold", "0.99") == 0
    report = json.loads((rig.data / "cast" / "report.json").read_text(encoding="utf-8"))
    assert report["threshold"] == 0.99


def test_no_names_in_the_logs(rig, caplog):
    caplog.set_level("DEBUG")
    assert run_cast(rig) == 0
    assert "Belra" not in caplog.text


# -- the audition -------------------------------------------------------------------------------

def test_audition_lists_closest_pairs(rig, tmp_path, capsys):
    assert run_cast(rig) == 0
    assert main(["lock", "101", "--specs", str(rig.out)]) == 0
    assert main(["audition", "--specs", str(rig.out)]) == 0
    page = rig.data / "audition" / "index.html"
    html = page.read_text(encoding="utf-8")
    report = json.loads((rig.data / "cast" / "report.json").read_text(encoding="utf-8"))
    assert len(report["closest_pairs"]) == 10
    for pair in report["closest_pairs"]:
        assert f"{pair['cosine']:.3f}" in html
    for name in ("Arkon", "Belra", "Coros", "Dunna", "Ebbo"):
        assert name in html
    assert "British" in html and "American" in html and "locked" in html
    assert "provisional" in html
    clips = sorted(p.name for p in (rig.data / "audition" / "clips").glob("*.wav"))
    assert clips == [f"{g}.wav" for g in (101, 102, 103, 104, 105)]
    srcs = set(re.findall(r'src="(clips/\d+\.wav)"', html))
    assert srcs == {f"clips/{c}" for c in clips}
    assert str(page) in capsys.readouterr().out


def test_audition_escapes_names(tmp_path, fake_bank, fake_engine):
    spec = make([entry(1, "A<b>&", "male")], fake_bank)[0]
    spec = spec_from_dict({**spec_to_dict(spec), "name": "A<b>&"})
    page = audition.build_audition([spec], fake_engine, tmp_path / "audition",
                                   report={"closest_pairs": [], "threshold": 0.8},
                                   bank=fake_bank, decision=Decision())
    html = page.read_text(encoding="utf-8")
    assert "A<b>&" not in html and "A&lt;b&gt;&amp;" in html
