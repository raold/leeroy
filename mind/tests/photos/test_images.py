from pathlib import Path

import pytest

from leeroy_mind.photos import images


def jpeg(w=32, h=24, marker=0xc0):
    return (b'\xff\xd8\xff\xe0\x00\x04ab\xff' + bytes([marker]) + b'\x00\x0b\x08'
            + h.to_bytes(2, 'big') + w.to_bytes(2, 'big') + b'\x01\x01\x11\x00\xff\xd9')


@pytest.mark.parametrize('marker', [0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf])
def test_baseline_progressive_and_other_sof(marker):
    assert images.check_jpeg(jpeg(marker=marker)) == (32, 24)


@pytest.mark.parametrize('data,code', [(b'PNG', 'not_jpeg'), (b'\xff\xd8\xff', 'not_jpeg'),
    (jpeg(8), 'bad_size'), (jpeg(9000), 'bad_size'),
    (jpeg() + b'0' * images.MAX_IMAGE_BYTES, 'too_big'),
    (b'\xff\xd8\xff\xe0\x00\x00', 'not_jpeg')])
def test_refusals(data, code):
    with pytest.raises(images.ImageRefused, match=code):
        images.check_jpeg(data)


def test_stage_and_adopt_preserve_bytes_and_refuse_reuse(tmp_path):
    data = jpeg()
    token = images.stage(tmp_path, data)
    assert images.TOKEN_RE.fullmatch(token)
    staged = tmp_path / images.INCOMING / f'{token}.jpg'
    assert staged.read_bytes() == data
    relative, w, h, size = images.adopt(tmp_path, token, 1)
    assert (w, h, size) == (32, 24, len(data))
    assert (tmp_path / relative).read_bytes() == data
    assert staged.with_suffix('.adopted').read_bytes() == data
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(images.ImageRefused, match='reused'):
        images.adopt(tmp_path, token, 2)
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert before == after


def test_stage_collision_never_overwrites(tmp_path, monkeypatch):
    tokens = iter(['a'*32, 'a'*32, 'b'*32])
    monkeypatch.setattr(images.secrets, 'token_hex', lambda _: next(tokens))
    assert images.stage(tmp_path, jpeg()) == 'a'*32
    assert images.stage(tmp_path, jpeg(64)) == 'b'*32
    assert (tmp_path / images.INCOMING / ('a'*32 + '.jpg')).read_bytes() == jpeg()


def test_bad_tokens_and_missing_files(tmp_path):
    for token in ('../bad', 'a'*31, 'A'*32, '', 'a'*33):
        with pytest.raises(images.ImageRefused, match='bad_token'):
            images.adopt(tmp_path, token, 1)
    with pytest.raises(images.ImageRefused, match='missing'):
        images.adopt(tmp_path, 'a'*32, 1)


@pytest.mark.parametrize('where', ['file', 'incoming', 'images'])
def test_adopt_refuses_links(tmp_path, where):
    data = tmp_path / 'data'
    data.mkdir()
    token = images.stage(data, jpeg())
    external = tmp_path / 'external'
    external.mkdir()
    if where == 'file':
        path = data / images.INCOMING / (token + '.jpg')
        saved = external / 'photo.jpg'
        path.rename(saved)
        try:
            path.symlink_to(saved)
        except OSError:
            pytest.skip('symlinks unavailable')
    else:
        path = data / (images.INCOMING if where == 'incoming' else images.IMAGES)
        if path.exists():
            path.rename(external / 'saved')
        try:
            path.symlink_to(external, target_is_directory=True)
        except OSError:
            pytest.skip('symlinks unavailable')
    with pytest.raises(images.ImageRefused, match='link'):
        images.adopt(data, token, 1)


def test_existing_adopted_target_is_never_overwritten(tmp_path):
    token = images.stage(tmp_path, jpeg())
    (tmp_path / images.IMAGES).mkdir()
    target = tmp_path / images.IMAGES / '1.jpg'
    target.write_bytes(b'keep')
    relative, *_ = images.adopt(tmp_path, token, 1)
    assert (tmp_path / relative).read_bytes() == jpeg()
    assert target.read_bytes() == b'keep'
