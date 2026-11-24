"""WoW turns stay on WoW ([talk] wow_topic, default true; owner, 2026-11-20). A blind A/B of 176
WoW-context turns, the new live model against Orion, found the facts on par and the talk worse:
replies drifting into real life mid-raid (a loot channel's answer turned into a shift at work),
because the model takes her voice sheet's "Unasked, you bring up" hooks literally; a written
register (capitalised full sentences without contractions, "It is ...", "dear" and "honey" in
raid chat); persona tics said verbatim; lines running long (mostly her flirt mid-raid); DKP and
gold mixed up in loot talk. The pipeline reads this module only with the switch on, and only on
a WoW turn (`talk/grounding.py`'s `wow_turn`, plus say and yell while he is in his raid): a
whisper, a DM, a group DM and #general are never touched.

- `held`: the turns the topic hold is for, and `hold_text` its one line ("Stay on ..."). A held
  turn's voice block leaves out the topics she brings up unasked, and her Discord "where you
  live" line. A turn whose own instruction asks for something else is never held: an opener
  (unless the engine drew it a game topic), a bit, a flirt, a greeting or goodbye, an emote's
  answer, and a reply to a line that asks about her life (`asks_life`).
- `loot_turn` and `DKP_RULE`: a loot channel's turn, or one whose line or instruction is about
  loot, is told that bids are DKP and that DKP is hers alone; `dkp_transfer` reads a line that
  treats it as something to hand over (a soft tell).
- `public_flirt`, `public_flirt_line` and the PUBLIC_FLIRT_ constants: her flirts on the game's
  public channels (whisper or nothing while he raids, else rare, one line, generated short).
- `game_topics`: an opener drawn on the raid's or the party's channel while he raids brings up
  a game topic of hers (what she wants in game), never her life.
- `predict` and `trim`: on the game's typed channels only (guild, officer, party, raid, raid
  warning; never a whisper, Discord or a voice room) a tighter num_predict, and a draft past her
  usual words cut back to its first line, else its first sentence, else its leading whole
  clauses (talk/length.py's `_cut`); never fewer than MIN_WORDS words, never an answer to a line
  of his that asks something (the owner's rule: a cut never removes his answer).

Pure: no clock, no I/O, no logging."""
from __future__ import annotations

import math
import re

from leeroy_mind.talk.turns import TurnKind

GAME_CHANNELS = frozenset({"guild", "officer", "party", "raid", "raid_warning"})
RAID_CHANNELS = frozenset({"party", "raid", "raid_warning"})
HIS_RAID_CHANNELS = frozenset({"say", "yell"})  # a WoW turn only while he raids
# The kinds whose instruction is the channel's business: an answer, a follow-on, an aside, the
# raid's own calls and reactions. Not an opener (its topic is its instruction), a bit, a flirt,
# a greeting, a goodbye or an emote's answer.
HELD_KINDS = frozenset({TurnKind.REPLY_HUMAN, TurnKind.CONTINUE, TurnKind.ASIDE,
                        TurnKind.CALLOUT, TurnKind.URGENT, TurnKind.KILL, TurnKind.WIPE_REACT,
                        TurnKind.GZ})

HOLD = "Stay on what this channel is about ({about}): bring up your job, home or day only if someone asks."
# What each WoW channel is about, in the words of the hold line
ABOUT_GAME = {"guild": "the game and the guild", "officer": "guild business",
              "party": "the group's run", "raid": "the raid", "raid_warning": "the raid",
              "say": "the raid", "yell": "the raid"}
ABOUT_DISCORD = {"loot-and-dkp": "loot and DKP", "strategy": "boss strategy",
                 "logs": "how the pulls went", "raid-planning": "the next raid's lineup",
                 "raid-schedule": "the raid schedule", "announcements": "guild news",
                 "officer-chat": "guild business", "pvp-arena": "battlegrounds and arena",
                 "professions-and-gold": "professions and gold",
                 "achievements-and-mounts": "achievements and mounts",
                 "lfg-and-alts": "groups and alts"}
ABOUT_ROOM = {"raid": "the raid", "raid2": "the raid", "officers": "guild business",
              "tanks": "tanking", "healers": "healing", "casters": "caster play",
              "melee": "melee play", "loot_dkp": "loot and DKP", "lore": "lore and boss mechanics"}
ABOUT_CLASS = "your class"
ABOUT_DEFAULT = "the game"

DKP_RULE = ("Loot here goes by DKP: bids are DKP, never gold, and bind-on-pickup drops aren't "
            "traded. DKP is each raider's own: it can't be given, lent or pooled.")
# A line that treats DKP as something to hand over (the re-measure, 2026-11-24: two loot lines
# offered a member DKP or asked for hers): a soft tell on a WoW turn, asked again with DKP_HINT
# and dropped when the retry still has it (Pipeline._generate)
DKP_TELL = "dkp transfer"
DKP_HINT = ("DKP is each raider's own to bid: it can't be given, lent, handed over or pooled. "
            "Say it another way.")
