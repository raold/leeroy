"""Voice specs, Kokoro style blends (on the fake bank's seven packs) and the fake engine."""
import dataclasses
import hashlib

import numpy as np
import pytest

from conftest import BANK_SHA, PACK, voices_bin
from leeroy_voice.decision import Decision
from leeroy_voice.engines.fake import FakeEngine
from leeroy_voice.voices import (GROUPS, SWAP_DONORS, SpecError, StyleBank, spec_from_dict,
                                 spec_to_dict)


def spec(**kw): return spec_from_dict({"version": 1, "guid": 101, "name": "Arkon", "engine": "kokoro",
    "lang": "a", "timbre": {"am_michael": 0.7, "am_fenrir": 0.3}, "prosody": {}, "speed": 1.05,
    "pitch_st": -1.0, "mic": "desk_mic", "gain_db": 0.0, "locked": False,
    "voices_sha256": BANK_SHA, "ref": "", **kw})


def her(**kw):
    return spec(**{"guid": 102, "name": "Belra", "timbre": {"af_kore": 0.5, "af_sarah": 0.5}, **kw})


def test_round_trip(): assert spec_from_dict(spec_to_dict(spec())) == spec()


@pytest.mark.parametrize("change, field", [
    ({"timbre": {"am_michael": 0.6, "am_fenrir": 0.3}}, "timbre"),     # weights summing to 0.9
    ({"speed": 1.3}, "speed"), ({"pitch_st": 3}, "pitch_st"), ({"gain_db": 4}, "gain_db"),
    ({"lang": "c"}, "lang"),
    ({"timbre": {"zm_yunxi": 1.0}}, "timbre"),                        # a base not in GROUPS
], ids=["weights-0.9", "speed-1.3", "pitch-3", "gain-4", "lang-c", "not-in-groups"])
def test_checks(change, field):
    with pytest.raises(SpecError, match=f"^{field}:"):
        spec(**change)


@pytest.mark.parametrize("change", [
    {"version": 2}, {"guid": 0}, {"guid": True}, {"name": ""}, {"name": " Arkon"},
    {"name": "A" * 65}, {"engine": "orpheus"},
    {"timbre": {}}, {"timbre": {"am_michael": 1.2, "am_fenrir": -0.2}},
    {"timbre": {"am_michael": "0.7", "am_fenrir": 0.3}}, {"timbre": ["am_michael"]},
    {"prosody": {"am_michael": 0.5}},                                 # prosody weights 0.5
    {"speed": float("nan")}, {"speed": True}, {"pitch_st": "-1"}, {"locked": 0},
    {"mic": ""}, {"mic": "Desk Mic"}, {"voices_sha256": "abc"}, {"voices_sha256": BANK_SHA.upper()},
    {"ref": "102.ref.wav"},                                           # Kokoro takes no reference
    {"engine": "chatterbox"},                                         # a cloning engine needs one
    {"timbre": {"am_michael": 0.5, "af_kore": 0.5}},                  # two genders
    {"prosody": {"af_nicole": 1.0}},                                  # a woman's prosody on his
    {"timbre": {"am_michael": 0.5, "bm_george": 0.5}},                # a British base, US spec
    {"lang": "b", "timbre": {"af_kore": 0.5, "af_sarah": 0.5}},       # US timbre, no UK prosody
    {"lang": "b", "timbre": {"bf_emma": 1.0}, "prosody": {"af_nicole": 1.0}},   # US prosody, UK
])
def test_more_checks(change):
    with pytest.raises(SpecError):
        spec(**change)


def test_cloning_spec_names_its_clip():          # Task 22: a cloning engine and its ref together
    s = spec(engine="chatterbox", ref="102.ref.wav")
    assert s.ref == "102.ref.wav" and spec_from_dict(spec_to_dict(s)) == s


@pytest.mark.parametrize("ref", [
    "../102.ref.wav", "..", ".", "refs/102.ref.wav", r"refs\102.ref.wav", r"C:\refs\102.ref.wav",
    "C:102.ref.wav", "/102.ref.wav", r"\\host\share\102.ref.wav", "https://example.com/102.wav",
    "102.ref.wav:stream", "102|ref.wav", "102?.wav", "102*.wav", '"102.wav"', "<102>.wav",
])
def test_ref_is_a_bare_file_name(ref):            # no folder, drive, stream or URL
    with pytest.raises(SpecError, match="^ref:"):
        spec(engine="chatterbox", ref=ref)


