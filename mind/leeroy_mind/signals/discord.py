"""What his next Discord message and his emoji say about a member's line. Pure functions over
text held only for the call: every result is a kind and a number, never a word of either side.

The lexicons are the ones talk already reads him with: a question (talk.asking), a laugh
(server.reacting), warmth and flirting (life.state, talk.filters), pushback (talk.select), plus
the few cues below. They are narrow lexical heuristics with no calibrated accuracy: a missing
kind is not its opposite, and his silence is never one of them."""
import re

WINDOW_S = 1800.0     # his next message in that conversation counts as his reply this long
TURNS_SCALE = 5       # turns_after is min(his further replies in the conversation / 5, 1)
MIN_TOPIC_WORDS = 1   # shared content words that keep a topic

_VS16 = '️'
POSITIVE_EMOJI = frozenset(e.replace(_VS16, '') for e in (
    '👍', '❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '💕', '💖', '💗', '💘', '💞', '😍', '🥰', '😘',
    '😻', '🫶', '😂', '🤣', '😆', '😄', '😁', '😊', '☺️', '🙂', '😀', '😎', '🤩', '🔥', '💯', '👏',
    '🙌', '🎉', '💪', '💀', '😈', '😏', '🤤', '👌', '✅', '🥹', '🤗', '👀'))
NEGATIVE_EMOJI = frozenset(e.replace(_VS16, '') for e in (
    '👎', '😡', '😠', '🤬', '🤮', '🤢', '🙄', '😒', '💩', '🤦', '🤦‍♂️', '🤦‍♀️', '🤨', '😐', '😑',
    '🚫', '❌', '🥱', '😴', '🤡'))
_HEART = re.compile('[❤🧡💛💚💙💜💕💖💗💘💞😍🥰😘🫶]')
# Beyond talk.select's pushback: he says she repeats herself, missed his question, or should stop.
_OBJECT = re.compile(
    r"\b(?:you (?:already|just) (?:said|told me|asked)(?: me)?(?: that| this)?"
    r"|(?:you )?said that already|you(?:'re| are|re) repeating yourself|stop repeating"
    r"|not what i (?:asked|said|meant)|i didn'?t (?:ask|say) that|that'?s not what i (?:asked|said|meant)"
    r"|(?:that|this|it) (?:makes|made) no sense|answer (?:the|my) question)\b"
    r"|^\W*(?:please |pls |ok |okay )?stop(?: it| that| saying\b.*| doing\b.*| asking\b.*)?\W*$", re.I)


def reaction_kind(emoji):
    """'react_positive', 'react_negative' or None for an emoji of his on a member's message. The
    guild's own emoji (:tank:, a class) and anything unlisted say nothing."""
    if type(emoji) is not str:
        return None
    plain = emoji.replace(_VS16, '')
    if plain in POSITIVE_EMOJI:
        return 'react_positive'
    if plain in NEGATIVE_EMOJI:
        return 'react_negative'
    return None


def _question(text):
    from leeroy_mind.talk.asking import is_question
    return '?' in text or is_question(text)


def read(his, hers):
    """{kind: 1.0} for his reply `his` to her line `hers` (both read here and dropped)."""
    from leeroy_mind.life.state import WARM_RE
    from leeroy_mind.nightly.evidence import content_words
    from leeroy_mind.server.reacting import _LAUGH
    from leeroy_mind.talk.filters import flirty
    from leeroy_mind.talk.select import pushback_kind
    his, hers = his or '', hers or ''
    plain = his.replace('’', "'").replace('ʼ', "'")
    out = {}
    objected = pushback_kind(plain) is not None or _OBJECT.search(plain) is not None
    if objected:
        out['objected'] = 1.
    asked = _question(plain)
    if asked and not objected:  # "what?" is pushback, not interest
        out['asked_back'] = 1.
    if _LAUGH.search(plain):
        out['laughed'] = 1.
    if not objected and (flirty(plain) or WARM_RE.search(plain) or _HEART.search(plain)):
        out['affection'] = 1.
    mine, theirs = content_words(plain, ()), content_words(hers, ())
    if len(mine & theirs) >= MIN_TOPIC_WORDS:
        out['topic_kept'] = 1.
    elif (asked and not objected and mine and theirs and _question(hers)
          and not {'laughed', 'affection'} & set(out)):
        # She asked him something; he answered none of it and asked about something else. An
        # answer in his own words shares no word with her question either, so only his own
        # unrelated question counts.
        out['topic_changed'] = 1.
    return out
