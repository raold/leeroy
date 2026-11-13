"""Quiet-channel admission; only public guild facts or the starter's own life."""
from dataclasses import replace
from datetime import datetime

from leeroy_mind.discord.channels import is_group
from leeroy_mind.server.seeds import Seed
from . import store as groups

PUBLIC_KINDS = frozenset({'raid_before','raid_after','loot','calendar','birthday','reset','weekend','plan','his_day'})


def definition(channel):
    from leeroy_mind.discord.layout import Channel
    return Channel(channel,'Group chat','groups','Small private group chat; no romance')


def seeds(conn, source, *, now, local, human, config):
    if not config.enabled:
        return [], {}
    out, audiences = [], {}
    day = local.date().isoformat()
    for row in conn.execute('SELECT * FROM discord_group WHERE closed IS NULL ORDER BY id'):
        channel = f'gdm:{row["id"]}'
        audience = groups.readers(conn,channel)
        if human not in audience or not 3 <= len(audience) <= 5:
            continue
        last = conn.execute("SELECT max(ts) FROM discord_message WHERE channel=? AND origin='human' AND deleted IS NULL",
                            (channel,)).fetchone()[0]
        if now-(last if last is not None else row['created']) >= 7*86400:
            continue
        receipt = conn.execute('SELECT value FROM meta WHERE key=?',(day_key(day,channel),)).fetchone()
        if receipt is not None and (not str(receipt[0]).isdigit() or int(receipt[0])>=2):
            continue
        audiences[channel] = audience
        pending = conn.execute('SELECT value FROM meta WHERE key=?',(f'groups:first:{row["id"]}',)).fetchone()
        if pending:
            # Until her introduction actually lands, nobody opens on another subject.
            if (row['created_by'] in audience and row['created_by'] != human
                    and datetime.fromtimestamp(row['created'],local.tzinfo).date() < local.date()):
                out.append(Seed(f'group_start:{row["id"]}','group_start',(channel,),
                    'You made this small group chat for these friends and the owner. Introduce it in your own words.',
                    (row['created_by'],),weight=4))
            continue
        for seed in source:
            if seed.officers_only:
                continue
            if seed.kind == 'life':
                if len(seed.about)!=1 or seed.about[0] not in audience:
                    continue
            elif seed.kind not in PUBLIC_KINDS:
                continue
            out.append(replace(seed,key=f'group:{row["id"]}:{seed.key}',channels=(channel,),weight=seed.weight*.25))
    return out,audiences


def day_key(day,channel):
    return f'server:group:{day}:{channel}'


def reserve(store,channel,day):
    with store.transaction():
        key = day_key(day,channel)
        raw = store.get_meta(key) or '0'
        if not raw.isdigit() or int(raw)>=2:
            return False
        store.set_meta(key,str(int(raw)+1))
    return True


def valid(app,conversation,speaker):
    service = getattr(app,'groups',None)
    current = service.readers(conversation.channel) if service is not None else frozenset()
    if not current or current != conversation.readers or speaker not in current:
        return False
    if conversation.seed.kind in {'life','group_start'} and not conversation.messages:
        return speaker in conversation.seed.about
    return True
