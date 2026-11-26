"""The switchboard lists every boolean switch the config loaders define, and judges each table
by its loader's rules (a table without a rule reads unknown, never invalid)."""
import dataclasses
import importlib
import tempfile
import time
import tomllib
from pathlib import Path

import pytest

import leeroy_mind
from leeroy_mind import config as core
from leeroy_mind.console import opsview as ops
from leeroy_mind.console import switches as S
from leeroy_mind.console.switchvalidation import valid_table

PACKAGE = Path(leeroy_mind.__file__).parent
# Dataclasses in a */config.py that are not a mind.toml table.
NOT_TABLES = {("bakeoff", "Candidate"), ("guild", "DkpConfig"), ("guild", "GuildConfig"),
              ("realm", "RealmLimits")}


def by_hand() -> dict:
    """Tables whose loader is a function, not a dataclass or a rule table: their boolean keys, as
    each reader reads an empty mind.toml (every key missing)."""
    from leeroy_mind.fingerprint.config import enabled as fingerprint
    from leeroy_mind.memory.callbacks import load_callbacks_config
    from leeroy_mind.nightly.prune import load as prune
    from leeroy_mind.personas.looks_diversity import enabled as looks
    from leeroy_mind.playstyle.service import enabled as playstyle
    from leeroy_mind.raidplay.respond import settings as raidplay
    from leeroy_mind.tuning.nudge_memories import enabled as nudge_memories
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "mind.toml"
        path.write_text("", encoding="utf-8")
        return {"fingerprint": {"enabled": fingerprint(path)},
                "callbacks": {"enabled": load_callbacks_config(path)},
                "looks": {"diversity": looks(root)}, "playstyle": {"enabled": playstyle(path)},
                "raidplay": {"respond": raidplay(path)[0]}, "prune": {"enabled": prune(path).enabled},
                "tuning": {"nudge_memories": nudge_memories(root)}}


def deadline():
    return time.monotonic() + 10


def _is_bool_rule(check):
    try:
        return check(True) and check(False) and not check(2) and not check("x")
    except Exception:
        return False


def loader_booleans():
    """{table: {key: default}} for every boolean key a loader defines."""
    out = {}
    for path in sorted(PACKAGE.glob("*/config.py")):
        package = path.parent.name
        module = importlib.import_module(f"leeroy_mind.{package}.config")
        for name, cls in vars(module).items():
            if (not isinstance(cls, type) or not dataclasses.is_dataclass(cls)
                    or cls.__module__ != module.__name__ or (package, name) in NOT_TABLES):
                continue
            assert package not in out, f"two config dataclasses in {package}/config.py: name one in NOT_TABLES"
            out[package] = {f.name: f.default for f in dataclasses.fields(cls) if type(f.default) is bool}
    defaults = tomllib.loads(core.DEFAULT_TOML)
    for table in ("talk", "romance", "life"):
        out[table] = {k: defaults[table][k] for k, (check, _) in core._RULES[table].items() if _is_bool_rule(check)}
    from leeroy_mind.endpoint import ControlCfg, _RULES as control
    out["control"] = {k: getattr(ControlCfg(), k) for k, (check, _) in control.items() if _is_bool_rule(check)}
    for table, keys in by_hand().items():
        assert table not in out
        out[table] = dict(keys)
    return out


def test_every_boolean_key_of_every_loader_is_a_row_or_excluded():
    rows = {(r[0], r[1]): r for r in S.SWITCHES}
    assert len(rows) == len(S.SWITCHES), "a switch is listed twice"
    missing, wrong_default = [], []
    for table, keys in loader_booleans().items():
        for key, value in keys.items():
            if (table, key) in S.EXCLUDED:
                continue
            row = rows.get((table, key))
            if row is None:
                missing.append(f"{table}.{key}")
            elif S.is_number(row) or S.default(row) is not value:
                wrong_default.append(f"{table}.{key}")
    assert not missing, f"add these to console/switches.py SWITCHES (or EXCLUDED, with a reason): {missing}"
    assert not wrong_default, f"the row's default differs from the loader's: {wrong_default}"


def test_every_mind_toml_table_is_accounted_for():
    known = loader_booleans()
    sections = set(core.EXTENSION_SECTIONS) | set(core._RULES)
    unaccounted = sorted(sections - set(known) - S.NO_SWITCHES)
    assert not unaccounted, f"new mind.toml tables the switchboard does not know: {unaccounted}"
    assert not S.NO_SWITCHES & {t for t, keys in known.items() if keys}


