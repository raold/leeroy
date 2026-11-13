"""Offline audio requests. Native synthesis, no mixer, effects, microphone or GPU admission."""
from dataclasses import dataclass
import io
import os
from pathlib import Path
import re
import stat
import time
import wave
import numpy as np
import soundfile as sf

from .wire import Render, Rendered, Transcribe, Transcript
from .textear import ear
from .logs import log
from .stt.phantoms import is_phantom

MAX_BYTES=2097152
_PATH=re.compile(r'discord/voice/[0-9a-f]{32}\.wav')


def folder(root):
    path=Path(root)
    for part in (None,'discord','voice'):
        if part:
            path/=part
        path.mkdir(exist_ok=True)
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('unsafe_path')
    return path


@dataclass
class NoteJob:
    request: Render
    ended: bool = False


class Notes:
    def __init__(self,service,root):
        self.service,self.root=service,Path(root)
        self.pending=set()
        self.format='OGG' if sf.check_format('OGG','VORBIS') else 'WAV'
        log('voice_note_format',code='ogg' if self.format=='OGG' else 'wav')

    def request(self,msg):
        if self.pending:
            result=Rendered(msg.id,False,'',0,0,'busy') if isinstance(msg,Render) else Transcript(msg.id,False,'','busy')
            self.service._send(result); return
        self.pending.add(msg.id)
        if isinstance(msg,Render):
            # Lower than every live tier; one queue serializes access to the engines.
            self.service._tts_queue.put((99,next(self.service._tts_seq),0,NoteJob(msg)))
        else:
            s=self.service
            # Like _hear, refresh on the event loop before handing work to STT.
            # with_names returns a new corrector; later roster changes cannot
            # change this note's snapshot. The recent-speaker deque is bounded.
            if s._roster_stale:
                s._roster_stale=False
                s._corrector=s._corrector.with_names(s.occupancy.names.values())
            present=tuple(s._speakers)
            hotwords=tuple(s._hotwords(present))
            s._stt_pool.submit(self.transcribe,msg,s._corrector,present,hotwords)

    def reply(self,msg,ok,reason,*,path='',ms=0,size=0,text=''):
        result=(Rendered(msg.id,ok,path,ms,size,reason) if isinstance(msg,Render)
                else Transcript(msg.id,ok,text,reason))
        def done():
            self.pending.discard(msg.id)
            self.service._send(result)
        self.service._post(done)

    def render(self,msg):
        s=self.service; started=time.perf_counter()
        if len(msg.text.split())>60 or len(msg.text)>2000:
            self.reply(msg,False,'too_long'); return
        spec=s._specs.get(msg.guid)
        if spec is None:
            self.reply(msg,False,'no_voice'); return
        try:
            x=s._live_expressive() if s._speaks_expressive(spec) else None
            # CPU expressive has a separate room worker/admission slot. A note never
            # claims that slot or races its worker; the admitted CUDA queue is shared,
            # otherwise this optional transport uses her native Kokoro blend.
            if x is s._expressive_cpu:
                x=None
            dry=None
            rate=s._engine.sample_rate
            if x is not None:
                try:
                    said=ear(msg.text,lexicon=s._lexicon,engine=x.name)
                    dry=x.synth(' '.join(said.sentences),spec,x.levers('none'),spec.speed,timeout_s=18)
                    rate=x.sample_rate
                except Exception:
                    dry=None
            if dry is None:
                said=ear(msg.text,lexicon=s._lexicon,engine=s._engine.name)
                dry=s._engine.synth(said.text,spec,s._bank.style(spec,'none',s._decision),spec.speed)
            audio=np.asarray(dry,dtype=np.float32)
            ms=round(len(audio)*1000/rate)
            if audio.ndim!=1 or not np.isfinite(audio).all() or not 200<=ms<=65000:
                self.reply(msg,False,'too_long'); return
        except Exception:
            self.reply(msg,False,'engine_error'); return
        try:
            dest=folder(self.root)/(msg.id+('.ogg' if self.format=='OGG' else '.wav'))
            temporary=dest.with_suffix(dest.suffix+'.part')
            with temporary.open('xb') as stream:
                sf.write(stream,audio,rate,format=self.format,
                         subtype='VORBIS' if self.format=='OGG' else 'PCM_16')
            size=temporary.stat().st_size
            if not 1<=size<=MAX_BYTES:
                self.reply(msg,False,'too_long'); return
            folder(self.root)
            # rename on Windows refuses an existing target; never overwrite an old note.
            if dest.exists():
                raise FileExistsError
            temporary.rename(dest)
            path=dest.relative_to(self.root).as_posix()
        except Exception:
            self.reply(msg,False,'write_failed'); return
        log('voice_note_rendered',id=msg.id,ms=ms,bytes=size,render_ms=round((time.perf_counter()-started)*1000))
        self.reply(msg,True,'ok',path=path,ms=ms,size=size)

    def transcribe(self,msg,corrector,present,hotwords):
        s=self.service
        if s._recognizer is None:
            self.reply(msg,False,'no_voice'); return
        try:
            if not _PATH.fullmatch(msg.path):
                raise ValueError
            path=folder(self.root)/Path(msg.path).name
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or not 44<=info.st_size<=MAX_BYTES:
                raise ValueError
            with path.open('rb') as stream:
                opened=os.fstat(stream.fileno())
                if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):
                    raise ValueError
                data=stream.read(MAX_BYTES+1)
            folder(self.root)
            with wave.open(io.BytesIO(data),'rb') as wav:
                if wav.getparams()[:3]!=(1,2,16000) or not 3200<=wav.getnframes()<=960000:
                    raise ValueError
                audio=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(np.float32)/32768
            heard=s._recognizer.transcribe(audio,list(hotwords))
            if heard is None or is_phantom(heard,min_confidence=s._cfg.min_confidence):
                raise ValueError
            text=corrector.correct(heard.text,present=present).strip()
            if not text or len(text)>2000:
                raise ValueError
        except Exception:
            self.reply(msg,False,'engine_error'); return
        self.reply(msg,True,'ok',text=text)
