"""Members react to the human's text emotes (owner, 2026-11-07). The app hands the engine his
live text emotes while he is online (on_text_emote, with the stored game_event row's id); a
member's own, another player's, the mind's echoes and replayed ones never come here.

- His emote at an online cast member with a persona: after EMOTE_BACK_S she emotes back at him,
  from a table keyed by his emote and her stance toward him (emote_back): a woman's lust,
  drawn, fond or none, and "man". Men read as friends, so they get friendly or awkward emotes,
  never a romantic one, and so does a woman with no attitude toward him; a woman whose forward
  dial is at 0 (she flirts with nobody: romance.flirt_chance) answers from her fond row. Every
  emote is answered but the utility and combat ones (emote_names.UTILITY_EMOTES): those the
  table does not name get a friendly or awkward FALLBACK, and his rudeness (RUDE_EMOTES) her
  temper (a glare, anger or tears). Once her emote is out she sometimes says one short line
  (wants_line: LINE_P_ROMANTIC for a romantic emote, else LINE_P), an EMOTE_REACT turn through
  the ordinary pipeline that is told exactly what he did (the template EMOTE_AT_YOU): a romantic
  one is answered by whisper, any other by /say, since his emote's target is always within emote
  range (channels.EMOTE_CHANNELS). No line is drawn, or queued, while she has an answer to him
  (a REPLY_HUMAN) planned or said one in the last guards.REPLY_S: her emote still goes.
- His social emote at nobody (JOIN_EMOTES; also one at someone outside the cast, an NPC or a
  stranger): 0-2 members in his group, or with nobody free there those in his zone, join in with
  a fitting emote of their own, at nobody. At most one of them adds a line (EMOTE_JOINED), on his
  group's chat when grouped with him, else /say.
- Limits: COOLDOWN_S per member and PER_MINUTE reactions guild-wide, counted when a reaction is
  planned (EmoteLimits). Nothing in a fight (COMBAT, WIPE_SILENCE, WIPE_DEBRIEF), in the login
  quiet, nor from a member existing talk would refuse (offline, AFK, no persona, dead, the raid
  rules). The emote back checks again after its delay, and her line is made and queued only once
  her emote went out. Emotes and lines leave through the engine's send, so the silent latch holds
  them too.
- The clocks that start conversations with him (engine._open_to_him, reactions._start_whisper)
  wait out his emote exchanges as they wait out his lines: his emote at a member or social one,
  and every emote back, count as his talk (_his_talk_at), and her line, an answer to him, does
  too (Pipeline._answered_at). A line that started a conversation with him never takes the
  guards' minute from her line, nor holds it back when it was her own (guards._ANSWERS).

Once her emote back at his emote went out, the live side writes a receipt (meta
emote_names.answered_key(event id) = her guid): the night's attraction pass counts only those,
his romantic emotes at a woman with an attitude as a flirt (at most two a night) and his rude
ones as a hurt (nightly/attraction.py). Her line is an ordinary sent line, remembered like any
other. The words are two named prompt templates, read when the line is made: the owner's overlay
mind-data/prompts/<name>.txt replaces one, with {name} (what he is called) and {emote} filled in
and anything else, braces too, used as written; one that cannot be read gives the built-in words.
Built-in words stay non-explicit. Logs carry guids, emote names and outcomes, never chat text."""
import math
import re
from collections import deque
from pathlib import Path
from typing import NamedTuple

from leeroy_mind.bridge import BridgeError
from leeroy_mind.discord.channels import is_discord
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.personas.schema import Persona
from leeroy_mind.prompts import load_prompt
from leeroy_mind.protocol import Emote, TextEmote
from leeroy_mind.talk.channels import choose_channel
from leeroy_mind.talk.emote_names import (FRIENDLY_EMOTES, ROMANTIC_EMOTES, RUDE_EMOTES,
                                          UTILITY_EMOTES, answered_key)
from leeroy_mind.talk.guards import REPLY_S
from leeroy_mind.talk.pipeline import TurnMeta, log
from leeroy_mind.talk.reactions import FIGHT_PHASES, Reactions
from leeroy_mind.talk.romance import flirt_chance
from leeroy_mind.talk.turns import Turn, TurnKind

