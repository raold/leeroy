"""[selfies] in mind.toml and the worker's command line (P2 Task 7)."""
import os
from pathlib import Path

import pytest

import leeroy_mind
from leeroy_mind.config import ConfigError, load_config
from leeroy_mind.selfies.config import (SelfieCfg, load_selfie_config, worker_command,
                                        worker_data, worker_env, worker_python)

CHECKOUT = Path(leeroy_mind.__file__).resolve().parents[2]
VENV = CHECKOUT / "selfie" / ".venv"


def test_selfies_are_off_by_default_and_paths_follow_the_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("LEEROY_SELFIE_DATA", raising=False)
    cfg = load_selfie_config(tmp_path / "missing.toml")
    assert cfg == SelfieCfg() and cfg.enabled is False
    (tmp_path / "mind.toml").write_text("[talk]\nchattiness = 4\n")
    assert load_selfie_config(tmp_path / "mind.toml") == SelfieCfg()
    assert (cfg.nsfw_max, cfg.steps, cfg.guidance, cfg.size, cfg.min_free_gib) == (
        0.2, 30, 5.0, 1024, 11.5)
    assert worker_python(cfg) == (VENV / "Scripts" / "python.exe" if os.name == "nt"
                                  else VENV / "bin" / "python")
    assert worker_data(cfg) == CHECKOUT / "selfie-data"
    monkeypatch.setenv("LEEROY_SELFIE_DATA", str(tmp_path / "elsewhere"))
    assert worker_data(cfg) == tmp_path / "elsewhere"


def test_a_table_sets_every_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LEEROY_SELFIE_DATA", str(tmp_path / "ignored"))
    python, data = tmp_path / "py" / "python.exe", tmp_path / "sd"
    (tmp_path / "mind.toml").write_text(
        "[selfies]\nenabled = true\n"
        f'python = "{python.as_posix()}"\ndata = "{data.as_posix()}"\n'
        "nsfw_max = 0.1\nsteps = 25\nguidance = 6\nsize = 896\nmin_free_gib = 12\n",
        encoding="utf-8")
    cfg = load_selfie_config(tmp_path / "mind.toml")
    assert cfg == SelfieCfg(enabled=True, python=python, data=data, nsfw_max=0.1, steps=25,
                            guidance=6.0, size=896, min_free_gib=12.0)
    assert type(cfg.guidance) is float and type(cfg.min_free_gib) is float
    assert worker_python(cfg) == python and worker_data(cfg) == data


@pytest.mark.parametrize("line, key", [
    ("nsfw_max = 2", "nsfw_max"), ("nsfw_max = 0", "nsfw_max"), ("steps = 9", "steps"),
    ("steps = 30.0", "steps"), ("guidance = 13", "guidance"), ("size = 1000", "size"),
    ("min_free_gib = 3", "min_free_gib"), ("min_free_gib = 24", "min_free_gib"),
    ("enabled = 1", "enabled"), ("python = 'python.exe'", "python"), ("data = ''", "data"),
])
def test_a_bad_value_names_the_key(tmp_path, line, key):
    (tmp_path / "mind.toml").write_text(f"[selfies]\n{line}\n")
    with pytest.raises(ConfigError, match=f"^selfies\\.{key}:"):
        load_selfie_config(tmp_path / "mind.toml")


def test_an_unknown_key_warns_and_a_bad_file_is_a_config_error(tmp_path, caplog):
    (tmp_path / "mind.toml").write_text("[selfies]\nstepz = 20\n")
    assert load_selfie_config(tmp_path / "mind.toml") == SelfieCfg()
    assert "selfies.stepz" in caplog.text
    (tmp_path / "mind.toml").write_text("[selfies\n")
    with pytest.raises(ConfigError):
        load_selfie_config(tmp_path / "mind.toml")
    (tmp_path / "mind.toml").write_text("selfies = 3\n")
    with pytest.raises(ConfigError, match="^selfies: expected a table"):
        load_selfie_config(tmp_path / "mind.toml")


def test_the_mind_config_leaves_the_selfies_table_alone(tmp_path, caplog):
    (tmp_path / "mind.toml").write_text("[selfies]\nenabled = true\n")
    load_config(tmp_path / "mind.toml")
    assert "selfies" not in caplog.text


def test_the_worker_command_runs_the_selfie_package_isolated(tmp_path):
    cfg = SelfieCfg(python=tmp_path / "python.exe", data=tmp_path / "sd", min_free_gib=12.0)
    cmd = worker_command(cfg)
    assert worker_command(SelfieCfg())[1:7] == ["-I", "-X", "utf8", "-m", "leeroy_selfie",
                                                "worker"]
    assert cmd == [str(tmp_path / "python.exe"), "-I", "-X", "utf8", "-m", "leeroy_selfie",
                   "worker", "--data", str(tmp_path / "sd"), "--min-free-gib", "12"]
    assert worker_command(SelfieCfg(min_free_gib=11.5))[-1] == "11.5"


def test_the_worker_env_is_this_one_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMETHING_OF_OURS", "kept")
    before = dict(os.environ)
    env = worker_env(SelfieCfg(data=tmp_path / "sd"))
    assert env["SOMETHING_OF_OURS"] == "kept" and env["PATH"] == os.environ["PATH"]
    assert {k: env[k] for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
                                "HF_HUB_DISABLE_TELEMETRY", "LEEROY_SELFIE_DATA")} == {
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
        "LEEROY_SELFIE_DATA": str(tmp_path / "sd")}
    assert dict(os.environ) == before  # a copy: the mind's own environment is untouched
