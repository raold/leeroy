"""Members answer him in the guild's Discord (D1 task 5). The desk hands each post of his to
on_his_post, which takes his `discord` lease on the GPU and queues the answer turns; the talk
pipeline composes, types and stores them (discord.transport), never in the game, so none of it
needs him in game.

Who answers (plan):
- a DM (`dm:<guid>`): that member, whenever she has a persona;
- a post that @mentions members by character name, or that replies to a member's message: those
  members, the one he replied to first and then the mentions in his order, up to MAX_NAMED, and
  only those the channel's audience holds (an officers' channel: its officers);
- any other channel post: one member, or two with SECOND_P, drawn from the channel's audience
  whose Discord presence is online or idle (not away, with a persona; her body in the
  game need not be online, ruling R14), weighted by warmth toward him (romance.WHISPER_WEIGHT
  and FOND_SHARE for a woman's stance, affinity, the talkative dial) times fit: CLASS_FIT for a
  class channel's own class, ROLE_FIT for raiders and officers in RAID_CHANNELS.
Each answer is a REPLY_HUMAN turn on `discord:<id>` (or the DM, to him), its meta why WHY with
DiscordTarget(reply_to=his post, thread=its thread), after a delay of 2-25 s by her presence
(answer_delay). A post's answers are one job, so each comes after the one before it.

The prompt: the turn's instruction is purpose_line, the channel's recent messages (context_lines:
that channel or thread only, so only what its audience reads, and none for a speaker outside it)
and his message, fitted under the compose's extra cap (prompt.EXTRA_CAP_TOKENS): the oldest lines
go first, and a long line or post is cut. Memory recall looks for his words (recall_query: the
message he replied to, his thread's last lines and his post; ruling R13). Both are built again
just before composing (an instruction hook), so a later answer sees the ones before it; once his
post is gone (deleted) the hook drops the turn there (TurnWithdrawn, GONE), with no model call.

The lease: on_his_post takes the arbiter's live lease LEASE as his login takes "human": the first
holder's hand-over stops the night's run and preloads the live model, and a live request waits
for it (handback, which the nightly wiring's live admission awaits). Each further post or action
of his (on_his_action) keeps it LEASE_IDLE_S longer, then it is released. His login's lease is
its own. Logs carry ids, channel ids and counts, never a text or a name."""
import asyncio
import logging
import random
import re

from leeroy_mind.arc.live import away_now
from leeroy_mind.discord import layout, owntime
from leeroy_mind.discord.channels import CHANNEL_PREFIX, is_discord, is_group
from leeroy_mind.discord.presence import RNG_SEED, Presence, local_time, presence_of
from leeroy_mind.discord.transport import DiscordTarget, channel_id
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.realm.config import RealmConfig
from leeroy_mind.talk.pipeline import TurnMeta, TurnWithdrawn
from leeroy_mind.talk.prompt import EXTRA_CAP_TOKENS
from leeroy_mind.talk.romance import FOND_SHARE, WHISPER_AFFINITY_SCALE, WHISPER_WEIGHT
from leeroy_mind.talk.select import pick_speakers
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import truncate_words
from leeroy_mind.tuning.effective import dials_of

log = logging.getLogger("leeroy_mind.discord")

LEASE = "discord"  # his Discord's lease on the GPU arbiter
LEASE_IDLE_S = 600.0
WHY = "discord:reply"
GONE = "discord_deleted_parent"  # the drop reason of an answer whose post was deleted
MAX_NAMED = 3
SECOND_P = 0.35  # a drawn answer brings a second member this often
CLASS_FIT = 4.0
ROLE_FIT = 3.0
RAID_CHANNELS = frozenset({"raid-planning", "strategy", "loot-and-dkp"})
ANSWERING = frozenset({"online", "idle"})
# the delay before an answer: BASE_DELAY_S scaled by her presence, within MIN and MAX
BASE_DELAY_S = (2.0, 10.0)
DELAY_SCALE = {"online": 1.0, "idle": 1.6, "dnd": 2.5, "offline": 2.5}
MIN_DELAY_S, MAX_DELAY_S = 2.0, 25.0
CONTEXT_LINES = 12
# Replies on her own time ([talk] dm_own_time; owntime.py): how often owed DMs are looked at,
# and the meta key of his newest DM post a member has answered (or let go)
OWED_TICK_S = 30.0
ANSWERED_KEY = "discord:dm:answered:{guid}"
# his newest DM post she deferred (on_his_post, after a photo's look): only a deferred post is
# owed, so the tick never answers a post before the desk hands it over, nor one she let be
DEFERRED_KEY = "discord:dm:deferred:{guid}"
# A late answer stays owed until hers is stored: one queued is not queued again for RETRY_S,
# and after TRIES that came to nothing (a drop, a silence) the post is let go
RETRY_S = 600.0
TRIES = 2
QUERY_LINES = 2  # his thread's last lines that go into recall's query with his post
# what the instruction may hold: the compose cuts its extra at EXTRA_CAP_TOKENS (4 characters
# a token), less room for anything else it adds
INSTRUCTION_CHARS = EXTRA_CAP_TOKENS * 4 - 100
HIS_CHARS = 500
LINE_CHARS = 200
_MENTION = re.compile(r"(?<!\w)@(\w+)")


