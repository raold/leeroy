import asyncio
from dataclasses import replace
import io
from pathlib import Path
from types import SimpleNamespace
import time
import wave
import pytest

from conftest import run,eventually
from test_service import Rig,spec
from leeroy_voice.wire import Render,Rendered,Transcribe,Transcript,VoiceDef,Speak,Status,parse,encode,WireError


def inventory(root):
    return {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_offline_native_render_is_atomic_without_playback_or_duck(tmp_path,fake_bank):
    async def go():
        async with Rig(tmp_path,fake_bank) as r:
            await r.mind.send(VoiceDef(101,'Arkon',spec()))
            before=inventory(r.service.notes.root)
            r.service._fx=lambda *a,**kw:pytest.fail('voice notes must not use effects')
            started=time.perf_counter()
            await r.mind.send(Render('a'*32,101,'Meet after the raid.'))
            result=await r.next_of(Rendered)
            elapsed=(time.perf_counter()-started)*1000
            print(f'fake_render_ms={elapsed:.2f}')
            assert result.ok and result.reason=='ok'
            path=r.service.notes.root/result.path
            assert path.read_bytes().startswith((b'OggS',b'RIFF'))
            assert path.stat().st_size==result.bytes and 200<=result.ms<=65000
            assert not list(path.parent.glob('*.part'))
            assert not r.played() and not r.service._ducked and not r.service.mixer.active()
            assert all(p.read_bytes()==data for p,data in before.items())
            assert r.service._status().render and not r.service._online
    run(go())


def test_a_note_never_starts_while_room_lines_wait_or_play(tmp_path,fake_bank):
    """Room playback first: a note asked for while a room line waits or plays is refused busy at
    once (the mind sends her line as text), so it never holds up the room's synthesis; one asked
    for when the room is quiet renders."""
    async def go():
        async with Rig(tmp_path,fake_bank) as r:
            await r.snapshot()
            r.engine.gate.clear()
            def line(ident,text):
                return Speak(ident,101,text,'guild_hall',False,'p3','banter','none','sober',int((r.clock.now()+120)*1000))
            await r.mind.send(line('room-first','First ordinary room line.'))
            await eventually(lambda:'room-first' in r.service._jobs)
            before=inventory(r.service.notes.root)
            await r.mind.send(Render('b'*32,101,'Later private audio note.'))
            refused=await r.next_of(Rendered)
            assert refused.id=='b'*32 and not refused.ok and refused.reason=='busy'
            r.engine.gate.set()
            await r.sees('room-first','started')
            await r.mind.send(Render('c'*32,101,'Later private audio note.'))
            assert (await r.next_of(Rendered)).reason=='busy'  # still playing
            await r.play_out()
            await r.sees('room-first','done')
            assert inventory(r.service.notes.root)==before
            await r.mind.send(Render('d'*32,101,'Later private audio note.'))
            assert (await r.next_of(Rendered)).ok
            assert r.engine.live().count('Later private audio note.')==1
            assert not any(ident in ('b'*32,'c'*32,'d'*32) for ident,_,_ in r.played())
    run(go())


def test_one_render_and_one_transcription_run_side_by_side(tmp_path,fake_bank):
    """A render and a transcription use different workers: one of each at a time, so his note
    is never refused because a member's note is rendering (a second of a kind is busy)."""
    async def go():
        async with Rig(tmp_path,fake_bank) as r:
            await r.mind.send(VoiceDef(101,'Arkon',spec()))
            folder=r.service.notes.root/'discord/voice';folder.mkdir(parents=True)
            path=folder/('9'*32+'.wav')
            with wave.open(str(path),'wb') as wav:
                wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(b'\0'*32000)
            relative=path.relative_to(r.service.notes.root).as_posix()
            r.engine.gate.clear()
            await r.mind.send(Render('a'*32,101,'An invented private note.'))
            await eventually(lambda:'a'*32 in r.service.notes.pending)
            await r.mind.send(Render('b'*32,101,'Another invented private note.'))
            busy=await r.next_of(Rendered)
            assert busy.id=='b'*32 and busy.reason=='busy'
            await r.mind.send(Transcribe('c'*32,relative))
            heard=await r.next_of(Transcript)
            assert heard.id=='c'*32 and heard.ok and heard.text=='hello team'
            await r.mind.send(Transcribe('e'*32,relative))
            assert (await r.next_of(Transcript)).ok  # the first one is done: not busy
            r.engine.gate.set()
            done=await r.next_of(Rendered)
            assert done.id=='a'*32 and done.ok
    run(go())


def test_long_and_missing_voice_refused_without_files(tmp_path,fake_bank):
    async def go():
        async with Rig(tmp_path,fake_bank) as r:
            await r.mind.send(VoiceDef(101,'Arkon',spec()))
            before=inventory(r.service.notes.root)
            await r.mind.send(Render('c'*32,101,'word '*61))
            assert (await r.next_of(Rendered)).reason=='too_long'
            await r.mind.send(Render('d'*32,102,'An invented short note.'))
            assert (await r.next_of(Rendered)).reason=='no_voice'
            assert inventory(r.service.notes.root)==before
    run(go())


def test_transcribe_uses_recognizer_without_ptt_state_or_deleting_audio(tmp_path,fake_bank):
    async def go():
        async with Rig(tmp_path,fake_bank) as r:
            folder=r.service.notes.root/'discord/voice'; folder.mkdir(parents=True)
            path=folder/('e'*32+'.wav')
            with wave.open(str(path),'wb') as wav:
                wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(b'\0'*32000)
            before=inventory(r.service.notes.root)
            await r.mind.send(Transcribe('f'*32,path.relative_to(r.service.notes.root).as_posix()))
            result=await r.next_of(Transcript)
            assert result.ok and result.text=='hello team'
            assert not r.service._pressing and not r.service._clips and not r.service._ducked
            assert inventory(r.service.notes.root)==before
            await r.mind.send(Transcribe('1'*32,'../private.wav'))
            assert not (await r.next_of(Transcript)).ok
    run(go())


def test_note_refreshes_names_before_first_ptt_and_after_roster_change(tmp_path,fake_bank):
    from fakes import FakeRecognizer
    from leeroy_voice.stt import Transcript as HeardText
    recognizer=FakeRecognizer(results=[HeardText('I spoke with bellra',.9,1100),
                                       HeardText('I spoke with coross',.9,1100)])
    async def go():
        async with Rig(tmp_path,fake_bank,recognizer=recognizer) as r:
            folder=r.service.notes.root/'discord/voice';folder.mkdir(parents=True)
            path=folder/('4'*32+'.wav')
            with wave.open(str(path),'wb') as wav:
                wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(b'\0'*32000)
            before=inventory(r.service.notes.root)
            for ident,name in (('5'*32,'Belra'),('6'*32,'Coros')):
                await r.mind.send(VoiceDef(101,name,spec()|{'name':name}))
                await eventually(lambda:r.service.occupancy.names.get(101)==name)
                assert r.service._roster_stale
                await r.mind.send(Transcribe(ident,path.relative_to(r.service.notes.root).as_posix()))
                result=await r.next_of(Transcript)
                assert result.ok and result.text=='I spoke with '+name
                assert not r.service._roster_stale
                assert not r.service._pressing and not r.service._clips
            assert inventory(r.service.notes.root)==before
    run(go())


def test_queued_note_keeps_corrector_and_bounded_hotwords_snapshot(tmp_path,fake_bank):
    from fakes import FakeRecognizer
    from leeroy_voice.stt import Transcript as HeardText
    from leeroy_voice.service import SPEAKERS_KEPT
    recognizer=FakeRecognizer(default=HeardText('I spoke with bellra',.9,1100))
    async def go():
        async with Rig(tmp_path,fake_bank,recognizer=recognizer) as r:
            folder=r.service.notes.root/'discord/voice';folder.mkdir(parents=True)
            path=folder/('7'*32+'.wav')
            with wave.open(str(path),'wb') as wav:
                wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(b'\0'*32000)
            await r.mind.send(VoiceDef(101,'Belra',spec()|{'name':'Belra'}))
            await eventually(lambda:r.service.occupancy.names.get(101)=='Belra')
            for i in range(SPEAKERS_KEPT+5):r.service._note_speaker(f'Fake{i}')
            expected=tuple(r.service._speakers)
            queued=[];pool=r.service._stt_pool
            r.service._stt_pool=SimpleNamespace(submit=lambda fn,*args:queued.append((fn,args)))
            try:
                await r.mind.send(Transcribe('8'*32,path.relative_to(r.service.notes.root).as_posix()))
                await eventually(lambda:bool(queued))
                await r.mind.send(VoiceDef(101,'Coros',spec()|{'name':'Coros'}))
                await eventually(lambda:r.service.occupancy.names.get(101)=='Coros')
                # Another STT submission may refresh the shared corrector while
                # this request waits in the pool. Its immutable snapshot stays.
                r.service._corrector=r.service._corrector.with_names(['Coros'])
                r.service._speakers.clear();r.service._note_speaker('Coros')
            finally:r.service._stt_pool=pool
            fn,args=queued.pop();pool.submit(fn,*args)
            result=await r.next_of(Transcript)
            assert result.ok and result.text=='I spoke with Belra'
            assert tuple(recognizer.calls[0][1])==expected and len(expected)==SPEAKERS_KEPT
            assert not r.service._pressing and not r.service._clips
    run(go())


def test_new_wire_checks_apply_to_encode_and_parse():
    for message in (Render('bad',101,'hello'),Rendered('a'*32,True,'bad',1,1,'ok'),
                    Transcript('a'*32,False,'','arbitrary')):
        with pytest.raises(WireError):encode(message,1)
    old=b'{"v":1,"type":"status","ts":1,"ok":true,"mic":"closed","gpu":"none","stt":"off","ui":false,"queue":0,"errors":[]}'
    assert parse(old).render is False


def test_cpu_expressive_slot_is_reserved_for_room_work(tmp_path,fake_bank):
    async def go():
        async with Rig(tmp_path,fake_bank) as r:
            await r.mind.send(VoiceDef(101,'Arkon',spec()))
            cpu=SimpleNamespace(name='expressive',sample_rate=24000,
                synth=lambda *a,**kw:pytest.fail('note raced the CPU room worker'))
            r.service._expressive_cpu=cpu
            r.service._speaks_expressive=lambda spec:True
            r.service._live_expressive=lambda:cpu
            r.service._gpu=SimpleNamespace(owns=lambda *a:pytest.fail('note acquired the GPU'))
            try:
                await r.mind.send(Render('2'*32,101,'An invented native note.'))
                assert (await r.next_of(Rendered)).ok
                assert r.service._cpu_job is None
                assert 'An invented native note.' in r.engine.live()
            finally:
                r.service._expressive_cpu=None
                r.service._gpu=None
    run(go())


def test_wav_fallback_is_selected_once_and_keeps_old_audio(tmp_path,fake_bank,monkeypatch):
    from leeroy_voice import notes
    monkeypatch.setattr(notes.sf,'check_format',lambda *a:False)
    async def go():
        async with Rig(tmp_path,fake_bank) as r:
            await r.mind.send(VoiceDef(101,'Arkon',spec()))
            folder=r.service.notes.root/'discord/voice';folder.mkdir(parents=True)
            old=folder/'old.wav';old.write_bytes(b'old retained audio')
            before=inventory(r.service.notes.root)
            await r.mind.send(Render('3'*32,101,'An invented native note.'))
            result=await r.next_of(Rendered)
            assert result.ok and result.path.endswith('.wav')
            with wave.open(str(r.service.notes.root/result.path),'rb') as wav:
                assert wav.getnchannels()==1 and wav.getsampwidth()==2
            assert all(path.read_bytes()==data for path,data in before.items())
    run(go())
