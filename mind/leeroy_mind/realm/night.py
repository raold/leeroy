"""The realm's night stage (plan Task 19): tomorrow's Trade line candidates for each rival
guild, a few realm forum posts with their way back in, the texts of waiting letters and the
write-ups of offline events, on the reflection model while he is offline.

parts.install_night adds the stage after "stories" on the reflection model, only when the
Realm is enabled at attach (attach below). The stage reads [realm] enabled again when it starts
and does nothing while it is false, or while the Realm's own switch is off; its counter
(count_units, registered at import) counts 0 then.

Each unit is one STRUCTURE JSON call (Reasoner.json), screened, then committed on its own with
nothing awaited inside the commit (NightContext.commit):
- beat:<guild>: one per active rival guild, from that guild's prompt facts (Ledger.known) and
  its progress: 0-2 Trade lines, each a private night_beat_v1 rule fact only that guild knows
  (`own`), read back with beat_lines_in_unit. A line the screen refuses is dropped.
- post:fact:<id> or post:rumor:<id>: at most 3, about a new public fact with an event origin or
  a forum candidate (Ledger.forum_candidates), by a competitive or drama-prone member or a
  rival officer: a realm_post row and its public forum_post_v1 fact, which every realm guild
  knows by `forum`. A post about an event fact also writes its way back in: Ledger.back_in's
  memories, embedded before the commit and written inside it by put_notable_in_unit through
  the night's writer. A rumor has no realm fact behind it, so its post writes no memory.
- letter:<id>: at most 3 realm_mail rows waiting for text (mail.texts_wanted), each from a
  sender with a stored persona: the body (and a subject when the row has none), written with
  mail.write_text, which makes it 'ready'. The text stays in the mail row: no fact and no
  memory; the sender's memory of it is the post office's, once it is sent.
- writeup:<id>: at most 2 scored or ended realm_event rows of the last week without one: the
  write-up goes into the event's result.
- post:reply:<id> and letter:reply:<id>: his Board replies (board.waiting), taken before the
  night's own posts and letters, oldest first, inside the same caps. A forum reply that passes the
  forum screen and the fact's own (screen_rule_text, checked before any model call) becomes his
  own public forum_post_v1 fact (key reply:<id>), which every realm guild knows by `forum`; the
  post's author answers it in the thread while they can still write, else the night's author pool
  (_author), and nobody leaves it posted. It writes the way back in as a post does. A letter reply that passes the letter screen becomes the member's private memory
  with him (key board-reply:<id>) and her answer, a realm_mail 'reply' row ready for the post
  office. His text refused is settled 'refused' with its reason and never asked again; an answer
  refused (by its screen, and a forum answer by its fact's own too, before the commit) or not
  given (the model's error) fails the unit and his reply waits, for at most board.ANSWER_NIGHTS nights
  (board.answer_failed counts each once): on the last it is settled 'refused', no_answer. A reply
  that stopped waiting before the commit (withdrawn, or settled) is left alone (board.settle).
A unit whose output the screen refuses fails, writes nothing and waits for a later night. The
public units (beats, posts, write-ups) wait while the screen is not ready (no blocklist, or his
guild's name unknown); letters are private and do not.

The count is in 20-second JSON equivalents (budget.UNIT_CALLS["realm"]): every unit above is
1, the weekly meeting 7 and the monthly applicant 6 (Tasks 27 and 28 add their units with
register_kind). ctx.settings.realm_units caps it, and the budget's first DEGRADE step lowers
that to what fits. The units go in KEEP's order, so the first cut are the write-ups, then posts,
letters and beats, then the applicant and the meeting last. A unit cut tonight is still
waiting tomorrow; the units a night already finished count against its caps on a resumed run.
"""
import hashlib
import json
import logging
from typing import Awaitable, Callable, Sequence

from leeroy_mind import board
from leeroy_mind.config import ConfigError
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.scheduler import SchedulerError
from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.ground import LEAVES_RE
from leeroy_mind.nightly.llm import ReflectionError, schema, system_prompt
from leeroy_mind.nightly.runner import NightContext
from leeroy_mind.personas.screens import names_real_name
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.talk.filters import find_tells, is_silence, repair
from leeroy_mind.text import truncate_words
from leeroy_mind.wow import boss_name

from . import mail
from .config import LIMITS, load_realm_config
from .fact_store import FactStore
from .ledger import Ledger, put_notable_in_unit
from .ports import GUILD_NAME_KEY
from .provenance import public_fact_in_unit
from .records import FactRow, PreparedBackIn, Rejected
from .rule_facts import put_rule_fact_in_unit, rule_fact_in_unit, screen_rule_text
from .screen import Screen, insults_a_group

STAGE = "realm"
WEIGHTS = {"meeting": 7, "applicant": 6, "beat": 1, "letter": 1, "post": 1, "writeup": 1}
KEEP = ("meeting", "applicant", "beat", "letter", "post", "writeup")  # cut from the end
CAPS = {"meeting": 1, "applicant": 1, "beat": 3, "letter": 3, "post": 3, "writeup": 2}
PUBLIC = frozenset({"beat", "post", "writeup"})  # need the screen
FRESH_S = LIMITS.week_s       # how new a fact or an event must be for the night to take it up
POST_MAX = 200                # a post is its forum_post_v1 fact's text
LETTER_MAX, SUBJECT_MAX = 400, 40
WRITEUP_MAX = 400
MEMORY_MAX = 200              # a memory's text
_U32 = 0xFFFFFFFF