def answer_delay(state: str, rng) -> float:
    """Seconds before a member answers: slower when idle, slowest on do not disturb (or offline,
    for a DM), always 2-25 s."""
    d = rng.uniform(*BASE_DELAY_S) * DELAY_SCALE.get(state, DELAY_SCALE["offline"])
    return min(MAX_DELAY_S, max(MIN_DELAY_S, d))


def fold(text: str) -> str:
    """A message on one line: its lines joined by " / ", so a continuation line never reads as
    another speaker's."""
    return " / ".join(part.strip() for part in text.split("\n") if part.strip())


def _clip(text: str, n: int) -> str:
    cut = truncate_words(text, n)
    return cut if cut == text else cut + "..."


def warmth(p, rel) -> float:
    """How readily a member answers him: 1, plus a woman's stance (WHISPER_WEIGHT; a fond woman
    FOND_SHARE of a lust one's) and her affinity toward him, all times her talkative dial."""
    stance = p.romance.stance if p.identity.player_gender == "female" else "none"
    attitude = (FOND_SHARE * WHISPER_WEIGHT["lust"] if stance == "fond"
                else WHISPER_WEIGHT.get(stance, 0.0))
    affinity = max(0.0, rel.affinity if rel is not None else 0.0) / WHISPER_AFFINITY_SCALE
    return (1.0 + attitude + affinity) * dials_of(p).mult("talkative")


