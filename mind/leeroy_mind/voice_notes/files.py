"""Strict WAV uploads and identity-checked audio reads. No audio file is deleted."""
import os
from pathlib import Path
import secrets
import struct
from leeroy_mind import clock
from leeroy_mind.imagefiles import image_info
from leeroy_mind.photos.images import _safe_dir, _write_new, TOKEN_RE, ImageRefused

MAX_BYTES=2097152
INCOMING=Path('discord/incoming-voice')
VOICE=Path('discord/voice')


class AudioRefused(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def check_wav(data):
    if len(data)>MAX_BYTES:
        raise AudioRefused('too_big')
    if len(data)<44 or data[:4]!=b'RIFF' or data[8:12]!=b'WAVE' or struct.unpack_from('<I',data,4)[0]!=len(data)-8:
        raise AudioRefused('bad_wav')
    pos,fmt,payload=12,None,None
    while pos<len(data):
        if pos+8>len(data):
            raise AudioRefused('bad_wav')
        kind,size=struct.unpack_from('<4sI',data,pos); pos+=8
        if pos+size>len(data):
            raise AudioRefused('bad_wav')
        part=data[pos:pos+size]
        if kind==b'fmt ':
            if fmt is not None or size!=16:
                raise AudioRefused('bad_wav')
            fmt=struct.unpack('<HHIIHH',part)
        elif kind==b'data':
            if payload is not None:
                raise AudioRefused('bad_wav')
            payload=part
        else:
            raise AudioRefused('bad_wav')
        pos+=size+(size%2)
    if pos!=len(data) or fmt!=(1,1,16000,32000,2,16) or payload is None or len(payload)%2:
        raise AudioRefused('bad_wav')
    ms=len(payload)*1000//32000
    if not 200<=ms<=60000:
        raise AudioRefused('duration')
    return ms


def safe_dir(root,relative,create=False):
    try:
        return _safe_dir(Path(root),relative,create=create)
    except ImageRefused as error:
        raise AudioRefused(error.code) from None


def read_audio(path,root):
    path,root=Path(path),Path(root)
    info=image_info(path,root,maximum=MAX_BYTES)
    if info is None:
        return None
    try:
        with path.open('rb') as stream:
            opened=os.fstat(stream.fileno())
            if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino) or image_info(path,root,maximum=MAX_BYTES) is None:
                return None
            data=stream.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES or not (data.startswith(b'OggS') or data.startswith(b'RIFF')):
            return None
        return data
    except OSError:
        return None


def stage(root,data):
    ms=check_wav(data)
    folder=safe_dir(root,INCOMING,True)
    token=secrets.token_hex(16)
    _write_new(folder/(token+'.wav'),data)
    safe_dir(root,INCOMING)
    return token,ms,len(data)


def staged_count(root,day):
    try:
        folder=safe_dir(root,INCOMING)
    except AudioRefused as error:
        if error.code=='missing':
            return 0
        raise
    count=0
    for path in folder.glob('*.wav'):
        if TOKEN_RE.fullmatch(path.stem) and (info:=image_info(path,Path(root),maximum=MAX_BYTES)) is not None:
            count+=clock.local_at(info.st_mtime).date().isoformat()==day
    return count


def adopt(root,token):
    if type(token) is not str or not TOKEN_RE.fullmatch(token):
        raise AudioRefused('bad_token')
    incoming=safe_dir(root,INCOMING)
    dest=safe_dir(root,VOICE,True)/(token+'.wav')
    data=read_audio(incoming/(token+'.wav'),root)
    if data is None:
        raise AudioRefused('missing')
    ms=check_wav(data)
    try:
        _write_new(incoming/(token+'.claimed'),b'claimed')
        _write_new(dest,data)
    except FileExistsError:
        raise AudioRefused('reused') from None
    safe_dir(root,VOICE)
    return (VOICE/dest.name).as_posix(),ms,len(data)


WORDS={'too_big':'That recording is over 2 MiB.','bad_wav':'Use a mono 16 kHz, 16-bit PCM WAV recording.',
       'duration':'Record between 0.2 and 60 seconds.','bad_token':'Record or upload the voice note again.',
       'missing':'That recording is missing. Record it again.','link':'That audio path is a link and cannot be used.',
       'reused':'That recording has already been posted.'}