# Tasks 27 and 28: kind -> (units(ctx) -> unit names, run(ctx, unit)), each run committing its
# own unit with ctx.commit(STAGE, unit, ...).
HEAVY: dict[str, tuple[Callable[[NightContext], Sequence[str]],
                       Callable[[NightContext, str], Awaitable[None]]]] = {}

_ROLES = {"progression": "a progression raiding guild racing for first kills",
          "gankers": "a world PvP guild that likes to gank",
          "drama": "a social guild known for its drama"}
_DIFFICULTY = {0: "10", 1: "25", 2: "10 heroic", 3: "25 heroic"}
_OCCASIONS = {
    "absence": "{called} has not logged in for a while: the letter says they are missed and "
               "hopes to see them back soon.",
    "birthday": "It is {called}'s birthday: the letter wishes them a happy birthday.",
    "gift": "The letter goes with a small gift.",
    "holiday": "The letter goes with a holiday gift.",
}
_OCCASION = "The letter goes with this mail."

BEAT_DEFAULT = """Tomorrow the officers of {guild}, {role} of the {faction}, may say something
in the realm's Trade channel. From their week below, write at most two short lines they could
post there, or none when there is nothing worth saying: their progress, a boast, a jab about
the race for first kills, recruiting or the week's news. Each line is at most 120 characters
and reads like a player typing in Trade. Never name or tag another guild, never use anyone's
real name, never insult a group of people, never flirt and never tell anyone to leave their
guild."""
POST_DEFAULT = """{author} is writing one post for the realm's forum about the news below: an
opinion, a boast, a complaint or a joke, in their own voice. Write the post, at most 200
characters, as a player would type it. Never name a guild, never use anyone's real name, never
insult a group of people, never flirt and never tell anyone to leave their guild."""
LETTER_DEFAULT = """{sender} is writing an in-game letter to {called}. {occasion} Write it as
{sender} would type it, warm and in their own voice: a subject of at most 40 characters and a
body of at most 400 characters. Never use anyone's real name and never say anyone is leaving
the guild."""
REPLY_POST_DEFAULT = """{author} wrote a post on the realm's forum and {called} replied to it. Write
{author}'s answer to the reply, in their own voice, at most 200 characters, as a player would type
it. Never name a guild, never use anyone's real name, never insult a group of people, never flirt
and never tell anyone to leave their guild."""
REPLY_LETTER_DEFAULT = """{sender} sent {called} an in-game letter and {called} wrote back. Write
{sender}'s answer as {sender} would type it, warm and in their own voice: a subject of at most 40
characters and a body of at most 400 characters. Never use anyone's real name and never say anyone
is leaving the guild."""
WRITEUP_DEFAULT = """Write a short write-up of a guild event that ran while {called} was away,
for the guild's records: what happened, who did well and one light detail. Use only the details
below. At most 400 characters. Never use anyone's real name, never flirt and never say anyone
is leaving the guild."""

BEAT_SCHEMA = schema("realm_beat", {
    "lines": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 120}}})
POST_SCHEMA = schema("realm_post", {"text": {"type": "string", "minLength": 1, "maxLength": POST_MAX}})
LETTER_SCHEMA = schema("realm_letter", {
    "subject": {"type": "string", "minLength": 1, "maxLength": SUBJECT_MAX},
    "body": {"type": "string", "minLength": 1, "maxLength": LETTER_MAX}})
WRITEUP_SCHEMA = schema("realm_writeup", {
    "text": {"type": "string", "minLength": 1, "maxLength": WRITEUP_MAX}})


class _Refused(Exception):
    """A unit's output the screen refused; the message is the reason only, never the text."""

    def __init__(self, kind: str, reason: str):
        super().__init__(f"{kind} refused: {reason}")


def register_kind(kind: str, *, units: Callable[[NightContext], Sequence[str]],
                  run: Callable[[NightContext, str], Awaitable[None]]) -> None:
    """Tasks 27 and 28: the weekly meeting's or the monthly applicant's units tonight, and how
    one runs (it commits its own unit). Their weights and their place in the cut are fixed here."""
    if kind not in ("meeting", "applicant"):
        raise ValueError(f"unknown realm night kind {kind!r}")
    HEAVY[kind] = (units, run)


def realm_enabled(ctx: NightContext) -> bool:
    """[realm] enabled in the night's mind.toml, read now; an unreadable file is off."""
    try:
        return load_realm_config(ctx.settings.data_dir / "mind.toml").enabled
    except ConfigError:
        return False


def own_guild(store) -> tuple[int, str]:
    """His guild's id and name from the cached armory read (meta realm:guild_name), else (0, "")."""
    try:
        value = json.loads(store.get_meta(GUILD_NAME_KEY) or "null")
    except ValueError:
        return 0, ""
    if type(value) is not dict:
        return 0, ""
    guild, name = value.get("guild"), value.get("name")
    if type(guild) is not int or not 1 <= guild <= _U32 or type(name) is not str or not name.strip():
        return 0, ""
    return guild, name