def test_rows_and_exclusions_name_real_keys():
    known = loader_booleans()
    for row in S.SWITCHES:
        if not S.is_number(row):
            assert row[1] in known[row[0]], row[:2]
    for table, key in S.EXCLUDED:
        assert key in known[table]
    numbers = [r[:2] for r in S.SWITCHES if S.is_number(r)]
    assert numbers == [("talk", "dm_mirror"), ("life", "availability")]


def test_every_listed_table_has_a_validation_rule_and_its_defaults_are_valid():
    for table in {r[0] for r in S.SWITCHES}:
        assert valid_table(table, {}, b"") is True, table


def test_a_table_without_a_rule_is_unknown_not_invalid():
    assert valid_table("not_a_table", {"enabled": True}, b"") is None


LIVE_DECISIONS = "[server]\nenabled=true\n[decisions]\nenabled=true\nbank_items_per_day=10\nloot_poll_days=14\n"


def test_decisions_reads_on_with_its_live_keys(tmp_path):
    (tmp_path / "mind.toml").write_text(LIVE_DECISIONS)
    rows = ops.switches_view(tmp_path, deadline(), mind_started=None)["rows"]
    row = next(r for r in rows if r["table"] == "decisions")
    assert row["state"] == "on" and row["unmet"] == []


@pytest.mark.parametrize("text", ["[decisions]\nenabled=1", "[decisions]\nloot_poll_days=2",
                                  "[decisions]\nbank_items_per_day=51", "[decisions]\nsurprise=true",
                                  "[decisions]\nbank_restock_per_week=true"])
def test_decisions_invalid_as_its_loader_says(tmp_path, text):
    from leeroy_mind.decisions.config import OFF, load_decisions_config
    (tmp_path / "mind.toml").write_text("[server]\nenabled=true\n" + text.replace("[decisions]", "[decisions]\nenabled=true", 1)
                                        if "enabled" not in text else text)
    assert load_decisions_config(tmp_path / "mind.toml") == OFF
    rows = ops.switches_view(tmp_path, deadline(), mind_started=None)["rows"]
    assert next(r for r in rows if r["table"] == "decisions")["state"] == "invalid"


@pytest.mark.parametrize("text,invalid", [
    ("[culture]\nenabled=true\nmemes_per_week=15", "culture"), ("[culture]\nbots=1", "culture"),
    ("[groups]\nmax_open=13", "groups"), ("[groups]\nenabled=true\nextra=1", "groups"),
    ("[voice_notes]\nshare=0.6", "voice_notes"), ("[voice_notes]\nmax_words=4", "voice_notes"),
    ("[turing]\nwindows=61", "turing"), ("[turing]\ndays=0", "turing"),
    ("[outreach]\nenabled=1", "outreach"), ("[godmode]\nenabled=true\nx=1", "godmode"),
    ("[voice]\nport=0", "voice"), ("[prune]\nrows_per_night=0", "prune"), ("[tuning]\nnudge_memories=1", "tuning")])
def test_new_tables_are_invalid_as_their_loaders_say(tmp_path, text, invalid):
    (tmp_path / "mind.toml").write_text(text)
    rows = ops.switches_view(tmp_path, deadline(), mind_started=None)["rows"]
    assert all(r["state"] == "invalid" for r in rows if r["table"] == invalid)


