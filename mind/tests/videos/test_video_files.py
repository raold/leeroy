"""His video uploads (videos/files.py): staging, the one ffmpeg read, adoption and the facts in
meta. ffmpeg is faked except in the last test, which runs the real one on clips it makes itself."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from leeroy_mind.photos.images import IMAGES, TOKEN_RE
from leeroy_mind.videos import files
from leeroy_mind.videos.config import VideosConfig, load_videos_config
from photos.test_images import jpeg

MP4 = b'\x00\x00\x00\x18ftypmp42' + bytes(range(64))
WEBM = b'\x1a\x45\xdf\xa3' + bytes(range(64))
TOOLS = ('ffmpeg', 'ffprobe')


class FakeTools:
    """ffprobe and ffmpeg as files._run would run them: what each was asked, and set answers."""
    def __init__(self, *, ms=4000, w=320, h=180, picture=True, probe_rc=0, stills=(True, True)):
        self.calls, self.ms, self.w, self.h = [], ms, w, h
        self.picture, self.probe_rc, self.stills = picture, probe_rc, list(stills)

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[0] == 'ffprobe':
            streams = [{'codec_type': 'video' if self.picture else 'audio'}]
            form = {'duration': f'{self.ms / 1000:.6f}'} if self.ms else {'duration': 'N/A'}
            return subprocess.CompletedProcess(args, self.probe_rc, json.dumps(
                {'streams': streams, 'format': form}).encode(), b'')
        ok = self.stills.pop(0) if self.stills else False
        n = args.count('-i')
        sheet = jpeg(n * self.w + files.GAP * (n - 1), self.h)
        return subprocess.CompletedProcess(args, 0 if ok else 1, sheet if ok else b'', b'')


@pytest.fixture
def tools(monkeypatch):
    fake = FakeTools()
    monkeypatch.setattr(files, '_run', fake)
    return fake


def inventory(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('data,mime,want', [
    (MP4, 'video/mp4', ('.mp4', 'mov')), (MP4, 'video/quicktime', ('.mov', 'mov')),
    (b'\x00\x00\x00\x08wide' + MP4, 'video/quicktime', ('.mov', 'mov')),
    (WEBM, 'video/webm', ('.webm', 'matroska'))])
def test_sniff_takes_each_declared_family(data, mime, want):
    assert files.sniff(data, mime) == want


@pytest.mark.parametrize('data,mime,code', [
    (MP4, 'video/avi', 'not_video'), (WEBM, 'video/mp4', 'not_video'), (MP4, 'video/webm', 'not_video'),
    (b'\x89PNG\r\n\x1a\n' + bytes(16), 'video/mp4', 'not_video'), (b'', 'video/mp4', 'not_video'),
    (MP4 + bytes(files.MAX_VIDEO_BYTES), 'video/mp4', 'too_big')])
def test_sniff_refuses_what_is_not_its_declared_video(data, mime, code):
    with pytest.raises(files.VideoRefused, match=code):
        files.sniff(data, mime)


def test_stage_reads_once_and_adopt_moves_the_video_and_copies_its_stills(tmp_path, tools):
    token, facts = files.stage(tmp_path, MP4, 'video/mp4', TOOLS)
    assert TOKEN_RE.fullmatch(token)
    assert facts == files.Facts('video/mp4', len(MP4), 4000, 320, 180, 3)
    incoming = tmp_path / files.INCOMING
    assert (incoming / f'{token}.mp4').read_bytes() == MP4
    assert json.loads((incoming / f'{token}.json').read_text()) == facts.as_dict()
    assert (incoming / f'{token}.jpg').read_bytes() == jpeg(3 * 320 + 2 * files.GAP, 180)
    # stills at 10%, 50% and 85% of the length, each input forced to a local file
    probe, stills = tools.calls
    assert [stills[i + 1] for i, a in enumerate(stills) if a == '-ss'] == ['0.400', '2.000', '3.400']
    staged = files.staged_count(tmp_path, _today())
    assert staged == 1

    relative, w, h, size, meta = files.adopt(tmp_path, token, 7)
    assert relative == (IMAGES / '7.jpg').as_posix() and (w, h) == (3 * 320 + 2 * files.GAP, 180)
    assert (tmp_path / relative).read_bytes() == (incoming / f'{token}.jpg').read_bytes()
    assert meta == {**facts.as_dict(), 'path': 'discord/videos/7.mp4'} and size == len((tmp_path / relative).read_bytes())
    assert (tmp_path / 'discord/videos/7.mp4').read_bytes() == MP4
    assert not (incoming / f'{token}.mp4').exists()  # moved, not copied: 50 MiB is kept once
    assert (incoming / f'{token}.claimed').read_text() == '7'
    assert files.staged_count(tmp_path, _today()) == 1  # the facts stay, so the day's count does
    before = inventory(tmp_path)
    with pytest.raises(files.VideoRefused, match='reused'):
        files.adopt(tmp_path, token, 8)
    assert inventory(tmp_path) == before


def _today():
    import time
    from leeroy_mind import clock
    return clock.local_at(time.time()).date().isoformat()


def test_inputs_are_local_files_and_the_stills_come_back_on_a_pipe(tmp_path, tools):
    files.stage(tmp_path, WEBM, 'video/webm', TOOLS)
    for call in tools.calls:
        inputs = [i for i, a in enumerate(call) if a == '-i']
        assert inputs
        for i in inputs:
            assert call[i - 4:i] == ['-protocol_whitelist', 'file', '-f', 'matroska']
            assert call[i + 1].startswith('file:') and Path(call[i + 1][5:]).is_relative_to(tmp_path)
        assert not any('://' in a for a in call)
    assert tools.calls[1][-5:] == ['-f', 'image2pipe', '-c:v', 'mjpeg', 'pipe:1']


@pytest.mark.parametrize('ms,times', [(None, ['0.000']), (1500, ['0.750']), (2000, ['0.200', '1.000', '1.700'])])
def test_short_or_unknown_lengths_take_one_still(tmp_path, monkeypatch, ms, times):
    fake = FakeTools(ms=ms)
    monkeypatch.setattr(files, '_run', fake)
    token, facts = files.stage(tmp_path, MP4, 'video/mp4', TOOLS)
    stills = fake.calls[1]
    assert [stills[i + 1] for i, a in enumerate(stills) if a == '-ss'] == times
    assert facts.ms == ms and facts.frames == len(times)


def test_a_failed_spread_falls_back_to_one_still_from_the_start(tmp_path, monkeypatch):
    fake = FakeTools(stills=(False, True))
    monkeypatch.setattr(files, '_run', fake)
    _, facts = files.stage(tmp_path, MP4, 'video/mp4', TOOLS)
    assert facts.frames == 1 and fake.calls[2].count('-i') == 1 and '0.000' in fake.calls[2]


@pytest.mark.parametrize('fake,code', [
    (FakeTools(probe_rc=1), 'unreadable'), (FakeTools(picture=False), 'no_picture'),
    (FakeTools(stills=(False, False)), 'unreadable'), (FakeTools(w=4), 'unreadable')])
def test_an_unreadable_video_is_kept_but_never_counted_or_adoptable(tmp_path, monkeypatch, fake, code):
    monkeypatch.setattr(files, '_run', fake)
    with pytest.raises(files.VideoRefused, match=code):
        files.stage(tmp_path, MP4, 'video/mp4', TOOLS)
    kept = list((tmp_path / files.INCOMING).iterdir())
    assert [p.suffix for p in kept] == ['.mp4'] and kept[0].read_bytes() == MP4
    assert files.staged_count(tmp_path, _today()) == 0
    with pytest.raises(files.VideoRefused, match='missing'):
        files.adopt(tmp_path, kept[0].stem, 1)


def test_no_tools_means_no_staging(tmp_path):
    with pytest.raises(files.VideoRefused, match='tools'):
        files.stage(tmp_path, MP4, 'video/mp4', None)
    assert not (tmp_path / 'discord').exists()


def test_a_timeout_reads_as_unreadable(tmp_path, monkeypatch):
    def slow(args, timeout):
        raise subprocess.TimeoutExpired(args, timeout)
    monkeypatch.setattr(files, '_run', slow)
    with pytest.raises(files.VideoRefused, match='unreadable'):
        files.stage(tmp_path, MP4, 'video/mp4', TOOLS)


@pytest.mark.parametrize('tamper', ['token', 'facts', 'size', 'stills', 'id'])
def test_adopt_refuses_what_staging_did_not_leave(tmp_path, tools, tamper):
    token, _ = files.stage(tmp_path, MP4, 'video/mp4', TOOLS)
    incoming = tmp_path / files.INCOMING
    mid = 3
    if tamper == 'token':
        token = '../' + token[3:]
    elif tamper == 'facts':
        (incoming / f'{token}.json').write_text('{"mime": "video/mp4"}')
    elif tamper == 'size':
        (incoming / f'{token}.mp4').write_bytes(MP4 + b'x')
    elif tamper == 'stills':
        (incoming / f'{token}.jpg').write_bytes(b'not a jpeg')
    else:
        mid = 0
    before = inventory(tmp_path)
    with pytest.raises(files.VideoRefused, match='bad_token' if tamper in ('token', 'id') else 'missing'):
        files.adopt(tmp_path, token, mid)
    assert inventory(tmp_path) == before


def test_adopt_never_overwrites_what_a_rolled_back_post_left(tmp_path, tools):
    token, _ = files.stage(tmp_path, MP4, 'video/mp4', TOOLS)
    (tmp_path / 'discord/videos').mkdir(parents=True)
    (tmp_path / IMAGES).mkdir()
    (tmp_path / 'discord/videos/5.mp4').write_bytes(b'left')
    (tmp_path / IMAGES / '5.jpg').write_bytes(b'left too')
    relative, *_, meta = files.adopt(tmp_path, token, 5)
    assert relative == (IMAGES / f'5-{token}.jpg').as_posix() and meta['path'] == f'discord/videos/5-{token}.mp4'
    assert (tmp_path / 'discord/videos/5.mp4').read_bytes() == b'left'
    assert (tmp_path / IMAGES / '5.jpg').read_bytes() == b'left too'
    assert (tmp_path / meta['path']).read_bytes() == MP4


def test_facts_in_meta_are_read_strictly(store):
    good = {'mime': 'video/mp4', 'bytes': 9, 'ms': 1200, 'w': 32, 'h': 24, 'frames': 1,
            'path': 'discord/videos/4.mp4'}
    rows = {4: good, 5: good | {'path': '../mind.db'}, 6: good | {'path': 'discord/videos/6.webm'},
            7: good | {'frames': 2}, 8: good | {'ms': 'long'}, 9: good | {'mime': 'video/avi'},
            10: good | {'path': 'discord/videos/10-' + 'a' * 32 + '.mp4', 'ms': None}}
    for mid, value in rows.items():
        store.set_meta(files.META_KEY.format(mid), json.dumps(value))
    store.set_meta(files.META_KEY.format(11), 'not json')
    found = files.by_messages(store.conn, [*rows, 11, 12, 'x', -1])
    assert sorted(found) == [4, 10]
    assert found[4] == good and found[10]['ms'] is None


@pytest.mark.parametrize('ms,words', [(None, ''), (0, ''), (400, '1 second'), (12_400, '12 seconds'),
                                      (89_000, '89 seconds'), (95_000, 'about 2 minutes'),
                                      (300_000, 'about 5 minutes')])
def test_duration_words(ms, words):
    assert files.duration_words(ms) == words


@pytest.mark.parametrize('text,want', [
    ('', VideosConfig()), ('[videos]\nenabled = true', VideosConfig(True)),
    ('[videos]\nenabled = false', VideosConfig()), ('[videos]\nenabled = 1', VideosConfig()),
    ('[videos]\nenabled = true\nsize = 5', VideosConfig()), ('videos = 1', VideosConfig()),
    ('[videos\n', VideosConfig())])
def test_the_switch_is_off_unless_exactly_true(tmp_path, text, want):
    (tmp_path / 'mind.toml').write_text(text)
    assert load_videos_config(tmp_path / 'mind.toml') == want
    assert load_videos_config(tmp_path / 'absent.toml') == VideosConfig()


@pytest.mark.skipif(files.tools() is None, reason='ffmpeg and ffprobe are not on PATH')
def test_real_ffmpeg_reads_synthetic_clips(tmp_path):
    """Clips ffmpeg draws itself (its test pattern): no media from anywhere else."""
    ffmpeg = files.tools()[0]
    made = tmp_path / 'made'
    made.mkdir()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    def make(name, *args):
        subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', *args, os.fspath(made / name)],
                       check=True, capture_output=True, timeout=60, creationflags=flags)
        return (made / name).read_bytes()
    clips = {
        'wide.mp4': ('video/mp4', make('wide.mp4', '-i', 'testsrc=size=640x360:rate=25', '-t', '4',
                                        '-pix_fmt', 'yuv420p')),
        'tall.mov': ('video/quicktime', make('tall.mov', '-i', 'testsrc=size=360x640:rate=25', '-t', '3',
                                              '-pix_fmt', 'yuv420p')),
        'blip.webm': ('video/webm', make('blip.webm', '-i', 'testsrc=size=320x240:rate=25', '-t', '1',
                                         '-c:v', 'libvpx')),
    }
    data = tmp_path / 'data'
    got = {name: files.stage(data, raw, mime, files.tools())[1] for name, (mime, raw) in clips.items()}
    assert (got['wide.mp4'].frames, got['wide.mp4'].ms, got['wide.mp4'].w, got['wide.mp4'].h) == (3, 4000, 768, 432)
    assert (got['tall.mov'].frames, got['tall.mov'].w, got['tall.mov'].h) == (3, 288, 512)
    assert (got['blip.webm'].frames, got['blip.webm'].ms) == (1, 1000)
    for mid, token in enumerate(sorted(p.stem for p in (data / files.INCOMING).glob('*.json')), 100):
        files.adopt(data, token, mid)
    assert sorted(p.suffix for p in (data / 'discord/videos').iterdir()) == ['.mov', '.mp4', '.webm']
    assert shutil.which('ffmpeg')  # the same PATH the console looks in
