"""Every message of his gets a member's answer (owner, 2026-11-15: "make sure they actually reply
something. doesn't have to be a profound reply but something, and needs to add to the
conversation").

Three layers, cheapest first:

1. Planning. Every line of his that talk answers at all is planned at least one answer
   (select.REPLY_COUNT_REMARK no longer draws none; a whisper is always answered by its member,
   WHISPER_REPLY_P; his hello in the login quiet keeps one answer; a Discord channel post with
   nobody online or idle draws from the rest of its audience). The first answer carries
   TurnMeta.owed_answer: the guild's pace never holds it back (Pipeline._blocked), its guard
   review keeps the best draft instead of dropping it (LiveReview.owed), and its deadline is at
   least OWED_DEADLINE_S. A whisper answer and an answer to his Discord DM are owed already
   (owes_him). When an owed turn ends with nothing said, the next answer to the same line in
   its job inherits the debt (Pipeline._run).
2. In the turn (rescue_text): an owed answer the model left silent, whose drafts both kept a
   tell, whose request failed or whose time ran out (RESCUABLE) is asked a third time with a
   plainer instruction (OWED_HINT). Only tells a reader must never see block that draft
   (BLOCKING: the fatal ones, an unbacked shared-history claim, a flirt at a member, and a
   summons out of the game where her arc doesn't allow one, which the plain ask warns of); a
   repeated move or a long draft no longer cost him his answer (a long one is cut to its
   budget). With no usable draft, a short line built from his own message is the answer
   (fallback_line): never empty, never emoji alone, never his words said back, and always a
   reaction plus a question back. The same fallback stands in when every reviewed draft only
   said his message back (guard_echo) or styling left nothing.
3. The watchdog (OwedWatch, ticked by the app every second, working every TICK_S): a message of
   his still unanswered GRACE_S after it, with no answer to it in flight, gets a rescue turn,
   logged `owed_rescue` with a reason code: from the member a whisper or DM went to, else a
   member present where he said it (one he named first, then the reply draw). At most
   RESCUE_TRIES rescues a message, RETRY_S apart; a message older than MAX_AGE_S is let go
   (`owed_given_up`).

Deliberate silences are held, never dropped: the watchdog waits and answers just after them,
logging `owed_held` once per reason: his raid's boss fight and wipe silence (RaidState.allows),
a /say whose hearers are all dead, a Free GPU pause, his whisper's member being AFK, him being
offline in game (for a game line), a DM she answers on her own time ([talk] dm_own_time) or a
group DM's own pacing, a selfie request still being made and his voice note still being
transcribed.

Not owed, by design: his bot commands and raid orders (app.his_command, the raid lead's
claims), emotes, addon pastes (Recount, RBS, DBM and the like: a paste burst has its own single
answer, pastes.py), a line where nobody with a persona can hear him (an empty voice room, a
whisper to someone offline or outside the cast, a party of strangers: `owed_given_up
reason=nobody_present|target_offline`), and anything said while the mind was not running for a
game line (they never reached it; Discord posts are read back from mind.db after a restart).
Logs carry ids, channels, counts and codes, never a text."""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field

log = logging.getLogger("leeroy_mind.talk")

# The watchdog: how long after his message it steps in (no answer, none in flight), by medium
GRACE_S = {"whisper": 45.0, "game": 60.0, "voice": 45.0, "discord": 150.0}
TICK_S = 5.0
START_GRACE_S = 60.0  # after the mind starts: the bridge, his session and the model settle
RETRY_S = 90.0
RESCUE_TRIES = 2
MAX_AGE_S = {"game": 900.0, "discord": 1800.0}  # a boss fight can hold an answer ten minutes
# Lines he sends this close together, before anyone answers, are one message to answer
BURST_S = 90.0
# An owed answer's deadline (after its delay): long enough for a cold model load or a night's
# hand-over, never the 20-30 s a social line gets
OWED_DEADLINE_S = {"game": 90.0, "discord": 300.0}
# The drop reasons an owed answer is rescued from inside its turn
RESCUABLE = frozenset({"tells", "silent", "llm_error", "expired"})
# Soft tells that still block a rescue's draft (fatal ones always do)
BLOCKING = frozenset({"history", "member flirt", "summons"})
OWED_HINT = ("Answer {human} now, in {length}, in your own voice. Say something real back to "
             "what {they} just said: react to it and add one thing of your own, an answer, a "
             "thought or a question. Don't say {their} words back, and don't answer (nothing).")
LONG_SLACK = 1.25
ADDON_TAG = re.compile(r"^\s*<(?:DBM|Deadly Boss Mods|BigWigs|BW|GTFO|RCLootCouncil)>", re.I)
AUTO_REPLY = re.compile(r"^\s*(?:AFK|DND|<AFK>|<Away)\b", re.I)


