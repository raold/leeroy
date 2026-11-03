import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

from leeroy_voice.lexicon import BUILTIN, PHONEMES, Lexicon, LexiconError, load_lexicon

# the public WoW nouns the plan names for misaki's check (Task 4 Step 5)
CHECK = ["Sindragosa", "Lich King", "Arthas", "Ulduar", "Icecrown", "Saurfang", "Putricide",
         "Rotface", "Festergut", "Lana'thel", "Valithria", "Marrowgar", "Deathwhisper", "Algalon",
         "Yogg-Saron", "Mimiron", "Thorim", "Hodir", "Freya", "Anub'arak", "Naxxramas", "Dalaran",
         "Orgrimmar", "Thrall", "Garrosh", "Malygos", "Sartharion", "Onyxia", "Wintergrasp",
         "Archavon"]
KT = "kˌɛlθˈuzɑd"


def local(tmp_path: Path, text: str) -> Path:
    (tmp_path / "lexicon.toml").write_text(text, encoding="utf-8")
    return tmp_path


def test_whole_words_only():
    lx = Lexicon({"Ony": "ˈɑni"})
    assert lx.apply("Onyxia and Ony") == "Onyxia and [Ony](/ˈɑni/)"


def test_any_case_kept_as_written():
    lx = Lexicon({"Kel'Thuzad": KT})
    assert lx.apply("kel'thuzad, then Kel\u2019Thuzad's phylactery") == (
        f"[kel'thuzad](/{KT}/), then [Kel\u2019Thuzad](/{KT}/)'s phylactery")


def test_compounds_and_markup_left_alone():
    lx = Lexicon({"Yogg": "jˈɑ\u0261", "Saron": "sˈɛɹɑn"})
    assert lx.apply("Yogg-Saron, then Yogg") == "Yogg-Saron, then [Yogg](/jˈɑ\u0261/)"
    assert lx.apply("[Yogg](/jˈɑ\u0261/) again") == "[Yogg](/jˈɑ\u0261/) again"


def test_longest_entry_wins():
    lx = Lexicon({"Lich": "lˈɪʧ", "Lich King": "lˈɪʧ kˈɪŋ"})
    assert lx.apply("the lich king and the Lich") == (
        "the [lich king](/lˈɪʧ kˈɪŋ/) and the [Lich](/lˈɪʧ/)")


@pytest.mark.parametrize("entries", [
    {"": "ˈɑni"}, {"Ony": ""}, {"Ony": "ˈɑ/ni"}, {"Ony": "ˈɑni]"}, {"Ony": "ˈɑni)"},
    {"O]ny": "ˈɑni"}, {"Ony!": "ˈɑni"}, {"Ony": 3},
    {"Ony": "ˈɑgi"},                 # an ASCII g: Kokoro has only U+0261 and would drop it
])
def test_bad_entries_refused(entries):
    with pytest.raises(LexiconError):
        Lexicon(entries)


def test_words():
    lx = Lexicon({"Kel'Thuzad": KT}, extra_words=["Ulduar", "ICC", "ulduar"])
    assert lx.words() == ["ICC", "Kel'Thuzad", "Ulduar"]
    assert Lexicon({}).words() == [] and Lexicon({}).apply("as it was") == "as it was"


def test_local_file_overrides_builtin(tmp_path):   # voice-data/lexicon.toml entry wins over the built-in one
    lx = load_lexicon(local(tmp_path, '"Kel\'Thuzad" = "kˈɛlθuzæd"\nBelra = "bˈɛlɹə"\n'
                                      'check = ["Coros"]\n'))
    assert lx.apply("Kel'Thuzad and Belra") == "[Kel'Thuzad](/kˈɛlθuzæd/) and [Belra](/bˈɛlɹə/)"
    assert {"Coros", "Belra", "Kel'Thuzad", "Sindragosa"} <= set(lx.words())
    lx = load_lexicon(local(tmp_path, '"kel\'thuzad" = "kˈɛlθuzæd"\n'))       # any case
    assert lx.apply("Kel'Thuzad") == "[Kel'Thuzad](/kˈɛlθuzæd/)"
    assert load_lexicon(tmp_path / "absent").apply("Kel'Thuzad") == f"[Kel'Thuzad](/{KT}/)"


