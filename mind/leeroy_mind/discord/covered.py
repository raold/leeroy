"""Which of his DM posts an answer of hers was built with (review of #368, P1-1).

"Answered" used to be read from id order: his posts before her last answer. But an answer she
was already composing lands with a higher id than a post of his that arrived meanwhile, though
it was built without it; that post was then never put to her and every count called it
answered. Now each answer says what it covers: when a member's answer to his DM is stored, the
ids of his posts its instruction listed (TurnMeta.covers) are written to meta
`discord:built:<her message id>` as a comma list, with the message (discord.transport). A post
of his is answered only when some stored answer covers it.

An answer with no receipt (stored before receipts existed, or posted outside the talk
pipeline) keeps the old reading for the posts up to the one it replies to: `floor` is the
largest such reply_to. Her brb and back (pair lines) and her messages that reply to nothing
(an opener, the later messages of a burst, a carried whisper answer) cover nothing.

Read-only helpers on a connection, for the talk (DiscordTalk.unanswered, _coalesce), the owed
watch and health (talk.owed)."""
from __future__ import annotations

from leeroy_mind.discord.transport import PAIR_LINE_SQL

BUILT_KEY = "discord:built:{}"
ANSWERS = 200  # of her newest answers in a DM read for their receipts


def parse(value) -> tuple[int, ...]:
    """A receipt's ids; () for anything that is not a comma list of positive integers."""
    if not isinstance(value, str) or not value or len(value) > 400:
        return ()
    parts = value.split(",")
    if not all(p.isascii() and p.isdecimal() and len(p) <= 12 for p in parts):
        return ()
    return tuple(int(p) for p in parts if int(p) > 0)


def receipt(ids) -> str:
    return ",".join(str(int(i)) for i in ids)


class Covered:
    """His posts in one DM that her answers cover: `at[post]` is when the first answer built
    with it was stored; `legacy` are (reply_to, ts) of her answers without a receipt."""

    __slots__ = ("at", "legacy")

    def __init__(self, at: dict[int, float], legacy: list[tuple[int, float]]):
        self.at, self.legacy = at, legacy

    @property
    def floor(self) -> int:
        return max((r for r, _ in self.legacy), default=0)

    def has(self, post: int) -> bool:
        """Some stored answer covers `post`."""
        return post in self.at or post <= self.floor

    def by(self, post: int, after: float, until: float) -> bool:
        """...and it was stored after `after` and no later than `until` (health's window)."""
        t = self.at.get(post)
        if t is not None and t <= until:  # a receipt names the post: it cannot precede it
            return True
        return any(r >= post and after < ts <= until for r, ts in self.legacy)


def read(conn, channel: str, thread: int | None = None, *, any_thread: bool = False) -> Covered:
    """The receipts of her newest ANSWERS answers in DM `channel` (its feed, or `thread`;
    `any_thread`: every thread, as health counts)."""
    where = "" if any_thread else " AND m.thread IS ?"
    args = (channel,) if any_thread else (channel, thread)
    rows = conn.execute(
        "SELECT m.reply_to, m.ts, k.value FROM discord_message m LEFT JOIN meta k "
        "ON k.key = 'discord:built:' || m.id WHERE m.channel = ? AND m.origin = 'mind' "
        f"AND m.reply_to IS NOT NULL{where} AND "
        + PAIR_LINE_SQL.replace("turn", "m.turn") + " ORDER BY m.id DESC LIMIT ?",
        (*args, ANSWERS)).fetchall()
    at: dict[int, float] = {}
    legacy: list[tuple[int, float]] = []
    for reply_to, ts, value in rows:
        ids = parse(value)
        if not ids:
            legacy.append((reply_to, ts))
            continue
        for i in ids:
            if i not in at or ts < at[i]:
                at[i] = ts
    return Covered(at, legacy)
