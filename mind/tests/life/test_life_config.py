"""`[life]` in mind.toml (availability, state, wow_vs_life), the availability world dial, the
module pin and LifeService. Invented members only."""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from leeroy_mind import config
from leeroy_mind.life import config as life_config


def test_life_defaults_and_invalid(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(config, "_WARNED", set())
    from leeroy_mind.config import load_config
    assert load_config(tmp_path / "absent.toml").life.availability == 0
    (tmp_path / "m.toml").write_bytes(b"[life]\navailability = 11\nstate = 1\n")
    cfg = load_config(tmp_path / "m.toml")
    assert (cfg.life.availability, cfg.life.state) == (0, False)
    assert "life_availability_invalid" in caplog.text


def test_world_dial_merges(tmp_path):
    from leeroy_mind.tuning.dials import WORLD_DIALS
    assert WORLD_DIALS["availability"].section == "life"
    from leeroy_mind.config import load_config
    from leeroy_mind.tuning.world import file_values, merge
    cfg = load_config(tmp_path / "absent.toml")
    assert file_values(cfg)["availability"] == 0
    merged = merge(cfg, SimpleNamespace(world={"availability": 6}))
    assert merged.life.availability == 6 and merged.life.wow_vs_life == 5


def test_life_values_read_and_an_invalid_one_reads_as_its_default(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(config, "_WARNED", set())
    from leeroy_mind.config import ConfigError, load_config
    from leeroy_mind.nightly.settings import load_life
    p = tmp_path / "m.toml"
    p.write_bytes(b"[life]\navailability = 6\nstate = true\nwow_vs_life = 3\n")
    assert load_config(p).life == config.LifeCfg(availability=6, state=True, wow_vs_life=3)
    for bad in (b"true", b"-1", b"6.0", b'"6"'):
        p.write_bytes(b"[life]\navailability = " + bad + b"\n")
        assert load_config(p).life.availability == 0
    assert caplog.text.count("life_availability_invalid") == 4
    p.write_bytes(b"[life]\navailability = 11\n")
    load_config(p)
    load_config(p)
    assert caplog.text.count("life_availability_invalid") == 5  # once per value
    p.write_bytes(b"[life]\nstate = 2\n")
    assert load_config(p).life.state is False and "life_state_invalid" in caplog.text
    for bad in (b"11", b"true", b'"5"'):
        p.write_bytes(b"[talk]\nchattiness = 7\n[life]\nwow_vs_life = " + bad + b"\n")
        cfg = load_config(p)          # never blocks the rest of mind.toml
        assert cfg.life.wow_vs_life == 5 and cfg.talk.chattiness == 7
        load_config(p)
        with pytest.raises(ConfigError, match="life.wow_vs_life"):
            load_life(p)              # the night still refuses its theme, as before
    assert caplog.text.count("life_wow_vs_life_invalid") == 3   # once per value
    assert "chattiness" not in caplog.text


def test_a_life_that_is_no_table_reads_as_empty(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(config, "_WARNED", set())
    p = tmp_path / "m.toml"
    p.write_bytes(b"life = 3\n[talk]\nchattiness = 7\n")
    cfg = config.load_config(p)
    assert cfg.life == config.LifeCfg() and cfg.talk.chattiness == 7
    config.load_config(p)
    assert caplog.text.count("life_invalid") == 1
    p.write_bytes(b'life = "x6"\n')
    assert config.load_config(p).life == config.LifeCfg()
    assert caplog.text.count("life_invalid") == 2 and "x6" not in caplog.text


def test_the_default_template_says_what_the_code_does(tmp_path):
    import tomllib
    life = tomllib.loads(config.DEFAULT_TOML)["life"]
    assert life == {"day_plans": False, "dungeons": False, "availability": 0, "state": False,
                    "dm_wait_cap_s": 75, "wow_vs_life": 5}
    assert config.load_config(tmp_path / "absent.toml").life == config.LifeCfg()


def test_pin_reads_back_and_refuses_nonsense():
    assert (life_config.availability(), life_config.state_on()) == (0, False)
    life_config.pin(6, True)
    assert (life_config.availability(), life_config.state_on()) == (6, True)
    for bad in (11, -1, True, 2.0, "6", None):
        life_config.pin(bad, 1)
        assert (life_config.availability(), life_config.state_on()) == (0, False)


def test_the_cap_pins_reads_back_and_is_stored(store, tmp_path):
    assert life_config.dm_wait_cap() == 0  # until pinned: no cap, today's behaviour
    life_config.pin(6, False, 75)
    assert life_config.dm_wait_cap() == 75
    life_config.pin(6, False)  # a pin without a cap clears it
    assert life_config.dm_wait_cap() == 0
    for bad in (-1, 3601, True, 7.5, "75", None):
        life_config.pin(6, False, bad)
        assert life_config.dm_wait_cap() == 0
    for raw, value in ((None, 0), ("75", 75), ("0", 0), ("3600", 3600), ("3601", 0), ("x", 0),
                       ("-5", 0)):
        if raw is not None:
            store.set_meta(life_config.CAP_KEY, raw)
        assert life_config.stored_cap(store.conn) == value
    (tmp_path / "mind.toml").write_bytes(b"[life]\ndm_wait_cap_s = 9000\n")
    assert config.load_config(tmp_path / "mind.toml").life.dm_wait_cap_s == 75  # the default


@pytest.mark.parametrize("raw,value", [(None, 0), ("6", 6), ("0", 0), ("10", 10), ("11", 0),
                                       ("-1", 0), ("x6", 0), ("", 0), (" 6", 0)])
def test_the_stored_value_the_console_reads(store, raw, value):
    if raw is not None:
        store.set_meta(life_config.META_KEY, raw)
    assert life_config.stored(store.conn) == value


def _service(store, clock, tmp_path, availability):
    from conftest import seed_test_cast
    from leeroy_mind.config import load_config
    from leeroy_mind.life.service import LifeService
    from leeroy_mind.personas.io import PersonaCache
    seed_test_cast(store)
    cfg = load_config(tmp_path / "absent.toml")
    cfg = replace(cfg, life=replace(cfg.life, availability=availability))
    return LifeService(store=store, clock=clock, personas=PersonaCache(store),
                       world=SimpleNamespace(), cfg=cfg), cfg


def test_service_pins_and_writes_meta_only_when_the_value_changed(store, clock, tmp_path):
    service, cfg = _service(store, clock, tmp_path, 0)
    assert service.availability == 0 and life_config.availability() == 0
    assert store.get_meta(life_config.META_KEY) is None  # 0 and nothing stored: nothing written
    writes = []
    real = store.set_meta
    store.set_meta = lambda k, v: (writes.append((k, v)), real(k, v))
    service.configure(replace(cfg, life=replace(cfg.life, availability=6)))
    assert service.availability == 6 and life_config.availability() == 6
    assert store.get_meta(life_config.META_KEY) == "6"
    service.configure(replace(cfg, life=replace(cfg.life, availability=6)))
    service.configure(replace(cfg, life=replace(cfg.life, availability=0)))
    assert writes == [(life_config.META_KEY, "6"), (life_config.META_KEY, "0")]
    assert life_config.availability() == 0


def test_service_presence_is_none_at_zero_and_her_day_above(store, tmp_path):
    from leeroy_mind.life.day import Block
    from leeroy_mind.testing.fake_clock import FakeClock
    ny = ZoneInfo("America/New_York")
    clock = FakeClock(datetime(2026, 11, 19, 7, 0, tzinfo=ny))   # Thursday 12:00 in the UK
    service, cfg = _service(store, clock, tmp_path, 0)
    assert service.presence(101, medium="discord") is None
    service.configure(replace(cfg, life=replace(cfg.life, availability=6)))
    lp = service.presence(101, medium="discord")
    assert lp is not None and lp.block is Block.WORK          # early shifts: 06:00-14:30
    assert service.presence(101, medium="game", in_his_group=True).attention.name == "FULL"
    assert service.presence(999, medium="discord") is None     # no persona


def test_the_engine_owns_the_service_and_each_config_pins_it(store, clock, tmp_path):
    from engine_rig import make_rig
    rig = make_rig(store, clock, tmp_path)
    assert rig.engine.life.availability == 0 and life_config.availability() == 0
    cfg = rig.engine.cfg
    rig.engine.apply_config(replace(cfg, life=replace(cfg.life, availability=4, state=True)))
    assert rig.engine.life.availability == 4
    assert (life_config.availability(), life_config.state_on()) == (4, True)
    assert store.get_meta(life_config.META_KEY) == "4"


def test_a_failed_meta_write_never_stops_a_reconfigure(store, clock, tmp_path, caplog):
    import sqlite3
    from engine_rig import make_rig
    rig = make_rig(store, clock, tmp_path)

    def locked(key, value):
        raise sqlite3.OperationalError("database is locked")
    store.set_meta = locked
    cfg = rig.engine.cfg
    rig.engine.apply_config(replace(cfg, talk=replace(cfg.talk, chattiness=9),
                                    memory=replace(cfg.memory, depth_days=9),
                                    life=replace(cfg.life, availability=6)))
    assert rig.engine.cfg.talk.chattiness == 9 and rig.engine.budget._c == 9
    assert rig.engine.recall.tau_days == 9
    assert rig.engine.life.availability == 6 and life_config.availability() == 6
    assert "life_meta_failed reason=OperationalError" in caplog.text
    assert "locked" not in caplog.text.split("life_meta_failed", 1)[1].splitlines()[0]
