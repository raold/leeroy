"""His Discord actions: the control endpoint's `discord` op (endpoint.discord_args checks and
cleans the arguments), then DiscordDesk.handle, which writes through discord.store as him, origin
human. None of it needs him in game. Logs carry the action, the channel id and the outcome, never
a text, an emoji or a name.

His post is also a chat line of his, as the members' answers are (ruling R7): origin human, on the
talk's channel (`discord:<id>`, a DM's `dm:<guid>` as it is), to a DM's member, with the world's
audience for it, stored with the message in one transaction. The app threads it
(MindApp.on_his_discord_line), so threads, the secrecy he asks for (memory.privacy), confide and
the night read both sides of the talk, a DM exactly as a whisper. It has no session and moves no
game clock: no world history, no bystander clock, no AFK or login quiet (ruling R11), and nothing
of it reaches the game. Meta LINE_KEY links the post to its line.

His edit of a post edits that line too, so an edit that asks for secrecy makes the thread secret;
an edit never loosens it: one that drops the line's ask (memory.privacy.DONT_TELL_RE) leaves the
line as she read it. His delete leaves the line: she may have read it, and a delete does not
unsay it. A post from before these lines has none, and its edit changes only the message."""
import asyncio
import logging
from pathlib import Path

from leeroy_mind.discord import layout
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.channels import CHANNEL_PREFIX
from leeroy_mind.discord.store import DiscordRefused
from leeroy_mind.logs import log as log_event
from leeroy_mind.memory.privacy import DONT_TELL_RE
from leeroy_mind.protocol import Chat

LINE_KEY = "discord:line:{}"  # meta: his post's id -> the id of its chat line

STOPPING = "The mind is stopping."
NO_HUMAN = "The mind doesn't know who you are yet."
NOT_MEMBER = "That member isn't in the guild's cast, so they can't be messaged."
NO_PERSONA = "That member has no persona, so they can't write back."

WORDS = {
    "empty": "There's nothing to send.",
    "too_long": "That's too long (2000 characters at most).",
    "deleted": "That message was deleted.",
    "not_yours": "That message isn't yours.",
    "not_emoji": "That isn't a single emoji.",
    "too_many": "That message has 20 different reactions already.",
    "too_many_pins": "That channel has 50 pinned messages already.",
    "exists": "That message already has a thread.",
    "bad_name": "A thread name is 1 to 60 characters.",
    "no_parent": "The message you are replying to isn't in this channel.",
    "deleted_parent": "The message you are replying to was deleted.",
    "missing": "There is no such message.",
    "no_thread": "That thread isn't in this channel.",
    "bad_origin": "That message can't be posted that way.",
    "bad_channel": "There is no such channel.",
}


class _Refused(Exception):
    def __init__(self, outcome: str, words: str):
        super().__init__(words)
        self.outcome = outcome