def owes_him(pipe, turn, meta) -> bool:
    """The turn carries an answer he is owed: the first answer to a line of his
    (TurnMeta.owed_answer), an answer to his whisper or to his Discord DM. A selfie request's
    answer is its service's to retry (photos/requests.py): never rescued here (the watchdog
    waits for the request to end)."""
    if not pipe.human.guid or meta.elsewhere or meta.photo_request is not None:
        return False
    return bool(meta.owed_answer or meta.whisper_reply or pipe._answers_his_dm(turn, meta))


def owed_deadline(channel: str) -> float:
    from leeroy_mind.discord.transport import is_discord
    return OWED_DEADLINE_S["discord" if is_discord(channel) else "game"]


# -- the fallback line -------------------------------------------------------------------------

_TOKEN = re.compile(r"[a-z][a-z']*")
_STOP = frozenset("""about after again also always another around been before being both but
could does doing done each even ever from have having here into just keep know like little made
make many more most much must never only other over really same should some still such than that
their them then there these they thing think this those through very want wants wanna was were
what when where which while will with would your youre you're tonight anyone good people time
guys yeah okay nice thanks thank back lets let's gonna pretty maybe getting going something
everyone everybody yall y'all fam love baby babe dont don't cant can't wont won't im i'm ive
i've its it's thats that's whats what's hows how's whos who's honestly literally right well
""".split())
_SUMMON = re.compile(r"\b(?:come (?:here|to|find|over|meet)|meet (?:me|us|at|in)|get (?:here|over)"
                     r"|on me\b|summon|port (?:to|me))", re.I)
_THANKS = re.compile(r"\b(?:thanks|thank you|thx|ty|tyvm|appreciate)\b", re.I)
_FRAMES = {
    "greeting": ("hey {called}! good to see you, what are you up to?",
                 "hi {called} :) how's your day going?",
                 "heyy {called}, what's new with you?"),
    "goodbye": ("night {called}, talk soon", "later {called}! get some rest",
                "bye {called}, see you tomorrow?"),
    "summon": ("omw {called}", "on my way, give me a sec", "coming! where exactly?"),
    "thanks": ("anytime {called}", "of course, what else is going on?",
               "no problem :) what's next?"),
    "question_topic": ("{topic}... honestly not sure yet. what do you think?",
                       "good question about {topic}, i'd have to think. why do you ask?",
                       "hmm, {topic}? i'm torn. what's your take?"),
    "question": ("hmm good question. what's your take?",
                 "honestly not sure, what are you thinking?",
                 "ooh i'd have to think about that one. you first?"),
    "remark_topic": ("wait, {topic}? tell me more", "ha, {topic}. then what happened?",
                     "{topic} huh? how'd that go?"),
    "remark": ("ha, i hear you. how are you feeling about it?", "mm, tell me more?",
               "fair. so what's next for you?"),
}


def topic_word(text: str, avoid=()) -> str:
    """One word of his message to answer about: the longest that says something (four letters
    or more, not a filler word, not a name in `avoid`), the first of the longest."""
    banned = {a.casefold() for a in avoid if a}
    words = [w.strip("'") for w in _TOKEN.findall(text.casefold())]
    words = [w for w in words if len(w) >= 4 and w not in _STOP and w not in banned
             and not re.search(r"(.)\1\1", w)]
    return max(words, key=len) if words else ""


def fallback_line(his: str, *, called: str, seed, avoid=()) -> str:
    """A short answer to his message `his`, built from it alone: a greeting, goodbye, summons or
    thanks gets its own kind of line, a question or a remark one about a word of his (topic_word,
    never more of his words), and every line but a goodbye asks him something back. `seed` keeps
    a turn's choice steady."""
    from leeroy_mind.talk.select import classify_human_line
    text = " ".join(his.split())
    kind = classify_human_line(text) if text else "remark"
    if _THANKS.search(text):
        kind = "thanks"
    elif _SUMMON.search(text):
        kind = "summon"
    topic = topic_word(text, avoid=(called, *avoid))
    if kind in ("question_all", "remark"):
        base = "question" if kind == "question_all" else "remark"
        kind = f"{base}_topic" if topic else base
    rng = random.Random(f"owed:{seed}:{kind}")
    return rng.choice(_FRAMES[kind]).format(called=called or "you", topic=topic)


def _trim(text: str, words: int) -> str:
    """At most about `words` words: whole sentences while they fit (the first always), else the
    first sentence cut at a word."""
    if words <= 0 or len(text.split()) <= LONG_SLACK * words:
        return text
    sentences = re.findall(r"[^.!?]+[.!?]*\s*", text) or [text]
    out = ""
    for s in sentences:
        if out and len((out + s).split()) > words:
            break
        out += s
    out = out.strip()
    if len(out.split()) > LONG_SLACK * words:
        out = " ".join(out.split()[:words])
    return out