@pytest.mark.parametrize("text, match", [
    ("Ony = 3\n", r"lexicon\.toml.*Ony"),
    ('Ony = "ˈɑgi"\n', r"lexicon\.toml.*Ony"),
    ('Ony = "\n', r"lexicon\.toml"),
    ('check = "Ony"\n', r"lexicon\.toml.*check"),
    ("[ipa]\nOny = \"ˈɑni\"\n", r"lexicon\.toml.*ipa"),
])
def test_bad_local_file_named(tmp_path, text, match):
    with pytest.raises(LexiconError, match=match):
        load_lexicon(local(tmp_path, text))


def test_builtin_file_parses(tmp_path):            # every entry of data/wow_lexicon.toml: non-empty IPA, no "/" or "]"
    data = tomllib.loads(BUILTIN.read_text(encoding="utf-8"))
    check = data.pop("check")
    assert data["Kel'Thuzad"] == KT                 # the measured misread (draft 4.2)
    for word, ipa in data.items():
        assert isinstance(ipa, str) and ipa.strip(), word
        assert not set(ipa) & set("/[]()"), word
        assert set(ipa) - {" "} <= PHONEMES, word
    assert set(CHECK) <= set(check) | set(data)
    assert set(CHECK) | set(data) <= set(load_lexicon(tmp_path).words())


def test_phonemes_are_kokoros():
    """Every phoneme an entry may use is in Kokoro's vocabulary (kokoro-onnx's config.json,
    read without importing the package): one outside it would be dropped silently."""
    spec = importlib.util.find_spec("kokoro_onnx")
    if spec is None or spec.origin is None:
        pytest.skip("kokoro-onnx is not installed (the tts extra)")
    config = Path(spec.origin).with_name("config.json")
    vocab = json.loads(config.read_text(encoding="utf-8"))["vocab"]
    assert PHONEMES <= set(vocab) and "g" not in PHONEMES and "\u0261" in PHONEMES


# -- respellings (Task 22): a word an engine that reads letters says wrong ------------------------

def test_respell_whole_words_in_any_case():
    lx = Lexicon({}, respellings={"chatterbox-turbo": {"Testy": "Testai"}})
    assert lx.respell("testy and TESTY, Testy's Testymonial", "chatterbox-turbo") == (
        "Testai and Testai, Testai's Testymonial")
    assert lx.respell("Testy", "indextts2") == "Testy" and lx.respell("Testy", "kokoro") == "Testy"
    assert Lexicon({}).respell("Testy", "chatterbox-turbo") == "Testy"


def test_respell_comes_from_the_local_file(tmp_path):
    root = local(tmp_path, '[respell.chatterbox-turbo]\nTesty = "Testai"\n')
    lx = load_lexicon(root)
    assert lx.respell("hey Testy", "chatterbox-turbo") == "hey Testai"
    assert "Testy" not in lx.words() and "Testai" not in lx.words()


def test_respell_lives_beside_ipa_entries(tmp_path):
    root = local(tmp_path, f'Arkon = "{KT}"\n[respell.chatterbox-turbo]\nTesty = "Testai"\n')
    lx = load_lexicon(root)
    assert lx.apply("Arkon") == f"[Arkon](/{KT}/)"
    assert lx.respell("Testy", "chatterbox-turbo") == "Testai"


def test_the_builtin_lexicon_respells_nothing(tmp_path):
    lx = load_lexicon(tmp_path)
    assert lx.respell("Arthas and Sindragosa", "chatterbox-turbo") == "Arthas and Sindragosa"


@pytest.mark.parametrize("text", [
    '[respell]\nTesty = "Testai"\n',                     # a word, not an engine
    '[respell.chatterbox-turbo]\nTesty = 3\n',
    '[respell.chatterbox-turbo]\n"Te]sty" = "Testai"\n',
    '[respell.chatterbox-turbo]\nTesty = "Te(stai"\n',
    'respell = 3\n',
])
def test_bad_respellings_refused(tmp_path, text):
    with pytest.raises(LexiconError):
        load_lexicon(local(tmp_path, text))
