"""Bounded voice-note RPCs and DM-only receipts. Never takes a model/GPU lease."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
import uuid

from leeroy_mind.discord import layout, store as discord
from leeroy_mind.discord.channels import channel_id
from leeroy_mind.discord.presence import presence_of, local_time, RNG_SEED
from leeroy_mind.discord.text import TextRefused, clean_text
from leeroy_mind.logs import log
from leeroy_mind.protocol import Chat
from leeroy_mind.tuning.effective import dials_of
from leeroy_mind.voice import wire
from .config import load_config, VoiceNotesConfig
from . import files

TIMEOUT=20.0
# A restarted mind can tick before the voice service, still loading its models, has greeted it:
# a pending recording waits this long for the connection, then settles as a real failure.
RECOVERY_WAIT_S=300.0
PLACEHOLDER='(voice message)'
FAILURE=('{they} sent a private voice note, but you cannot play it right now. Do not invent its '
         'contents; you may say you will listen later.')
PENDING_KEY='voice_note:pending:'


def rows(conn,ids):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='discord_voice'").fetchone() is None:
        return {}
    return {r['message']:dict(r) for r in conn.execute('SELECT v.* FROM discord_voice v JOIN discord_message m '
        'ON m.id=v.message WHERE m.deleted IS NULL AND v.message IN (SELECT value FROM json_each(?))',
        (json.dumps(list(ids)),))}


def failure(forms):
    """The answering member's instruction for his note she cannot play, in his forms."""
    return FAILURE.format(they=forms[0].capitalize())


def counts(store,day):
    raw=store.get_meta('server:day:'+day)
    try:
        value=json.loads(raw) if raw is not None else {}
        if type(value) is not dict:
            raise ValueError
        for key,count in value.items():
            if key.startswith('voice_note') and (type(count) is not int or count<0):
                raise ValueError
        # Existing server counters must remain valid too.
        for key in ('conversations','messages','reactions','dms'):
            if type(value.get(key,0)) is not int or value.get(key,0)<0:
                raise ValueError
        return value
    except (ValueError,TypeError):
        return None