class DiscordDesk:
    """The running mind's side of the `discord` op (endpoint.ControlEndpoint.discord runs one at
    a time). {"ok": True[, "id"]} or {"ok": False, "error"}, in the console's words."""

    def __init__(self, app):
        self.app = app
        from leeroy_mind.photos.config import PhotosConfig, load_photos_config
        self.photos = (load_photos_config(Path(app.data_dir) / 'mind.toml')
                       if getattr(app, 'data_dir', None) is not None else PhotosConfig())
        from leeroy_mind.voice_notes.service import VoiceNotes
        app.voice_notes = VoiceNotes(app)
        if app.voice_notes.config.enabled:
            if (engine:=getattr(app,'engine',None)) is not None:
                engine.voice_notes = app.voice_notes
            if (drain := getattr(app,'register_shutdown_drain',None)) is not None:
                drain(app.voice_notes.close)
        self._photo_posts: list[float] = []
        self._photo_tasks: set[asyncio.Task] = set()
        self._photo_memories: set[tuple[int, int]] = set()
        if self.photos.enabled:
            if (engine := getattr(app, 'engine', None)) is not None:
                engine.photos_enabled = True
                engine.remember_discord_photo = self.remember_photo
            if (drain := getattr(app, 'register_shutdown_drain', None)) is not None:
                drain(self.close)

    async def handle(self, args: dict) -> dict:
        action, channel = args.get("action"), args.get("channel", "")
        try:
            reply = self._handle(args)
            outcome = "ok"
        except DiscordRefused as e:
            reply = {"ok": False, "error": WORDS.get(e.code, "That can't be done.")}
            outcome = e.code
        except _Refused as e:
            reply, outcome = {"ok": False, "error": str(e)}, e.outcome
        finally:
            store = getattr(self.app, "store", None)
            if store is not None and not store.in_unit:
                store.conn.commit()  # end a read outside a unit, so it never pins a snapshot
        log_event("discord", action=action, channel=channel, message=args.get("message"),
                  outcome=outcome)
        return reply

    def _handle(self, args: dict) -> dict:
        app = self.app
        if app.stopping:
            raise _Refused("stopping", STOPPING)
        me = app.human_guid
        if not (type(me) is int and me > 0):
            raise _Refused("no_human", NO_HUMAN)
        store, now, action = app.store, app.clock.now(), args["action"]
        if action == "post":
            self._check_dm(args["channel"], me)
            if 'voice' in args:
                from leeroy_mind.voice_notes.files import AudioRefused, WORDS as AUDIO_WORDS
                try:
                    return app.voice_notes.human_post(args,self)
                except AudioRefused as error:
                    raise _Refused('voice_note_'+error.code,AUDIO_WORDS.get(error.code,'The recording could not be saved.')) from None
            image = args.get('image')
            attachment = None
            if image is not None:
                from leeroy_mind.photos import uploads
                if not self.photos.enabled:
                    raise _Refused('photos_off', uploads.WORDS['off'])
                self._photo_posts = [t for t in self._photo_posts if now - t < 60]
                if len(self._photo_posts) >= uploads.MINUTE_MAX:
                    raise _Refused('photos_minute', uploads.WORDS['minute'])
                # Not externally visible: adopt fills this row before commit.
                attachment = dstore.AttachmentIn('upload', 'discord/images/pending', 16, 16, 1)
            with store.transaction():
                if image is not None:
                    day = app.clock.local().date().isoformat()
                    count = uploads.day_count(store.conn, day)
                    if count >= uploads.DAY_MAX:
                        raise _Refused('photos_day', uploads.WORDS['day'])
                mid = dstore.post(store, channel=args["channel"], author=me, origin="human",
                                  text=args["text"], reply_to=args.get("reply_to"),
                                  thread=args.get("thread"), now=now, attachment=attachment)
                if image is not None:
                    from leeroy_mind.photos.images import adopt, ImageRefused
                    try:
                        path, w, h, size = adopt(app.data_dir, image, mid)
                    except ImageRefused as error:
                        raise _Refused('photos_' + error.code, uploads.WORDS[error.code]) from None
                    store.conn.execute('UPDATE discord_attachment SET path=?,width=?,height=?,bytes=? '
                                       'WHERE message=?', (path, w, h, size, mid))
                    store.set_meta(uploads.DAY_KEY.format(day), str(count + 1))
                line, chat = self._his_line(mid, me, now)
                server = getattr(app, 'server', None)
                if server is not None and (peer := layout.dm_peer(args['channel'])) is not None:
                    store.conn.execute('DELETE FROM meta WHERE key=?', (f'server:dm:open:{peer}',))
            if server is not None:
                # Stop immediately, even when vision delays the existing D1 answer hook.
                server.engine.his_post(args['channel'])
                server.dms.his_post(args['channel'])
                server.reactions.after_post(mid, author=me, channel=args['channel'], text=args['text'], his=True)
            if image is not None:
                self._photo_posts.append(now)
            try:
                app.on_his_discord_line(line, chat, now)
            except Exception as e:  # the post is stored: its answers still come
                log_event("discord_thread_failed", level=logging.WARNING, message=mid, line=line,
                          error=type(e).__name__)
            hook = getattr(app, "discord_talk", None)
            if hook is not None:
                if image is not None and hasattr(hook, 'begin_photo'):
                    # The control request has a 10s deadline; vision runs after commit.
                    hook.begin_photo()
                    self._spawn_photo(self._look_then_answer(mid))
                else:
                    hook.on_his_post(mid)
            return {"ok": True, "id": mid}
        message = args["message"]
        if action == "thread":
            thread = dstore.open_thread(store, message, name=args["name"], now=now)
            self._his_action()
            return {"ok": True, "id": thread}
        if action == "react":
            dstore.react(store, message, member=me, emoji=args["emoji"], now=now)
        elif action == "unreact":
            dstore.unreact(store, message, member=me, emoji=args["emoji"], now=now)
        elif action == "edit":
            with store.transaction():
                dstore.edit(store, message, author=me, text=args["text"], now=now)
                self._edit_line(message)
        elif action == "delete":
            dstore.delete(store, message, author=me, now=now)
        elif action == "pin":
            dstore.pin(store, message, now=now)
        else:
            dstore.unpin(store, message)
        self._his_action()
        return {"ok": True}

    def _spawn_photo(self, work, *, memory=None) -> None:
        task = asyncio.create_task(work, name='discord-photo')
        self._photo_tasks.add(task)
        task.add_done_callback(self._photo_done)
        if memory is not None:
            task.add_done_callback(lambda _: self._photo_memories.discard(memory))

    def _photo_done(self, task) -> None:
        self._photo_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log_event('discord_photo_failed', level=logging.WARNING, code=type(error).__name__)

    async def _look_then_answer(self, message: int) -> None:
        from leeroy_mind.imagefiles import read_image
        from leeroy_mind.photos.look import look_at
        app = self.app
        with app.store.transaction():
            row = dstore.attachments(app.store.conn, [message]).get(message)
        if row is None:
            return
        data = await asyncio.to_thread(read_image, app.data_dir / row['path'], app.data_dir)
        look = await look_at(app, data) if data is not None else None
        if look:
            dstore.set_look(app.store, message, look, now=app.clock.now())
        if not app.stopping:
            app.discord_talk.on_his_post(message)

    def remember_photo(self, member: int, message: int) -> None:
        if not self.photos.enabled or (member, message) in self._photo_memories:
            return
        with self.app.store.transaction():
            photo = dstore.attachments(self.app.store.conn, [message]).get(message)
        if photo is None or photo['kind'] != 'upload' or not photo['look']:
            return
        self._photo_memories.add((member, message))
        self._spawn_photo(self._remember_photo(member, message), memory=(member, message))

    async def _remember_photo(self, member: int, message: int) -> None:
        from leeroy_mind.memory.writer import MemoryCandidate
        from leeroy_mind.photos.look import sentences
        from leeroy_mind.text import truncate_words
        app = self.app
        with app.store.transaction():
            photo = dstore.attachments(app.store.conn, [message]).get(message)
            row = app.store.conn.execute('SELECT author,channel,ts FROM discord_message WHERE id=?',
                                         (message,)).fetchone()
            evidence = app.store.get_meta(LINE_KEY.format(message))
        if not photo or not photo['look'] or row is None or row['author'] != app.human_guid or not evidence:
            return
        peer = layout.dm_peer(row['channel'])
        if peer is not None and peer != member:
            return
        place = row['channel'] if peer else CHANNEL_PREFIX + row['channel']
        if member not in app.world.audience(place, from_guid=app.human_guid, to_guid=peer or 0):
            return
        prefix = (f'{app.engine.human.called} sent me a photo: ' if peer else
                  f"{app.engine.human.called} posted a photo in #{row['channel']}: ")
        await app.writer.write(MemoryCandidate(owner=member, kind='observation',
            text=truncate_words(prefix + sentences(photo['look'])[0], 200), t_event=int(row['ts']),
            actors=(app.human_guid,), importance=4, valence=0., source='chat',
            evidence=(f'chat:{evidence}',), key=f'discord_photo:{message}'))

    async def close(self) -> None:
        tasks = list(self._photo_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _his_line(self, message: int, me: int, now: float) -> tuple[int, Chat]:
        """His stored post `message` as his chat line (the module docstring), in the caller's
        transaction; its id and the Chat it was stored from."""
        store = self.app.store
        channel, text = store.conn.execute(
            "SELECT channel, text FROM discord_message WHERE id = ?", (message,)).fetchone()
        peer = layout.dm_peer(channel)
        place, to = (channel, peer) if peer is not None else (CHANNEL_PREFIX + channel, 0)
        chat = Chat(ts=int(now * 1000), channel=place, from_guid=me,
                    from_name=store.get_meta("human_name") or "", to_guid=to, text=text, map=0,
                    zone=0)
        line = store.insert_chat_line(
            chat, session_id=None, origin="human", ts=now,
            audience=self.app.world.audience(place, from_guid=me, to_guid=to))
        store.set_meta(LINE_KEY.format(message), str(line))
        return line, chat

    def _edit_line(self, message: int) -> None:
        """His edited post `message`'s chat line takes its new text, in the caller's transaction
        (the module docstring): never when that would drop the line's ask for secrecy."""
        store = self.app.store
        line = store.get_meta(LINE_KEY.format(message))
        if line is None:
            return  # a post from before the lines
        (text,) = store.conn.execute("SELECT text FROM discord_message WHERE id = ?",
                                     (message,)).fetchone()
        row = store.conn.execute("SELECT text FROM chat_line WHERE id = ?", (int(line),)).fetchone()
        if row is None or (DONT_TELL_RE.search(row[0]) and not DONT_TELL_RE.search(text)):
            return
        store.conn.execute("UPDATE chat_line SET text = ? WHERE id = ?", (text, int(line)))

    def _his_action(self) -> None:
        """An action of his other than a post keeps his Discord lease (discord.talk)."""
        touch = getattr(getattr(self.app, "discord_talk", None), "on_his_action", None)
        if touch is not None:
            touch()

    def _check_dm(self, channel: str, me: int) -> None:
        peer = layout.dm_peer(channel)
        if peer is None:
            return
        conn = self.app.store.conn
        from leeroy_mind.personas.tiers import is_cast
        if not is_cast(conn,peer):
            raise _Refused("not_member", NOT_MEMBER)
        if peer == me or not conn.execute("SELECT 1 FROM cast_member WHERE guid = ?",
                                          (peer,)).fetchall():
            raise _Refused("not_member", NOT_MEMBER)
        if not conn.execute("SELECT 1 FROM persona_core WHERE guid = ?", (peer,)).fetchall():
            raise _Refused("no_persona", NO_PERSONA)