def beat_lines_in_unit(store, guild, *, human, night=None) -> tuple[FactRow, ...]:
    """A rival guild's Trade line candidates from one night's beat (the newest night with any
    when `night` is None), in their order, each read back in full as an active rule fact. They
    are private: that guild's own lines for the light minds to say, never another guild's
    prompt facts (Ledger.known leaves them out). The caller holds the Store unit."""
    if not store.in_unit:
        raise RuntimeError("beat lines require an active Store unit")
    if type(guild) is not int or not 1 <= guild <= _U32:
        raise ValueError("invalid guild")
    prefix = "rule:night_beat_v1:"
    found = []
    for fact, key in store.conn.execute(
            "SELECT fact, producer_key FROM realm_rule_origin WHERE policy='night_beat_v1' "
            "AND state='active' AND substr(producer_key, 1, ?)=?", (len(prefix), prefix)):
        parts = key[len(prefix):].split(":")
        if len(parts) == 3 and parts[1] == str(guild) and parts[2].isdigit():
            found.append((parts[0], int(parts[2]), fact))
    if night is None and found:
        night = max(n for n, _, _ in found)
    lines = []
    for _, _, fact in sorted(f for f in found if f[0] == night):
        row = rule_fact_in_unit(store, fact, human=human)
        if type(row) is FactRow:
            lines.append(row)
    return tuple(lines)


class _Ports:
    """What the ledger reads at night: his identity, frozen for the run, and the mind clock."""

    def __init__(self, ctx: NightContext):
        self._ctx = ctx

    def human(self):
        return self._ctx.ident

    def now(self):
        return self._ctx.clock.now()