def test_live_shaped_config_has_no_invalid_or_unknown_row(tmp_path):
    (tmp_path / "mind.toml").write_text(
        "[talk]\naware=true\ntexture=true\nthink_private=true\ndm_mirror=5\nshape=true\n"
        "[life]\navailability=6\nstate=true\n[server]\nenabled=true\n"
        "[learn]\nenabled=true\napply=true\nauto_lessons=false\ncycle=true\n"
        "[voice]\nenabled=true\nautostart=true\n[voice_notes]\nenabled=true\nshare=0.15\n"
        "[groups]\nenabled=true\nmax_open=6\nmember_starts_per_week=1\n"
        "[culture]\nenabled=true\ncalendar=true\nloot=true\nbots=true\nemoji=true\nmemes=true\nmemes_per_week=3\n"
        "[outreach]\nenabled=true\n[godmode]\nenabled=true\n[turing]\nenabled=true\nwindows=8\n"
        "[lab]\nenabled=true\n[bakeoff]\ncapture=true\n[romance]\nrealism=true\n"
        "[selfies]\nenabled=true\nvariety=true\nvaried_sets=true\nidentity_sets=true\n"
        "[photos]\nenabled=true\nsend=true\nrequests=true\nscene_from_words=true\nunsolicited=true\n"
        "[training]\nenabled=true\nlab=false\nplaybooks=true\n"
        "[guards]\nenabled=true\nsemantic_enabled=true\nphrase_cooldown=true\n"
        + LIVE_DECISIONS.split("[server]\nenabled=true\n")[1])
    rows = ops.switches_view(tmp_path, deadline(), mind_started=None)["rows"]
    by = {(r["table"], r["key"]): r for r in rows}
    assert not [k for k, r in by.items() if r["state"] in ("invalid", "unknown")]
    on = [("learn", "apply"), ("talk", "shape"), ("talk", "texture"), ("talk", "think_private"),
          ("voice", "enabled"), ("voice", "autostart"), ("voice_notes", "enabled"), ("groups", "enabled"),
          ("culture", "enabled"), ("culture", "memes"), ("outreach", "enabled"), ("godmode", "enabled"),
          ("turing", "enabled"), ("lab", "enabled"), ("bakeoff", "capture"), ("romance", "realism"),
          ("life", "state"), ("selfies", "variety"), ("selfies", "varied_sets"), ("selfies", "identity_sets"),
          ("photos", "scene_from_words"), ("photos", "unsolicited"), ("guards", "semantic_enabled"),
          ("guards", "phrase_cooldown"), ("decisions", "enabled")]
    assert [k for k in on if by[k]["state"] != "on"] == []
    assert by["training", "lab"]["state"] == "off" and by["learn", "auto_lessons"]["state"] == "off"
    assert by["life", "availability"]["state"] == "number" and by["life", "availability"]["value"] == 6
    assert by["talk", "dm_mirror"]["value"] == 5


def test_defaults_show_when_a_key_is_absent(tmp_path):
    rows = ops.switches_view(tmp_path, deadline(), mind_started=None)["rows"]
    by = {(r["table"], r["key"]): r for r in rows}
    assert by["guards", "phrase_cooldown"]["state"] == "on" and by["talk", "think_private"]["state"] == "on"
    assert by["talk", "dm_mirror"]["value"] == 5 and by["life", "availability"]["value"] == 0
    assert by["training", "lab"]["state"] == "on" and by["decisions", "enabled"]["state"] == "on"
    # shipped features default on (2026-11-25); the owner's explicit offs stay off
    assert all(by[k]["state"] == "on" for k in by if S.default(next(r for r in S.SWITCHES if r[:2] == k)) is True)
    assert [k for k in (("talk", "coherence_length"), ("talk", "dm_own_time"), ("heat", "public_cap"),
                        ("lab", "promote")) if by[k]["state"] != "off"] == []
    assert all("blocklist" in r for r in rows if (r["table"], r["key"]) == ("realm", "enabled"))
    assert sum("blocklist" in r for r in rows) == 1


def test_life_is_judged_a_field_at_a_time_as_its_loader_falls_back(tmp_path):
    from leeroy_mind.config import load_config

    def by():
        return {(r["table"], r["key"]): r for r in ops.switches_view(tmp_path, deadline(), mind_started=None)["rows"]}
    (tmp_path / "mind.toml").write_text('[life]\navailability=6\nstate=true\nwow_vs_life="bad"\n')
    life = load_config(tmp_path / "mind.toml").life
    assert (life.availability, life.state) == (6, True)
    assert by()["life", "state"]["state"] == "on"
    assert by()["life", "availability"]["state"] == "number" and by()["life", "availability"]["value"] == 6
    (tmp_path / "mind.toml").write_text("[life]\navailability=6\nstate=1\n")
    assert load_config(tmp_path / "mind.toml").life.availability == 6
    assert by()["life", "state"]["state"] == "invalid"
    assert by()["life", "availability"]["state"] == "number" and by()["life", "availability"]["value"] == 6
    (tmp_path / "mind.toml").write_text("[life]\navailability=11\nstate=true\n")
    assert by()["life", "availability"]["state"] == "invalid" and by()["life", "state"]["state"] == "on"
    (tmp_path / "mind.toml").write_text("life = 3\n")
    assert by()["life", "availability"]["state"] == "invalid" and by()["life", "state"]["state"] == "invalid"