class VoiceNotes:
    def __init__(self,app,config=None):
        self.app=app
        self.config=config or (load_config(Path(app.data_dir)/'mind.toml') if getattr(app,'data_dir',None) else VoiceNotesConfig())
        self._pending={}
        self._tasks=set()
        self._active=set()
        self._waiting={}  # message id -> monotonic time recovery first found the voice service down
        self._closed=False

    def receive(self,msg):
        if not isinstance(msg,(wire.Rendered,wire.Transcript)):
            return False
        future=self._pending.get(msg.id)
        if future is not None and not future.done():
            future.set_result(msg)
        return True

    async def request(self,msg):
        future=asyncio.get_running_loop().create_future()
        self._pending[msg.id]=future
        try:
            if self._closed:
                return None
            client=self.app.voice.client
            if not client.greeted or client.send(msg) is False:
                return None
            return await asyncio.wait_for(future,TIMEOUT)
        except (TimeoutError,OSError,RuntimeError):
            return None
        except Exception:
            return None
        finally:
            self._pending.pop(msg.id,None)

    def eligible(self,turn,meta,p,text):
        app,cfg=self.app,self.config
        peer=layout.dm_peer(channel_id(turn.channel))
        voice=getattr(app,'voice',None)
        # A DM thread's messages have no player on the page. Only a voice service that reports
        # `render` can render one: an older one answers with an error, after the whole wait.
        if (not cfg.enabled or self._closed or getattr(app,'stopping',False) or peer!=turn.speaker
                or app.human_guid<=0 or meta.realism_correction or not meta.turn_id
                or (meta.discord is not None and meta.discord.thread is not None)
                or voice is None or turn.speaker not in voice.specs or not voice.client.greeted
                or voice.client.status is None or not getattr(voice.client.status,'render',False)):
            return False
        arbiter=getattr(app,'arbiter',None)
        if arbiter is not None and not arbiter.holders:
            return False
        # an item: a game link or placeholder, or Discord's [Item Name] (talk.items), stays text
        if not 3<=len(text.split())<=cfg.max_words or not re.search(r'\w',text) or re.search(r'\|[Hh]|\[item|<item|\[[^\]\n]+\]',text,re.I):
            return False
        talkative=dials_of(p).level('talkative')
        if not talkative:
            return False
        member=app.world.discord_members().get(turn.speaker)
        if member is None or presence_of(p,local=local_time(app.clock.local(),p.identity.region),
            body_online=turn.speaker in app.world.online,body_zone=member.zone,rng_seed=RNG_SEED,
            in_his_group=app.world.human_online and app.world.in_group(turn.speaker)).state!='online':
            return False
        day=app.clock.local().date().isoformat(); value=counts(app.store,day)
        if value is None or value.get('voice_notes',0)>=cfg.guild_day or value.get(f'voice_note:{turn.speaker}',0)>=cfg.per_member_day:
            return False
        draw=int.from_bytes(sha256(f'{turn.speaker}:{meta.turn_id}'.encode()).digest()[:8],'big')/2**64
        return draw<min(1,cfg.share*talkative/5)

    async def prepare(self,turn,meta,p,text):
        if not self.eligible(turn,meta,p,text):
            return None
        ident=uuid.uuid4().hex
        result=await self.request(wire.Render(ident,turn.speaker,text))
        if not isinstance(result,wire.Rendered) or not result.ok:
            log('voice_note_fallback',reason=result.reason if isinstance(result,wire.Rendered) else 'unavailable')
            return None
        if result.path not in (f'discord/voice/{ident}.ogg',f'discord/voice/{ident}.wav'):
            log('voice_note_fallback',reason='bad_path'); return None
        data=await asyncio.to_thread(files.read_audio,Path(self.app.data_dir)/result.path,Path(self.app.data_dir))
        if data is None or len(data)!=result.bytes:
            log('voice_note_fallback',reason='missing'); return None
        if not self.eligible(turn,meta,p,text):
            log('voice_note_fallback',reason='changed'); return None
        return result

    def record(self,message,guid,result):
        """Join the Discord message's transaction; races recheck caps before attachment."""
        if result is None:
            return
        day=self.app.clock.local().date().isoformat(); value=counts(self.app.store,day)
        if value is None or value.get('voice_notes',0)>=self.config.guild_day or value.get(f'voice_note:{guid}',0)>=self.config.per_member_day:
            return
        self.app.store.conn.execute('INSERT INTO discord_voice VALUES (?,?,?,?,?,1)',
            (message,'mind',result.path,result.ms,result.bytes))
        value['voice_notes']=value.get('voice_notes',0)+1
        key=f'voice_note:{guid}'; value[key]=value.get(key,0)+1
        self.app.store.set_meta('server:day:'+day,json.dumps(value))

    def human_post(self,args,desk):
        app=self.app
        if not self.config.enabled:
            return {'ok':False,'error':'Voice notes are off in this mind.'}
        if layout.dm_peer(args['channel']) is None:
            return {'ok':False,'error':'Voice notes are only for a direct message.'}
        now=app.clock.now(); me=app.human_guid
        with app.store.transaction():
            mid=discord.post(app.store,channel=args['channel'],author=me,origin='human',text=PLACEHOLDER,
                reply_to=args.get('reply_to'),thread=args.get('thread'),now=now)
            path,ms,size=files.adopt(app.data_dir,args['voice'])
            app.store.conn.execute('INSERT INTO discord_voice VALUES (?,?,?,?,?,0)',(mid,'human',path,ms,size))
            line,chat=desk._his_line(mid,me,now)
            app.store.set_meta(PENDING_KEY+str(mid),'pending')
            app.store.conn.execute('DELETE FROM meta WHERE key=?',(f'server:dm:open:{layout.dm_peer(args["channel"])}',))
        server=getattr(app,'server',None)
        if server is not None:
            server.engine.his_post(args['channel']); server.dms.his_post(args['channel'])
        if (hook:=getattr(app,'discord_talk',None)) is not None:
            hook.begin_photo()  # same existing bounded Discord lease; no deferral receipt yet
        self._start(mid,line,chat,path,desk)
        return {'ok':True,'id':mid}

    def _start(self,mid,line,chat,path,desk):
        self._active.add(mid)
        task=asyncio.create_task(self._transcribe(mid,line,chat,path,desk),name='discord-voice-transcript')
        self._tasks.add(task)
        def done(task):
            self._active.discard(mid)
            self._done(task)
        task.add_done_callback(done)

    def _connected(self):
        voice=getattr(self.app,'voice',None)
        return voice is not None and bool(voice.client.greeted)

    def tick(self):
        """Resume one durable receipt at a time after a shutdown or interrupted handoff.

        Only this receipt selects recovery work; a transcript failure is terminal too.
        The normal owed-DM tick cannot see a pending recording as an owed post.
        A pending recording waits for the voice service's connection (at most RECOVERY_WAIT_S),
        so a restart never turns it into a failure; a ready one needs no voice service.
        """
        if not self.config.enabled or self._closed or self.app.stopping or self._tasks:
            return
        app=self.app
        rows=app.store.conn.execute("SELECT key FROM meta WHERE key LIKE ? ORDER BY key LIMIT 16",
                                   (PENDING_KEY+'%',)).fetchall()
        for row in rows:
            key=row[0]
            try:
                mid=int(key[len(PENDING_KEY):])
            except ValueError:
                continue
            if mid in self._active:
                continue
            message=app.store.conn.execute('SELECT m.deleted,v.path FROM discord_message m JOIN discord_voice v '
                "ON v.message=m.id WHERE m.id=? AND v.origin='human'",(mid,)).fetchone()
            line=app.store.get_meta(f'discord:line:{mid}')
            chatrow=app.store.conn.execute('SELECT * FROM chat_line WHERE id=?',(line,)).fetchone() if line else None
            if message is None or message['deleted'] is not None or chatrow is None:
                with app.store.transaction():
                    app.store.conn.execute('DELETE FROM meta WHERE key=?',(key,))
                self._waiting.pop(mid,None)
                continue
            if app.store.get_meta(key)!='ready' and not self._connected():
                since=self._waiting.setdefault(mid,app.clock.monotonic())
                if app.clock.monotonic()-since<RECOVERY_WAIT_S:
                    continue
            self._waiting.pop(mid,None)
            chat=Chat(ts=int(chatrow['ts']*1000),channel=chatrow['channel'],from_guid=chatrow['from_guid'],
                from_name=chatrow['from_name'],to_guid=chatrow['to_guid'],text=chatrow['text'],map=chatrow['map'],zone=chatrow['zone'])
            self._start(mid,int(line),chat,message['path'],app.discord)
            break

    def _done(self,task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log('voice_note_transcript_failed',reason='write_failed')

    async def _transcribe(self,mid,line,chat,path,desk):
        if self.app.store.get_meta(PENDING_KEY+str(mid))=='ready':
            self._handoff(mid,line,chat)
            return
        result=await self.request(wire.Transcribe(uuid.uuid4().hex,path))
        app=self.app
        row=app.store.conn.execute('SELECT deleted,text FROM discord_message WHERE id=?',(mid,)).fetchone()
        if self._closed or app.stopping or row is None or row[0] is not None:
            return
        reason=result.reason if isinstance(result,wire.Transcript) else 'unavailable'
        try:
            text=clean_text(result.text) if isinstance(result,wire.Transcript) and result.ok else ''
        except TextRefused:
            text,reason='','engine_error'  # no post could hold it: a failure, never a retry
        with app.store.transaction():
            if text:
                # An owner edit made while STT ran wins over this old recording. The transcript
                # is the post's own words, not his edit: `edited` stays unset.
                if row[1]==PLACEHOLDER:
                    app.store.conn.execute('UPDATE discord_message SET text=? WHERE id=? AND deleted IS NULL',
                                           (text,mid))
                    desk._edit_line(mid)
                app.store.conn.execute('UPDATE discord_voice SET transcript_ok=1 WHERE message=?',(mid,))
            app.store.set_meta(PENDING_KEY+str(mid),'ready')
        if not text:
            log('voice_note_transcript_failed',reason=reason)
        stored=app.store.conn.execute('SELECT text FROM chat_line WHERE id=?',(line,)).fetchone()[0]
        self._handoff(mid,line,replace(chat,text=stored))

    def _handoff(self,mid,line,chat):
        app=self.app
        hook=getattr(app,'discord_talk',None)
        if self._closed or app.stopping or hook is None:
            return
        # Threading has a memory-only cursor as well as rows, so do not roll its committed
        # thread back if a later handoff fails. Its row makes this step idempotent on retry.
        row=app.store.conn.execute('SELECT thread_id FROM chat_line WHERE id=?',(line,)).fetchone()
        if row is not None and row[0] is None:
            try:
                app.on_his_discord_line(line,chat,app.clock.now())
            except Exception as e:  # as the desk's typed post: the note is stored, answers still come
                log('discord_thread_failed',level=logging.WARNING,message=mid,line=line,
                    error=type(e).__name__)
        # No await between answer handoff and its receipt. A crash before this commit
        # leaves a ready note, not another STT request.
        with app.store.transaction():
            hook.on_his_post(mid)
            app.store.conn.execute('DELETE FROM meta WHERE key=?',(PENDING_KEY+str(mid),))

    async def close(self):
        self._closed=True
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks),return_exceptions=True)
        for future in self._pending.values():
            future.cancel()