class _Night:
    """One run's view of the realm: the ledger, his guild, the screen, and each unit."""

    def __init__(self, ctx: NightContext, live: Callable[[], bool] | None = None):
        self.ctx, self.store = ctx, ctx.store
        self.live = live or (lambda: True)
        self.ledger = Ledger(ctx.store, _Ports(ctx), FactStore(ctx.store))
        self.guild = own_guild(ctx.store)[0]
        self.screen = Screen(ctx.settings.data_dir, lambda: own_guild(self.store)[1],
                             lambda: ctx.ident)

    @property
    def public_ready(self) -> bool:
        return self.guild > 0 and self.screen.ready

    # -- tonight's candidates ------------------------------------------------------------------

    def _rivals(self):
        with self.store.transaction():
            return self.store.conn.execute(
                "SELECT id, role, faction, name FROM realm_guild WHERE status='active' AND id!=? "
                "ORDER BY id", (self.guild,)).fetchall()

    def _posted(self, prefix: str) -> set[str]:
        with self.store.transaction():
            return {row[0] for row in self.store.conn.execute(
                "SELECT thread FROM realm_post WHERE substr(thread, 1, ?)=?", (len(prefix), prefix))}

    def _fresh_facts(self, need: int) -> list[FactRow]:
        """New public facts with an event origin, never posted, the newest first."""
        if need <= 0:
            return []
        posted = self._posted("fact:")
        out = []
        with self.store.transaction():
            ids = [row[0] for row in self.store.conn.execute(
                "SELECT f.id FROM realm_fact f JOIN realm_fact_origin o ON o.fact=f.id "
                "WHERE f.public=1 AND o.state='active' AND f.ts>=? ORDER BY f.ts DESC, f.id DESC "
                "LIMIT 50", (self.ctx.clock.now() - FRESH_S,))]
            for fact_id in ids:
                if f"fact:{fact_id}" in posted:
                    continue
                read = public_fact_in_unit(self.store, fact_id, human=self.ctx.ident)
                if type(read) is not Rejected:
                    out.append(read[0])
                    if len(out) == need:
                        break
        return out

    def _fresh_rumors(self, need: int):
        if need <= 0:
            return []
        posted = self._posted("rumor:")
        return [c for c in self.ledger.forum_candidates(limit=min(20, need + len(posted)))
                if f"rumor:{c.rumor_id}" not in posted][:need]

    def _replies(self, kind: str, need: int) -> list[str]:
        """His waiting Board replies of this kind, oldest first."""
        with self.store.transaction():
            rows = board.waiting(self.store.conn, kind, need)
        return [f"{kind}:reply:{row['id']}" for row in rows]

    def candidates(self, kind: str, need: int) -> list[str]:
        """Up to `need` units of `kind` the night could take up now: for posts and letters, his
        waiting Board replies first."""
        if need <= 0 or (kind in PUBLIC and not self.public_ready):
            return []
        if kind in HEAVY:
            return list(HEAVY[kind][0](self.ctx))[:need]
        if kind == "beat":
            return [f"beat:{row['id']}" for row in self._rivals()][:need]
        if kind == "post":
            replies = self._replies("post", need)
            facts = [f"post:fact:{f.id}" for f in self._fresh_facts(need - len(replies))]
            return (replies + facts + [f"post:rumor:{c.rumor_id}"
                                       for c in self._fresh_rumors(need - len(replies) - len(facts))])
        if kind == "letter":
            replies = self._replies("letter", need)
            with self.store.transaction():
                wanted = mail.texts_wanted(self.store.conn, limit=need - len(replies))
            return replies + [f"letter:{want.id}" for want in wanted
                              if self.ctx.personas.get(want.sender) is not None]
        if kind == "writeup":
            return [f"writeup:{row['id']}" for row in self._events()][:need]
        return []

    def _events(self):
        with self.store.transaction():
            rows = self.store.conn.execute(
                "SELECT id, result FROM realm_event WHERE state IN ('scored','ended') AND end>=? "
                "ORDER BY end, id", (self.ctx.clock.now() - FRESH_S,)).fetchall()
        return [row for row in rows if _writeup_due(row["result"])]

    # -- running one unit ----------------------------------------------------------------------

    async def run(self, unit: str) -> None:
        kind, _, key = unit.partition(":")
        if kind in HEAVY:
            await HEAVY[kind][1](self.ctx, unit)
            return
        if kind in PUBLIC and not self.public_ready:
            return  # The screen went away mid-night: the unit waits.
        if kind in ("post", "letter") and key.startswith("reply:"):
            reply_id = int(key[6:])
            try:
                await (self._post_reply if kind == "post" else self._letter_reply)(reply_id, unit)
            except (_Refused, ReflectionError) as e:  # His reply's answer failed tonight.
                if getattr(e, "fatal", False) or not self._no_answer(unit, reply_id):
                    raise
            return
        await {"beat": self._beat, "post": self._post, "letter": self._letter,
               "writeup": self._writeup}[kind](key, unit)

    def _commit(self, unit: str, write) -> bool:
        if not self.live():
            return False  # The Realm went off: nothing written, the unit waits.
        return self.ctx.commit(STAGE, unit, write)

    def _system(self, name: str, default: str, **fields) -> str:
        return system_prompt(name, default, self.ctx.settings.data_dir, human=self.ctx.ident, **fields)

    def _public(self, text: str, speaker_kind: str, limit: int) -> str | None:
        """Why a public text, repaired first (filters.repair), is dropped, or None. The
        screen's own length is a Trade line's; a post or a write-up may be longer, to `limit`."""
        if not text:
            return "empty"
        reason = self.screen.check(text, speaker_kind=speaker_kind)
        return None if reason == "too_long" and len(text) <= limit else reason

    def _private(self, text: str) -> str | None:
        """Why a letter's repaired text is dropped, or None: it is his alone, so the public
        screen's guild-name and flirt rules do not apply."""
        if not text:
            return "empty"
        if names_real_name(text, self.ctx.ident):
            return "real_name"
        if LEAVES_RE.search(text):
            return "leaves"
        if insults_a_group(text):
            return "group_insult"
        if is_silence(text) or find_tells(text):
            return "tells"
        return None

    async def _beat(self, key: str, unit: str) -> None:
        ctx, guild = self.ctx, int(key)
        row = next((r for r in self._rivals() if r["id"] == guild), None)
        if row is None:
            ctx.skip(STAGE, unit)  # Retired since planning.
            return
        facts = self.ledger.known(guild)
        with self.store.transaction():
            progress = self.store.conn.execute(
                "SELECT map, boss_id, difficulty, state, best_pct FROM realm_progress WHERE guild=? "
                "ORDER BY updated DESC LIMIT 8", (guild,)).fetchall()
        user = json.dumps({"guild": row["name"], "news": [f.text for f in facts], "progress": [
            {"boss": boss_name(p["map"], p["boss_id"], "a boss"),
             "size": _DIFFICULTY.get(p["difficulty"], "any"), "state": p["state"],
             **({"best_percent": p["best_pct"]} if p["state"] == "progressing" else {})}
            for p in progress]}, ensure_ascii=False)
        system = self._system("realm_beat", BEAT_DEFAULT, guild=row["name"],
                              role=_ROLES.get(row["role"], "a guild"), faction=row["faction"].title())
        reply = await ctx.reasoner.json(system, user, BEAT_SCHEMA)
        lines = []
        for text in reply["lines"][:2]:
            text = repair(text)
            reason = self._public(text, "officer", 0)
            if reason is None:
                lines.append(text)
            else:
                log("realm_night_dropped", night=ctx.night, unit=unit, reason=reason)
        now, used = ctx.clock.now(), [f.id for f in facts]

        def write(store):
            for i, text in enumerate(lines):
                fact = put_rule_fact_in_unit(
                    store, policy="night_beat_v1", key=f"{ctx.night}:{guild}:{i}", kind="beat",
                    text=text, public=False, known_by=[[guild, "own"]],
                    inputs={"night": ctx.night, "guild": guild, "line": i, "facts": used},
                    ts=now, human=ctx.ident)
                if type(fact) is Rejected:
                    log("realm_night_dropped", night=ctx.night, unit=unit, reason=fact.reason)
        self._commit(unit, write)

    def _author(self, key: str, about: int):
        """(guid, author_kind, name, who) of the post's author, or None: a competitive or
        drama-prone member, or an officer of a rival guild, chosen by the night's seed."""
        ctx = self.ctx
        former = self.ledger.former_members()
        pool = []
        for guid in ctx.members():
            p = ctx.personas.get(guid)
            if (guid != about and guid not in former and p is not None
                    and ("competitor" in (p.game.motivation, p.game.thinks_as)
                         or p.big_five.neuroticism.score >= 7)):
                pool.append(("member", guid))
        actors = self._actors(about)
        pool += [("actor", guid) for guid in actors]
        if not pool:
            return None
        kind, guid = ctx.rng(STAGE, key).choice(pool)
        if kind == "member":
            return self._as_member(guid)
        return self._as_actor(actors[guid])

    def _actors(self, about: int = 0) -> dict:
        """The officers who may write on the forum, by guid: active, of an active rival guild,
        never excluded, not in the cast, and neither `about` nor him."""
        excluded = self.ledger.actor_exclusions()
        with self.store.transaction():
            roster = {row[0] for row in self.store.conn.execute("SELECT guid FROM cast_member")}
            officers = self.store.conn.execute(
                "SELECT a.guid, a.name, g.name AS guild, g.role FROM realm_actor a JOIN realm_guild g "
                "ON g.id=a.guild WHERE a.role='officer' AND a.status='active' AND g.status='active' "
                "AND g.id!=? ORDER BY a.guid", (self.guild,)).fetchall()
        # World generation must not revoke an existing actor's card-based admission.
        out = {row["guid"]: row for row in officers
                if row["guid"] not in excluded and row["guid"] not in roster
                and row["guid"] not in (about, self.ctx.human)}
        from leeroy_mind.world.realm import voices
        out.update({g:r for g,r in voices(self.store,self.ctx.settings.data_dir,self.ctx.clock.now()).items()
                    if g not in out and g not in (about,self.ctx.human) and r["guild_id"] != self.guild})
        return out

    def _as_member(self, guid: int):
        return guid, "member", self.ctx.name(guid), {"who": "a member of the guild",
                                                     "card": self.ctx.card(guid)}

    @staticmethod
    def _as_actor(row):
        if isinstance(row,dict) and row.get("world"):
            return row["guid"], "actor", row["name"], {"who":f"a member of {row['guild']}","card":row["card"]}
        return row["guid"], "actor", row["name"], {
            "who": f"an officer of {row['guild']}, {_ROLES.get(row['role'], 'a guild')}"}

    def _still_writes(self, guid: int, author_kind: str):
        """A post's own author as _author gives one, while they can still write: a member of
        tonight's cast, never a former one, with a persona; or an officer _actors allows. Else
        None."""
        ctx = self.ctx
        if author_kind == "member":
            if (guid in ctx.members() and guid not in self.ledger.former_members()
                    and ctx.personas.get(guid) is not None):
                return self._as_member(guid)
            return None
        row = self._actors().get(guid) if author_kind == "actor" else None
        return None if row is None else self._as_actor(row)

    async def _prepare(self, plans) -> tuple[PreparedBackIn, ...]:
        """The way back in's one vector, embedded before the commit; without one the memories
        are stored without it, as the writer stores any it cannot embed."""
        if not plans:
            return ()
        text = plans[0].text  # One fact: every plan carries its text.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            vector = (await self.ctx.embed([text]))[0]
        except (LLMError, SchedulerError):
            vector = None
        try:
            return tuple(PreparedBackIn(plan, vector, digest) for plan in plans)
        except ValueError:
            return tuple(PreparedBackIn(plan, None, digest) for plan in plans)

    async def _post(self, key: str, unit: str) -> None:
        ctx = self.ctx
        subject, _, ident = key.partition(":")
        fact = None
        if subject == "fact":
            with self.store.transaction():
                read = public_fact_in_unit(self.store, int(ident), human=ctx.ident)
            fact = None if type(read) is Rejected else read[0]
            news, about = (None, 0) if fact is None else (fact.text, 0)
        else:
            found = next((c for c in self.ledger.forum_candidates() if c.rumor_id == int(ident)), None)
            news, about = (None, 0) if found is None else (found.safe_text, found.about)
        author = self._author(key, about) if news is not None else None
        if author is None:
            ctx.skip(STAGE, unit)  # Its subject is gone, or nobody would write it tonight.
            return
        guid, author_kind, name, who = author
        system = self._system("realm_post", POST_DEFAULT, author=name)
        user = json.dumps({"author": name, **who, "news": news}, ensure_ascii=False)
        reply = await ctx.reasoner.json(system, user, POST_SCHEMA)
        text = " ".join(repair(reply["text"], speaker=name).split())
        reason = self._public(text, "member" if author_kind == "member" else "officer", POST_MAX)
        if reason is not None:
            raise _Refused("post", reason)
        prepared = await self._prepare(self.ledger.back_in(fact.id, forum=True) if fact else ())
        knowers = sorted({self.guild} | {row["id"] for row in self._rivals()})
        now = ctx.clock.now()

        def write(store):
            if store.conn.execute("SELECT 1 FROM realm_post WHERE thread=?", (key,)).fetchone():
                return  # Posted meanwhile.
            if author_kind == "actor" and "card" in who and self._still_writes(guid,author_kind) != author:
                return
            post = put_rule_fact_in_unit(
                store, policy="forum_post_v1", key=key, kind="forum_post", text=text, public=True,
                known_by=[[g, "forum"] for g in knowers],
                inputs={"night": ctx.night, "subject": key, "author": guid, "author_kind": author_kind},
                ts=now, human=ctx.ident)
            if type(post) is Rejected:
                raise _Refused("post", post.reason)
            store.conn.execute(
                "INSERT INTO realm_post(ts, author, author_kind, thread, text, fact) VALUES (?,?,?,?,?,?)",
                (now, guid, author_kind, key, post.text, post.id))
            self._notables(store, unit, prepared, now)
        self._commit(unit, write)

    def _notables(self, store, unit: str, prepared, now: float) -> None:
        """A post's way back in, inside its commit."""
        ctx = self.ctx
        for item in prepared:
            result = put_notable_in_unit(store, ctx.writer, item, human=ctx.ident, now=now)
            if type(result) is Rejected:
                log("realm_night_notable_refused", night=ctx.night, unit=unit,
                    owner=item.plan.owner, reason=result.reason)

    # -- his Board replies ---------------------------------------------------------------------

    def _waiting_reply(self, reply_id: int, kind: str):
        with self.store.transaction():
            return self.store.conn.execute(
                "SELECT id, ts, parent, to_guid, text FROM board_reply WHERE id=? AND kind=? "
                "AND state='waiting'", (reply_id, kind)).fetchone()

    def _refuse_reply(self, unit: str, reply_id: int, reason: str) -> None:
        """His text refused: settled with the reason, never asked again; no model call."""
        ctx = self.ctx

        def write(store):
            board.settle(store.conn, reply_id, state="refused", night=ctx.night, now=ctx.clock.now(),
                         reason=reason)
        if self._commit(unit, write):
            log("realm_night_reply", night=ctx.night, unit=unit, outcome="refused", reason=reason)

    def _no_answer(self, unit: str, reply_id: int) -> bool:
        """His reply's answer failed tonight: the night is counted once (board.answer_failed). On
        the board.ANSWER_NIGHTS-th it is settled refused, no_answer, and the unit is done: True.
        Before that, with the Realm off, or once it stopped waiting, False: it waits."""
        ctx = self.ctx
        if not self.live():
            return False
        with self.store.transaction():
            if not _waiting(self.store, reply_id):
                return False
            nights = board.answer_failed(self.store.conn, reply_id, ctx.night)
        if nights < board.ANSWER_NIGHTS:
            return False
        self._refuse_reply(unit, reply_id, board.NO_ANSWER)
        return True

    def _thread_back_in(self, thread: str):
        """Ledger.back_in's plans for a thread about an event fact; none for a rumor's."""
        subject, _, ident = thread.partition(":")
        if subject != "fact" or not (ident.isascii() and ident.isdigit()) or int(ident) < 1:
            return ()
        return self.ledger.back_in(int(ident), forum=True)

    async def _post_reply(self, reply_id: int, unit: str) -> None:
        ctx = self.ctx
        reply = self._waiting_reply(reply_id, "post")
        if reply is None:
            ctx.skip(STAGE, unit)  # Withdrawn or settled since planning.
            return
        with self.store.transaction():
            parent = self.store.conn.execute(
                "SELECT author, author_kind, thread, text FROM realm_post WHERE id=?",
                (reply["parent"],)).fetchone()
        text = reply["text"]
        reason = "post_gone" if parent is None else self._public(text, "member", POST_MAX)
        if reason == "not_ready":
            return  # The screen went away mid-night: it waits.
        if reason is None and type(screened := screen_rule_text(text, human=ctx.ident)) is Rejected:
            reason = screened.reason  # His forum fact would refuse it: no answer is asked for.
        if reason is not None:
            self._refuse_reply(unit, reply_id, reason)
            return
        key = f"reply:{reply_id}"
        author = self._still_writes(parent["author"], parent["author_kind"]) or self._author(key, 0)
        answer = None
        if author is not None:
            guid, author_kind, name, who = author
            system = self._system("realm_reply_post", REPLY_POST_DEFAULT, author=name,
                                  called=ctx.ident.called)
            user = json.dumps({"author": name, **who, "post": parent["text"], "reply": text},
                              ensure_ascii=False)
            got = await ctx.reasoner.json(system, user, POST_SCHEMA)
            answer = " ".join(repair(got["text"], speaker=name).split())
            reason = self._public(answer, "member" if author_kind == "member" else "officer", POST_MAX)
            if reason is None and type(screened := screen_rule_text(answer, human=ctx.ident)) is Rejected:
                reason = screened.reason  # Its forum fact would refuse it.
            if reason is not None:
                raise _Refused("post", reason)  # His reply waits for another night.
        prepared = await self._prepare(self._thread_back_in(parent["thread"]))
        # Raise outside commit_unit: its rollback catches unit errors, while the
        # reply runner must see this refusal to count the bounded failed night.
        if (answer is not None and author_kind == "actor" and "card" in who
                and self._still_writes(guid,author_kind) != author):
            raise _Refused("post", "author_gone")
        knowers = [[g, "forum"] for g in sorted({self.guild} | {row["id"] for row in self._rivals()})]
        inputs = {"night": ctx.night, "reply": reply_id, "to": reply["parent"]}
        now, outcome = ctx.clock.now(), {}

        def write(store):
            if not _waiting(store, reply_id):
                return  # He withdrew it meanwhile: left alone.
            his = put_rule_fact_in_unit(
                store, policy="forum_post_v1", key=key, kind="forum_post", text=text, public=True,
                known_by=knowers, inputs=inputs | {"author": ctx.human, "author_kind": "owner"},
                ts=now, human=ctx.ident)
            if type(his) is Rejected:  # His own text cannot be a forum fact: never asked again.
                board.settle(store.conn, reply_id, state="refused", night=ctx.night, now=now,
                             reason=his.reason)
                outcome.update(outcome="refused", reason=his.reason)
                return
            post_id = None
            if answer is not None:
                fact = put_rule_fact_in_unit(
                    store, policy="forum_post_v1", key=f"{key}:answer", kind="forum_post", text=answer,
                    public=True, known_by=knowers,
                    inputs=inputs | {"author": guid, "author_kind": author_kind}, ts=now, human=ctx.ident)
                if type(fact) is Rejected:
                    raise _Refused("post", fact.reason)
                post_id = store.conn.execute(
                    "INSERT INTO realm_post(ts, author, author_kind, thread, text, fact) VALUES (?,?,?,?,?,?)",
                    (now, guid, author_kind, parent["thread"], fact.text, fact.id)).lastrowid
            state = "posted" if post_id is None else "answered"
            if not board.settle(store.conn, reply_id, state=state, night=ctx.night, now=now,
                                fact=his.id, answer_post=post_id):
                raise RuntimeError("board reply changed inside its commit")
            self._notables(store, unit, prepared, now)
            outcome.update(outcome=state)
        if self._commit(unit, write) and outcome:
            log("realm_night_reply", night=ctx.night, unit=unit, **outcome)

    async def _letter(self, key: str, unit: str) -> None:
        ctx, mail_id = self.ctx, int(key)
        with self.store.transaction():
            row = self.store.conn.execute(
                "SELECT sender, kind, subject, items, state FROM realm_mail WHERE id=?", (mail_id,)).fetchone()
        p = None if row is None or row["state"] != "awaiting_text" else ctx.personas.get(row["sender"])
        if p is None:
            ctx.skip(STAGE, unit)  # Sent, skipped or gone meanwhile.
            return
        try:
            items = json.loads(row["items"])
        except ValueError:
            items = []
        called = ctx.ident.called
        occasion = _OCCASIONS.get(row["kind"], _OCCASION).format(called=called)
        system = self._system("realm_letter", LETTER_DEFAULT, sender=p.character_name,
                              called=called, occasion=occasion)
        user = json.dumps({"from": p.character_name, "card": ctx.card(row["sender"]),
                           "kind": row["kind"], "subject": row["subject"] or None, "items": items},
                          ensure_ascii=False)
        reply = await ctx.reasoner.json(system, user, LETTER_SCHEMA)
        body = repair(reply["body"], speaker=p.character_name)
        subject = row["subject"] or " ".join(repair(reply["subject"], speaker=p.character_name).split())
        reason = self._private(body) or self._private(subject)
        if reason is None and not mail.text_ok(subject, body, ctx.ident):
            reason = "too_long"  # Native's bounds are bytes: subject 1-64, body 0-512.
        if reason is not None:
            raise _Refused("letter", reason)

        def write(store):
            # A row that is no longer waiting was sent or skipped meanwhile: nothing to write.
            mail.write_text(store, mail_id, subject=subject, body=body, human=ctx.ident)
        self._commit(unit, write)

    async def _letter_reply(self, reply_id: int, unit: str) -> None:
        ctx = self.ctx
        reply = self._waiting_reply(reply_id, "letter")
        if reply is None:
            ctx.skip(STAGE, unit)  # Withdrawn or settled since planning.
            return
        member, text = reply["to_guid"], reply["text"]
        with self.store.transaction():
            theirs = self.store.conn.execute("SELECT subject, body FROM realm_mail WHERE id=?",
                                             (reply["parent"],)).fetchone()
        p = ctx.personas.get(member)
        reason = ("letter_gone" if theirs is None else "no_persona" if p is None
                  else self._private(text))
        if reason is not None:
            self._refuse_reply(unit, reply_id, reason)
            return
        called = ctx.ident.called
        system = self._system("realm_reply_letter", REPLY_LETTER_DEFAULT, sender=p.character_name,
                              called=called)
        user = json.dumps({"from": p.character_name, "card": ctx.card(member),
                           "their_letter": {"subject": theirs["subject"], "body": theirs["body"]},
                           "reply": text}, ensure_ascii=False)
        got = await ctx.reasoner.json(system, user, LETTER_SCHEMA)
        body = repair(got["body"], speaker=p.character_name)
        subject = " ".join(repair(got["subject"], speaker=p.character_name).split())
        reason = self._private(body) or self._private(subject)
        if reason is None and not mail.text_ok(subject, body, ctx.ident):
            reason = "too_long"  # Native's bounds are bytes: subject 1-64, body 0-512.
        if reason is not None:
            raise _Refused("letter", reason)  # His reply waits for another night.
        now = ctx.clock.now()
        memory = MemoryRow(
            id=None, owner=member, kind="event", text=_wrote_back(called, text),
            t_event=int(reply["ts"]), t_created=int(now), t_accessed=int(now), access_n=0,
            actors=(ctx.human,), place=None, boss_id=None, raid_id=None, importance=5, valence=0.4,
            source="game", evidence=(f"board-reply:{reply_id}",), key=f"board-reply:{reply_id}",
            scope="private", scope_with=ctx.human)
        answered = []

        def write(store):
            if not _waiting(store, reply_id):
                return  # He withdrew it meanwhile: left alone.
            ctx.writer.put(memory)
            mail_id = mail.insert_mail(store.conn, now, member, "reply", subject=subject, body=body,
                                       state="ready")
            if not board.settle(store.conn, reply_id, state="answered", night=ctx.night, now=now,
                                answer_mail=mail_id):
                raise RuntimeError("board reply changed inside its commit")
            answered.append(mail_id)
        if self._commit(unit, write) and answered:
            log("realm_night_reply", night=ctx.night, unit=unit, outcome="answered")

    async def _writeup(self, key: str, unit: str) -> None:
        ctx, event = self.ctx, int(key)
        with self.store.transaction():
            row = self.store.conn.execute(
                "SELECT kind, organiser, start, end, place, result, state FROM realm_event WHERE id=?",
                (event,)).fetchone()
        if row is None or row["state"] not in ("scored", "ended") or not _writeup_due(row["result"]):
            ctx.skip(STAGE, unit)
            return
        result = json.loads(row["result"])
        organiser = ctx.personas.get(row["organiser"])
        from leeroy_mind.world.realm import voices
        world_author = voices(self.store,ctx.settings.data_dir,ctx.clock.now()).get(row["organiser"])
        user = json.dumps({"event": row["kind"], "place": row["place"],
                           "organiser": organiser.character_name if organiser else None,
                           "minutes": round((row["end"] - row["start"]) / 60), "result": result},
                          ensure_ascii=False)
        system = self._system("realm_writeup", WRITEUP_DEFAULT, called=ctx.ident.called)
        if world_author:
            user = json.dumps({"event":row["kind"],"place":row["place"],"result":result,
                "minutes":round((row["end"]-row["start"])/60),"author":world_author["name"],
                "guild":world_author["guild"],"card":world_author["card"]},ensure_ascii=False)
        reply = await ctx.reasoner.json(system, user, WRITEUP_SCHEMA)
        text = " ".join(repair(reply["text"]).split())
        reason = self._public(text, "member", WRITEUP_MAX)
        if reason is not None:
            raise _Refused("writeup", reason)
        original = row["result"]

        def write(store):
            if world_author and voices(store,ctx.settings.data_dir,ctx.clock.now()).get(row["organiser"]) != world_author:
                return
            current = store.conn.execute("SELECT result FROM realm_event WHERE id=?", (event,)).fetchone()
            if current is None or current["result"] != original:
                return  # Changed meanwhile: a later night looks again.
            store.conn.execute("UPDATE realm_event SET result=? WHERE id=?", (json.dumps(
                result | {"writeup": text}, ensure_ascii=False, sort_keys=True), event))
        self._commit(unit, write)


