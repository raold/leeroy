"""Checkpointed, model-free friendship groups and feud departures."""
import logging
from datetime import date

from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.protocol import Chat
from leeroy_mind.server.config import load_server_config
from .config import load_groups_config
from . import store as groups

log = logging.getLogger(__name__)


FRIEND = 30       # both ways: an established friendship (seeded at 40, regressing 1% a week)
WARM = 1          # both ways, with SHARED things done together: a friendship that grew
SHARED = 3        # raids both raided in, and scenes the night accepted between the two
SHARED_DAYS = 30
HIS_DAYS = 60     # he wrote her privately within this: she is someone he talks to


def closeness(store,members,feuds,*,human,now):
    """Who he talks to, and how close each pair of them is, from stored facts only.

    Returns (his, close): `his` maps a member he wrote privately (whisper or DM) in HIS_DAYS
    to his line count; `close` maps a pair to the lesser of their two affinities when
    neither dislikes the other, they have no feud and it is FRIEND or more, or WARM or more
    with SHARED raids and scenes together in SHARED_DAYS. The old bar (40 both ways) was the
    seed value itself: the first weekly regression put every seeded friendship under it.
    """
    members = set(members)
    his = {r[0]:r[1] for r in store.conn.execute("SELECT to_guid,count(*) FROM chat_line WHERE from_guid=? "
        "AND origin='human' AND ts>=? AND (channel='whisper' OR channel GLOB 'dm:*') GROUP BY to_guid",
        (human,now-HIS_DAYS*86400)) if r[0] in members}
    liking = {(r[0],r[1]):r[2] for r in store.conn.execute(
        'SELECT a,b,affinity FROM relationship WHERE affinity>0 AND a!=? AND b!=?',(human,human))}
    shared = {}
    raids = {}
    for raid,guid in store.conn.execute("SELECT a.raid_id,a.guid FROM guild_attendance a JOIN guild_raid g "
            "ON g.raid_id=a.raid_id WHERE a.status='raided' AND g.started>=?",(now-SHARED_DAYS*86400,)):
        raids.setdefault(raid,[]).append(guid)
    wanted = {pair for pair in map(frozenset,liking) if len(pair)==2 and pair<=members}
    for roster in raids.values():
        roster = set(roster)
        for pair in wanted:
            if pair<=roster:
                shared[pair] = shared.get(pair,0)+1
    for a,b in store.conn.execute("SELECT a,b FROM encounter WHERE status='done' AND night>=date(?,'unixepoch')",
                                  (now-SHARED_DAYS*86400,)):
        pair = frozenset((a,b))
        if pair in wanted:
            shared[pair] = shared.get(pair,0)+1
    close = {}
    for pair in wanted:
        a,b = tuple(pair)
        least = min(liking.get((a,b),0),liking.get((b,a),0))
        if pair in feuds or least<=0:
            continue
        if least>=FRIEND or least>=WARM and shared.get(pair,0)>=SHARED:
            close[pair] = least
    return his,close


REST_DAYS = 60     # a closed group's members are not put together again for this long


def barred(store,*,human,now):
    """Member sets the night must not make again: every group closed in REST_DAYS, and every
    group he left, for good unless he later made a group of those members himself. Without
    this the same members were put together again the next week (pick is deterministic)."""
    out = []
    his_own = [(r[0],r[1]) for r in store.conn.execute(
        'SELECT id,created FROM discord_group WHERE created_by=?',(human,))]
    sets = {}
    for grp,guid in store.conn.execute('SELECT grp,guid FROM discord_group_member WHERE guid!=?',(human,)):
        sets.setdefault(grp,set()).add(guid)
    for row in store.conn.execute('SELECT g.id,g.closed,(SELECT max(m.left) FROM discord_group_member m '
            'WHERE m.grp=g.id AND m.guid=?) AS he_left FROM discord_group g WHERE g.closed IS NOT NULL',(human,)):
        members = sets.get(row[0],set())
        reopened = any(created>row[1] and sets.get(gid,set())==members for gid,created in his_own)
        if members and (row[1]>=now-REST_DAYS*86400 or row[2] is not None and not reopened):
            out.append(members)
    return out


