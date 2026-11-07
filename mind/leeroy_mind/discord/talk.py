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
  whose Discord presence is online or idle and whom the game can voice now (online, not AFK, not
  away, with a persona), weighted by warmth toward him (romance.WHISPER_WEIGHT and FOND_SHARE
  for a woman's stance, affinity, the talkative dial) times fit: CLASS_FIT for a class channel's
  own class, ROLE_FIT for raiders and officers in RAID_CHANNELS.
Each answer is a REPLY_HUMAN turn on `discord:<id>` (or the DM, to him), its meta why WHY with
DiscordTarget(reply_to=his post, thread=its thread), after a delay of 2-25 s by her presence
(answer_delay). A post's answers are one job, so each comes after the one before it.

The prompt: the turn's instruction is purpose_line, the channel's recent messages (context_lines:
that channel or thread only, so only what its audience reads, and none for a speaker outside it)
and his message, fitted under the compose's extra cap (prompt.EXTRA_CAP_TOKENS): the oldest lines
go first, and a long line or post is cut. It is built again just before composing (an
instruction hook), so a later answer sees the ones before it.

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
from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import CHANNEL_PREFIX, is_discord
from leeroy_mind.discord.presence import RNG_SEED, Presence, local_time, presence_of
from leeroy_mind.discord.transport import DiscordTarget, channel_id
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.realm.config import RealmConfig
from leeroy_mind.talk.pipeline import TurnMeta
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
        engine = getattr(app, "engine", None)
        if engine is not None:
            engine.add_instruction_hook(self._instruction_hook)
        drain = getattr(app, "register_shutdown_drain", None)
        if drain is not None:
            drain(self.close)

    # -- his actions -----------------------------------------------------------------------

    def on_his_post(self, message_id: int) -> None:
        """His post was stored: take or keep the lease, then queue the answers."""
        self._take_lease()
        try:
            items = self.plan(message_id)
        except Exception:
            log.exception("discord_plan_failed: message=%d", message_id)
            return
        if items:
            self.app.engine._queue(items)

    def on_his_action(self) -> None:
        """Any other action of his (a reaction, an edit, a pin, a thread): a lease he holds is
        kept LEASE_IDLE_S from now; with none, nothing is taken."""
        if self._held:
            self._until = self.app.clock.now() + LEASE_IDLE_S

    # -- who answers -----------------------------------------------------------------------

    def plan(self, message_id: int) -> list[tuple[Turn, TurnMeta]]:
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
        if peer is not None:
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
        for g in chosen:
            p = app.personas.get(g)
            turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=g, channel=place,
                        to_guid=hg if peer is not None else 0, addressee=hg, trigger_line=None,
                        priority=Priority.REPLY_HUMAN,
                        delay=answer_delay(self._presence(g, p, local).state, self.rng))
            turn.instruction = self.instruction(g, place, message_id, thread) or ""
            meta = TurnMeta(app.engine._new_id(), answers_human=True, why=WHY,
                            discord=DiscordTarget(reply_to=message_id, thread=thread))
            items.append((turn, meta))
        log.info("discord_answers: message=%d channel=%s how=%s speakers=%s", message_id,
                 channel, how, ",".join(str(g) for g in chosen) or "-")
        return items

    def _named(self, text: str, replied: int | None, audience) -> list[int]:
        """The members he addressed: the author of the message he replied to, then those he
        @mentioned, in the channel's audience, with a persona, up to MAX_NAMED."""
        hg, personas = self.app.human_guid, self.app.personas
        names = {m.name.casefold(): g for g, m in self.app.world.members.items() if g != hg}
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
            if g == hg or g not in world.online or g in world.afk or g in away:
                continue
            p = app.personas.get(g)
            if p is None or self._presence(g, p, local).state not in ANSWERING:
                continue
            w = warmth(p, app.store.get_relationship(g, hg)) * self._fit(ch, g, p)
            if w > 0:
                weights[g] = w
        n = 2 if len(weights) > 1 and self.rng.random() < SECOND_P else 1
        return pick_speakers(weights, n, self.rng)

    def _fit(self, ch: layout.Channel, g: int, p) -> float:
        world = self.app.world
        m = world.members.get(g)
        fit = 1.0
        if m is not None and layout.CLASS_CHANNEL.get(m.class_) == ch.id:
            fit *= CLASS_FIT
        if ch.id in RAID_CHANNELS and (world.is_officer(g) or (m is not None and layout.role_of(
                m, p, trial_rank=self._trial_rank()) in ("raider", "officer"))):
            fit *= ROLE_FIT
        return fit

    def _presence(self, g: int, p, local) -> Presence:
        world = self.app.world
        m = world.members.get(g)
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
        if layout.dm_peer(channel) is not None:
            return f"You are in a private message with {self.app.engine.human.called}"
        ch = layout.channel(channel)
        if ch is None:
            return "You are on the guild's Discord"
        return f"You are in #{ch.name} on the guild's Discord: {ch.topic}"

    def context_lines(self, conn, channel: str, thread: int | None, *,
                      limit: int = CONTEXT_LINES) -> list[str]:
        """The latest `limit` messages of a store channel's feed (or of `thread`) as "Name: text",
        oldest first, each on one line (fold), deleted messages left out."""
        rows = conn.execute(
            "SELECT author, text FROM discord_message WHERE channel = ? AND thread IS ? "
            "AND deleted IS NULL ORDER BY id DESC LIMIT ?", (channel, thread, limit)).fetchall()
        return [f"{self._name(a)}: {fold(t)}" for a, t in reversed(rows)]

    def _name(self, g: int) -> str:
        app = self.app
        if g == app.human_guid:
            return app.engine.human.called
        m = app.world.members.get(g)
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
        with store.transaction():
            row = store.conn.execute("SELECT text, deleted FROM discord_message WHERE id = ?",
                                     (message,)).fetchone()
            if row is None or row[1] is not None:
                return None
            lines = self.context_lines(store.conn, cid, thread) if reads else []
        their = app.engine.human.forms[2]
        purpose = self.purpose_line(cid)
        tail = f"Answer {their} message: {_clip(fold(row[0]), HIS_CHARS)}"
        lines = [_clip(x, LINE_CHARS) for x in lines]
        while lines and len("\n".join([purpose, "Recent messages:", *lines, tail])) > (
                INSTRUCTION_CHARS):
            lines.pop(0)
        head = [purpose, "Recent messages:\n" + "\n".join(lines)] if lines else [purpose]
        return "\n".join([*head, tail])

    def _instruction_hook(self, turn: Turn, meta: TurnMeta, previous: str) -> str:
        """The engine's last step before composing: an answer turn's instruction built again
        from the store as it is now."""
        target = meta.discord
        if (meta.why != WHY or target is None or target.reply_to is None
                or not is_discord(turn.channel)):
            return previous
        fresh = self.instruction(turn.speaker, turn.channel, target.reply_to, target.thread)
        return previous if fresh is None else fresh

    # -- the lease -------------------------------------------------------------------------

    def _take_lease(self) -> None:
        arbiter = getattr(self.app, "arbiter", None)
        if arbiter is None:
            return
        loop = asyncio.get_running_loop()
        self._until = self.app.clock.now() + LEASE_IDLE_S
        if not self._held:
            self._held, self._arbiter = True, arbiter
            first = arbiter.hold(LEASE)
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
        tasks = [t for t in (self._timer, *self._handbacks) if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._timer = None
        if self._held:
            await self._release()
