"""Checkpointed, model-free friendship groups and feud departures."""
from datetime import date

from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.protocol import Chat
from .config import load_groups_config
from . import store as groups


def friends(store,a,b,feuds):
    if frozenset((a,b)) in feuds:
        return False
    return all((r:=store.get_relationship(x,y)) is not None and r.affinity>=40 for x,y in ((a,b),(b,a)))


def _left(store,channel,guid,name,now):
    """A code-built system notice, not words attributed to the member."""
    text = f'{name} left the group'
    mid = store.conn.execute("INSERT INTO discord_message(channel,author,origin,bot,text,ts) "
        "VALUES(?,0,'bot','group-system',?,?)",(channel,text,now)).lastrowid
    chat = Chat(ts=int(now*1000),channel=channel,from_guid=0,from_name='System',to_guid=0,text=text,map=0,zone=0)
    line = store.insert_chat_line(chat,session_id=None,origin='game',ts=now,
        audience=groups.readers(store.conn,channel,include_closed=True))
    store.set_meta(f'discord:line:{mid}',str(line))


def settle(ctx,config):
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
    if not ctx.human or not config.member_starts_per_week:
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
    for starter in members:
        chosen = [starter]
        for guid in members:
            if guid!=starter and all(friends(store,guid,other,feuds) for other in chosen):
                chosen.append(guid)
            if len(chosen)==4:
                break
        if len(chosen)<2 or any(set(chosen)<=prior for prior in existing):
            continue
        name = redact_real_name(ctx.persona(starter).character_name+' & friends',ctx.ident,ctx.ident.called)[:40]
        channel = groups.create(store,human=ctx.human,members=chosen,title=name,created_by=starter,now=now,config=config)
        store.set_meta('groups:first:'+channel.removeprefix('gdm:'),'1')
        store.set_meta(key,str(int(raw)+1))
        break


async def stage_groups(ctx):
    config = load_groups_config(ctx.settings.data_dir/'mind.toml')
    if not config.enabled:
        return
    async def one(unit):
        ctx.commit('language',unit,lambda store:settle(ctx,config))
    await ctx.map_units('language',['groups'],one)
