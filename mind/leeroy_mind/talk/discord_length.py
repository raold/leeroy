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
# setting itself): 55% quick, 30% a couple of sentences, 15% a paragraph. The quick share is
# split (2026-11-15): 30% only a few words ("lmao same", "wait which boss"), 25% a quick line;
# asked for 20 words, the model wrote about 20, so only 6% of channel messages had 7 or fewer.
# A conversation's opener and an answer to him keep the old mix (opener_draw).
CHANNEL_BANDS = ((0.30, 6), (0.55, 20), (0.85, 45), (1.0, 0))
SHORT_WORDS, QUICK_WORDS, FEW_WORDS = 6, 25, 50  # the wording each band reads (prompt.discord_length)
# A quick burst (server.engine, realism.plan) is a couple of sentences split into 2-3 messages:
# its draw falls in that band.
BURST_BAND = (0.55, 0.85)


def burst_draw(rng) -> float:
    """A channel message's band draw for a burst: within BURST_BAND (a couple of sentences)."""
    low, high = BURST_BAND
    return low + (high - low) * rng.random()


def opener_draw(rng) -> float:
    """The band draw of a server conversation's opener, which carries its subject, and of an
    answer to him (Pipeline._discord_words), which owes him something real: never only a few
    words; the mix these had before the few-words band (55% a quick line, 30% a couple of
    sentences, 15% a paragraph)."""
    u = rng.random()
    short_edge, quick_edge = CHANNEL_BANDS[0][0], CHANNEL_BANDS[1][0]
    return u if u >= quick_edge else short_edge + u * (quick_edge - short_edge) / quick_edge


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


def fit_words(text: str, words: int) -> str:
    """A reply over `words` words cut to that many, then back to its last sentence end
    (trim_to_sentence); as it is when it is no longer. An owed answer is cut this way instead
    of being asked again for its length (2026-11-15)."""
    parts = text.split()
    return text if len(parts) <= words else trim_to_sentence(" ".join(parts[:words]))


def trim_to_sentence(text: str) -> str:
    """A reply cut off by num_predict, back to its last sentence end (_SENTENCE_END); as it is
    when it has none, or when it ends on one."""
    ends = [m.end() for m in _SENTENCE_END.finditer(text)]
    return text[:ends[-1]].rstrip() if ends else text
