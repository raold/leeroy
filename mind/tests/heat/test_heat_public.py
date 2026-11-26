"""[heat] public_cap: off by default (the owner's "whatever channel" ruling stands), and when on
it holds in three places: the resolver, the overlay loader and a guard on the generated line."""
import asyncio

import pytest

from conftest import make_persona
from engine_rig import OVERLAY, make_rig, save_member
from heat.rig import CAPPED, ON, attach
from leeroy_mind import heat as H
from leeroy_mind import prompts
from leeroy_mind.heat import guard, overlays
from leeroy_mind.heat.config import HeatConfig, load_heat_config
from leeroy_mind.heat.live import resolve_for
from leeroy_mind.heat.table import tier_index
from leeroy_mind.talk.pipeline import HEAT_PUBLIC, OWED_REFUSED
from leeroy_mind.talk.romance import BUILTIN_ROMANCE
from leeroy_mind.tuning.state import EMPTY

PUBLIC = ("guild", "officer", "party", "raid", "say", "yell", "voice:1", "discord:general",
          "discord:officer-chat", "gdm:3")
PRIVATE = ("whisper", "dm:104")


def write(tmp_path, body: str):
    (tmp_path / "mind.toml").write_bytes(body.encode())
    return load_heat_config(tmp_path / "mind.toml")


def test_the_table_is_on_by_default_its_cap_off_and_an_invalid_one_is_off(tmp_path):
    # shipped features default on (2026-11-25); public_cap stays off (owner ruling 2026-11-25)
    assert load_heat_config(tmp_path / "absent.toml") == HeatConfig(True, False) == HeatConfig()
    assert write(tmp_path, "") == HeatConfig()
    assert write(tmp_path, "[heat]\nenabled = false\n") == HeatConfig(False, False)
    assert write(tmp_path, "[heat]\nenabled = true\n") == HeatConfig(True, False)
    assert write(tmp_path, "[heat]\nenabled = true\npublic_cap = true\n") == HeatConfig(True, True)
    for bad in ("[heat]\nenabled = 1\n", "[heat]\nenabled = true\nlevel = 9\n",
                "heat = 3\n", "[heat]\npublic_cap = 'yes'\n", "[heat\n"):
        assert write(tmp_path, bad) == HeatConfig(False, False)


def test_heat_is_a_known_section_of_mind_toml(tmp_path):
    from leeroy_mind.config import EXTENSION_SECTIONS
    assert "heat" in EXTENSION_SECTIONS


def lust():
    return make_persona(104, "Dunna", player_gender="female", stance="lust")


@pytest.mark.parametrize("channel", PUBLIC)
def test_with_the_cap_off_a_public_channel_reads_as_before_the_dial(channel):
    assert resolve_for(lust(), EMPTY, 9, ON, channel=channel).tier == "explicit"


@pytest.mark.parametrize("channel", PUBLIC)
def test_with_the_cap_on_every_public_channel_is_flirty_at_most(channel):
    for dial in range(1, 11):
        for session in (None, "open", "clean"):
            r = resolve_for(lust(), EMPTY, dial, CAPPED, channel=channel, session=session,
                            late=True)
            assert tier_index(r.tier) <= tier_index("flirty"), (dial, session)


@pytest.mark.parametrize("channel", PRIVATE)
def test_the_cap_never_touches_a_whisper_or_his_dm(channel):
    assert resolve_for(lust(), EMPTY, 9, CAPPED, channel=channel).tier == "explicit"


def _files(tmp_path):
    folder = tmp_path / "prompts" / "heat"
    folder.mkdir(parents=True)
    for name in overlays.FILES:
        (folder / f"{name}.txt").write_bytes(f"FILE-{name}".encode())
    (tmp_path / "prompts" / "romance_explicit.txt").write_bytes(b"LEGACY-explicit")


def test_the_loader_never_opens_a_private_file_for_a_public_channel(tmp_path, monkeypatch):
    """Every private-only file, every public channel: no read at all with the cap on."""
    _files(tmp_path)
    opened = []
    real = prompts.read_overlay

    def spy(name, data_dir, folder=""):
        opened.append((folder, name))
        return real(name, data_dir, folder)
    monkeypatch.setattr(overlays, "read_overlay", spy)
    for name in overlays.PRIVATE_ONLY:
        assert overlays.variants(name, tmp_path, private=False, public_cap=True) == ()
    for tier in ("frank", "explicit", "suggestive", "flirty", "warm"):
        text = overlays.band_text(tier, tmp_path, private=False, public_cap=True)
        assert "FILE-" not in text and "LEGACY" not in text
    assert not [o for o in opened if o[1] in overlays.PRIVATE_ONLY or o[1] == "suggestive"]
    assert overlays.band_text("explicit", tmp_path, private=False, public_cap=True) \
        == BUILTIN_ROMANCE["flirty"]
    # in private, and with the cap off, the files are read
    assert overlays.band_text("explicit", tmp_path, private=True, public_cap=True) \
        == "FILE-explicit"
    assert overlays.band_text("frank", tmp_path, private=False, public_cap=False) == "FILE-frank"
    assert ("heat", "explicit") in opened and ("heat", "frank") in opened