class DiscordTalk:
    """See the module docstring. `app` gives store, clock, engine, world, personas, human_guid,
    arbiter (None: no lease), rng and realm (its cfg's trial_rank)."""

    LEASE_IDLE_S = LEASE_IDLE_S

    def __init__(self, app, *, rng: random.Random | None = None):
        self.app = app
        self.rng = rng or getattr(app, "rng", None) or random.Random()
        self._held = False
        self._arbiter = None
        self._until = 0.0
        self._timer: asyncio.Task | None = None
        self._handbacks: set[asyncio.Task] = set()
        self._owed_at = -float("inf")
        self._due: dict[int, tuple[float, bool]] = {}  # his DM post id -> (when, urgent)
        self._owes: tuple[float, frozenset[int]] = (-float("inf"), frozenset())
        self._queued: dict[int, tuple[float, int]] = {}  # his DM post -> (last queued, tries)
        engine = getattr(app, "engine", None)
        if engine is not None:
            engine.add_instruction_hook(self._instruction_hook)
        drain = getattr(app, "register_shutdown_drain", None)
        if drain is not None:
            drain(self.close)

    # -- his actions -----------------------------------------------------------------------

    def on_his_post(self, message_id: int, *, immediate: bool = False,
                    speakers=None) -> asyncio.Task | None:
        """Queue his post's answers; return their job so an asynchronous source can own it.
        `speakers` keeps only those members' answers (a retry of one owed answer)."""
        self._take_lease()
        try:
            row = self.app.store.conn.execute('SELECT channel FROM discord_message WHERE id=?',(message_id,)).fetchone()
            if row is not None and is_group(row[0]):
                groups = getattr(self.app,'groups',None)
                if groups is not None:
                    groups.his_post(self,message_id)
                return
            server = getattr(self.app, 'server', None)
            if server is not None:
                row = self.app.store.conn.execute('SELECT channel,author,deleted FROM discord_message WHERE id=?',
                                                  (message_id,)).fetchone()
                if row and row[1] == self.app.human_guid and row[2] is None:
                    server.engine.his_post(row[0])
            if not immediate and self.own_time() and self._already(message_id):
                return
            if not immediate and self._later(message_id):
                return
            items = self.plan(message_id)
            if speakers is not None:
                items = [(turn, meta) for turn, meta in items if turn.speaker in speakers]
        except Exception as error:
            log.warning("discord_plan_failed: message=%d error=%s", message_id, type(error).__name__)
            return
        if items:
            self._answered(message_id)
            return self.app.engine._queue(items)

    # -- replies on her own time -------------------------------------------------------------

    def own_time(self) -> bool:
        talk = getattr(getattr(getattr(self.app, "engine", None), "cfg", None), "talk", None)
        return bool(getattr(talk, "dm_own_time", False))

    def _already(self, message_id: int) -> bool:
        """His DM post the tick answered already (a photo's, during its look): not twice."""
        post = self._post(message_id)
        if post is None:
            return False
        done = self.app.store.get_meta(ANSWERED_KEY.format(guid=post[0]))
        if done is not None and done.isdigit() and int(done) >= message_id:
            log.info("discord_dm_already_answered: guid=%d message=%d", post[0], message_id)
            return True
        return False

    def _post(self, message_id: int):
        """(member, his post's ts) for his live DM post `message_id`; None for anything else."""
        hg = self.app.human_guid
        row = self.app.store.conn.execute(
            "SELECT channel, author, origin, ts, deleted FROM discord_message WHERE id = ?",
            (message_id,)).fetchone()
        if row is None or row[1] != hg or row[2] != "human" or row[4] is not None:
            return None
        peer = layout.dm_peer(row[0])
        if peer is None or peer == hg or self.app.personas.get(peer) is None:
            return None
        return peer, row[3]

    def _due_of(self, guid: int, message_id: int, post_ts: float) -> float:
        """When she answers his DM post (owntime.due_at), cached by post id."""
        if message_id in self._due:
            return self._due[message_id][0]
        app, conn, hg = self.app, self.app.store.conn, self.app.human_guid
        channel = f"dm:{guid}"
        # her last line to him in either medium: this DM, or a whisper (one conversation)
        hers = max((t for t in (
            conn.execute("SELECT max(ts) FROM discord_message WHERE channel = ? AND origin = 'mind' "
                         "AND author = ? AND ts <= ?", (channel, guid, post_ts)).fetchone()[0],
            conn.execute("SELECT max(ts) FROM chat_line WHERE channel = 'whisper' AND from_guid = ? "
                         "AND to_guid = ? AND ts <= ?", (guid, hg, post_ts)).fetchone()[0])
                    if t is not None), default=None)
        last_hers = conn.execute(
            "SELECT coalesce(max(id), 0) FROM discord_message WHERE channel = ? AND origin = 'mind' "
            "AND id < ?", (channel, message_id)).fetchone()[0]
        his = [r[0] for r in conn.execute(
            "SELECT text FROM discord_message WHERE channel = ? AND origin = 'human' "
            "AND deleted IS NULL AND id > ? AND id <= ?", (channel, last_hers, message_id))]
        urgent = any(owntime.URGENT_RE.search(t) for t in his)
        due = owntime.due_at(
            app.personas.get(guid), post_ts=post_ts, now=app.clock.now(),
            local_now=app.clock.local(), message_id=message_id, urgent=urgent,
            talking=hers is not None and post_ts - hers <= owntime.TALKING_S,
            insist=len(his) >= owntime.INSIST)
        self._due[message_id] = (due, urgent)
        while len(self._due) > 256:
            del self._due[next(iter(self._due))]
        return due

    def _later(self, message_id: int) -> bool:
        """With dm_own_time, whether his DM post waits for her (tick answers it then)."""
        if not self.own_time():
            return False
        post = self._post(message_id)
        if post is None:
            return False
        guid, ts = post
        wait = self._due_of(guid, message_id, ts) - self.app.clock.now()
        if wait <= MAX_DELAY_S:
            return False
        self.app.store.set_meta(DEFERRED_KEY.format(guid=guid), str(message_id))
        self._owes = (-float("inf"), frozenset())
        log.info("discord_dm_later: guid=%d message=%d wait_s=%d", guid, message_id, int(wait))
        return True

    def _answered(self, message_id: int, guid: int | None = None) -> None:
        """Her answer to his DM post `message_id` is queued (or the post is let go): the tick
        leaves it, and every post of his before it in that DM, alone. With the switch off and
        nothing deferred in that DM, nothing is written."""
        if guid is None:
            post = self._post(message_id)
            if post is None:
                return
            guid = post[0]
        deferred = self.app.store.get_meta(DEFERRED_KEY.format(guid=guid))
        if not self.own_time() and deferred is None:
            return
        self.app.store.set_meta(ANSWERED_KEY.format(guid=guid), str(message_id))
        if deferred is not None and (not deferred.isdigit() or int(deferred) <= message_id):
            self.app.store.conn.execute("DELETE FROM meta WHERE key = ?",
                                        (DEFERRED_KEY.format(guid=guid),))

    def _owed(self) -> list[tuple[int, int, float]]:
        """(member, his newest DM post, its ts) for every DM whose newest post of his is the one
        she deferred, has no answer of hers after it and is newer than the one she last answered.
        One small read when nothing is deferred."""
        app, conn, hg = self.app, self.app.store.conn, self.app.human_guid
        deferred = {}
        for key, value in conn.execute("SELECT key, value FROM meta "
                                       "WHERE key GLOB 'discord:dm:deferred:*'").fetchall():
            tail = key.rsplit(":", 1)[1]
            if tail.isdigit() and str(value).isdigit():
                deferred[int(tail)] = int(value)
        if not deferred:
            return []
        rows = conn.execute(
            "SELECT channel, max(id) FROM discord_message WHERE substr(channel, 1, 3) = 'dm:' "
            "AND origin = 'human' AND author = ? AND deleted IS NULL GROUP BY channel",
            (hg,)).fetchall()
        out = []
        for channel, mid in rows:
            guid = layout.dm_peer(channel)
            if guid is None or guid == hg or app.personas.get(guid) is None:
                continue
            if deferred.get(guid) != mid:
                continue  # not deferred, or his newest post is not handed over yet
            done = app.store.get_meta(ANSWERED_KEY.format(guid=guid))
            if done is not None and done.isdigit() and int(done) >= mid:
                continue
            # her answer to this post or a later one of his; her answer to an earlier post that
            # lands after this one (still typing when he wrote) is no answer to it
            if conn.execute("SELECT 1 FROM discord_message WHERE channel = ? AND id > ? "
                            "AND origin = 'mind' AND (reply_to IS NULL OR reply_to >= ?) LIMIT 1",
                            (channel, mid, mid)).fetchone():
                conn.execute("DELETE FROM meta WHERE key = ?", (DEFERRED_KEY.format(guid=guid),))
                self._queued.pop(mid, None)
                continue
            ts = conn.execute("SELECT ts FROM discord_message WHERE id = ?", (mid,)).fetchone()[0]
            out.append((guid, mid, ts))
        return out

    def owes(self, guid: int) -> bool:
        """Whether she owes him an answer to a DM she has not seen yet (DmOpeners waits)."""
        if not self.own_time():
            return False
        now = self.app.clock.now()
        if now - self._owes[0] > 10.0:  # one look serves a whole opener pass
            try:
                self._owes = (now, frozenset(g for g, _, _ in self._owed()))
            except Exception:
                self._owes = (now, frozenset())
        return guid in self._owes[1]

    def _model_ready(self) -> bool:
        """A deferred answer never takes the GPU from the night: it goes out while a live
        lease (his login, the server's, his Discord's, voice, a raid) already holds the model."""
        arbiter = getattr(self.app, "arbiter", None)
        return arbiter is None or bool(arbiter.holders)

    def tick(self, now: float) -> None:
        """Every OWED_TICK_S: answer each DM she deferred whose moment has come, and let one
        older than LET_GO_S go. With the switch turned off, what she deferred is answered at once
        instead of stranded (restarts included: the deferral is in meta)."""
        if now - self._owed_at < OWED_TICK_S:
            return
        on = self.own_time()
        self._owed_at = now
        groups = getattr(self.app,'groups',None)
        if groups is not None:
            groups.tick(self,now)
        try:
            owed, away = self._owed(), away_now(self.app.store, now)
        except Exception as e:
            log.warning("discord_owed_failed: reason=%s", type(e).__name__)
            return
        for guid, mid, ts in owed:
            try:
                self._tick_one(now, guid, mid, ts, on, away)
            except Exception as e:  # one DM never stops the others
                log.warning("discord_owed_one_failed: guid=%d message=%d reason=%s", guid, mid,
                            type(e).__name__)

    def _tick_one(self, now, guid, mid, ts, on, away) -> None:
        if not on:
            urgent, due = True, now  # stranded by the switch: answered as the switch now says
        else:
            if now - ts > owntime.LET_GO_S:
                self._answered(mid, guid)
                log.info("discord_dm_let_go: guid=%d message=%d", guid, mid)
                return
            due = self._due_of(guid, mid, ts)
            urgent = self._due[mid][1]
        if guid in away or now < due:
            return
        last, tries = self._queued.get(mid, (-float("inf"), 0))
        if now - last < RETRY_S:
            return  # queued already: owed until her answer is stored
        if tries >= TRIES:
            self._queued.pop(mid, None)
            self._answered(mid, guid)
            log.info("discord_dm_let_go: guid=%d message=%d tries=%d", guid, mid, tries)
            return
        if not urgent and not self._model_ready():
            return
        items = self.plan(mid)
        if not items:
            self._answered(mid, guid)
            return
        self._queued[mid] = (now, tries + 1)
        while len(self._queued) > 256:
            del self._queued[next(iter(self._queued))]
        # Its own lease keeps the model for the answer even if the one that admitted it ends;
        # held beside another, it hands nothing over (the night is not woken)
        self._take_lease()
        self.app.engine._queue(items)
        log.info("discord_dm_answered_later: guid=%d message=%d late_s=%d", guid, mid,
                 int(now - ts))

    def on_his_action(self) -> None:
        """Any other action of his (a reaction, an edit, a pin, a thread): a lease he holds is
        kept LEASE_IDLE_S from now; with none, nothing is taken. An edit may add or drop urgent
        words, so the cached answer moments are read again."""
        self._due.clear()
        self._owes = (-float("inf"), frozenset())
        groups = getattr(self.app,'groups',None)
        if groups is not None:
            groups.refresh_due()
        if self._held:
            self._until = self.app.clock.now() + LEASE_IDLE_S

    def begin_photo(self) -> None:
        """Pin the live model before the post's asynchronous vision step."""
        self._take_lease()

    def _photos(self, conn, ids):
        if not getattr(self.app.engine, 'photos_enabled', False):
            return {}
        from .store import attachments
        return attachments(conn, ids)

    # -- who answers -----------------------------------------------------------------------

    def plan(self, message_id: int, *, group_chosen=None) -> list[tuple[Turn, TurnMeta]]:
        """The answer turns for his post `message_id` (see the module docstring); none for a
        message that is not his, is deleted or is on a channel the layout lacks."""
        app, store = self.app, self.app.store
        hg = app.human_guid
        replied = None
        with store.transaction():
            row = store.conn.execute(
                "SELECT channel, thread, author, origin, text, reply_to, deleted "
                "FROM discord_message WHERE id = ?", (message_id,)).fetchone()
            if row is not None and row[5] is not None:
                parent = store.conn.execute(
                    "SELECT author, origin, deleted FROM discord_message WHERE id = ?",
                    (row[5],)).fetchone()
                if parent is not None and parent[1] == "mind" and parent[2] is None:
                    replied = parent[0]
        if (row is None or not hg or row[6] is not None or row[3] != "human"
                or row[2] != hg):
            return []
        channel, thread, text = row[0], row[1], row[4]
        peer = layout.dm_peer(channel)
        group_named = frozenset()
        if is_group(channel):
            groups = getattr(app,'groups',None)
            if groups is None or not groups.config.enabled:
                return []
            place = channel
            audience = groups.readers(channel)
            from leeroy_mind.groups.audience import message_readers, visible
            prior = message_readers(store.conn,message_id)
            if hg not in audience or not visible(prior,channel,speaker=hg,to_guid=0,readers=audience):
                return []
            chosen,how = self._named(text,replied,audience),'named'
            group_named = frozenset(chosen)
            if group_chosen is not None:
                how = 'named' if chosen else 'drawn'
                chosen = [g for g in group_chosen if g in audience and g != hg and app.personas.get(g)]
            elif not chosen:
                chosen,how = self._draw(layout.Channel(channel,'Group chat','groups',''),audience),'drawn'
        elif peer is not None:
            place, how = channel, "dm"
            chosen = [peer] if peer != hg and app.personas.get(peer) is not None else []
        else:
            ch = layout.channel(channel)
            if ch is None:
                return []
            place = CHANNEL_PREFIX + channel
            audience = app.world.audience(place, from_guid=hg)
            chosen, how = self._named(text, replied, audience), "named"
            if not chosen:
                chosen, how = self._draw(ch, audience), "drawn"
        local = app.clock.local()
        items = []
        from leeroy_mind.talk.select import classify_human_line
        for g in chosen:
            p = app.personas.get(g)
            turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=g, channel=place,
                        to_guid=hg if peer is not None else 0, addressee=hg, trigger_line=None,
                        priority=Priority.REPLY_HUMAN,
                        delay=answer_delay(self._presence(g, p, local).state, self.rng))
            turn.instruction = self.instruction(g, place, message_id, thread) or ""
            meta = TurnMeta(app.engine._new_id(), answers_human=True, why=WHY,
                            discord=DiscordTarget(reply_to=message_id, thread=thread),
                            discord_named=g in group_named if is_group(channel) else how == "named",
                            first_question_answer=not items and classify_human_line(text) == "question_all",
                            recall_query=self.recall_query(message_id) or "")
            requests = getattr(getattr(app, 'photos', None), 'requests', None)
            if requests is not None:
                requests.decorate(turn, meta, message_id)
            items.append((turn, meta))
        log.info("discord_answers: message=%d channel=%s how=%s speakers=%s", message_id,
                 channel, how, ",".join(str(g) for g in chosen) or "-")
        return items

    def _named(self, text: str, replied: int | None, audience) -> list[int]:
        """The members he addressed: the author of the message he replied to, then those he
        @mentioned, in the channel's audience, with a persona, up to MAX_NAMED."""
        hg, personas = self.app.human_guid, self.app.personas
        names = {m.name.casefold(): g for g, m in self.app.world.discord_members().items()
                 if g != hg}
        out = [] if replied is None else [replied]
        out += [g for m in _MENTION.finditer(text)
                if (g := names.get(m.group(1).casefold())) is not None]
        return [g for g in dict.fromkeys(out) if g != hg and g in audience
                and personas.get(g) is not None][:MAX_NAMED]

    def _draw(self, ch: layout.Channel, audience) -> list[int]:
        app = self.app
        world, hg = app.world, app.human_guid
        away = away_now(app.store, app.clock.now())
        local = app.clock.local()
        weights: dict[int, float] = {}
        for g in sorted(audience):
            if g == hg or g in away:
                continue
            p = app.personas.get(g)
            if p is None or self._presence(g, p, local).state not in ANSWERING:
                continue
            w = warmth(p, app.store.get_relationship(g, hg, lens=True)) * self._fit(ch, g, p)
            if w > 0:
                weights[g] = w
        n = 2 if len(weights) > 1 and self.rng.random() < SECOND_P else 1
        return pick_speakers(weights, n, self.rng)

    def _fit(self, ch: layout.Channel, g: int, p) -> float:
        world = self.app.world
        m = world.discord_members().get(g)
        fit = 1.0
        if m is not None and layout.CLASS_CHANNEL.get(m.class_) == ch.id:
            fit *= CLASS_FIT
        if ch.id in RAID_CHANNELS and (world.is_officer(g) or (m is not None and layout.role_of(
                m, p, trial_rank=self._trial_rank()) in ("raider", "officer"))):
            fit *= ROLE_FIT
        return fit

    def _presence(self, g: int, p, local) -> Presence:
        world = self.app.world
        m = world.discord_members().get(g)
        return presence_of(p, local=local_time(local, p.identity.region),
                           body_online=g in world.online, body_zone=m.zone if m else 0,
                           rng_seed=RNG_SEED,
                           in_his_group=world.human_online and world.in_group(g))

    def _trial_rank(self) -> int:
        rank = getattr(getattr(getattr(self.app, "realm", None), "cfg", None), "trial_rank",
                       None)
        return rank if type(rank) is int else RealmConfig().trial_rank

    # -- the prompt ------------------------------------------------------------------------

    def purpose_line(self, channel: str) -> str:
        """Where the member is, for a store channel id ("general", "dm:<guid>")."""
        if is_group(channel):
            return 'You are in a small private group chat with the people present. Keep this conversation within this group; no romance.'
        if layout.dm_peer(channel) is not None:
            return f"You are in a private message with {self.app.engine.human.called}"
        ch = layout.channel(channel)
        if ch is None:
            return "You are on the guild's Discord"
        return f"You are in #{ch.name} on the guild's Discord: {ch.topic}"

    def context_lines(self, conn, channel: str, thread: int | None, *,
                      limit: int = CONTEXT_LINES, exclude: int = 0) -> list[str]:
        """The latest `limit` messages of a store channel's feed (or of `thread`) as "Name: text",
        oldest first, each on one line (fold), deleted messages left out."""
        rows = conn.execute(
            "SELECT author, text, id FROM discord_message WHERE channel = ? AND thread IS ? "
            "AND deleted IS NULL AND id != ? "
            "AND (turn IS NULL OR substr(turn,1,19)!='realism_correction:') ORDER BY id DESC LIMIT ?",
            (channel, thread, exclude, limit)).fetchall()
        if is_group(channel):
            from leeroy_mind.groups.audience import message_readers
            current = self.app.groups.readers(channel)
            rows = [r for r in rows if current and current <= message_readers(conn,r[2])]
        from leeroy_mind.photos.notes import by_messages
        photos = by_messages(conn, [r[2] for r in rows],
            uploads=getattr(self.app.engine,'photos_enabled',False),
            members=getattr(self.app.engine,'photos_send',False),
            memes=getattr(self.app.engine,'culture_memes',False))
        return [f"{self._name(a)}: {fold(t)}" + (' ' + photos[mid] if mid in photos else '')
                for a, t, mid in reversed(rows)]

    def _name(self, g: int) -> str:
        app = self.app
        if g == 0:
            return "a server bot"  # culture.bots' posts: author 0, never a member
        if g == app.human_guid:
            return app.engine.human.called
        m = app.world.discord_members().get(g)
        if m is not None:
            return m.name
        p = app.personas.get(g)
        return p.character_name if p is not None else "someone"

    def instruction(self, speaker: int, place: str, message: int,
                    thread: int | None) -> str | None:
        """The answer turn's instruction for `speaker` on `place` (a turn channel), answering
        his message `message`; None once it is gone or deleted. A speaker outside the channel's
        audience sees none of its messages."""
        app, store = self.app, self.app.store
        cid = channel_id(place)
        reads = speaker in app.world.audience(place, from_guid=speaker, to_guid=app.human_guid)
        if not reads:
            return None
        with store.transaction():
            row = store.conn.execute("SELECT text, deleted, ts FROM discord_message WHERE id = ? AND channel = ?",
                                     (message, cid)).fetchone()
            if row is None or row[1] is not None:
                return None
            if is_group(cid):
                from leeroy_mind.groups.audience import message_readers, visible
                if not visible(message_readers(store.conn,message),place,speaker=speaker,to_guid=0,
                               readers=app.groups.readers(place)):
                    return None
            lines = self.context_lines(store.conn, cid, thread, exclude=message) if reads else []
            photo = self._photos(store.conn, [message]).get(message) if reads else None
        their = app.engine.human.forms[2]
        purpose = self.purpose_line(cid)
        tail = f"Answer {their} message: {_clip(fold(row[0]), HIS_CHARS)}"
        if getattr(app,'voice_notes',None) is not None and app.voice_notes.config.enabled:
            from leeroy_mind.voice_notes.service import rows as voice_rows, failure
            note=voice_rows(store.conn,[message]).get(message)
            if note is not None and not note['transcript_ok']:
                tail=failure(app.engine.human.forms)
        late = ""
        if (layout.dm_peer(cid) is not None or is_group(cid)) and self.own_time():
            p = app.personas.get(speaker)
            if p is not None:
                late = owntime.late_note(p, post_ts=row[2], now=app.clock.now(),
                                         local_now=app.clock.local(), forms=app.engine.human.forms)
        if photo is not None:
            subject, _, possessive = app.engine.human.forms
            verb, says = ('are', 'say') if subject == 'they' else ('is', 'says')
            ownership = (f'{subject.capitalize()} posted a photo; it is {possessive} photo; '
                         f'do not assume {subject} {verb} in it unless {subject} {says} so. ')
            label = 'What it shows: '
            answer = f'Answer {their} message: '
            message_text = truncate_words(fold(row[0]), HIS_CHARS)
            look = photo['look'] or "You can't make it out well."
            # Drop look detail first, then the caption. Ownership is never clipped,
            # including when the engine applies its final instruction budget.
            room = max(0, INSTRUCTION_CHARS - len(purpose) - len(ownership)
                       - len(label) - len(answer) - 2)
            look = truncate_words(look, max(0, room - len(message_text)))
            message_text = truncate_words(message_text, max(0, room - len(look)))
            tail = ownership + label + look + '\n' + answer + message_text
        tail = f"{late}\n{tail}" if late else tail
        lines = [_clip(x, LINE_CHARS) for x in lines]
        while lines and len("\n".join([purpose, "Recent messages:", *lines, tail])) > (
                INSTRUCTION_CHARS):
            lines.pop(0)
        head = [purpose, "Recent messages:\n" + "\n".join(lines)] if lines else [purpose]
        return "\n".join([*head, tail])

    def recall_query(self, message: int) -> str | None:
        """What memory recall looks for in an answer to his post `message`: the message it
        replies to, the last QUERY_LINES of its thread before it, then its own text, each on one
        line (ruling R13); None once it is gone or deleted. All of it is from the post's own
        channel, so the speaker's audience reads it."""
        store = self.app.store
        with store.transaction():
            row = store.conn.execute(
                "SELECT channel, thread, text, reply_to, deleted FROM discord_message "
                "WHERE id = ?", (message,)).fetchone()
            if row is None or row[4] is not None:
                return None
            channel, thread, text, reply_to = row[0], row[1], row[2], row[3]
            if is_group(channel):
                from leeroy_mind.groups.audience import message_readers
                current = self.app.groups.readers(channel)
                if not current or not current <= message_readers(store.conn,message):
                    return None
                prior = store.conn.execute('SELECT id,text FROM discord_message WHERE channel=? AND thread IS ? '
                    'AND deleted IS NULL AND id<? ORDER BY id DESC LIMIT ?', (channel,thread,message,QUERY_LINES)).fetchall()
                return ' '.join(fold(t) for mid,t in reversed(prior) if current <= message_readers(store.conn,mid))+' '+fold(text)
            parts = []
            if reply_to is not None:
                parts += [t for (t,) in store.conn.execute(
                    "SELECT text FROM discord_message WHERE id = ? AND channel = ? "
                    "AND deleted IS NULL", (reply_to, channel)).fetchall()]
            if thread is not None:
                parts += [t for (t,) in reversed(store.conn.execute(
                    "SELECT text FROM discord_message WHERE channel = ? AND thread = ? "
                    "AND id < ? AND id != ? AND deleted IS NULL ORDER BY id DESC LIMIT ?",
                    (channel, thread, message, reply_to or 0, QUERY_LINES)).fetchall())]
        return " ".join(fold(t) for t in [*parts, text])

    def _instruction_hook(self, turn: Turn, meta: TurnMeta, previous: str) -> str:
        """The engine's last step before composing: an answer turn's instruction, and its
        recall query, built again from the store as it is now. His post gone (deleted), the
        turn is dropped (TurnWithdrawn, GONE) before the model is asked."""
        target = meta.discord
        if (meta.why != WHY or target is None or target.reply_to is None
                or not is_discord(turn.channel)):
            return previous
        fresh = self.instruction(turn.speaker, turn.channel, target.reply_to, target.thread)
        query = self.recall_query(target.reply_to)
        if fresh is None or query is None:
            raise TurnWithdrawn(GONE)
        meta.recall_query = query
        return fresh

    # -- the lease -------------------------------------------------------------------------

    def _take_lease(self) -> None:
        arbiter = getattr(self.app, "arbiter", None)
        if arbiter is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("discord_lease_skipped: reason=no_event_loop")
            return  # no timer could release a lease taken here
        self._until = self.app.clock.now() + LEASE_IDLE_S
        if not self._held:
            if getattr(arbiter, 'paused', False):
                return
            first = arbiter.hold(LEASE)
            self._held, self._arbiter = True, arbiter
            if first:
                task = loop.create_task(arbiter.hand_over(), name="discord-gpu-handback")
                self._handbacks.add(task)
                task.add_done_callback(self._handback_done)
            log.info("discord_lease_taken: first=%s", first)
        if self._timer is None or self._timer.done():
            self._timer = loop.create_task(self._expire(), name="discord-lease")

    async def _expire(self) -> None:
        clock = self.app.clock
        while (left := self._until - clock.now()) > 0:
            await clock.sleep(left)
        await self._release()

    async def _release(self) -> None:
        arbiter, self._arbiter, self._held = self._arbiter, None, False
        if arbiter is not None:
            await arbiter.release_live(LEASE)
            log.info("discord_lease_released")

    def _handback_done(self, task: asyncio.Task) -> None:
        """A failed hand-over keeps the lease (the arbiter's rule); the live model's first
        request loads it. Logged here."""
        self._handbacks.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            log.warning("discord_handback_failed: error=%s", type(e).__name__)

    async def handback(self) -> None:
        """Wait for the hand-overs his posts started (the live admission's wait)."""
        while self._handbacks:
            await asyncio.wait(set(self._handbacks))

    async def close(self) -> None:
        """The app's shutdown drain: the lease timer and any hand-over stop, and a lease still
        held is released."""
        arbiter = self._arbiter or getattr(self.app, "arbiter", None)
        tasks = [t for t in (self._timer, *self._handbacks) if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._timer = None
        if self._held:
            await self._release()
        settled = getattr(arbiter, "settled", None)
        if settled is not None:
            await settled()
