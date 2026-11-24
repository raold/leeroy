"""No sexual wording in the game's public channels (owner, 2026-11-24: the WoW re-measure found
three raid lines with sexual words, flirts the explicit band had asked for on a channel the whole
raid reads).

One hard screen, on every model line typed in guild, officer, party, raid, raid warning, say or
yell, answers he is owed included, whatever [heat] or [talk] say: a draft that hits it is asked
again once clean (HINT), an owed answer's fallback is screened for it too, and an owed answer
whose only problem is this goes to him as a whisper instead (Pipeline._speak_now); the last check
before the bridge drops anything that still hits it. A whisper, a Discord DM, a group DM, a
Discord channel and a voice room are never read here (the voice rule has its own screen).

The words are the [heat] voice rule's (heat/guard.py voice_hit: its list, the public guard's and
the owner's prompts/heat/public_block.txt) and a few phrases more (MORE). Words a raid uses in
play are kept out: "on top of you" (the meters), "ride" alone (mounts), "inside" alone (the
instance), "suck" alone, "hot" alone.

Pure but for the owner's block list, read through heat/overlays.py."""
from __future__ import annotations

import re
from pathlib import Path

from leeroy_mind.talk.wowtopic import PUBLIC_CHANNELS

TELL = "public sexual"
HINT = ("Other people read this channel: nothing sexual, nothing about bodies, undressing or bed. "
        "Keep it clean; anything more belongs in a whisper.")
OWED = "others read this channel, so nothing sexual"
MORE = re.compile(
    r"\b(?:inside (?:of )?me|feel (?:you|u) inside|(?:you|u) inside (?:of )?me"
    r"|ride (?:you|u|me)|mak(?:e|ing) love|turn(?:s|ed|ing)? (?:me|you|u) on|turned on"
    r"|hot for (?:you|u|me)|spank(?:ing)? (?:me|you|u)|lick (?:you|u|me)"
    r"|go(?:ing)? down on (?:you|u|me)|breasts?|booty call|take (?:you|u|me) to bed"
    r"|between (?:my|your|ur) (?:legs|thighs)|(?:my|your|ur) (?:legs|thighs) apart"
    r"|make (?:me|you|u) scream|scream(?:ing)? (?:my|your|ur) name|(?:you|u|me) face down"
    r"|bend (?:you|u) over|bent over (?:my|your|ur|the) (?:desk|bed|table)"
    r"|pin (?:you|u|me) down)\b",
    re.IGNORECASE)


def public_channel(channel: str) -> bool:
    """A game channel others read (PUBLIC_CHANNELS)."""
    return isinstance(channel, str) and channel in PUBLIC_CHANNELS


def sexual(text: str, data_dir: Path) -> bool:
    """`text` holds sexual wording: the voice rule's screen (heat.guard.voice_hit) or MORE."""
    if not text:
        return False
    from leeroy_mind.heat.guard import voice_hit
    return bool(MORE.search(text)) or voice_hit(text, data_dir)


def screened(channel: str, text: str, data_dir: Path) -> bool:
    """A line on a public game channel with sexual wording: never posted there."""
    return public_channel(channel) and sexual(text, data_dir)


__all__ = ["HINT", "MORE", "OWED", "PUBLIC_CHANNELS", "TELL", "public_channel", "screened",
           "sexual"]