_WHO = r"(?:you|u|ya|me|him|her|them|us)"
_POSS = r"(?:my|your|ur|his|her|our|their)"
_GIVE = (r"(?:give|gives|gave|giving|hand|hands|handed|handing|lend|lends|lent|lending|loan|"
         r"loaned|loaning|send|sends|sent|sending|transfer|transferred|transferring|pass|passed|"
         r"passing|donate|donated|donating|gift|gifted|gifting|pool|pooled|pooling|borrow|"
         r"borrowed|borrowing|share|shared|sharing|spot|spotting)")
_DKP_TRANSFER = re.compile(
    rf"\b{_GIVE}\s+{_WHO}\s+(?:(?:{_POSS}|some|all|extra|spare|\d+)\s+)?dkp\b"
    rf"|\b{_GIVE}\s+(?:{_POSS}|all(?: of)? {_POSS}|some of {_POSS})\s+dkp\b"
    rf"|\b{_GIVE}\s+\d+\s+dkp\s+(?:over\s+)?to\s+{_WHO}\b"
    rf"|\b(?:got|have|has)\s+(?:(?:some|extra|spare|\d+)\s+)?dkp\s+for\s+{_WHO}\b"
    rf"|\b(?:pool|combine|merge)\s+(?:our|their|your|ur)\s+dkp\b",
    re.IGNORECASE)
LOOT_CHANNELS = frozenset({"discord:loot-and-dkp", "voice:loot_dkp"})
# (never "os" alone: the Obsidian Sanctum; never "roll" alone: a roll in a fight)
_LOOT = re.compile(r"\b(?:loot(?:ed|ing|s)?|dkp|bids?|bidd(?:ing|ed)|ms ?/ ?os|ms or os|mainspec|"
                   r"offspec|main spec|off spec|loot council|roll(?:s|ed|ing)? (?:on|for)|tokens?)\b|"
                   r"\{\{item:\d+\}\}|\[[^\[\]\n]{2,60}\]", re.IGNORECASE)

# A line that asks about her life ("how was work", "kids asleep?"): the reply may go there. Never
# a word the game uses too: "good job", "the boss", "cat form", "my pet", "hearth home".
_LIFE = re.compile(r"\b(?:irl|real life|(?:at|from|after|before|off|for|to) work|"
                   r"work(?:ing)? (?:today|tomorrow|tonight|late|early)|how(?:'s|s| is| was) work|"
                   r"(?:your|my|her|day|new|old) job|shifts?|office|kids?|children|husband|wife|"
                   r"boyfriend|girlfriend|family|mum|mom|dad|baby|dog|weekend|school|college|"
                   r"dinner|lunch|breakfast|weather|sleep|(?:at|back|got) home|house)\b",
                   re.IGNORECASE)

GAME_TOPIC = "the raid tonight, and how it's going for you"

# Her flirts at him on the game's public channels (the re-measure, 2026-11-24: flirt turns sent
# in /raid and /guild rose from 1 to 25 in three days, with similar play; trimmed to ~7 words
# they stopped being flirts). While he raids (in a raid group or a raid instance) a flirt never
# goes on a public channel: she whispers him instead when the whisper planner has room, or the
# turn stays what it was (TalkEngine._social_turn). Elsewhere a social turn becomes one at
# PUBLIC_FLIRT_SHARE of her flirt chance, at most one every PUBLIC_FLIRT_GAP_S guild-wide, and
# it is generated as one line of PUBLIC_FLIRT_WORDS or fewer (its num_predict, its shape and
# PUBLIC_FLIRT_LINE), never cut down: a draft still too long after the retry is dropped.
PUBLIC_CHANNELS = frozenset({"guild", "officer", "party", "raid", "raid_warning", "say", "yell"})
PUBLIC_FLIRT_SHARE = 0.25
PUBLIC_FLIRT_GAP_S = 900.0
PUBLIC_FLIRT_WORDS = 10
PUBLIC_FLIRT_LINE = ("Everyone in {where} reads this: one light line, {words} words at most, "
                     "and save anything more for a whisper.")
_WHERE = {"guild": "the guild", "officer": "officer chat", "party": "the party",
          "raid": "the raid", "raid_warning": "the raid", "say": "earshot", "yell": "the zone"}
# What her whisper says set it off (reactions.WHISPER_WHY "raid_flirt")
RAID_FLIRT_WHY = ("You're in {name}'s raid and you'd rather say this where only {they} can "
                  "read it.")

MIN_WORDS = 3  # a trim never leaves fewer words
PREDICT_MIN, PREDICT_MAX = 24, 80  # a game turn's num_predict: never above the live preset's
PREDICT_PER_WORD, PREDICT_PAD = 1.6, 16


def wow_topic_channel(channel: str, *, his_raid: bool) -> bool:
    """A channel the topic rules are for (the pipeline also needs the switch and no template):
    grounding.wow_turn's, and say or yell while he is in his raid."""
    from leeroy_mind.talk.grounding import wow_turn
    return wow_turn(channel) or (his_raid and channel in HIS_RAID_CHANNELS)


