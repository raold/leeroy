import pytest

from leeroy_mind.config import load_config
from leeroy_mind.photos.config import PhotosConfig, load_photos_config


def test_missing_and_off(tmp_path):
    assert load_photos_config(tmp_path / 'mind.toml') == PhotosConfig()


def test_enabled(tmp_path):
    path = tmp_path / 'mind.toml'
    path.write_text('[photos]\nenabled=true\nsend=true')
    load_config(path)
    assert load_photos_config(path) == PhotosConfig(True, True)


@pytest.mark.parametrize('body', ['enabled=1', 'send="true"', 'other=true', 'enabled='])
def test_invalid_off(tmp_path, body, caplog):
    path = tmp_path / 'mind.toml'
    path.write_text('[photos]\n' + body)
    assert load_photos_config(path) == PhotosConfig()
    assert 'photos_config_invalid key=' in caplog.text
