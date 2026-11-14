"""The guild's server bots (D5 Part C, `[culture] bots`): the guild's real events posted in its
Discord by bots, never by members, the way every real guild server has them.

  signups    a raid night the calendar desk posted, three days ahead    #raid-schedule
             "<title> — <weekday HH:MM>. React :tank: :healer: :dps:"; each member's calendar
             signup then shows as their role reaction on it (a tentative one as a shrug)
  loot       a charged guild loot award with its unique DKP debit         #loot-and-dkp
             raid chat's own award line (guild/lines.py loot_award)
  logs       a guild raid that ended after at least one pull              #logs
             "Logs are up: <raid> — <kills/wipes as the calendar says it>, <duration>"
  birthday   a member's card birthday, on the local date                 #general
  firsts     a first-ever guild kill the Realm announced, and a cast       #announcements
             member's realm-first achievement: "GUILD FIRST: ..." / "REALM FIRST: ..."
  reminders  a posted raid night (meeting: #officer-chat), an hour ahead   #raid-schedule

Every post is built by code from rows: no model is called and nothing is made up. Each goes out
exactly once: its receipt (meta culture:bot:<bot>:<ref>) is written in the post's own transaction,
so a restart between them can neither lose the receipt nor post twice. Inside the server's quiet
window nothing is posted; what fell due waits and goes out at the window's end, oldest first, at
most PER_TICK a minute. A post whose subject has passed (a reminder after its event, a birthday
after its day) is never sent late. Events from before the bots were first switched on (meta
culture:bots:since) are not posted, so switching them on never floods the server with history.
Logs carry bot ids, message ids and counts, never a post's text or a name."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
import logging
from pathlib import Path

from leeroy_mind.discord import store as dstore

log = logging.getLogger(__name__)

BOTS = ("signups", "loot", "logs", "birthday", "firsts", "reminders")
RECEIPT = "culture:bot:{bot}:{ref}"
SINCE_KEY = "culture:bots:since"
TICK_S = 60.0
PER_TICK = 10  # posts a minute at most
SIGNUP_AHEAD_S = 3 * 86400.0
REMIND_S = 3600.0
LOOKBACK_S = 3 * 86400.0  # loot, logs and firsts are posted within three days of happening
BIRTHDAY_HOUR = 9  # the birthday bot's local morning
CHANNEL = {"signups": "raid-schedule", "loot": "loot-and-dkp", "logs": "logs",
           "birthday": "general", "firsts": "announcements", "reminders": "raid-schedule"}
# The page's names for the bots (console/static/js/comms/discordview.mjs mirrors these).
NAMES = {"signups": "Raid Signups", "loot": "Loot Bot", "logs": "Logs Bot",
         "birthday": "Birthday Bot", "firsts": "Guild Firsts", "reminders": "Reminders"}
TENTATIVE = "🤷"
REMINDER_CHANNEL = {"raid": "raid-schedule", "meeting": "officer-chat", "anniversary": "general"}


@dataclass(frozen=True, slots=True)
class Post:
    bot: str
    ref: str
    channel: str
    text: str
    due: float  # when it fell due: the order a held queue goes out in
    until: float  # never posted at or after this


@dataclass
class _Context:
    now: float
    local: datetime
    since: float
    names: dict
    personas: dict
    human: int
    ident: object
    library: Path | None
    emoji: bool

    def role(self, code: str) -> str:
        from leeroy_mind.server.reacting import UNICODE_ROLES
        return code if self.emoji else UNICODE_ROLES[code]


def _duration(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60)))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _signups(conn, c: _Context):
    for cid, title, at, updated in conn.execute(
            "SELECT id,title,at,updated FROM realm_calendar WHERE kind='raid' AND state='posted' "
            "AND event_id IS NOT NULL AND at>? AND at<=? ORDER BY at,id", (c.now, c.now + SIGNUP_AHEAD_S)):
        when = datetime.fromtimestamp(at, c.local.tzinfo)
        from leeroy_mind.server.reacting import ROLE_PALETTE
        roles = " ".join(c.role(code) for code in ROLE_PALETTE)
        yield Post("signups", str(cid), CHANNEL["signups"],
                   f"{title} — {when:%A %H:%M}. React {roles}", max(at - SIGNUP_AHEAD_S, updated), at)


def _loot(conn, c: _Context):
    from leeroy_mind.culture.origins import _item_name, loot_record
    from leeroy_mind.guild.lines import template
    for lid, note in conn.execute(
            "SELECT id,note FROM dkp_ledger WHERE reason='item' AND ts>=? AND ts<=? "
            "AND note GLOB 'guild:loot:*' ORDER BY ts,id LIMIT 256", (max(c.since, c.now - LOOKBACK_S), c.now)):
        if _done(conn, "loot", str(lid)):
            continue
        found = loot_record(conn, "loot:close_loot:" + note)  # the receipt and its unique debit
        if found is None:
            continue
        record, ts = found
        award = record["award"]
        winner = c.names.get(award["guid"])
        item = _item_name(conn, record, library=c.library, ident=c.ident)
        if winner is None or item == "an item":
            continue
        yield Post("loot", str(lid), CHANNEL["loot"],
                   template("loot_award", item=f"[{item}]", winner=winner, paid=award["paid"]),
                   ts, ts + LOOKBACK_S)


def _logs(conn, c: _Context):
    from leeroy_mind.guild.raids import CATALOG
    from leeroy_mind.realm.calendar import result_text
    for rid, key, size, heroic, kills, wipes, started, ended in conn.execute(
            "SELECT raid_id,raid_key,size,heroic,kills,wipes,started,ended FROM guild_raid "
            "WHERE ended>=? AND ended<=? AND kills+wipes>0 ORDER BY ended,raid_id",
            (max(c.since, c.now - LOOKBACK_S), c.now)):
        raid = CATALOG.get(key)
        if raid is None:
            continue
        yield Post("logs", str(rid), CHANNEL["logs"],
                   f"Logs are up: {raid.name} {size}{' heroic' if heroic else ''} — "
                   f"{result_text(key, kills, wipes, False)}, {_duration(ended - started)}",
                   ended, ended + LOOKBACK_S)


def _birthdays(conn, c: _Context):
    day = c.local.date()
    start = datetime.combine(day, time(BIRTHDAY_HOUR), tzinfo=c.local.tzinfo).timestamp()
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=c.local.tzinfo).timestamp()
    for guid in sorted(c.personas):
        p = c.personas[guid]
        if guid == c.human or p.real_life.birthday != f"{day:%m-%d}" or guid not in c.names:
            continue
        yield Post("birthday", f"{guid}:{day.year}", CHANNEL["birthday"],
                   f"Happy birthday {c.names[guid]}! 🎂", start, end)


def _firsts(conn, c: _Context):
    from leeroy_mind.culture.origins import _safe_name
    low = max(c.since, c.now - LOOKBACK_S)
    for command, payload, settled in conn.execute(
            "SELECT command_id,payload_json,settled_at FROM realm_announcement WHERE state='sent' "
            "AND first='ever' AND settled_at>=? AND settled_at<=? ORDER BY settled_at", (low, c.now)):
        try:
            data = json.loads(payload)
            boss, size, heroic = _safe_name(data["boss_name"], ident=c.ident), data["size"], data["heroic"]
        except (TypeError, ValueError, KeyError):
            continue
        if boss is None or type(size) is not int or type(heroic) is not bool:
            continue
        yield Post("firsts", command, CHANNEL["firsts"],
                   f"GUILD FIRST: {boss} ({size}{' heroic' if heroic else ''}) is down!", settled, settled + LOOKBACK_S)
    for fid, ts, proof in conn.execute(
            "SELECT f.id,f.ts,o.proof_json FROM realm_fact f JOIN realm_fact_origin o ON o.fact=f.id "
            "WHERE o.policy='realm_first_achievement_v1' AND o.state='active' AND f.public=1 "
            "AND f.ts>=? AND f.ts<=? ORDER BY f.ts,f.id", (low, c.now)):
        try:
            output = json.loads(proof)["output_fields"]
            guid, name = output["guid"], _safe_name(output["name"], ident=c.ident)
        except (TypeError, ValueError, KeyError):
            continue
        if type(guid) is not int or guid not in c.names or name is None:
            continue
        yield Post("firsts", f"fact:{fid}", CHANNEL["firsts"],
                   f"REALM FIRST: {c.names[guid]} earned {name}!", ts, ts + LOOKBACK_S)


def _reminders(conn, c: _Context):
    for cid, kind, title, at in conn.execute(
            "SELECT id,kind,title,at FROM realm_calendar WHERE state='posted' AND at>? AND at<=? "
            "ORDER BY at,id", (c.now, c.now + REMIND_S)):
        channel = REMINDER_CHANNEL.get(kind)
        if channel is None:
            continue  # birthdays have their own bot; an offline raid is posted as it starts
        left = int((at - c.now) // 60)
        when = "in 1 hour" if left >= 55 else f"in {max(1, left)} minutes"
        yield Post("reminders", str(cid), channel, f"{title} {when}", at - REMIND_S, at)


SOURCES = {"signups": _signups, "loot": _loot, "logs": _logs, "birthday": _birthdays,
           "firsts": _firsts, "reminders": _reminders}


def _done(conn, bot: str, ref: str) -> bool:
    return conn.execute("SELECT 1 FROM meta WHERE key=?", (RECEIPT.format(bot=bot, ref=ref),)).fetchone() is not None


def due_posts(conn, c: _Context) -> list[Post]:
    """Every post due now and not yet made, oldest first. Reads only."""
    out = []
    for bot in BOTS:
        try:
            posts = list(SOURCES[bot](conn, c))
        except Exception as error:  # one source's trouble never stops the others
            log.warning("culture_bot_source_failed bot=%s code=%s", bot, type(error).__name__)
            continue
        out += [p for p in posts if p.due <= c.now < p.until and not _done(conn, p.bot, p.ref)]
    return sorted(out, key=lambda p: (p.due, BOTS.index(p.bot), p.ref))


def post_once(store, post: Post, *, now: float) -> int | None:
    """The post and its receipt in one transaction; None when the receipt was already there."""
    key = RECEIPT.format(bot=post.bot, ref=post.ref)
    with store.transaction():
        if store.get_meta(key) is not None:
            return None
        mid = dstore.post_bot(store, channel=post.channel, bot=post.bot, text=post.text, now=now)
        store.set_meta(key, str(mid))
        return mid


class ServerBots:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self._closed = False

    def _context(self, now: float) -> _Context:
        from leeroy_mind.culture.config import app_emoji
        app = self.app
        store = app.store
        since = store.get_meta(SINCE_KEY)
        try:
            since = float(since)
        except (TypeError, ValueError):
            since = None
        if since is None:  # the bots start from when they were first switched on
            store.set_meta(SINCE_KEY, str(now))
            since = now
        human = app.human_guid
        names = dict(store.conn.execute("SELECT guid,name FROM cast_member").fetchall())
        members = app.world.discord_members()
        personas = {g: p for g in members if g != human and (p := app.personas.get(g)) is not None}
        library = Path(app.data_dir) / "library.db"
        return _Context(now, app.clock.local(), since, names, personas, human, app.engine.human,
                        library if library.is_file() else None, app_emoji(app))

    def _quiet(self) -> bool:
        from leeroy_mind.server.clock import in_window
        return in_window(self.app.clock.local(), self.config.quiet_window)

    async def tick(self, now: float) -> None:
        """Every TICK_S: outside the quiet window, the due posts, oldest first, at most PER_TICK;
        then the role reactions of the signup posts."""
        app = self.app
        if self._closed or app.stopping or not self.config.enabled:
            return
        try:
            c = self._context(now)
            if not self._quiet():
                self._post(c)
        finally:
            if not app.store.in_unit:
                app.store.conn.commit()  # end a read outside a unit, so it never pins a snapshot

    def _post(self, c: _Context) -> None:
        app = self.app
        posted = 0
        for post in due_posts(app.store.conn, c):
            if posted >= PER_TICK or self._closed:
                break
            if _screened_out(post.text, c.ident):
                log.info("culture_bot_skipped bot=%s code=real_name", post.bot)
                continue
            try:
                mid = post_once(app.store, post, now=app.clock.now())
            except dstore.DiscordRefused as error:
                log.info("culture_bot_refused bot=%s code=%s", post.bot, error.code)
                continue
            if mid is None:
                continue
            posted += 1
            log.info("culture_bot_posted bot=%s message=%d", post.bot, mid)
            if post.bot != "signups":  # a signup post's reactions are the members' real signups
                self._react(mid, post)
        self.signup_reactions(c)

    def _react(self, mid: int, post: Post) -> None:
        reactor = getattr(getattr(self.app, "server", None), "reactions", None)
        if reactor is None:
            return
        try:
            reactor.after_post(mid, author=0, channel="discord:" + post.channel, text=post.text, his=False)
        except Exception:
            log.warning("culture_bot_reaction_schedule_failed message=%d", mid)

    def signup_reactions(self, c: _Context) -> int:
        """Each member's calendar signup for a raid night as their reaction on its signup post:
        their role for signed up, a shrug for tentative, nothing for a decline. Idempotent: a
        reaction already there is not added again."""
        from leeroy_mind.server.reacting import role_emoji
        store, added = self.app.store, 0
        rows = store.conn.execute(
            "SELECT c.event_id, CAST(m.value AS INTEGER) FROM realm_calendar c JOIN meta m "
            "ON m.key='culture:bot:signups:'||c.id WHERE c.kind='raid' AND c.event_id IS NOT NULL "
            "AND c.at>? ORDER BY c.at", (c.now - 86400,)).fetchall()
        for event, mid in rows:
            for guid, status in store.conn.execute(
                    "SELECT guid,status FROM realm_rsvp WHERE event=? ORDER BY ts,guid", (event,)).fetchall():
                p = c.personas.get(guid)
                if p is None or status not in ("signed_up", "tentative"):
                    continue
                emoji = role_emoji(p.play.raid_role, custom=c.emoji) if status == "signed_up" else TENTATIVE
                try:
                    with store.transaction():
                        if dstore.react(store, mid, member=guid, emoji=emoji, now=c.now, custom=c.emoji):
                            added += 1
                except dstore.DiscordRefused:
                    break  # the post is gone or full: nothing more on it
        if added:
            log.info("culture_bot_reacted count=%d", added)
        return added

    async def run(self):
        while not self._closed:
            try:
                await self.tick(self.app.clock.now())
            except Exception as error:
                log.warning("culture_bot_tick_failed code=%s", type(error).__name__)
            await self.app.clock.sleep(TICK_S)

    async def close(self):
        self._closed = True


def _screened_out(text: str, ident) -> bool:
    from leeroy_mind.personas.screens import names_real_name
    return ident is not None and names_real_name(text, ident)