def his_words(pipe, turn, meta, composed) -> str:
    """What he said that the turn answers: its trigger line, else his Discord post."""
    if composed is not None and composed.answers:
        return composed.answers[0]
    target = getattr(meta, "discord", None)
    if target is not None and target.reply_to:
        row = pipe.store.conn.execute("SELECT text FROM discord_message WHERE id = ?",
                                      (target.reply_to,)).fetchone()
        if row is not None:
            return row[0]
    if turn.trigger_line:
        rows = pipe.store.chat_lines([turn.trigger_line])
        if rows:
            return rows[0].text
    return ""


def fallback_for(pipe, turn, meta, composed=None) -> str:
    his = his_words(pipe, turn, meta, composed)
    names = [m.name for m in pipe.world.members.values()]
    return fallback_line(his, called=pipe.human.called, seed=meta.turn_id, avoid=names)


def fallback_lines(pipe, turn, meta, p, *, close: bool, reason: str, composed=None) -> list:
    """The fallback line styled as the member types, for an owed turn whose drafts are all gone;
    [] for any other turn."""
    if not owes_him(pipe, turn, meta):
        return []
    from leeroy_mind.talk.items import defuse
    from leeroy_mind.talk.style import Styled
    text = fallback_for(pipe, turn, meta, composed)
    lines = pipe._style(turn, p, text, close=close, two_lines=False, romance=meta.romance)
    lines = [Styled(defuse(s.line), defuse(s.plain)) for s in lines]
    if lines:
        meta.owed_rescued = reason
        log.info("owed_rescue_line: guid=%d kind=%s channel=%s reason=%s how=fallback",
                 turn.speaker, turn.kind, turn.channel, reason)
    return lines


async def rescue_text(pipe, turn, meta, composed, why: str, rejected: str, *,
                      spoken: bool = False) -> tuple[str, str | None]:
    """The owed answer's third try after `why` (RESCUABLE): one more model request with OWED_HINT,
    taken unless a BLOCKING or fatal tell is still there (a long draft is cut to its budget),
    else fallback_line. A Free GPU pause or a changed scope keeps the drop (the watchdog waits
    out the pause)."""
    if why not in RESCUABLE or meta.owed_rescued or not owes_him(pipe, turn, meta):
        return rejected, why
    from leeroy_mind.llm.client import GpuPaused, LLMError
    from leeroy_mind.llm.scheduler import Expired, SchedulerError
    from leeroy_mind.personas.screens import redact_real_name
    from leeroy_mind.talk.filters import find_tells, is_silence, repair, strip_copied
    from leeroy_mind.talk.pipeline import _ScopeChanged, _scope_valid, _with_hint, aliases
    from leeroy_mind.voice.comms import SPOKEN_HINT
    meta.owed_rescued = why
    now = pipe.clock.now()
    turn.deadline = max(turn.deadline, now + owed_deadline(turn.channel))
    if meta.borrow_credit is not None:
        pipe._mark_borrow(meta, reset=True)
    p = composed.persona
    name, own, him = p.character_name, aliases(p), pipe._his_names()
    words = pipe._discord_words(turn)
    length = (pipe._silent_length(words) if words
              else "a line or two" if meta.two_lines else "a line")
    they, _, their = pipe.human.forms
    messages = _with_hint(composed.messages, OWED_HINT.format(
        human=pipe.human.called, length=length, they=they, their=their))
    hg = pipe.human.guid
    summons_ok = pipe._summons_ok(turn) if pipe._counterpart(turn) == hg else None
    if summons_ok is False:  # her arc keeps it in the game: said up front, not after a miss
        from leeroy_mind.talk.pipeline import SUMMONS_HINT
        forms = dict(zip(("they", "them", "their"), pipe._forms_of(hg)))
        messages = _with_hint(messages, SUMMONS_HINT.format(**forms))
    if spoken:
        messages = _with_hint(messages, SPOKEN_HINT)
    emoji = turn.channel.startswith(("dm:", "discord:", "gdm:"))
    text = ""
    try:
        raw = await pipe._ask(turn, messages, meta=meta, attempt=2)
    except _ScopeChanged:
        return rejected, "scope_changed"
    except GpuPaused:
        return rejected, "gpu_paused"
    except (Expired, SchedulerError, LLMError) as e:
        log.info("owed_rescue_model_failed: guid=%d reason=%s", turn.speaker, type(e).__name__)
        raw = ""
    if not _scope_valid(meta):
        return raw or rejected, "scope_changed"
    if raw and not is_silence(raw):
        text = raw if composed.close else redact_real_name(raw, pipe.human, pipe.human.called)
        text = strip_copied(text, (name, *own), him=him)
        if is_silence(text):
            text = ""
    if text:
        tells = find_tells(text, recent_own=pipe._own_lines(turn), speaker=name,
                           answers=composed.answers, others=composed.others,
                           samples=composed.samples, support=composed.support,
                           people=list(pipe._protect(turn.channel)), window=composed.window,
                           held=composed.held, tonight=meta.again_tonight,
                           recent_moves=[(r.clause, r.main) for r in composed.moves],
                           summons_ok=summons_ok, keep_emoji=emoji)
        if pipe._flirts_at_a_member(turn, text):
            text = ""
        elif any(t.fatal or t.reason in BLOCKING for t in tells):
            log.info("owed_rescue_draft_refused: guid=%d tells=%s", turn.speaker,
                     ",".join(sorted({t.reason for t in tells if t.fatal or t.reason in BLOCKING})))
            text = ""
        else:
            text = repair(_trim(text, pipe._word_budget(turn, p, meta.two_lines)), speaker=name,
                          aliases=own, him=him, keep_emoji=emoji)
            if is_silence(text):
                text = ""
    how = "model" if text else "fallback"
    if not text:
        text = fallback_line(his_words(pipe, turn, meta, composed), called=pipe.human.called,
                             seed=meta.turn_id, avoid=[m.name for m in pipe.world.members.values()])
    log.info("owed_rescue_line: guid=%d kind=%s channel=%s reason=%s how=%s", turn.speaker,
             turn.kind, turn.channel, why, how)
    return text, None


