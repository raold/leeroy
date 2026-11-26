"""Shipped features default on (owner, 2026-11-25): every switch the live mind.toml sets true is
on when its key is missing (no file, an empty file, its table without the key), and a value of
the wrong kind still turns it off. The owner's explicit offs stay off by default."""
from types import SimpleNamespace

import pytest

from leeroy_mind.config import ConfigError, load_config
from shipped_off import SHIPPED, off

# the owner's rulings of 2026-11-25: these stay off when missing
RULED_OFF = (("talk", "coherence_length"), ("talk", "dm_own_time"), ("heat", "public_cap"),
             ("lab", "promote"))


def _reader(table):
    """What the mind reads [table] as, from the mind.toml at a path: an object with the keys."""
    from leeroy_mind.bakeoff.config import load as bakeoff
    from leeroy_mind.banter.config import load as banter
    from leeroy_mind.claims.config import load_claims_config
    from leeroy_mind.culture.config import load_culture_config
    from leeroy_mind.decisions.config import load_decisions_config
    from leeroy_mind.drama.config import load_drama_config
    from leeroy_mind.endpoint import load_control_config
    from leeroy_mind.fingerprint.config import enabled as fingerprint_on
    from leeroy_mind.godmode.config import load_godmode_config
    from leeroy_mind.groups.config import load_groups_config
    from leeroy_mind.guards.config import load_guards_config
    from leeroy_mind.guild.config import load_guild_config
    from leeroy_mind.heat.config import load_heat_config
    from leeroy_mind.lab.config import load as lab
    from leeroy_mind.learn.config import load_learn_config
    from leeroy_mind.memory.callbacks import load_callbacks_config
    from leeroy_mind.nightly.prune import load as prune
    from leeroy_mind.outreach.config import load_outreach_config
    from leeroy_mind.personas.config import load_personas_config
    from leeroy_mind.personas.looks_diversity import enabled as looks_on
    from leeroy_mind.photos.config import load_photos_config
    from leeroy_mind.playstyle.service import enabled as playstyle_on
    from leeroy_mind.raidlead.config import load as raidlead
    from leeroy_mind.raidplay.respond import settings as raidplay
    from leeroy_mind.realm.config import load_realm_config
    from leeroy_mind.selfies.config import load_selfie_config
    from leeroy_mind.server.config import load_server_config
    from leeroy_mind.signals.config import load_signals_config
    from leeroy_mind.training.config import load_training_config
    from leeroy_mind.turing.config import load as turing
    from leeroy_mind.videos.config import load_videos_config
    from leeroy_mind.voice.config import load_voice_config
    from leeroy_mind.voice_notes.config import load_config as voice_notes
    from leeroy_mind.watchdog.config import load as watchdog
    from leeroy_mind.world.config import load as world
    return {
        "bakeoff": bakeoff, "banter": banter, "claims": load_claims_config,
        "control": load_control_config, "culture": load_culture_config,
        "decisions": load_decisions_config, "drama": load_drama_config,
        "godmode": load_godmode_config, "groups": load_groups_config, "guards": load_guards_config,
        "guild": load_guild_config, "heat": load_heat_config, "lab": lab, "learn": load_learn_config,
        "outreach": load_outreach_config, "personas": load_personas_config,
        "photos": load_photos_config, "raidlead": raidlead, "realm": load_realm_config,
        "selfies": load_selfie_config, "server": load_server_config, "signals": load_signals_config,
        "training": load_training_config, "turing": turing, "videos": load_videos_config,
        "voice": load_voice_config, "voice_notes": voice_notes, "watchdog": watchdog, "world": world,
        "prune": prune,
        "talk": lambda p: load_config(p).talk, "life": lambda p: load_config(p).life,
        "romance": lambda p: load_config(p).romance,
        "fingerprint": lambda p: SimpleNamespace(enabled=fingerprint_on(p)),
        "callbacks": lambda p: SimpleNamespace(enabled=load_callbacks_config(p)),
        "looks": lambda p: SimpleNamespace(diversity=looks_on(p.parent)),
        "playstyle": lambda p: SimpleNamespace(enabled=playstyle_on(p)),
        "raidplay": lambda p: SimpleNamespace(respond=raidplay(p)[0]),
    }[table]


