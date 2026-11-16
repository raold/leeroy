"""What an opener that is not addressed to him brings up (TalkEngine._open_to_guild; owner's
"pass the Turing test" push, 2026-11-15: 82% of the members' typed lines were aimed at him). A
real guild mostly talks about its own business, so the opener's clock (BurstBudget.to_him) now
starts most conversations among the members: a kind of subject first (SUBJECT_WEIGHT, so no one
source dominates), then a topic within it and who says it.

The topics come from what the Discord server already reads (server.seeds.public_seeds: the last
and next raid, the weekly reset, loot, the calendar, a rival guild, a member's own life beat),
never an officers' subject, plus the member's own game (GAME_TOPICS, their wants and what they
do outside raids). A life beat is brought up only by its own member. Reads only."""
import random
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from leeroy_mind.personas.schema import Persona
from leeroy_mind.server.seeds import public_seeds, seed_subject
from leeroy_mind.talk.select import pick_speakers

SUBJECT_WEIGHT = {"raid": 3.0, "game": 2.5, "loot": 2.0, "life": 1.5, "calendar": 1.0,
                  "rival": 0.8}
# Each seed kind as the OPEN template's {topic} ("Bring up {topic} in one short line").
TOPIC_TEXT = {
    "raid_after": "how the last raid went ({fact})",
    "raid_before": "the next raid ({fact})",
    "reset": "the weekly reset today and what the guild should run this week",
    "loot": "a drop from the last raid ({fact})",
    "life": "something from your own life away from the game: {fact}",
    "calendar": "what's coming up on the guild calendar ({fact})",
    "birthday": "a guildie's birthday coming up ({fact})",
    "weekend": "what you're up to this weekend",
    "rival": "a rival guild ({fact})",
}
GAME_TOPICS = ("something about your class or spec you'd like the guild's take on",
               "something in the game that's been annoying you lately",
               "a dungeon, battleground or quest you'd like company for")
OUTSIDE_RAIDS = {"battlegrounds": "battlegrounds lately", "arena": "arena lately",
                 "gathering": "your gathering professions lately",
                 "crafting": "your crafting professions lately", "dungeons": "dungeons lately",
                 "auction_house": "the auction house lately", "fishing": "fishing lately",
                 "collecting": "your collection lately"}


@dataclass(frozen=True, slots=True)
class GuildTopic:
    subject: str
    key: str
    topic: str
    owner: int = 0  # only this member brings it up (their life beat); 0: anyone
    about: tuple[int, ...] = ()  # never raised by these members (their own loot or birthday)


_UNNAMED = re.compile(r"\[Item (\d+)\]")


def gather(conn: sqlite3.Connection, *, now: float, local: datetime,
           cast: Mapping[int, Persona], human: int,
           item_name: Callable[[int], str | None] = lambda item: None) -> list[GuildTopic]:
    """The guild's subjects right now; [] when the store cannot be read. An item the store's
    connection could not name (server.seeds names it from an attached library) is named by
    `item_name`; loot of his, and loot still unnamed, are left out. A loot topic is never its
    winner's to raise, nor a birthday topic one of the members it names."""
    try:
        seeds = public_seeds(conn, now=now, local=local, cast=cast, human=human)
    except sqlite3.Error:
        return []
    out = []
    for s in seeds:
        text = TOPIC_TEXT.get(s.kind)
        if text is None:
            continue
        if s.kind == "loot":
            fact = _UNNAMED.sub(lambda m: f"[{item_name(int(m.group(1))) or m.group(0)[1:-1]}]",
                                s.fact)
            if human in s.about or _UNNAMED.search(fact):
                continue
            s = replace(s, fact=fact)
        owner = s.about[0] if s.kind == "life" and s.about else 0
        about = (tuple(s.about) if s.kind == "loot" else
                 _named(s.fact, cast) if s.kind == "birthday" else ())
        out.append(GuildTopic(seed_subject(s), s.key, text.format(fact=s.fact), owner, about))
    return out


def _named(text: str, cast: Mapping[int, Persona]) -> tuple[int, ...]:
    """The members whose character name the text says (a birthday event's title)."""
    folded = text.casefold()
    return tuple(sorted(g for g, p in cast.items() if p.character_name and re.search(
        rf"(?<!\w){re.escape(p.character_name.casefold())}(?!\w)", folded)))


def game_topics(p: Persona) -> list[GuildTopic]:
    """The member's own game as topics anyone of theirs could raise (subject "game")."""
    def one(s: str) -> str:
        return " ".join(s.split()).rstrip(".")
    texts = [f"how close you are to {one(x)} in game" for x in p.game.wants if one(x)]
    texts += [OUTSIDE_RAIDS[x] for x in p.game.outside_raids if x in OUTSIDE_RAIDS]
    texts += list(GAME_TOPICS)
    return [GuildTopic("game", f"game:{p.guid}:{t}", t, p.guid) for t in texts]


def choose(topics: list[GuildTopic], weights: Mapping[int, float],
           personas: Mapping[int, Persona], rng: random.Random,
           used: set[str] = frozenset()) -> tuple[int, GuildTopic] | None:
    """(speaker, topic): a subject by SUBJECT_WEIGHT among those with a topic not in `used`
    that someone in `weights` may raise, then a topic of it (one of a member's own weighs what
    its member does) and its member, or a speaker by weight; for "game" a speaker first, then
    one of their own game topics. A topic about a member (GuildTopic.about) is never theirs to
    raise. None with nobody, and when nobody has any weight (all just spoke)."""
    if not any(w > 0 for w in weights.values()):
        return None
    free = [t for t in topics if t.key not in used]
    by_subject: dict[str, list[GuildTopic]] = {}
    for t in free:
        if (weights.get(t.owner, 0) > 0 if t.owner else
                any(w > 0 and g not in t.about for g, w in weights.items())):
            by_subject.setdefault(t.subject, []).append(t)
    by_subject.setdefault("game", [])  # every member has their own game
    subjects = sorted(s for s in by_subject if s in SUBJECT_WEIGHT)
    if not subjects:
        return None
    subject = rng.choices(subjects, weights=[SUBJECT_WEIGHT[s] for s in subjects], k=1)[0]
    if subject == "game":
        picked = pick_speakers(dict(weights), 1, rng)
        if not picked or personas.get(picked[0]) is None:
            return None
        own = [t for t in game_topics(personas[picked[0]]) if t.key not in used]
        return (picked[0], rng.choice(own)) if own else None
    options = by_subject[subject]
    positive = [w for w in weights.values() if w > 0]
    mean = sum(positive) / len(positive) if positive else 0.0
    topic = rng.choices(options, weights=[weights[t.owner] if t.owner else mean
                                          for t in options], k=1)[0]
    if topic.owner:
        return topic.owner, topic
    picked = pick_speakers({g: w for g, w in weights.items() if g not in topic.about}, 1, rng)
    return (picked[0], topic) if picked else None
