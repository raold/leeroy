"""Synthetic installed files only; never native control or general config exposure."""
import json
from pathlib import Path
import re
import time

import pytest

from leeroy_mind.console import native_switches as native, opsview as ops
from leeroy_mind.console.native_switch_keys import LEEROY, PLAYERBOTS


def files(root, *, leeroy="", playerbots=""):
    directory = root / "build/bin/RelWithDebInfo/configs/modules"
    directory.mkdir(parents=True)
    (directory / "mod_leeroy.conf").write_text(leeroy, encoding="utf-8")
    (directory / "playerbots.conf").write_text(playerbots, encoding="utf-8")
    return directory


def read(root):
    return ops.OpsView(root, root / "mind-data")._source("switches", time.monotonic() + 10, {})


def rows(value):
    return {row["key"]: row for source in value["native"]["sources"] for row in source["rows"]}


def test_installed_known_boolean_keys_only_and_never_running_state(tmp_path):
    directory = files(tmp_path, leeroy='Leeroy.MemberDungeon.Enable = "yes"\nLeeroy.KeepCast = 1\n'
        'Leeroy.BridgeKey = "PRIVATE_SENTINEL"\nLeeroy.NotAReal.Enable = "PRIVATE_SENTINEL"\n',
        playerbots='Playerbots.Enabled = true\nPlayerbots.AutoAvoidAoe = off\nPlayerbots.MinRandomBots = 1\n'
        'Playerbots.PlayerbotDatabaseInfo = "PRIVATE_SENTINEL"\nPlayerbots.RandomBotAutologin = "PRIVATE_SENTINEL"\n')
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    value = read(tmp_path)
    result = rows(value)
    assert result["Leeroy.MemberDungeon.Enable"]["configured"] == "on"
    assert result["Playerbots.AutoAvoidAoe"]["configured"] == "off"
    assert result["Playerbots.RandomBotAutologin"]["configured"] == "invalid"
    assert result["Leeroy.DayPlans.Enable"]["configured"] == "unset"
    assert all(r["effective"] == "unknown" for r in result.values())
    output = json.dumps(value)
    assert "PRIVATE_SENTINEL" not in output and "MinRandomBots" not in output and "BridgeKey" not in output
    assert all(s["status"] == "ok" for s in value["native"]["sources"])
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    assert not (tmp_path / "mind-data").exists()


@pytest.mark.parametrize("token,state", [("1", "on"), ('"TRUE"', "on"), ("Y", "on"), ("yes", "on"),
    ("on", "on"), ("0", "off"), ("False", "off"), ("no", "off"), ("N", "off"), ("off", "off"),
    ("2", "invalid"), ("", "invalid"), ("1 # not a core inline comment", "invalid")])
def test_core_boolean_tokens_first_assignment_and_comments(token, state):
    assert native.values(f'# Playerbots.Enabled = 0\n[header]\nPlayerbots.Enabled = {token}\n'
                         'Playerbots.Enabled = 0\n', PLAYERBOTS) == {"Playerbots.Enabled": state}


def test_missing_installed_files_do_not_fall_back_to_tracked_defaults(tmp_path):
    tracked = tmp_path / "config/modules"
    tracked.mkdir(parents=True)
    (tracked / "mod_leeroy.conf").write_text("Leeroy.MemberDungeon.Enable = 1")
    value = read(tmp_path)
    assert value["rows"]  # missing native files do not hide the mind's switches
    assert all(s["status"] == "missing" for s in value["native"]["sources"])
    assert all(r["configured"] == "unknown" for r in rows(value).values())


def test_one_bad_file_keeps_the_other_source_and_mind_rows(tmp_path):
    directory = files(tmp_path, playerbots="Playerbots.Enabled = 1")
    (directory / "mod_leeroy.conf").write_bytes(b"\xff")
    value = read(tmp_path)
    assert [s["status"] for s in value["native"]["sources"]] == ["error", "ok"]
    assert rows(value)["Playerbots.Enabled"]["configured"] == "on" and value["rows"]


def test_linked_parent_is_refused_before_any_read(tmp_path, monkeypatch):
    directory = files(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == directory or original(self))
    def forbidden(*args): pytest.fail("linked private file was read")
    value = native.view(tmp_path, read=forbidden, check=lambda: None)
    assert all(s["status"] == "error" for s in value["sources"])


def test_file_and_deadline_budgets_are_not_bypassed(tmp_path):
    directory = files(tmp_path)
    (directory / "mod_leeroy.conf").write_bytes(b" " * (512*1024 + 1))
    with pytest.raises(ops.Budget): read(tmp_path)
    with pytest.raises(ops.Budget):
        ops.OpsView(tmp_path, tmp_path)._source("switches", time.monotonic() - 1, {})


def test_all_literal_leeroy_boolean_switches_are_catalogued():
    root = Path(__file__).resolve().parents[3]
    found = {key for path in (root / "modules/mod-leeroy/src").rglob("*.cpp")
             for key in re.findall(r'GetOption<bool>\s*\(\s*"(Leeroy\.[A-Za-z0-9_.]+)"', path.read_text())}
    assert found and found == LEEROY
    assert {"Playerbots.Enabled", "Playerbots.AutoAvoidAoe", "Playerbots.RaidVehicleSupport",
            "Playerbots.RaidVehicleDiagnostics", "Playerbots.LichKingMechanics",
            "Playerbots.YoggBrainSupport"} <= PLAYERBOTS
    assert "Playerbots.MinRandomBots" not in PLAYERBOTS