def test_keys_checked():
    d = spec_to_dict(spec())
    with pytest.raises(SpecError, match="missing"):
        spec_from_dict({k: v for k, v in d.items() if k != "ref"})
    with pytest.raises(SpecError, match="unknown"):
        spec_from_dict({**d, "colour": "blue"})
    with pytest.raises(SpecError):
        spec_from_dict([("version", 1)])


def test_numbers_normalised_and_owned():
    s = spec(speed=1, gain_db=0, timbre={"am_michael": 1})
    assert (s.speed, s.gain_db, s.timbre) == (1.0, 0.0, {"am_michael": 1.0})
    assert all(type(v) is float for v in (s.speed, s.gain_db, s.timbre["am_michael"]))
    timbre = {"am_michael": 0.7, "am_fenrir": 0.3}
    s = spec(timbre=timbre)
    timbre["am_michael"] = 0.1                       # the caller's dict is not the spec's
    assert s.timbre == {"am_michael": 0.7, "am_fenrir": 0.3}
    with pytest.raises(SpecError):                   # a spec stays valid when changed
        dataclasses.replace(spec(), speed=1.3)
    assert spec_from_dict(spec_to_dict(her(lang="b", timbre={"bf_emma": 1.0}))).lang == "b"
    s = spec()
    s.timbre["am_michael"] = 0.9                     # changed in place: no longer a valid recipe
    with pytest.raises(SpecError):
        spec_to_dict(s)


def test_groups_are_the_english_voices():
    assert set(GROUPS) == {("f", "a"), ("m", "a"), ("f", "b"), ("m", "b")}
    sizes = [len(GROUPS[g]) for g in (("f", "a"), ("f", "b"), ("m", "a"), ("m", "b"))]
    assert sizes == [11, 4, 9, 4]
    for (gender, lang), grades in GROUPS.items():
        assert all(v.startswith(f"{lang}{gender}_") for v in grades)
    # the draft's section 7.2 table: the voices graded C or better
    assert {v: g for group in GROUPS.values() for v, g in group.items() if g[0] in "ABC" and
            g != "C-"} == {
        "af_heart": "A", "af_bella": "A-", "af_nicole": "B-", "af_aoede": "C+", "af_kore": "C+",
        "af_sarah": "C+", "af_alloy": "C", "af_nova": "C", "bf_emma": "B-", "bf_isabella": "C",
        "am_fenrir": "C+", "am_michael": "C+", "am_puck": "C+", "bm_fable": "C", "bm_george": "C"}


def test_swap_donors():
    assert SWAP_DONORS == {"fond": ({"a": "af_bella", "b": "bf_isabella"}, 0.5),
                           "drawn": ({"a": "af_nicole", "b": "bf_lily"}, 0.6),
                           "lust": ({"a": "af_nicole", "b": "bf_lily"}, 0.85)}


def test_bank(fake_bank, fake_voices_bin, tmp_path):
    assert fake_bank.sha256 == BANK_SHA == hashlib.sha256(fake_voices_bin.read_bytes()).hexdigest()
    assert fake_bank.names() == sorted(PACK)
    odd = tmp_path / "voices.bin"
    odd.write_bytes(voices_bin({"af_kore": PACK["af_kore"][:10]}))   # not Kokoro's 510 rows
    with pytest.raises(ValueError, match="af_kore"):
        StyleBank(odd)


def test_blend_is_the_weighted_sum(fake_bank):
    s = fake_bank.style(spec(), "none", Decision())
    assert np.allclose(s, 0.7 * PACK["am_michael"] + 0.3 * PACK["am_fenrir"])


def test_rounded_weights_scaled_to_one(fake_bank):
    s = fake_bank.style(spec(timbre={"am_michael": 0.704, "am_fenrir": 0.3}), "none", Decision())
    # scaled before or after the sum: float32 rounding differs by an ulp or two
    assert np.allclose(s, (0.704 * PACK["am_michael"] + 0.3 * PACK["am_fenrir"]) / 1.004, atol=1e-6)
    assert not np.allclose(s, 0.704 * PACK["am_michael"] + 0.3 * PACK["am_fenrir"], atol=1e-6)