def _waiting(store, reply_id: int) -> bool:
    """His reply, read again inside the commit, still waits."""
    return store.conn.execute("SELECT 1 FROM board_reply WHERE id=? AND state='waiting'",
                              (reply_id,)).fetchone() is not None


def _wrote_back(called: str, text: str) -> str:
    """The member's memory of his letter, cut to a memory's length with its quote kept."""
    head = f'{called} wrote back to my letter: "'
    return head + truncate_words(text, max(0, MEMORY_MAX - len(head) - 1)) + '"'


def _writeup_due(raw) -> bool:
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return type(result) is dict and "writeup" not in result


def _done(ctx: NightContext, kind: str) -> list[str]:
    with ctx.store.transaction():
        return [row[0] for row in ctx.store.conn.execute(
            "SELECT unit FROM night_step WHERE night=? AND stage=? AND substr(unit, 1, ?)=? "
            "ORDER BY finished, unit", (ctx.night, STAGE, len(kind) + 1, f"{kind}:"))]


def plan_units(ctx: NightContext, night: _Night | None = None) -> list[str]:
    """Tonight's units, done ones included: each kind in KEEP's order, the units this night
    already finished first, then new ones, to the kind's cap, each taken while its weight fits
    what is left of ctx.settings.realm_units."""
    night = night or _Night(ctx)
    left, chosen = ctx.settings.realm_units, []
    for kind in KEEP:
        if kind in ("meeting", "applicant") and kind not in HEAVY:
            continue
        done = _done(ctx, kind)[:CAPS[kind]]
        fresh = [u for u in night.candidates(kind, CAPS[kind] + len(done)) if u not in done]
        for unit in done + fresh[:CAPS[kind] - len(done)]:
            if WEIGHTS[kind] <= left:
                chosen.append(unit)
                left -= WEIGHTS[kind]
    return chosen