EMOTE_BACK_S = (2.0, 6.0)  # her emote back, after his
LINE_AFTER_S = (1.0, 3.0)  # her line's delay once her emote is out, before the model and typing
COOLDOWN_S = 45.0  # one reaction a member in this long
PER_MINUTE = 6  # reactions guild-wide in a minute, at most
MINUTE_S = 60.0
LINE_P_ROMANTIC, LINE_P = 0.5, 0.25  # how often a reaction adds a line
JOINERS = (0, 2)  # members joining his social emote
PRICKLY, TOUCHY = 3, 7  # agreeableness at or below: angry; neuroticism at or above: tears
_NAME = re.compile(r"[a-z0-9_]{1,32}")  # an emote name as native sends one (LeeroyEmotes.cpp)

ANY = "*"  # a table's answer for everyone its stances don't name
# her stance toward him ("lust", "drawn", "fond", "none"), or "man"
_AFFECTION = {"lust": ("kiss", "blush"), "drawn": ("blush", "giggle"), "fond": ("hug", "laugh"),
              "none": ("shy", "giggle"), "man": ("blink", "laugh", "shrug")}
_WINK = {"lust": ("wink", "kiss"), "drawn": ("blush", "giggle"), "fond": ("smile", "giggle"),
         "none": ("smile", "shy"), "man": ("grin", "nod")}
_FRIENDLY = {"lust": ("hug", "smile"), "drawn": ("smile", "hug"), "fond": ("hug", "smile"),
             "none": ("smile", "pat"), "man": ("nod", "pat")}
_PLAYFUL = {"man": ("laugh", "shrug"), ANY: ("giggle", "laugh")}
# Any other emote of his at her, but the utility ones: friendly or awkward.
FALLBACK = {"lust": ("smile", "giggle"), "drawn": ("smile", "shy"), "fond": ("smile", "wave"),
            "none": ("smile", "blink", "shrug"), "man": ("shrug", "nod", "confused")}


def _same(*answers: str) -> dict[str, tuple[str, ...]]:
    return {ANY: answers}


# His emote at her -> her emote back, by her stance (emote_back). RUDE_EMOTES go by her temper,
# UTILITY_EMOTES get nothing and anything else FALLBACK.
EMOTE_BACK: dict[str, dict[str, tuple[str, ...]]] = {
    **dict.fromkeys(sorted(ROMANTIC_EMOTES - {"hug", "wink"}), _AFFECTION),
    **dict.fromkeys(("wink", "blush"), _WINK),
    "hug": _same("hug"),
    **dict.fromkeys(sorted(FRIENDLY_EMOTES), _FRIENDLY),
    **dict.fromkeys(("tickle", "poke", "pinch", "pat", "pet", "ruffle", "tease", "bonk"),
                    _PLAYFUL),
    **dict.fromkeys(("cry", "sad"), _same("comfort")),
    "nod": _same("nod"),
    **dict.fromkeys(("wave", "hello", "greet", "hail", "bye", "welcome"), _same("wave")),
    **dict.fromkeys(("laugh", "rofl", "guffaw", "chuckle", "giggle", "joke"), _same("laugh")),
    "dance": _same("dance"),
    "cheer": _same("cheer"),
    **dict.fromkeys(("applaud", "clap", "golfclap"), _same("bow")),
    **dict.fromkeys(("congratulate", "commend", "praise"), _same("thank")),
    "thank": _same("yw"),
    "bow": _same("bow"),
    "salute": _same("salute"),
    "highfive": _same("highfive"),
    "toast": _same("toast"),
    "smile": _same("smile"),
}
# His social emote at nobody -> what the members who join in do.
JOIN_EMOTES: dict[str, tuple[str, ...]] = {
    "dance": ("dance",),
    **dict.fromkeys(("laugh", "rofl", "guffaw", "chuckle"), ("laugh", "chuckle")),
    "cheer": ("cheer",),
    **dict.fromkeys(("applaud", "clap", "golfclap"), ("applaud", "cheer")),
    **dict.fromkeys(("wave", "hello", "greet", "hail"), ("wave",)),
    "bye": ("wave", "bye"),
    "victory": ("cheer", "victory"),
    "toast": ("toast", "cheer"),
    "train": ("laugh",),
}