# -- the watchdog ------------------------------------------------------------------------------

@dataclass
class Owed:
    """His unanswered stretch in one conversation: `ids` his lines (game) or posts (Discord),
    oldest first; `first`/`last` their times."""
    medium: str  # "whisper", "game", "voice" or "discord"
    channel: str  # his line's channel, or the Discord store channel
    to: int  # a whisper's member, a DM's member; else 0
    ids: list[int]
    first: float
    last: float
    reply_to: str = ""
    rows: list = field(default_factory=list)  # the game's ChatLineRows, as `ids`
    rescues: int = 0
    rescued_at: float = -float("inf")
    tried: set[int] = field(default_factory=set)
    held: str = ""


def _game_medium(reply_to: str) -> str:
    return "whisper" if reply_to == "whisper" else "voice" if reply_to.startswith("voice:") else "game"


def is_addon_text(text: str) -> bool:
    from leeroy_mind.talk.pastes import is_addon_row
    return bool(is_addon_row(text) or ADDON_TAG.match(text) or AUTO_REPLY.match(text))


class OwedWatch:
    """See the module docstring. `app` gives store, clock, engine, world, discord_talk and,
    optionally, gpu and photos."""

    def __init__(self, app, *, rng: random.Random | None = None):
        self.app = app
        self.rng = rng or random.Random()
        self._game: dict[tuple[str, int], Owed] = {}
        self._discord: dict[str, Owed] = {}
        self._done: dict[int, None] = {}  # Discord posts answered, rescued out or let go
        self._started = app.clock.now()
        self._at = -float("inf")

    # -- what he said --------------------------------------------------------------------------

    def note_line(self, row, reply_to: str) -> None:
        """A line of his that talk answers (engine.on_human_chat, after its commands, emotes and
        pastes are left out): owed until a member answers it."""
        if is_addon_text(row.text):
            return
        whisper = reply_to == "whisper"
        key = ("whisper", row.to_guid) if whisper else (row.channel, 0)
        o = self._game.get(key)
        if o is None:
            o = self._game[key] = Owed(_game_medium(reply_to), row.channel,
                                       row.to_guid if whisper else 0, [], row.ts, row.ts,
                                       reply_to=reply_to)
        o.ids.append(row.id)
        o.rows.append(row)
        o.last = row.ts
        del o.ids[:-32], o.rows[:-32]

    # -- the tick ------------------------------------------------------------------------------

    def tick(self, now: float) -> None:
        if now - self._at < TICK_S:
            return
        self._at = now
        if now - self._started < START_GRACE_S:
            return
        try:
            self._tick_game(now)
        except Exception:
            log.exception("owed_tick_failed: medium=game")
        try:
            self._tick_discord(now)
        except Exception:
            log.exception("owed_tick_failed: medium=discord")

    def _paused(self) -> bool:
        gate = getattr(getattr(self.app, "scheduler", None), "gpu_gate", None)
        return bool(getattr(gate, "paused", False))

    def _hold(self, o: Owed, key, reason: str) -> None:
        if o.held != reason:
            o.held = reason
            log.info("owed_held: medium=%s channel=%s line=%d reason=%s", o.medium,
                     o.channel, o.ids[-1], reason)

    def _give_up(self, o: Owed, reason: str) -> None:
        log.info("owed_given_up: medium=%s channel=%s line=%d lines=%d reason=%s", o.medium,
                 o.channel, o.ids[-1], len(o.ids), reason)

    # -- game lines ----------------------------------------------------------------------------

    def _answered_at(self, o: Owed) -> float | None:
        """Up to when this stretch is answered: in a whisper, her newest line to him (it answers
        everything he said before it); elsewhere the newest of his lines a member has delivered
        an answer to (it answers that line and those before it)."""
        conn, hg = self.app.store.conn, self.app.engine.human.guid
        if o.medium == "whisper":
            row = conn.execute(
                "SELECT max(ts) FROM chat_line WHERE channel = 'whisper' AND origin = 'mind' "
                "AND from_guid = ? AND to_guid = ? AND ts >= ?", (o.to, hg, o.first)).fetchone()
        else:
            marks = ",".join("?" * len(o.ids))
            row = conn.execute(
                "SELECT max(l.ts) FROM utterance u JOIN chat_line l ON l.id = u.trigger_line "
                f"WHERE u.dropped IS NULL AND u.trigger_line IN ({marks})", o.ids).fetchone()
        return None if row is None or row[0] is None else float(row[0])

    def _settle(self, o: Owed) -> bool:
        """Drop the lines an answer covers; True when nothing is left owed."""
        at = self._answered_at(o)
        if at is None:
            return False
        keep = [(i, r) for i, r in zip(o.ids, o.rows) if r.ts > at]
        if not keep:
            return True
        o.ids, o.rows = [i for i, _ in keep], [r for _, r in keep]
        o.first, o.rescues, o.rescued_at, o.held = o.rows[0].ts, 0, -float("inf"), ""
        o.tried.clear()
        return False

    def _in_flight_game(self, o: Owed) -> bool:
        from leeroy_mind.talk.pipeline import REPLY_KINDS
        eng, hg = self.app.engine, self.app.engine.human.guid
        ids = set(o.ids)
        for t in eng.planned_turns():
            if t.trigger_line in ids:
                return True
            if (o.medium == "whisper" and t.speaker == o.to and t.channel == "whisper"
                    and t.to_guid == hg and t.kind in REPLY_KINDS):
                return True
        return False

    def _tick_game(self, now: float) -> None:
        eng = self.app.engine
        for key, o in list(self._game.items()):
            if self._settle(o):
                del self._game[key]
                continue
            if now - o.first > MAX_AGE_S["game"]:
                self._give_up(o, "too_old")
                del self._game[key]
                continue
            if now - o.last < GRACE_S[o.medium] or now - o.rescued_at < RETRY_S:
                continue
            if self._in_flight_game(o):
                continue
            if o.rescues >= RESCUE_TRIES:
                self._give_up(o, "tries")
                del self._game[key]
                continue
            if self._paused():
                self._hold(o, key, "gpu_paused")
                continue
            if not eng.world.human_online:
                self._hold(o, key, "human_offline")
                continue
            done = self._rescue_game(o, now)
            if done in ("nobody_present", "target_offline"):
                self._give_up(o, done)
                del self._game[key]
            elif done:
                self._hold(o, key, done)

    def _rescue_game(self, o: Owed, now: float) -> str | None:
        """Queue one rescue answer to the stretch's newest line; None when queued, else why
        not (a hold, or nobody_present / target_offline)."""
        from leeroy_mind.talk.channels import choose_channel
        from leeroy_mind.talk.pipeline import TurnMeta
        from leeroy_mind.talk.select import mentioned, pick_speakers, topic_words
        from leeroy_mind.talk.turns import Turn, TurnKind
        eng, w = self.app.engine, self.app.engine.world
        hg = eng.human.guid
        row = o.rows[-1]
        whisper = o.medium == "whisper"
        audience, cands = eng._reply_candidates(row, o.reply_to, now)
        if not cands:
            heard = [g for g in eng._candidates(audience, hearable=False)]
            if whisper:
                if row.to_guid in w.afk:
                    return "afk"
                if row.to_guid not in w.online:
                    return "target_offline"
            if any(not eng.raid.allows(g, TurnKind.REPLY_HUMAN, now) for g in heard):
                return "raid"  # present, and held back by his raid's fight or wipe
            if heard:
                return "dead"  # in /say: dead until rezzed
            return "nobody_present"
        fresh = [g for g in cands if g not in o.tried] or cands
        names = {g: w.members[g].name for g in fresh if g in w.members}
        named = [g for g in mentioned(row.text, names) if g in fresh]
        if whisper:
            speaker = row.to_guid
        elif named:
            speaker = named[0]
        else:
            ctx = eng._context(now, topic_words([row.text]))
            weights, pool = eng._weigh(fresh, last=hg, ctx=ctx, reply=True)
            picked = pick_speakers({g: v for g, v in weights.items() if v > 0}, 1, self.rng, pool)
            speaker = picked[0] if picked else self.rng.choice(sorted(fresh))
        channel = choose_channel(TurnKind.REPLY_HUMAN, speaker=speaker, world=w, raid=eng.raid,
                                 now=now, rng=eng.rng, reply_to=o.reply_to) or o.reply_to
        reason = self._reason(o)
        turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=speaker, channel=channel,
                    to_guid=hg if whisper else 0, addressee=hg, trigger_line=row.id,
                    delay=self.rng.uniform(1.0, 3.0))
        meta = TurnMeta(eng._new_id(), answers_human=True, whisper_reply=whisper,
                        why="reply:rescue", owed_answer=True)
        o.rescues += 1
        o.rescued_at = now
        o.tried.add(speaker)
        o.held = ""
        log.info("owed_rescue: medium=%s channel=%s line=%d lines=%d age_s=%d speaker=%d "
                 "reason=%s try=%d", o.medium, o.channel, row.id, len(o.ids), int(now - o.first),
                 speaker, reason, o.rescues)
        eng._queue([(turn, meta)])
        return None

    def _reason(self, o: Owed) -> str:
        """Why the stretch has no answer, from its turns' rows: the newest drop reason
        ("dropped:<reason>"), or "not_planned" when no answer was even tried."""
        marks = ",".join("?" * len(o.ids))
        row = self.app.store.conn.execute(
            f"SELECT dropped FROM utterance WHERE trigger_line IN ({marks}) AND dropped IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", o.ids).fetchone()
        return f"dropped:{row[0]}" if row is not None else "not_planned"

    # -- Discord -------------------------------------------------------------------------------

    def _tick_discord(self, now: float) -> None:
        app = self.app
        talk = getattr(app, "discord_talk", None)
        hg = app.engine.human.guid
        if talk is None or not hg:
            return
        rows = app.store.conn.execute(
            "SELECT id, channel, thread, ts FROM discord_message WHERE origin = 'human' AND "
            "author = ? AND deleted IS NULL AND ts >= ? ORDER BY id",
            (hg, now - MAX_AGE_S["discord"] - GRACE_S["discord"])).fetchall()
        live: dict[str, Owed] = {}
        for mid, channel, thread, ts in rows:
            if mid in self._done:
                continue
            key = f"{channel}#{thread or 0}"
            o = live.get(key)
            if o is None:
                o = self._discord.get(key) or Owed("discord", channel, _dm_peer(channel), [],
                                                   ts, ts)
                o.ids, o.first = [], ts
                live[key] = o
            o.ids.append(mid)
            o.last = ts
        self._discord = live
        for key, o in list(live.items()):
            covered = self._discord_answered(o)
            if covered:
                self._close(key, o, covered)
                continue
            if now - o.first > MAX_AGE_S["discord"]:
                self._give_up(o, "too_old")
                self._close(key, o, o.ids)
                continue
            if now - o.last < GRACE_S["discord"] or now - o.rescued_at < RETRY_S:
                continue
            if self._in_flight_discord(o):
                continue
            if o.rescues >= RESCUE_TRIES:
                self._give_up(o, "tries")
                self._close(key, o, o.ids)
                continue
            hold = self._discord_hold(o)
            if hold:
                self._hold(o, key, hold)
                continue
            if not self._rescue_discord(o, talk, now):
                self._give_up(o, "nobody_present")
                self._close(key, o, o.ids)

    def _close(self, key: str, o: Owed, done: list[int]) -> None:
        """His posts `done` (answered, given up) leave the watch; any after them stay owed."""
        for mid in done:
            self._done[mid] = None
        while len(self._done) > 4096:
            del self._done[next(iter(self._done))]
        left = [m for m in o.ids if m not in set(done)]
        if not left:
            self._discord.pop(key, None)
            return
        o.ids, o.rescues, o.rescued_at, o.held = left, 0, -float("inf"), ""
        o.tried.clear()

    def _discord_answered(self, o: Owed) -> list[int]:
        """His posts in the stretch a member has answered since: in a DM or a group DM a later
        message of the member's (hers in a DM) that replies to that post or a later one, or to
        nothing; in a channel an answer replying to that post or a later one."""
        conn = self.app.store.conn
        if o.channel.startswith(("dm:", "gdm:")):
            who, args = ("", (o.channel, o.ids[0])) if not o.to else (
                " AND author = ?", (o.channel, o.to, o.ids[0]))
            row = conn.execute("SELECT max(coalesce(reply_to, id - 1)) FROM discord_message "
                               f"WHERE channel = ? AND origin = 'mind'{who} AND id > ?",
                               args).fetchone()
        else:
            marks = ",".join("?" * len(o.ids))
            row = conn.execute(f"SELECT max(reply_to) FROM discord_message WHERE origin = 'mind' "
                               f"AND reply_to IN ({marks})", o.ids).fetchone()
        if row is None or row[0] is None:
            return []
        return [m for m in o.ids if m <= row[0]]

    def _place(self, o: Owed) -> str:
        from leeroy_mind.discord.channels import CHANNEL_PREFIX
        return o.channel if o.channel.startswith(("dm:", "gdm:")) else CHANNEL_PREFIX + o.channel

    def _in_flight_discord(self, o: Owed) -> bool:
        from leeroy_mind.talk.pipeline import REPLY_KINDS
        place = self._place(o)
        for t in self.app.engine.planned_turns():
            if t.channel == place and t.kind in REPLY_KINDS and (not o.to or t.speaker == o.to):
                return True
        requests = getattr(getattr(self.app, "photos", None), "requests", None)
        tasks = getattr(requests, "_tasks", None) or {}
        return any(m in tasks for m in o.ids)

    def _discord_hold(self, o: Owed) -> str:
        """A deliberate wait: a Free GPU pause, her own time for a DM, a group DM's pacing."""
        if self._paused():
            return "gpu_paused"
        store = self.app.store
        if o.channel.startswith("dm:") and o.to:
            deferred = store.get_meta(f"discord:dm:deferred:{o.to}")
            if deferred is not None and deferred.isdigit() and int(deferred) >= o.ids[0]:
                return "own_time"
        if any(store.get_meta(f"voice_note:pending:{mid}") is not None for mid in o.ids):
            return "voice_note"  # his voice note is still being transcribed
        if o.channel.startswith("gdm:"):
            for mid in o.ids:
                raw = store.get_meta(f"groups:owed:{mid}")
                if raw not in (None, "", "{}"):
                    return "group_pacing"
        try:
            marks = ",".join("?" * len(o.ids))
            if store.conn.execute(f"SELECT 1 FROM selfie_request WHERE message IN ({marks}) "
                                  "AND ended IS NULL LIMIT 1", o.ids).fetchone():
                return "photo_request"
        except Exception:
            pass  # an older database: no requests
        return ""

    def _rescue_discord(self, o: Owed, talk, now: float) -> bool:
        mid = o.ids[-1]
        try:
            items = talk.plan(mid)
        except Exception as e:
            log.warning("owed_plan_failed: message=%d error=%s", mid, type(e).__name__)
            return True  # retried on the next tick
        if not items:
            return False
        if not o.channel.startswith("dm:"):
            fresh = [it for it in items if it[0].speaker not in o.tried]
            items = (fresh or items)[:1]
        for turn, meta in items:
            meta.owed_answer = True
        speaker = items[0][0].speaker
        o.rescues += 1
        o.rescued_at = now
        o.tried.add(speaker)
        o.held = ""
        log.info("owed_rescue: medium=discord channel=%s message=%d posts=%d age_s=%d "
                 "speaker=%d reason=no_answer try=%d", o.channel, mid, len(o.ids),
                 int(now - o.first), speaker, o.rescues)
        take = getattr(talk, "_take_lease", None)
        if take is not None:
            take()
        self.app.engine._queue(items)
        return True


