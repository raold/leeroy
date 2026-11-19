"""FLUX is explicitly selected, with longer bounded waits only for its render workers."""
from dataclasses import replace

import pytest

from leeroy_mind.config import ConfigError
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config, worker_command
from leeroy_mind.selfies.worker import RENDER_S, START_S, SelfieWorker


def test_flux_config_reaches_every_render_command_but_not_the_embedding_worker(tmp_path):
    path = tmp_path / 'mind.toml'
    path.write_text('[selfies]\nenabled=true\nbackend="flux1-dev"\n')
    cfg = load_selfie_config(path)
    assert cfg.guidance == 3.5
    for identity in (False, True):
        command = worker_command(cfg, identity=identity)
        assert command[command.index('--backend') + 1] == 'flux1-dev'
        worker = SelfieWorker(command)
        assert worker._start_s == worker._render_s == 600
    assert '--backend' not in worker_command(cfg, embedding=True)
    assert '--backend' not in worker_command(replace(cfg, backend='realvisxl'))


def test_flux_guidance_default_preserves_explicit_choices_and_sdxl(tmp_path):
    path = tmp_path / 'mind.toml'
    path.write_text('[selfies]\nbackend="flux1-dev"\nguidance=4.0\n')
    assert load_selfie_config(path).guidance == 4.0
    path.write_text('[selfies]\nbackend="realvisxl"\n')
    assert load_selfie_config(path).guidance == 5.0
    path.write_text('[selfies]\nbackend="flux1-dev"\nunrecognized=true\n')
    assert load_selfie_config(path).guidance == 5.0


def test_default_and_explicit_timeouts_are_preserved():
    worker = SelfieWorker(worker_command(SelfieCfg()))
    assert (worker._start_s, worker._render_s) == (START_S, RENDER_S)
    worker = SelfieWorker(worker_command(SelfieCfg(backend='flux1-dev')), start_s=5, render_s=7)
    assert (worker._start_s, worker._render_s) == (5, 7)


def test_bad_or_unknown_config_never_enables_flux(tmp_path):
    path = tmp_path / 'mind.toml'
    for bad in ('"flux"', 'false', '12'):
        path.write_text(f'[selfies]\nbackend={bad}\n')
        with pytest.raises(ConfigError, match='selfies.backend'):
            load_selfie_config(path)
    path.write_text('[selfies]\nbackend="flux1-dev"\nunrecognized=true\n')
    assert load_selfie_config(path).backend == 'realvisxl'