# The named templates: mind-data/prompts/<name>.txt replaces one.
EMOTE_AT_YOU = "emote_at_you"
EMOTE_JOINED = "emote_joined"
TEMPLATES = {
    EMOTE_AT_YOU: "{name} just did /{emote} at you. React to that in one short line, your way.",
    EMOTE_JOINED: ("{name} just did /{emote}, and you joined in. Say something about it in one "
                   "short line, your way."),
}


class _Line(NamedTuple):
    """A line drawn for a reaction, made into a turn once her emote is out."""
    channel: str
    template: str
    emote: str
    why: str


def stance_key(p: Persona) -> str:
    """Her stance toward him, or "man": a man's stance is never an attitude toward him. A lust
    or drawn woman who flirts with nobody (her forward dial at 0) answers as a fond one."""
    if p.identity.player_gender != "female":
        return "man"
    stance = p.romance.stance
    if stance in ("lust", "drawn") and flirt_chance(p, 1.0) <= 0:
        return "fond"
    return stance


def temper(p: Persona) -> str:
    """His rudeness answered by who she is: anger when she is disagreeable, tears when she is
    easily hurt, else a glare."""
    if p.big_five.agreeableness.score <= PRICKLY:
        return "angry"
    if p.big_five.neuroticism.score >= TOUCHY:
        return "cry"
    return "glare"


def emote_back(emote: str, p: Persona, rng) -> str | None:
    """Her emote back at his `emote`; None for a utility or combat one, or a name native never
    sends."""
    emote = emote.strip().casefold()
    if emote in UTILITY_EMOTES or not _NAME.fullmatch(emote):
        return None
    if emote in RUDE_EMOTES:
        return temper(p)
    table = EMOTE_BACK.get(emote, FALLBACK)
    options = table.get(stance_key(p)) or table.get(ANY)
    return rng.choice(options) if options else None


def line_chance(emote: str) -> float:
    return LINE_P_ROMANTIC if emote in ROMANTIC_EMOTES else LINE_P


def wants_line(emote: str, rng) -> bool:
    """One draw: does this reaction add a line?"""
    return rng.random() < line_chance(emote)


def reply_channel(emote: str) -> str:
    """Where her line after his emote at her goes: a whisper for a romantic one, else /say."""
    return "whisper" if emote in ROMANTIC_EMOTES else "say"


def emote_instruction(name: str, data_dir: Path, *, called: str, emote: str) -> str:
    """The named template (the owner's overlay when there is one; the built-in words when it
    cannot be read, an editor holding it say) with {name} and {emote} filled in; anything else
    in it is used as written."""
    try:
        text = load_prompt(name, data_dir, TEMPLATES[name])
    except OSError as e:
        log.warning("emote_overlay_unreadable: name=%s reason=%s", name, type(e).__name__)
        text = TEMPLATES[name]
    return text.replace("{name}", called).replace("{emote}", emote)


class EmoteLimits:
    """COOLDOWN_S per member and PER_MINUTE reactions guild-wide."""

    def __init__(self, cooldown_s: float = COOLDOWN_S, per_minute: int = PER_MINUTE):
        self.cooldown_s, self.per_minute = cooldown_s, per_minute
        self._last: dict[int, float] = {}
        self._window: deque[float] = deque()

    def refusal(self, guid: int, now: float) -> str | None:
        while self._window and now - self._window[0] >= MINUTE_S:
            self._window.popleft()
        if now - self._last.get(guid, -math.inf) < self.cooldown_s:
            return "cooldown"
        if len(self._window) >= self.per_minute:
            return "per_minute"
        return None

    def take(self, guid: int, now: float) -> None:
        self._last[guid] = now
        self._window.append(now)