def pick(members,his,close,existing):
    """The closest group of two to four among the members he talks to, or None. Starters go
    by how much he writes them; each seat goes to whoever is closest to everyone seated."""
    known = [g for g in members if g in his]
    for starter in sorted(known,key=lambda g:(-his[g],g)):
        chosen = [starter]
        while len(chosen)<4:
            fits = [(sum(close[frozenset((g,o))] for o in chosen),his[g],-g) for g in known
                    if g not in chosen and all(frozenset((g,o)) in close for o in chosen)]
            if not fits:
                break
            chosen.append(-max(fits)[2])
        if len(chosen)>=2 and not any(set(chosen)<=prior for prior in existing):
            return chosen
    return None


def _left(store,channel,guid,name,now):
    """A code-built system notice, not words attributed to the member."""
    text = f'{name} left the group'
    mid = store.conn.execute("INSERT INTO discord_message(channel,author,origin,bot,text,ts) "
        "VALUES(?,0,'bot','group-system',?,?)",(channel,text,now)).lastrowid
    chat = Chat(ts=int(now*1000),channel=channel,from_guid=0,from_name='System',to_guid=0,text=text,map=0,zone=0)
    line = store.insert_chat_line(chat,session_id=None,origin='game',ts=now,
        audience=groups.readers(store.conn,channel,include_closed=True))
    store.set_meta(f'discord:line:{mid}',str(line))


def settle(ctx,config,*,can_start=False):
    store,now = ctx.store,ctx.clock.now()
    store.conn.execute("DELETE FROM meta WHERE key GLOB 'groups:owed:*' AND value='{}' AND NOT EXISTS "
        "(SELECT 1 FROM discord_message m WHERE m.id=CAST(substr(meta.key,13) AS INTEGER) AND m.ts>=?)",
        (now-90*86400,))
    feuds = {frozenset((r[0],r[1])) for r in store.conn.execute('SELECT a,b FROM feud WHERE ended IS NULL')}
    for row in store.conn.execute('SELECT id FROM discord_group WHERE closed IS NULL ORDER BY id').fetchall():
        channel = f'gdm:{row[0]}'
        members = sorted(groups.readers(store.conn,channel)-{ctx.human})
        for guid in members:
            current = groups.readers(store.conn,channel)
            if not current:
                break
            if any(frozenset((guid,other)) in feuds for other in current if other!=guid):
                p = ctx.personas.get(guid)
                groups.remove(store,channel,guid,human=ctx.human,now=now)
                name = redact_real_name(p.character_name if p else 'A member',ctx.ident,ctx.ident.called)
                _left(store,channel,guid,name,now)
    if not can_start or not ctx.human or not config.member_starts_per_week:
        return
    if store.conn.execute('SELECT count(*) FROM discord_group WHERE closed IS NULL').fetchone()[0]>=config.max_open:
        return
    week = date.fromisoformat(ctx.night).isocalendar()
    key = f'groups:starts:{week.year}-W{week.week:02}'
    raw = store.get_meta(key) or '0'
    if not raw.isdigit() or int(raw)>=config.member_starts_per_week:
        return
    from leeroy_mind.personas.tiers import is_cast
    members = [g for g in ctx.members() if is_cast(store.conn,g)]
    existing = [groups.readers(store.conn,f'gdm:{r[0]}') for r in store.conn.execute('SELECT id FROM discord_group WHERE closed IS NULL')]
    his,close = closeness(store,members,feuds,human=ctx.human,now=now)
    chosen = pick(members,his,close,existing+barred(store,human=ctx.human,now=now))
    # ids and counts only: why a night made no group must be readable from the log
    log.info('groups_night formed=%d talks_to=%d close_pairs=%d open=%d',int(chosen is not None),
             len(his),len(close),len(existing))
    if chosen is None:
        return
    starter = chosen[0]
    name = redact_real_name(ctx.persona(starter).character_name+' & friends',ctx.ident,ctx.ident.called)[:40]
    channel = groups.create(store,human=ctx.human,members=chosen,title=name,created_by=starter,now=now,config=config)
    store.set_meta('groups:first:'+channel.removeprefix('gdm:'),'1')
    store.set_meta(key,str(int(raw)+1))


async def stage_groups(ctx):
    config = load_groups_config(ctx.settings.data_dir/'mind.toml')
    if not config.enabled:
        return
    can_start = load_server_config(ctx.settings.data_dir/'mind.toml').enabled
    async def one(unit):
        ctx.commit('language',unit,lambda store:settle(ctx,config,can_start=can_start))
    await ctx.map_units('language',['groups'],one)