def read(path, table, key):
    """The key as the mind reads it; False when the reader refuses the table (ConfigError: the
    feature is then attached as off)."""
    try:
        return getattr(_reader(table)(path), key)
    except ConfigError:
        return False


CASES = [(table, key) for table, keys in SHIPPED.items() for key in keys]


@pytest.mark.parametrize("table,key", CASES)
def test_a_shipped_switch_is_on_when_its_key_is_missing(tmp_path, table, key):
    path = tmp_path / "mind.toml"
    assert read(path, table, key) is True  # no mind.toml at all
    path.write_text("", encoding="utf-8")
    assert read(path, table, key) is True  # nothing in it
    path.write_text(f"[{table}]\n", encoding="utf-8")
    assert read(path, table, key) is True  # its table, without the key


@pytest.mark.parametrize("table,key", CASES)
def test_a_shipped_switch_of_the_wrong_kind_is_off(tmp_path, table, key):
    path = tmp_path / "mind.toml"
    path.write_text(f'[{table}]\n{key} = "yes"\n', encoding="utf-8")
    assert read(path, table, key) is False


@pytest.mark.parametrize("table,key", CASES)
def test_a_shipped_switch_set_false_is_off(tmp_path, table, key):
    path = tmp_path / "mind.toml"
    path.write_text(f"[{table}]\n{key} = false\n", encoding="utf-8")
    assert read(path, table, key) is False


@pytest.mark.parametrize("table,key", RULED_OFF)
def test_the_owners_explicit_offs_stay_off_when_missing(tmp_path, table, key):
    path = tmp_path / "mind.toml"
    path.write_text(f"[{table}]\n", encoding="utf-8")
    assert read(path, table, key) is False


def test_drama_threads_records_when_missing_and_is_off_when_invalid(tmp_path):
    path = tmp_path / "mind.toml"
    assert read(path, "drama", "threads") == "record"
    path.write_text("[drama]\n", encoding="utf-8")
    assert read(path, "drama", "threads") == "record"
    for bad in ('"on"', '"loud"', "1"):
        path.write_text(f"[drama]\nthreads = {bad}\n", encoding="utf-8")
        assert read(path, "drama", "threads") == "off"
    path.write_text('[drama]\nthreads = "off"\n', encoding="utf-8")
    assert read(path, "drama", "threads") == "off"


def test_a_mind_toml_that_does_not_parse_turns_the_shipped_switches_off(tmp_path):
    path = tmp_path / "mind.toml"
    path.write_text("[talk\nshape = true\n", encoding="utf-8")
    on = [(table, key) for table, key in CASES
          if table not in ("talk", "life", "romance") and read(path, table, key)]
    assert on == []


def test_off_turns_every_shipped_switch_off_but_those_a_test_sets(tmp_path):
    path = tmp_path / "mind.toml"
    path.write_text(off('[selfies]\nenabled = true\n[talk]\nchattiness = 2\n[drama]\nthreads = "record"\n'),
                    encoding="utf-8")
    assert [(table, key) for table, key in CASES if read(path, table, key)] == [("selfies", "enabled")]
    assert load_config(path).talk.chattiness == 2 and read(path, "drama", "threads") == "record"
    path.write_text(off(), encoding="utf-8")
    assert read(path, "drama", "threads") == "off"
    path.write_text(off("[voice]\n", keep={"voice"}), encoding="utf-8")
    assert [(table, key) for table, key in CASES if read(path, table, key)] == [
        ("voice", "enabled"), ("voice", "autostart")]
