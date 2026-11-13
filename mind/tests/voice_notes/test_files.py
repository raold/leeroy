import io
import os
import struct
import wave
import pytest
from leeroy_mind.voice_notes import files
from leeroy_mind.voice_notes.config import load_config


def wav(*,seconds=1,channels=1,rate=16000,width=2):
    stream=io.BytesIO()
    with wave.open(stream,'wb') as writer:
        writer.setparams((channels,width,rate,0,'NONE','not compressed'))
        writer.writeframes(b'\0'*(round(seconds*rate)*channels*width))
    return stream.getvalue()


def inventory(root):
    return {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('data',[b'no',wav(channels=2),wav(rate=44100),wav(width=1),wav(seconds=61),wav(seconds=.1),b'x'*2097153,
    wav()+b'junk',wav()[:40]+struct.pack('<I',1)+wav()[44:]])
def test_bad_upload_never_writes(tmp_path,data):
    with pytest.raises(files.AudioRefused): files.stage(tmp_path,data)
    assert inventory(tmp_path)=={}


def test_adoption_keeps_every_file_and_token_cannot_be_reused(tmp_path):
    token,ms,size=files.stage(tmp_path,wav())
    before=inventory(tmp_path)
    path,got_ms,got_size=files.adopt(tmp_path,token)
    assert (ms,size)==(got_ms,got_size)==(1000,len(wav()))
    assert files.read_audio(tmp_path/path,tmp_path)==wav()
    assert all(p.read_bytes()==data for p,data in before.items())
    before=inventory(tmp_path)
    with pytest.raises(files.AudioRefused,match='reused'): files.adopt(tmp_path,token)
    assert inventory(tmp_path)==before


def test_stage_day_count_survives_adoption_and_restart(tmp_path):
    from leeroy_mind.clock import local_at
    token,_,_=files.stage(tmp_path,wav())
    day=local_at((tmp_path/files.INCOMING/(token+'.wav')).stat().st_mtime).date().isoformat()
    files.adopt(tmp_path,token)
    assert files.staged_count(tmp_path,day)==1


def test_audio_identity_and_symlink_checks(tmp_path):
    token,_,_=files.stage(tmp_path,wav()); path=tmp_path/files.INCOMING/(token+'.wav')
    outside=tmp_path.parent/'outside-note.wav'; outside.write_bytes(wav())
    link=tmp_path/'link.wav'
    try: link.symlink_to(outside)
    except OSError: pytest.skip('symlink privilege unavailable')
    assert files.read_audio(link,tmp_path) is None
    assert files.read_audio(outside,tmp_path) is None
    assert files.read_audio(path,tmp_path)==wav()


@pytest.mark.parametrize('table',['','enabled=true','enabled=true\nshare=.9','enabled=true\nmax_words=4',
    'enabled=true\nper_member_day=11','enabled=true\nguild_day=true','enabled=true\nextra=1'])
def test_startup_config_fails_closed(tmp_path,table):
    path=tmp_path/'mind.toml';path.write_text('[voice_notes]\n'+table)
    assert load_config(path).enabled is (table=='enabled=true')