def _dm_peer(channel: str) -> int:
    if channel.startswith("dm:"):
        tail = channel[3:]
        return int(tail) if tail.isdigit() else 0
    return 0


# -- the count -------------------------------------------------------------------------------

@dataclass
class Unanswered:
    """His messages in a stretch of time and how many got no member answer within `window`."""
    messages: dict[str, int] = field(default_factory=dict)  # by medium
    unanswered: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.messages.values())

    @property
    def missed(self) -> int:
        return sum(self.unanswered.values())


def _game_family(channel: str) -> str:
    if channel == "whisper":
        return "whisper"
    if channel.startswith("voice:"):
        return "voice"
    return channel


def count_unanswered(conn, human: int, since: float, now: float, *,
                     window: float = 300.0) -> Unanswered:
    """His messages since `since` (game chat, voice, Discord channels, DMs and group DMs) and
    those no member answered within `window` seconds, read-only: lines he sent together (BURST_S
    apart, nobody answering between) are one message; a whisper or DM is answered by its member's
    next line, a channel line by an answer to it (utterance.trigger_line) or, on Discord, a reply
    to it. His addon pastes, DBM-style auto-whispers and party/raid bot commands are not
    messages; raid orders the raid lead took can't be told apart here and still count. A message
    younger than `window` is left out (not yet due)."""
    from leeroy_mind.talk.world import looks_like_bot_command
    out = Unanswered()
    game = conn.execute("SELECT id, ts, channel, to_guid, text FROM chat_line WHERE from_guid = ? "
                        "AND origin = 'human' AND ts >= ? AND ts <= ? AND channel NOT LIKE 'dm:%' "
                        "AND channel NOT LIKE 'discord:%' AND channel NOT LIKE 'gdm:%' ORDER BY ts, id",
                        (human, since, now - window)).fetchall()
    stretches: list[tuple[str, int, list]] = []  # (key channel, to, [(id, ts)])
    open_: dict[tuple[str, int], list] = {}
    for lid, ts, channel, to, text in game:
        if is_addon_text(text):
            continue
        if channel in ("party", "raid", "raid_warning") and looks_like_bot_command(text):
            continue
        if not (channel in ("guild", "officer", "party", "raid", "raid_warning", "say", "yell",
                            "whisper") or channel.startswith("voice:")):
            continue
        key = (channel, to if channel == "whisper" else 0)
        cur = open_.get(key)
        if cur is not None and ts - cur[-1][1] <= BURST_S and not _game_reply(
                conn, key, human, [i for i, _ in cur], cur[-1][1], ts):
            cur.append((lid, ts))
            continue
        cur = open_[key] = [(lid, ts)]
        stretches.append((key[0], key[1], cur))
    for channel, to, lines in stretches:
        fam = _game_family(channel)
        out.messages[fam] = out.messages.get(fam, 0) + 1
        last = lines[-1][1]
        if not _game_reply(conn, (channel, to), human, [i for i, _ in lines], last, last + window):
            out.unanswered[fam] = out.unanswered.get(fam, 0) + 1
    posts = conn.execute("SELECT id, channel, thread, ts FROM discord_message WHERE origin = 'human' "
                         "AND author = ? AND deleted IS NULL AND ts >= ? AND ts <= ? ORDER BY id",
                         (human, since, now - window)).fetchall()
    dstretch: list[tuple[str, list]] = []
    dopen: dict[str, list] = {}
    for mid, channel, thread, ts in posts:
        key = f"{channel}#{thread or 0}"
        cur = dopen.get(key)
        if cur is not None and ts - cur[-1][1] <= BURST_S and not _discord_reply(
                conn, channel, [m for m, _ in cur], cur[-1][1], ts):
            cur.append((mid, ts))
            continue
        cur = dopen[key] = [(mid, ts)]
        dstretch.append((channel, cur))
    for channel, lines in dstretch:
        fam = ("discord_dm" if channel.startswith("dm:") else "group_dm"
               if channel.startswith("gdm:") else "discord_channel")
        out.messages[fam] = out.messages.get(fam, 0) + 1
        last = lines[-1][1]
        if not _discord_reply(conn, channel, [m for m, _ in lines], last, last + window):
            out.unanswered[fam] = out.unanswered.get(fam, 0) + 1
    return out