def count_units(ctx: NightContext) -> int:
    """The budget's counter: tonight's units not done yet, in 20-second JSON equivalents; 0
    while [realm] enabled is false."""
    if not realm_enabled(ctx):
        return 0
    return sum(WEIGHTS[unit.split(":", 1)[0]] for unit in plan_units(ctx)
               if not ctx.done(STAGE, unit))


async def run_units(ctx: NightContext, units: Sequence[str], *,
                    live: Callable[[], bool] | None = None) -> None:
    """Run these units as the stage does, at most settings.parallel at once; a unit's failure
    is that unit's alone."""
    night = _Night(ctx, live)
    await ctx.map_units(STAGE, list(units), night.run)


async def stage_realm(ctx: NightContext, *, live: Callable[[], bool] | None = None) -> None:
    """The realm's night: nothing while the Realm is off (mind.toml, or `live`, the Realm's own
    switch), else tonight's units (plan_units)."""
    if (live is not None and not live()) or not realm_enabled(ctx):
        log("realm_night_off", night=ctx.night)
        return
    night = _Night(ctx, live)
    if not night.public_ready:
        # Beats, posts and write-ups wait. Reasons are codes: no blocklist file, no cached guild
        # name, else a blocklist with no usable entry.
        reasons = (([] if night.screen.path.is_file() else ["blocklist_missing"])
                   + ([] if night.guild > 0 else ["guild_name_missing"]))
        log("realm_night_unready", level=logging.WARNING, night=ctx.night,
            reason=",".join(reasons or ["blocklist_empty"]))
    await ctx.map_units(STAGE, plan_units(ctx, night), night.run)


def attach(realm):
    """parts.install_night's hook: the stage, bound to this Realm's switch. No I/O."""
    def live() -> bool:
        return realm.enabled is True

    async def stage(ctx: NightContext) -> None:
        await stage_realm(ctx, live=live)
    return stage


register_counter(STAGE, count_units)
