"""The realm's screen: every rival or member line in a public channel passes it or is dropped."""
import pytest

from leeroy_mind.human import HumanIdentity
from leeroy_mind.realm.screen import BLOCKLIST, GROUP_NOUNS, INSULTS, REASONS, Screen

HUMAN = HumanIdentity(10, "Testmaster", "Testy", "Quillon", True, "he")
GUILD = "Testguild"


def make(root, *, guild=GUILD, blocklist="# synthetic tokens only\nzorbleflax\nquux frob\n"):
    if blocklist is not None:
        path = root / BLOCKLIST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(blocklist, encoding="utf-8")
    name = guild if callable(guild) else (lambda: guild)
    return Screen(root, name, lambda: HUMAN)


@pytest.fixture
def screen(tmp_path):
    made = make(tmp_path)
    assert made.ready
    return made


def check(screen, text, kind="officer"):
    return screen.check(text, speaker_kind=kind)


def test_screen_group_insult(screen):
    assert check(screen, "typical canadians scrubs") == "group_insult"
    assert check(screen, "typical huntard scrubs") is None
    assert check(screen, "lol women are so bad at pvp") == "group_insult"
    assert check(screen, "Lutherans are scrubs") == "group_insult"  # case-folded, plural
    assert check(screen, "catholics pulled the boss early tonight, other scrubs wiped") is None
    assert check(screen, "gz to the canadians on the kill") is None


def test_screen_blocklist(screen):
    assert check(screen, "what a ZORBLEFLAX pull") == "blocklist"
    assert check(screen, "zorbleflaxing again") is None  # whole words only
    assert check(screen, "total quux  frob tank") == "blocklist"
    assert check(screen, "a quux tank") is None


def test_screen_guild_name(screen):
    assert check(screen, "testguild cant even clear naxx") == "guild_name"
    assert check(screen, "<Testguild> recruiting? lol") == "guild_name"
    assert check(screen, "Testguild's tanks are asleep") == "guild_name"
    assert check(screen, "testguilders wipe again") is None


def test_screen_leaves_and_flirt(screen):
    assert check(screen, "she left the guild lol") == "leaves"
    assert check(screen, "hey cutie ;)") == "flirt"
    assert check(screen, "i want you so bad") == "flirt"
    assert check(screen, "your tank is so hot when he taunts") == "flirt"  # rivals never flirt
    assert check(screen, "your new tier looks so good on paper, shame about the parses") is None


def test_screen_real_name(screen):
    assert check(screen, "Quillon is bad") == "real_name"
    assert check(screen, "QUILLON can't heal") == "real_name"


def test_screen_tells_and_too_long(screen):
    assert check(screen, "happy to help with that pull") == "tells"
    assert check(screen, "your raid leader — asleep again") == "tells"  # the pipeline repairs first
    assert check(screen, "(nothing)") == "tells"
    assert check(screen, "") == "tells"
    assert check(screen, "lol " + "x" * 116) is None
    assert check(screen, "lol " + "x" * 117) == "too_long"


def test_screen_passes_mock_play(screen):
    for line in ("lol imagine wiping on patchwerk with that gear",
                 "nice parses, did ur healers afk the whole fight",
                 "wts [Frozen Orb] x2, cheaper than ur repair bill"):
        assert check(screen, line) is None
        assert check(screen, line, "member") is None


def test_screen_fails_closed_without_blocklist(tmp_path):
    made = make(tmp_path, blocklist=None)
    assert made.ready is False
    assert check(made, "gz") == "not_ready"
    assert make(tmp_path, blocklist="# nothing yet\n\n").ready is False
    armed = make(tmp_path)
    assert armed.ready and check(armed, "gz") is None
    (tmp_path / BLOCKLIST).unlink()
    assert armed.ready is False and check(armed, "gz") == "not_ready"


def test_screen_fails_closed_without_guild_name(tmp_path):
    assert make(tmp_path, guild="").ready is False
    assert make(tmp_path, guild="   ").ready is False

    def unavailable():
        raise RuntimeError("armory unavailable")
    made = make(tmp_path, guild=unavailable)
    assert made.ready is False and check(made, "gz") == "not_ready"


def test_screen_reloads_a_changed_blocklist(screen, tmp_path):
    assert check(screen, "what a blorptastic pull") is None
    (tmp_path / BLOCKLIST).write_text("blorptastic\n", encoding="utf-8")
    assert check(screen, "what a blorptastic pull") == "blocklist"


def test_screen_rejects_an_unknown_speaker(screen):
    with pytest.raises(ValueError):
        check(screen, "gz", "ganker")


def test_screen_vocabulary_is_neutral_and_reasons_are_closed():
    assert REASONS == ("not_ready", "blocklist", "group_insult", "guild_name", "leaves", "flirt",
                       "real_name", "tells", "too_long")
    assert not GROUP_NOUNS & INSULTS
    assert all(word == word.casefold() and word.strip() == word for word in GROUP_NOUNS | INSULTS)
