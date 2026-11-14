"""Longer member messages on the guild's Discord (owner, 2026-11-13: "Long and chatty").

`[talk] discord_words` (0-200, default 90) is about the most words a member's Discord message
runs to. A Discord turn (discord.channels.is_discord: a DM `dm:<guid>` or a channel
`discord:<id>`) is told to write the way people write on Discord (prompt.DISCORD_LENGTH, in place
of its template's one short line), gets the model room for it (preset_for: num_predict only,
never num_ctx or the blank-line stop), has a reply Ollama cut at num_predict trimmed back to its
last sentence end (trim_to_sentence), and keeps its words through the "long" tell and the typing
(Pipeline._word_budget, _style), typed within TYPING_CAP_S. 0 is the one short line on Discord,
exactly as before. A game turn never changes."""
import math
import re
from dataclasses import replace

from leeroy_mind.discord.channels import is_discord, is_private
from leeroy_mind.llm.presets import Preset
from leeroy_mind.text import EMOJI_RE

TYPING_CAP_S = 25.0  # the "is typing" a long Discord message shows, at most (realism's too)
TOKENS_PER_WORD = 1.6
SPARE_TOKENS = 24
# A sentence end: ".", "!", "?" or "…" (a run of them, with any closing quote or bracket after
# it) or an emoji, followed by a space or the end of the text; "3.5" and "example.com" are none.
_SENTENCE_END = re.compile(r"(?:[.!?…]+[\"'”’)\]]*|" + EMOJI_RE.pattern + r")(?=\s|$)")


# Channels (and group DMs) mix lengths the way a group chat does (owner, 2026-11-13): most
# messages a quick line, some a couple of sentences, now and then a paragraph of discord_words.
# His DMs are as full as she likes, up to dm_words. (draws below this edge, words; 0 is the
# setting itself): 55% quick, 30% a couple of sentences, 15% a paragraph.
CHANNEL_BANDS = ((0.55, 20), (0.85, 45), (1.0, 0))
QUICK_WORDS, FEW_WORDS = 25, 50  # the wording each band reads (prompt.discord_length)


def words_for(talk, channel) -> int:
    """The most words a turn on `channel` may run to under `talk` (MindConfig.talk): dm_words in
    a DM with him (discord_words for a config without it), discord_words on another Discord
    channel, else 0 (the game, or the setting at 0: one short line)."""
    if not is_discord(channel):
        return 0
    n = getattr(talk, "discord_words", 0)
    if is_private(channel):
        n = getattr(talk, "dm_words", n)
    return n if type(n) is int and n > 0 else 0


def band(cap: int, draw: float) -> int:
    """A channel message's words for its `draw` in [0, 1): its CHANNEL_BANDS band, never over
    `cap` (the setting)."""
    for edge, words in CHANNEL_BANDS:
        if draw < edge:
            return min(cap, words or cap)
    return cap


def num_predict(words: int, floor: int = 80) -> int:
    """Room for a reply of `words`: about TOKENS_PER_WORD tokens a word and SPARE_TOKENS more,
    never under `floor` (LIVE's own num_predict). 90 words: 168."""
    return max(floor, math.ceil(words * TOKENS_PER_WORD) + SPARE_TOKENS)


def preset_for(preset: Preset, words: int) -> Preset:
    """`preset` (LIVE, or LIVE_BIT) for one Discord request of `words`: num_predict only, so
    num_ctx (a different value reloads the model) and the stop stay as they are."""
    options = dict(preset.options)
    options["num_predict"] = num_predict(words, options.get("num_predict", 80))
    return replace(preset, options=options)


def trim_to_sentence(text: str) -> str:
    """A reply cut off by num_predict, back to its last sentence end (_SENTENCE_END); as it is
    when it has none, or when it ends on one."""
    ends = [m.end() for m in _SENTENCE_END.finditer(text)]
    return text[:ends[-1]].rstrip() if ends else text
