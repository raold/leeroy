"""Members react to the human's text emotes (owner, 2026-11-07). The app hands the engine his
live text emotes while he is online (on_text_emote); a member's own, another player's, the
mind's echoes and replayed ones never come here.

- His emote at an online cast member with a persona: after EMOTE_BACK_S she emotes back at him,
  from a small table keyed by his emote and her stance toward him (emote_back): a woman's lust,
  drawn, fond or none, and "man". Men read as friends, so they get friendly or awkward emotes,
  never a romantic one, and so does a woman with no attitude toward him. His rudeness
  (RUDE_EMOTES) is answered by her temper: a glare, anger or tears. An emote the table does not
  know gets nothing. Once her emote is out she sometimes says one short line (wants_line:
  LINE_P_ROMANTIC for a romantic emote, else LINE_P), an EMOTE_REACT turn through the ordinary
  pipeline that is told exactly what he did (the template EMOTE_AT_YOU): a romantic one is
  answered by whisper, any other by /say, since his emote's target is always within emote range
  (channels.EMOTE_CHANNELS).
- His social emote at nobody (JOIN_EMOTES; also one at someone outside the cast, an NPC or a
  stranger): 0-2 members in his group, or with nobody free there those in his zone, join in with
  a fitting emote of their own, at nobody. At most one of them adds a line (EMOTE_JOINED), on his
  group's chat when grouped with him, else /say.
- Limits: COOLDOWN_S per member and PER_MINUTE reactions guild-wide, counted when a reaction is
  planned (EmoteLimits). Nothing in a fight (COMBAT, WIPE_SILENCE, WIPE_DEBRIEF), in the login
  quiet, nor from a member existing talk would refuse (offline, AFK, no persona, dead, the raid
  rules). The emote back checks again after its delay, and her line is queued only once her
  emote went out. Emotes and lines leave through the engine's send, so the silent latch holds
  them too.

The night's attraction pass counts his romantic emotes at a woman with an attitude as a flirt and
his rude ones as a hurt (nightly/attraction.py reads ROMANTIC_EMOTES and RUDE_EMOTES); her line
is an ordinary sent line, remembered like any other. The words are two named prompt templates:
the owner's overlay mind-data/prompts/<name>.txt replaces one, with {name} (what he is called)
and {emote} filled in and anything else, braces too, used as written. Built-in words stay
non-explicit. Logs carry guids, emote names and outcomes, never chat text."""
import math
import re
from collections import deque
from pathlib import Path

from leeroy_mind.bridge import BridgeError
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.personas.schema import Persona
from leeroy_mind.prompts import load_prompt
from leeroy_mind.protocol import Emote, TextEmote
from leeroy_mind.talk.channels import choose_channel
from leeroy_mind.talk.pipeline import TurnMeta, log
from leeroy_mind.talk.reactions import FIGHT_PHASES, Reactions
from leeroy_mind.talk.turns import Turn, TurnKind

EMOTE_BACK_S = (2.0, 6.0)  # her emote back, after his
LINE_AFTER_S = (1.0, 3.0)  # her line's delay once her emote is out, before the model and typing
COOLDOWN_S = 45.0  # one reaction a member in this long
PER_MINUTE = 6  # reactions guild-wide in a minute, at most
MINUTE_S = 60.0
LINE_P_ROMANTIC, LINE_P = 0.5, 0.25  # how often a reaction adds a line
JOINERS = (0, 2)  # members joining his social emote

# His emotes a line answers by whisper and the night counts as a flirt at her.
ROMANTIC_EMOTES = frozenset({"kiss", "love", "flirt", "cuddle", "hug", "wink", "sexy",
                             "holdhand", "charm"})
# His emotes answered by her temper and counted as a hurt at night.
RUDE_EMOTES = frozenset({"rude", "slap", "spit", "insult", "mock", "smack", "punch", "threaten",
                         "snub", "shoo"})
PRICKLY, TOUCHY = 3, 7  # agreeableness at or below: angry; neuroticism at or above: tears
_NAME = re.compile(r"[a-z0-9_]{1,32}")  # an emote name as native sends one (LeeroyEmotes.cpp)

ANY = "*"  # a table's answer for everyone its stances don't name
# her stance toward him ("lust", "drawn", "fond", "none"), or "man"
_AFFECTION = {"lust": ("kiss", "blush"), "drawn": ("blush", "giggle"), "fond": ("hug", "laugh"),
              "none": ("shy", "giggle"), "man": ("blink", "laugh", "shrug")}
_WINK = {"lust": ("wink", "kiss"), "drawn": ("blush", "giggle"), "fond": ("smile", "giggle"),
         "none": ("smile", "shy"), "man": ("grin", "nod")}
_PLAYFUL = {"man": ("laugh", "shrug"), ANY: ("giggle", "laugh")}


def _same(*answers: str) -> dict[str, tuple[str, ...]]:
    return {ANY: answers}