class EmoteReactions(Reactions):
    """The engine's layer for his text emotes, on top of the event reactions. TalkEngine.__init__
    calls _init_emotes."""

    def _init_emotes(self) -> None:
        self.emote_limits = EmoteLimits()
        self._replied_at: dict[int, float] = {}  # a member's last REPLY_HUMAN line, said
        self._emoted_at = -math.inf  # his last emote a member answers or joins, or theirs back

    async def on_text_emote(self, ev: TextEmote, now: float, *, event_id: int = 0) -> None:
        """His text emote, which the app has stored (as game_event `event_id`, 0: none) and
        applied to the world. Plans and spawns the reactions; nothing here waits."""
        hg = self.human.guid
        if not hg or ev.guid != hg or ev.via_bridge or not self.world.human_online:
            return
        emote = ev.emote.strip().casefold()
        if not _NAME.fullmatch(emote):
            return  # not a name native sends: nothing to answer
        target = ev.target_guid if ev.target_guid in self.world.cast_guids() else 0
        if emote not in UTILITY_EMOTES and (target or emote in JOIN_EMOTES):
            self._emoted_at = now  # an exchange with the guild: nothing starts talk over it
        quiet = self._login_quiet(now)
        if quiet or self.raid.phase(now) in FIGHT_PHASES:
            log.info("emote_skipped: emote=%s guid=%d reason=%s", emote, target,
                     "login_quiet" if quiet else "fight")
            return
        if target:
            self._answer_emote(target, emote, now, event_id)
        else:
            self._join_emote(emote, now)

    def _his_talk_at(self) -> float:
        """His last line or an answer to him (Reactions._his_talk_at), or his last emote
        exchange with members: his emote at one or his social one, or an emote back."""
        return max(super()._his_talk_at(), self._emoted_at)

    def _first_line_said(self, turn: Turn, meta: TurnMeta) -> None:
        super()._first_line_said(turn, meta)
        if turn.kind is TurnKind.REPLY_HUMAN and not is_discord(turn.channel):
            self._replied_at[turn.speaker] = self.clock.now()

    def _replying(self, guid: int, now: float) -> bool:
        """She has an answer to him planned, or said one in the last REPLY_S: a line after her
        emote would take its place. Her answers in Discord are another place's (ruling R11)."""
        if now - self._replied_at.get(guid, -math.inf) < REPLY_S:
            return True
        return any(t.speaker == guid and t.kind is TurnKind.REPLY_HUMAN
                   and not is_discord(t.channel) for t in self.planned_turns())

    def _emote_blocked(self, guid: int, now: float) -> str | None:
        """Why a member can't react now as existing talk would refuse her, or None."""
        w = self.world
        if guid not in w.online:
            return "offline"
        if guid in w.afk:
            return "afk"
        if guid in self._away_now():
            return "away"
        if self.personas.get(guid) is None:
            return "no_persona"
        if self._is_dead(guid, now):
            return "dead"
        if not self.raid.allows(guid, TurnKind.EMOTE_REACT, now):
            return "raid"
        return None

    def _answer_emote(self, guid: int, emote: str, now: float, event_id: int) -> None:
        """His emote at her: her emote back, and maybe a line after it."""
        why = self._emote_blocked(guid, now) or self.emote_limits.refusal(guid, now)
        back = None if why else emote_back(emote, self.personas.get(guid), self.rng)
        if back is None:
            log.info("emote_skipped: emote=%s guid=%d reason=%s", emote, guid,
                     why or "not_answered")
            return
        self.emote_limits.take(guid, now)
        replying = self._replying(guid, now)
        line = (_Line(reply_channel(emote), EMOTE_AT_YOU, emote, f"emote:{emote}")
                if not replying and wants_line(emote, self.rng) else None)
        log.info("emote_planned: emote=%s guid=%d back=%s line=%s", emote, guid, back,
                 line.channel if line else "replying" if replying else "-")
        self._spawn(self._emote_back(guid, back, self.human.guid,
                                     self.rng.uniform(*EMOTE_BACK_S), line, event_id))

    def _join_emote(self, emote: str, now: float) -> None:
        """His social emote at nobody: 0-2 members in his group, or else in his zone, join in;
        at most one adds a line."""
        options = JOIN_EMOTES.get(emote)
        if options is None:
            return
        hg, w = self.human.guid, self.world
        free = [g for g in self._candidates(hearable=False)
                if self._emote_blocked(g, now) is None
                and self.emote_limits.refusal(g, now) is None]
        grouped = w.in_group(hg)
        pool = ([g for g in free if grouped and w.in_group(g)]
                or [g for g in free if w.human_zone and self._zone_of(g) == w.human_zone])
        joined = []
        for g in self.rng.sample(pool, min(self.rng.randint(*JOINERS), len(pool))):
            if self.emote_limits.refusal(g, now) is None:  # the minute may fill up
                self.emote_limits.take(g, now)
                joined.append(g)
        line = None
        if joined and not self._replying(joined[0], now) and wants_line(emote, self.rng):
            g = joined[0]
            group = w.group if grouped and w.in_group(g) else None
            channel = "say" if group is None else "raid" if group.raid else "party"
            line = _Line(channel, EMOTE_JOINED, emote, f"emote_join:{emote}")
        log.info("emote_join: emote=%s pool=%d joined=%d line=%s", emote, len(pool), len(joined),
                 line.channel if line else "-")
        for i, g in enumerate(joined):
            self._spawn(self._emote_back(g, self.rng.choice(options), 0,
                                         self.rng.uniform(*EMOTE_BACK_S),
                                         line if i == 0 else None))

    def _emote_turn(self, guid: int, line: _Line, now: float) -> tuple[Turn, TurnMeta] | None:
        """Her one short line after her emote, an EMOTE_REACT turn to him; None when
        choose_channel refuses it."""
        channel = choose_channel(TurnKind.EMOTE_REACT, speaker=guid, world=self.world,
                                 raid=self.raid, now=now, rng=self.rng, reply_to=line.channel)
        if channel is None:
            return None
        hg = self.human.guid
        text = emote_instruction(line.template, self.data_dir, called=self.human.called,
                                 emote=line.emote)
        delay = self.rng.uniform(*LINE_AFTER_S) * self._effects(guid).delay_mult
        turn = Turn(kind=TurnKind.EMOTE_REACT, speaker=guid, channel=channel,
                    to_guid=hg if channel == "whisper" else 0, addressee=hg, instruction=text,
                    priority=Priority.REPLY_HUMAN, max_lines=1, delay=delay)
        return turn, TurnMeta(self._new_id(), answers_human=True, why=line.why)

    async def _emote_back(self, guid: int, emote: str, target: int, delay: float,
                          line: _Line | None, event_id: int = 0) -> None:
        """After `delay`, her emote (at `target`, 0 for nobody) if she still may; once it is out,
        the receipt for his emote `event_id` (when it was aimed at her) and her line, if one was
        drawn and she has no answer to him under way."""
        await self.clock.sleep(delay)
        now = self.clock.now()
        why = self._emote_blocked(guid, now) or (
            None if self.world.human_online else "human_offline")
        if why is None:
            try:
                ack = await self.send(Emote(id="", guid=guid, emote=emote, target_guid=target))
            except BridgeError as e:
                why = type(e).__name__
            else:
                why = None if ack.ok else ack.error or "refused"
        if why:
            log.info("emote_dropped: guid=%d emote=%s reason=%s", guid, emote, why)
            return
        log.info("emote_sent: guid=%d emote=%s target=%d", guid, emote, target)
        self._emoted_at = max(self._emoted_at, self.clock.now())
        if event_id and target:
            try:
                self.store.set_meta(answered_key(event_id), str(guid))
            except Exception:  # the receipt only feeds the night: never the reaction
                log.warning("emote_receipt_failed: event=%d guid=%d", event_id, guid)
        if line is None:
            return
        if self._replying(guid, now):
            log.info("emote_line_skipped: guid=%d reason=replying", guid)
            return
        turn = self._emote_turn(guid, line, now)
        if turn is not None:
            self._queue([turn])