def test_the_guards_word_list(tmp_path):
    assert not guard.public_hit("you looked good tonight, come sit by me", tmp_path)
    assert not guard.public_hit("", tmp_path)
    assert guard.public_hit("i am so horny", tmp_path)
    assert guard.public_hit("NAKED on a tuesday", tmp_path)
    assert not guard.public_hit("essex is nice", tmp_path)  # whole words only
    folder = tmp_path / "prompts" / "heat"
    folder.mkdir(parents=True)
    (folder / "public_block.txt").write_bytes(b"# his own\nSpicy Word\n\nanother phrase\n")
    assert overlays.block_words(tmp_path) == ("spicy word", "another phrase")
    assert guard.public_hit("what a  spicy   WORD that is", tmp_path)
    assert not guard.public_hit("spicy words", tmp_path)
    assert HEAT_PUBLIC in OWED_REFUSED


def _rig(store, clock, tmp_path, config, reply):
    save_member(store, 104, player_gender="female", stance="lust", style="bold")
    r = make_rig(store, clock, tmp_path, script=reply)
    r.engine.whispers.set_rate(0.0)
    r.engine.budget.set_to_him_rate(0.0)
    attach(r.engine, store, clock, tmp_path, config)
    return r


def _flirt(r):
    """A public flirt of hers at him, through the whole pipeline."""
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn, TurnKind

    async def go():
        await r.session()
        turn = Turn(kind=TurnKind.FLIRT, speaker=104, channel="guild", addressee=1, max_lines=1)
        task = asyncio.create_task(r.engine._speak(turn, TurnMeta(r.engine._new_id())))
        for _ in range(200):
            if task.done():
                break
            await r.advance(1)
            await asyncio.sleep(0.002)
        task.result()
    asyncio.run(go())


def test_a_capped_public_prompt_reads_flirty_and_an_explicit_line_is_never_sent(
        store, clock, tmp_path):
    replies = iter(["so horny right now", "still naked over here", "third"])
    r = _rig(store, clock, tmp_path, CAPPED, lambda model, messages: next(replies))
    _flirt(r)
    prompts_seen = r.prompts()
    assert len(prompts_seen) == 2  # asked again once
    assert OVERLAY not in prompts_seen[0] and BUILTIN_ROMANCE["flirty"] in prompts_seen[0]
    assert guard.HINT in prompts_seen[1]
    assert r.sent.says() == []  # a hit twice: never sent


def test_a_capped_public_line_is_asked_again_and_the_clean_one_goes(store, clock, tmp_path):
    replies = iter(["so horny right now", "nice work out there today"])
    r = _rig(store, clock, tmp_path, CAPPED, lambda model, messages: next(replies))
    _flirt(r)
    assert [s.text for s in r.sent.says()] == ["nice work out there today"]


def test_with_the_cap_off_the_public_prompt_is_as_before(store, clock, tmp_path):
    """The prompt as before the dial; since 2026-11-24 the line goes through the game's public
    channels' hard screen whatever heat says (talk/publicscreen.py): a sexual draft is asked
    again clean, never sent there."""
    from leeroy_mind.talk import publicscreen
    replies = iter(["so horny right now", "nice work out there today"])
    r = _rig(store, clock, tmp_path, ON, lambda model, messages: next(replies))
    _flirt(r)
    assert OVERLAY in r.prompts()[0] and len(r.prompts()) == 2
    assert publicscreen.HINT in r.prompts()[1]
    assert [s.text for s in r.sent.says()] == ["nice work out there today"]


def test_the_guard_never_reads_a_private_line_or_a_turn_heat_does_not_reach(store, clock,
                                                                              tmp_path):
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn, TurnKind
    r = _rig(store, clock, tmp_path, CAPPED, lambda model, messages: "x")
    engine = r.engine
    hr = engine.heat.resolve(engine.personas.get(104), channel="guild")
    public = Turn(kind=TurnKind.FLIRT, speaker=104, channel="guild", addressee=1)
    private = Turn(kind=TurnKind.WHISPER, speaker=104, channel="whisper", to_guid=1, addressee=1)
    assert engine._heat_public(public, TurnMeta("t1", heat=hr), "so horny")
    assert not engine._heat_public(public, TurnMeta("t1", heat=hr), "so happy")
    assert not engine._heat_public(private, TurnMeta("t2", heat=hr), "so horny")
    assert not engine._heat_public(public, TurnMeta("t3"), "so horny")  # no heat on the turn
    none = H.resolve(9, H.Member(stance="none"))
    assert not engine._heat_public(public, TurnMeta("t4", heat=none), "so horny")
    engine.heat = None
    assert not engine._heat_public(public, TurnMeta("t5", heat=hr), "so horny")
