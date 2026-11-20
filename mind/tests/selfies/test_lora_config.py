"""The mind passes an explicitly selected adapter to rendering workers only."""
import pytest

from leeroy_mind.config import ConfigError
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config, worker_command


def config(tmp_path, extra="", *, backend="flux1-dev"):
    path = tmp_path / "mind.toml"
    adapter = tmp_path / "models with spaces" / "portrait.safetensors"
    path.write_text(f'[selfies]\nbackend = "{backend}"\n'
                    f'lora = "{adapter.as_posix()}"\n{extra}', encoding="utf-8")
    return path, adapter


@pytest.mark.parametrize("strength", [0, 0.8, 1.0, 2.0])
@pytest.mark.parametrize("identity", [False, True])
def test_render_workers_get_the_selected_file_and_strength(tmp_path, strength, identity):
    path, adapter = config(tmp_path, f"lora_scale = {strength}\n")
    cfg = load_selfie_config(path)
    cmd = worker_command(cfg, identity=identity)
    assert cfg.lora == adapter and cfg.lora_scale == strength
    assert cmd[cmd.index("--lora") + 1] == str(adapter)
    assert cmd[cmd.index("--lora-scale") + 1] == f"{strength:g}"
    assert "--nsfw-max" in cmd


def test_lora_defaults_off_and_strength_defaults_to_one(tmp_path):
    assert SelfieCfg().lora is None
    assert "--lora" not in worker_command(SelfieCfg())
    path, _ = config(tmp_path)
    assert load_selfie_config(path).lora_scale == 1.0


def test_embedding_worker_never_loads_the_render_adapter(tmp_path):
    path, _ = config(tmp_path)
    cmd = worker_command(load_selfie_config(path), embedding=True)
    assert "--lora" not in cmd and "--lora-scale" not in cmd


@pytest.mark.parametrize("strength", ["-0.1", "2.1", "nan", "inf", "true", '"1"'])
def test_invalid_strength_is_rejected(tmp_path, strength):
    path, _ = config(tmp_path, f"lora_scale = {strength}\n")
    with pytest.raises(ConfigError, match=r"selfies\.lora_scale"):
        load_selfie_config(path)


@pytest.mark.parametrize("value", ['"relative.safetensors"', '""', "false"])
def test_adapter_requires_an_explicit_absolute_path(tmp_path, value):
    path = tmp_path / "mind.toml"
    path.write_text(f'[selfies]\nbackend = "flux1-dev"\nlora = {value}\n')
    with pytest.raises(ConfigError, match=r"selfies\.lora"):
        load_selfie_config(path)


def test_adapter_requires_safetensors(tmp_path):
    path, _ = config(tmp_path)
    path.write_text(path.read_text().replace(".safetensors", ".pt"))
    with pytest.raises(ConfigError, match=r"selfies\.lora"):
        load_selfie_config(path)


def test_adapter_requires_flux(tmp_path):
    path, _ = config(tmp_path, backend="realvisxl")
    with pytest.raises(ConfigError, match="requires backend"):
        load_selfie_config(path)


def test_strength_without_adapter_is_rejected(tmp_path):
    path = tmp_path / "mind.toml"
    path.write_text('[selfies]\nlora_scale = 1.0\n')
    with pytest.raises(ConfigError, match="requires selfies.lora"):
        load_selfie_config(path)


def test_unknown_table_key_disables_the_adapter_with_the_backend(tmp_path):
    path, _ = config(tmp_path, "typo = true\n")
    cfg = load_selfie_config(path)
    assert cfg.backend == "realvisxl" and cfg.lora is None
    assert "--lora" not in worker_command(cfg)
