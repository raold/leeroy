"""The hard screen on sexual wording in the game's public channels (talk/publicscreen.py): which
channels it reads, which words it catches, and the words a raid uses in play that it leaves alone.
Invented lines only."""
import pytest

from leeroy_mind.talk import publicscreen as ps


@pytest.mark.parametrize("channel", ["guild", "officer", "party", "raid", "raid_warning", "say",
                                     "yell"])
def test_public_game_channels(channel):
    assert ps.public_channel(channel)


@pytest.mark.parametrize("channel", ["whisper", "dm:102", "gdm:4", "discord:general",
                                     "discord:loot-and-dkp", "voice:raid", "", None])
def test_never_a_private_discord_or_voice_channel(channel):
    assert not ps.public_channel(channel)


@pytest.mark.parametrize("text", [
    "tank in nothing, get naked", "so horny tonight", "thinking of you inside of me",
    "gonna make you scream", "screaming your name all night", "bend you over the anvil",
    "pin you down when we're done", "you turn me on so much", "take you to bed after",
    "slipping off my panties", "hot for u right now", "you face down on the bed later"])
def test_sexual_wording(text, tmp_path):
    assert ps.sexual(text, tmp_path)
    assert ps.screened("raid", text, tmp_path) and ps.screened("guild", text, tmp_path)
    assert not ps.screened("whisper", text, tmp_path)
    assert not ps.screened("discord:general", text, tmp_path)


@pytest.mark.parametrize("text", [
    "im on top of you on the meters", "ride with me to the next boss", "inside the instance now",
    "this boss sucks", "hot dps on that pull", "face down in the slime again lol",
    "bent the rules a bit on loot", "miss you already", "you look good in that tier set",
    "screaming at the tank to taunt", "pull when ready", "want you on my team tonight", ""])
def test_raid_words_and_plain_flirting_pass(text, tmp_path):
    assert not ps.sexual(text, tmp_path)


def test_the_owners_block_list_counts(tmp_path):
    (tmp_path / "prompts" / "heat").mkdir(parents=True)
    (tmp_path / "prompts" / "heat" / "public_block.txt").write_text("# mine\nvelvet rope\n",
                                                                     encoding="utf-8")
    assert ps.screened("guild", "bring the velvet rope", tmp_path)
    assert not ps.screened("guild", "bring the rope", tmp_path)