def test_style_shape_and_cache(fake_bank):
    s = fake_bank.style(spec(), "none", Decision())
    assert s.shape == (510, 1, 256) and s.dtype == np.float32 and not s.flags.writeable
    assert fake_bank.style(spec(), "none", Decision()) is s           # cached
    assert fake_bank.style(spec(), "lust", Decision(prosody_swap=True)) is s   # nothing to change
    assert fake_bank.style(her(), "drawn", Decision()) is fake_bank.style(her(), "none", Decision())
    recast = fake_bank.style(spec(timbre={"am_michael": 0.2, "am_fenrir": 0.8}), "none", Decision())
    assert np.allclose(recast, 0.2 * PACK["am_michael"] + 0.8 * PACK["am_fenrir"])   # not stale


def test_british_recipe(fake_bank):
    s = fake_bank.style(spec(lang="b", timbre={"af_kore": .5, "af_sarah": .5}, prosody={"bf_emma": 1.0}),
                        "none", Decision(uk_timbre_mix=True))
    assert np.allclose(s[..., :128], (.5 * PACK["af_kore"] + .5 * PACK["af_sarah"])[..., :128])
    assert np.allclose(s[..., 128:], PACK["bf_emma"][..., 128:])


def test_british_recipe_refused_until_decided(fake_bank):
    with pytest.raises(SpecError, match="uk_timbre_mix"):
        fake_bank.style(spec(lang="b", timbre={"af_kore": .5, "af_sarah": .5},
                             prosody={"bf_emma": 1.0}), "none", Decision())


def test_swap_only_when_decided(fake_bank):          # M13: a woman's voice
    her = spec(guid=102, name="Belra", timbre={"af_kore": 0.5, "af_sarah": 0.5})
    base = fake_bank.style(her, "none", Decision())
    assert np.allclose(fake_bank.style(her, "lust", Decision()), base)
    swapped = fake_bank.style(her, "lust", Decision(prosody_swap=True))
    assert np.allclose(swapped[..., 128:], .15 * base[..., 128:] + .85 * PACK["af_nicole"][..., 128:])
    assert np.allclose(swapped[..., :128], base[..., :128])


def test_british_swap_takes_the_british_donor(fake_bank):
    emma, lily = PACK["bf_emma"][..., 128:], PACK["bf_lily"][..., 128:]
    uk = her(lang="b", timbre={"bf_emma": 1.0})
    swapped = fake_bank.style(uk, "drawn", Decision(prosody_swap=True))
    assert np.allclose(swapped[..., 128:], .4 * emma + .6 * lily)
    mix = her(lang="b", prosody={"bf_emma": 1.0})                    # the British recipe
    swapped = fake_bank.style(mix, "lust", Decision(prosody_swap=True, uk_timbre_mix=True))
    assert np.allclose(swapped[..., 128:], .15 * emma + .85 * lily)


def test_male_timbre_ignores_attitude(fake_bank):    # M13
    assert np.allclose(fake_bank.style(spec(), "lust", Decision(prosody_swap=True)),
                       fake_bank.style(spec(), "none", Decision()))


def test_another_voices_file_refused(fake_bank):
    with pytest.raises(SpecError, match="voices_sha256"):
        fake_bank.style(spec(voices_sha256="0" * 64), "none", Decision())


def test_what_the_file_lacks_refused(fake_bank):
    with pytest.raises(SpecError, match="af_heart"):         # in GROUPS, not in the fake file
        fake_bank.style(her(timbre={"af_heart": 1.0}), "none", Decision())
    with pytest.raises(SpecError, match="af_bella"):         # the fond donor
        fake_bank.style(her(), "fond", Decision(prosody_swap=True))
    with pytest.raises(ValueError):
        fake_bank.style(her(), "smug", Decision())


def test_fake_engine_is_deterministic():
    engine = FakeEngine()
    assert (engine.name, engine.sample_rate) == ("fake", 24000)
    a = engine.synth("Pulling.", spec(), None, 1.0)
    assert a.dtype == np.float32 and a.ndim == 1 and len(a) == round(8 * 0.060 * 24000)
    assert np.array_equal(a, FakeEngine().synth("Pulling.", spec(), None, 1.0))
    b = engine.synth("Pulling.", her(), None, 1.0)
    spectrum = lambda x: int(np.argmax(np.abs(np.fft.rfft(x))))  # noqa: E731
    assert len(b) == len(a) and spectrum(b) != spectrum(a)      # the pitch follows the guid
    assert len(engine.synth("Pulling.", spec(), None, 1.2)) == round(len(a) / 1.2)   # like Kokoro
    assert len(FakeEngine(sample_rate=48000).synth("Pulling.", spec(), None, 1.0)) == 2 * len(a)
    assert np.max(np.abs(a)) <= 1.0