def _game_reply(conn, key, human, ids, after: float, until: float) -> bool:
    channel, to = key
    if channel == "whisper":
        return conn.execute("SELECT 1 FROM chat_line WHERE channel = 'whisper' AND origin = 'mind' "
                            "AND from_guid = ? AND to_guid = ? AND ts > ? AND ts <= ? LIMIT 1",
                            (to, human, after, until)).fetchone() is not None
    marks = ",".join("?" * len(ids))
    return conn.execute(f"SELECT 1 FROM utterance WHERE dropped IS NULL AND trigger_line IN ({marks}) "
                        "AND ts >= ? AND ts <= ? LIMIT 1",
                        (*ids, int(after), until)).fetchone() is not None


def _discord_reply(conn, channel, ids, after: float, until: float) -> bool:
    if channel.startswith(("dm:", "gdm:")):
        peer = _dm_peer(channel)
        who, args = ("", (channel, after, until)) if not peer else (
            " AND author = ?", (channel, peer, after, until))
        return conn.execute(f"SELECT 1 FROM discord_message WHERE channel = ? AND origin = 'mind'"
                            f"{who} AND ts > ? AND ts <= ? LIMIT 1", args).fetchone() is not None
    marks = ",".join("?" * len(ids))
    return conn.execute(f"SELECT 1 FROM discord_message WHERE origin = 'mind' AND reply_to IN ({marks}) "
                        "AND ts > ? AND ts <= ? LIMIT 1", (*ids, after, until)).fetchone() is not None