# His emote at her -> her emote back, by her stance (emote_back). RUDE_EMOTES go by her temper.
EMOTE_BACK: dict[str, dict[str, tuple[str, ...]]] = {
    **dict.fromkeys(("kiss", "love", "flirt", "sexy", "charm", "cuddle", "holdhand"), _AFFECTION),
    **dict.fromkeys(("wink", "blush"), _WINK),
    "hug": _same("hug"),
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


def stance_key(p: Persona) -> str:
    """Her stance toward him, or "man": a man's stance is never an attitude toward him."""
    return p.romance.stance if p.identity.player_gender == "female" else "man"


def temper(p: Persona) -> str:
    """His rudeness answered by who she is: anger when she is disagreeable, tears when she is
    easily hurt, else a glare."""
    if p.big_five.agreeableness.score <= PRICKLY:
        return "angry"
    if p.big_five.neuroticism.score >= TOUCHY:
        return "cry"
    return "glare"


def emote_back(emote: str, p: Persona, rng) -> str | None:
    """Her emote back at his `emote`, or None when the table does not answer it."""
    emote = emote.strip().casefold()
    if emote in RUDE_EMOTES:
        return temper(p)
    table = EMOTE_BACK.get(emote)
    if table is None:
        return None
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
    """The named template (the owner's overlay when there is one) with {name} and {emote}
    filled in; anything else in it is used as written."""
    text = load_prompt(name, data_dir, TEMPLATES[name])
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

    async def on_text_emote(self, ev: TextEmote, now: float) -> None:
        """His text emote, which the app has stored and applied to the world. Plans and spawns
        the reactions; nothing here waits."""
        hg = self.human.guid
        if not hg or ev.guid != hg or ev.via_bridge or not self.world.human_online:
            return
        emote = ev.emote.strip().casefold()
        if not _NAME.fullmatch(emote):
            return  # not a name native sends: nothing to answer
        target = ev.target_guid if ev.target_guid in self.world.cast_guids() else 0
        quiet = self._login_quiet(now)
        if quiet or self.raid.phase(now) in FIGHT_PHASES:
            log.info("emote_skipped: emote=%s guid=%d reason=%s", emote, target,
                     "login_quiet" if quiet else "fight")
            return
        if target:
            self._answer_emote(target, emote, now)
        else:
            self._join_emote(emote, now)

    def _emote_blocked(self, guid: int, now: float) -> str | None:
        """Why a member can't react now as existing talk would refuse her, or None."""
        w = self.world
        if guid not in w.online:
            return "offline"
        if guid in w.afk:
            return "afk"
        if self.personas.get(guid) is None:
            return "no_persona"
        if self._is_dead(guid, now):
            return "dead"
        if not self.raid.allows(guid, TurnKind.EMOTE_REACT, now):
            return "raid"
        return None

    def _answer_emote(self, guid: int, emote: str, now: float) -> None:
        """His emote at her: her emote back, and maybe a line after it."""
        why = self._emote_blocked(guid, now) or self.emote_limits.refusal(guid, now)
        back = None if why else emote_back(emote, self.personas.get(guid), self.rng)
        if back is None:
            log.info("emote_skipped: emote=%s guid=%d reason=%s", emote, guid,
                     why or "not_answered")
            return
        self.emote_limits.take(guid, now)
        line = (self._emote_line(guid, reply_channel(emote), EMOTE_AT_YOU, emote, now,
                                 why=f"emote:{emote}")
                if wants_line(emote, self.rng) else None)
        log.info("emote_planned: emote=%s guid=%d back=%s line=%s", emote, guid, back,
                 line[0].channel if line else "-")
        self._spawn(self._emote_back(guid, back, self.human.guid,
                                     self.rng.uniform(*EMOTE_BACK_S), line))

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
        if joined and wants_line(emote, self.rng):
            g = joined[0]
            group = w.group if grouped and w.in_group(g) else None
            channel = "say" if group is None else "raid" if group.raid else "party"
            line = self._emote_line(g, channel, EMOTE_JOINED, emote, now,
                                    why=f"emote_join:{emote}")
        log.info("emote_join: emote=%s pool=%d joined=%d line=%s", emote, len(pool), len(joined),
                 line[0].channel if line else "-")
        for i, g in enumerate(joined):
            self._spawn(self._emote_back(g, self.rng.choice(options), 0,
                                         self.rng.uniform(*EMOTE_BACK_S),
                                         line if i == 0 else None))

    def _emote_line(self, guid: int, channel: str, template: str, emote: str, now: float, *,
                    why: str) -> tuple[Turn, TurnMeta] | None:
        """Her one short line after her emote, an EMOTE_REACT turn to him; None when
        choose_channel refuses it."""
        channel = choose_channel(TurnKind.EMOTE_REACT, speaker=guid, world=self.world,
                                 raid=self.raid, now=now, rng=self.rng, reply_to=channel)
        if channel is None:
            return None
        hg = self.human.guid
        text = emote_instruction(template, self.data_dir, called=self.human.called, emote=emote)
        delay = self.rng.uniform(*LINE_AFTER_S) * self._effects(guid).delay_mult
        turn = Turn(kind=TurnKind.EMOTE_REACT, speaker=guid, channel=channel,
                    to_guid=hg if channel == "whisper" else 0, addressee=hg, instruction=text,
                    priority=Priority.REPLY_HUMAN, max_lines=1, delay=delay)
        return turn, TurnMeta(self._new_id(), answers_human=True, why=why)

    async def _emote_back(self, guid: int, emote: str, target: int, delay: float,
                          line: tuple[Turn, TurnMeta] | None) -> None:
        """After `delay`, her emote (at `target`, 0 for nobody) if she still may, then her
        line, if one was drawn, once the emote went out."""
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
        if line is not None:
            self._queue([line])