def about(channel: str) -> str:
    """What the hold line says the channel is about."""
    if channel in ABOUT_GAME:
        return ABOUT_GAME[channel]
    if channel.startswith("discord:"):
        from leeroy_mind.discord.layout import CLASS_CHANNEL
        cid = channel.removeprefix("discord:")
        if cid in CLASS_CHANNEL.values():
            return ABOUT_CLASS
        return ABOUT_DISCORD.get(cid, ABOUT_DEFAULT)
    if channel.startswith("voice:"):
        from leeroy_mind.voice.rooms import CLASS_ROOM
        room = channel.removeprefix("voice:")
        if room in CLASS_ROOM.values():
            return ABOUT_CLASS
        return ABOUT_ROOM.get(room, ABOUT_DEFAULT)
    return ABOUT_DEFAULT


def hold_text(channel: str) -> str:
    return HOLD.format(about=about(channel))


def asks_life(text: str) -> bool:
    """The line names her life: her work, home, family, pets, meals, sleep or the weather."""
    return bool(_LIFE.search(text or ""))


def held(kind: TurnKind, line: str = "", *, game_topic: bool = False) -> bool:
    """The topic hold is for this turn (on a WoW turn with the switch on): one of HELD_KINDS
    answering a `line` that does not ask about her life, or an opener whose topic the engine drew
    from her game topics (`game_topic`)."""
    if kind is TurnKind.OPEN:
        return game_topic
    return kind in HELD_KINDS and not asks_life(line)


def loot_turn(channel: str, texts=()) -> bool:
    """A loot channel's turn, or one whose line, topic or instruction (`texts`) is about loot."""
    return channel in LOOT_CHANNELS or any(_LOOT.search(t or "") for t in texts)


def dkp_transfer(text: str) -> bool:
    """The line treats DKP as something one raider can give, lend, hand over or pool."""
    return bool(_DKP_TRANSFER.search(text or ""))


def public_flirt(kind: TurnKind, channel: str) -> bool:
    """A FLIRT typed on a game channel others read (guild, officer, party, raid, say, yell):
    rare, one short line, generated short (Pipeline._public_flirt)."""
    return kind is TurnKind.FLIRT and channel in PUBLIC_CHANNELS


def public_flirt_line(channel: str, words: int = PUBLIC_FLIRT_WORDS) -> str:
    """PUBLIC_FLIRT_LINE for a flirt on `channel`."""
    return PUBLIC_FLIRT_LINE.format(where=_WHERE.get(channel, "the guild"), words=words)


def game_topics(wants) -> list[str]:
    """An opener's topics on the raid's channel while he raids: what she wants in game, else
    GAME_TOPIC."""
    out = [f"how close you are to {w} in game" for w in (" ".join(str(x).split()).rstrip(".")
                                                          for x in wants) if w]
    return out or [GAME_TOPIC]


def game_channel(channel: str) -> bool:
    """The game's typed channels, where the length rule here applies."""
    return channel in GAME_CHANNELS


def predict(words: int) -> int:
    """The num_predict of a game turn whose lines hold `words` words."""
    return max(PREDICT_MIN, min(PREDICT_MAX, math.ceil(PREDICT_PER_WORD * max(0, words))
                                + PREDICT_PAD))


_SENTENCE = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s|$)")


def trim(text: str, limit: int, *, floor: int = MIN_WORDS) -> str | None:
    """`text` cut back to fit `limit` words: its first line, else its first sentence, else its
    leading whole clauses (never ending on a dangling word or inside a quote); None when it
    already fits or no cut leaves `floor` words or more."""
    from leeroy_mind.talk.length import _clauses, _cut
    words = (text or "").split()
    if limit <= 0 or len(words) <= limit:
        return None
    floor = max(MIN_WORDS, floor)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if len(lines) > 1 and floor <= len(lines[0].split()) <= limit:
        return lines[0]
    flat = " ".join(words)
    m = _SENTENCE.search(flat)
    if m and m.end() < len(flat):
        head = flat[:m.end()].strip()
        if floor <= len(head.split()) <= limit:
            return head
    cut = _cut(_clauses(flat), 0, limit, sentence=False, floor=floor)
    return cut or None


__all__ = ["ABOUT_CLASS", "DKP_HINT", "DKP_RULE", "DKP_TELL", "GAME_CHANNELS", "GAME_TOPIC",
           "HELD_KINDS", "HOLD", "LOOT_CHANNELS", "MIN_WORDS", "PUBLIC_CHANNELS",
           "PUBLIC_FLIRT_GAP_S", "PUBLIC_FLIRT_LINE", "PUBLIC_FLIRT_SHARE", "PUBLIC_FLIRT_WORDS",
           "RAID_FLIRT_WHY", "about", "asks_life", "dkp_transfer", "game_channel", "game_topics",
           "held", "hold_text", "loot_turn", "predict", "public_flirt", "public_flirt_line", "trim",
           "wow_topic_channel"]
