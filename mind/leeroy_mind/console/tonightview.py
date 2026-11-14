"""Read-only, content-free Tonight cards. Each source has its own bounded worker."""
import asyncio
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import time

from . import calendarview, raidview, selfieview, views, items
from .feed import human_guid
from .opsview import OpsView, Missing, Budget, database, require
from leeroy_mind.realm.bank import BankLedger, STOCK
from leeroy_mind.discord.channels import group_sql
from leeroy_mind.groups.config import load_groups_config

SECTIONS = ('raid','waiting','loot','bank','kit','week','guild')


def clean_kit(reply):
    from leeroy_mind.guild.kitread import KIT, TARGETS
    if not isinstance(reply,dict) or not isinstance(reply.get('groups'),list) or not isinstance(reply.get('items'),list):
        return None
    groups, stock, seen = [], [], set()
    for row in reply['groups']:
        if (not isinstance(row,dict) or row.get('kind') not in TARGETS or row['kind'] in seen
                or any(type(row.get(k)) is not int or row[k]<0 for k in ('have','target','short'))
                or row['target']!=TARGETS[row['kind']] or row['short']!=max(0,row['target']-row['have'])):
            return None
        seen.add(row['kind'])
        groups.append({k:row[k] for k in ('kind','have','target','short')})
    if seen != set(TARGETS):
        return None
    seen = set()
    for row in reply['items']:
        if (not isinstance(row,dict) or type(row.get('id')) is not int or row['id'] not in KIT or row['id'] in seen
                or type(row.get('count')) is not int or not 0<=row['count']<=0xFFFFFFFF
                or not isinstance(row.get('name'),str) or len(row['name'])>200):
            return None
        seen.add(row['id'])
        stock.append({'id':row['id'],'count':row['count'],'name':row['name'],'kind':KIT[row['id']]})
    read_at = reply.get('read_at')
    return {'groups':groups,'items':stock,'read_at':read_at if type(read_at) in (int,float) and math.isfinite(read_at) else None}


def result(status='ok',why=None,**data):
    return {'status':status,'why':why,**data}


def _person(names,guid):
    return {'guid':guid,'name':names.get(str(guid),names.get(guid,f'#{guid}'))}


def _calendar(conn,now,section):
    require(conn,'realm_calendar','realm_rsvp')
    view = calendarview.calendar_view(conn,now=now)
    today = datetime.fromtimestamp(now).date()
    events = []
    for row in view['upcoming']:
        date = datetime.fromtimestamp(row['at']).date()
        if section == 'raid':
            keep = row['kind'] == 'raid' and date == today
        else:
            keep = now <= row['at'] <= now+7*86400
        if keep:
            event = {'title':row['title'],'at':row['at'],'kind':row['kind']}
            if section == 'raid':
                event.update({k:[_person(view['names'],g) for g in row['signups'][k]]
                              for k in ('signed_up','tentative','declined')})
            events.append(event)
    return result(events=events)


def _waiting(conn,data_dir,now):
    require(conn,'discord_message','realm_mail','board_reply','selfie_set')
    human = human_guid(conn)
    if not human:
        raise Missing('no_owner')
    group_channels = set()
    if load_groups_config(data_dir/'mind.toml').enabled and 'discord_group' in views.tables(conn):
        group_channels = {f'gdm:{r[0]}' for r in conn.execute('SELECT g.id FROM discord_group g '
            'JOIN discord_group_member m ON m.grp=g.id WHERE g.closed IS NULL AND m.guid=? AND m.left IS NULL',
            (human,))}
    # Select only identities and times; private message contents never enter this view.
    rows = conn.execute("SELECT channel,author,ts,id FROM (SELECT channel,author,ts,id,"
                        "row_number() OVER (PARTITION BY channel,author ORDER BY ts DESC,id DESC) AS rank "
                        f"FROM discord_message WHERE deleted IS NULL AND (channel GLOB 'dm:[0-9]*' OR (? AND {group_sql()})) AND ts<=?) "
                        "WHERE rank=1",(bool(group_channels),now)).fetchall()
    threads = {}
    group_threads = {}
    for channel,author,at,mid in rows:
        if channel in group_channels and author > 0:
            people = group_threads.setdefault(channel,{})
            who = human if author == human else 0
            people[who] = max(people.get(who,(float('-inf'),0)),(at,mid))
            continue
        peer = channel[3:]
        if peer.isascii() and peer.isdigit() and int(peer)>0 and author in (human,int(peer)):
            threads.setdefault(channel,{})[author] = (at,mid)
    dms = sum(v.get(int(c[3:]),(float('-inf'),0)) > v.get(human,(float('-inf'),0)) for c,v in threads.items())
    dms += sum(v.get(0,(float('-inf'),0)) > v.get(human,(float('-inf'),0)) for v in group_threads.values())
    letters = conn.execute("SELECT count(*) FROM realm_mail m WHERE state IN ('sent','taken') "
        "AND ts<=? AND NOT EXISTS (SELECT 1 FROM board_reply r WHERE r.kind='letter' AND r.parent=m.id "
        "AND r.state IN ('waiting','answered','posted'))",(now,)).fetchone()[0]
    queue = selfieview.queue_view(conn,data_dir,now=now)
    if not queue['available']:
        raise Missing('photo_tables')
    # Open polls still waiting for his vote (he is a voter in every poll; a vote is final)
    polls = conn.execute("SELECT count(*) FROM guild_poll p WHERE status='open' AND closes>? AND NOT EXISTS "
        "(SELECT 1 FROM guild_poll_vote v WHERE v.poll=p.id AND v.member=?)", (now, human)).fetchone()[0]         if {'guild_poll', 'guild_poll_vote'} <= views.tables(conn) else None
    return result(dms=dms,letters=letters,selfies=len(queue['queue']),polls=polls)


def _presence(status):
    world = (status or {}).get('world') or {}
    connected = isinstance(world,dict) and world.get('connected') is True
    human = world.get('human_online') if connected and type(world.get('human_online')) is bool else None
    online = world.get('cast_online') if connected and type(world.get('cast_online')) is int else None
    return {'human_online':human,'cast_online':online, 'gpu': (status or {}).get('gpu')}


def _guild(conn,status):
    require(conn,'night_run')
    row = conn.execute('SELECT night,finished,error FROM night_run WHERE finished IS NOT NULL '
                       'ORDER BY finished DESC LIMIT 1').fetchone()
    night = None
    if row:
        try:
            failures = json.loads(row[2])
        except (ValueError,TypeError):
            failures = None
        count = sum(failures.values()) if isinstance(failures,dict) and all(type(v) is int and v>=0 for v in failures.values()) else None
        night = {'date':row[0],'finished':row[1],'failed_units':count}
    return result(**_presence(status),night=night)


def section_view(name,conn,data_dir,*,now,status,kit,kit_why):
    try:
        if name == 'outreach':
            from .outreachview import tonight_view as outreach_view
            return outreach_view(conn,now=now)
        if name in ('raid','week'):
            return _calendar(conn,now,name)
        if name == 'waiting':
            return _waiting(conn,data_dir,now)
        if name == 'loot':
            require(conn,'guild_raid','guild_attendance','dkp_ledger')
            stats = raidview.dkp_stats(conn)
            names = views.names(conn)
            by_guid = {m['guid']:m for m in stats['members']}
            return result(due=[{**_person(names,g),'raids':by_guid[g]['since_item']} for g in stats['due'][:5]])
        if name == 'bank':
            require(conn,'realm_bank')
            ledger = BankLedger(conn,None)
            low = []
            for stock in STOCK.values():
                for item,target in stock:
                    have = ledger.balance(item)
                    if have < target/2:
                        low.append({'id':item,'have':have,'target':target})
            low.sort(key=lambda r:(r['have']/r['target'],r['id']))
            low = low[:6]
            known = items.item_names(data_dir/'library.db',{r['id'] for r in low})
            return result(items=[{**r,'name':known.get(r['id'],{}).get('name',f"Item {r['id']}")} for r in low])
        if name == 'kit':
            require(conn,'meta')
            if kit is None:
                return result('unavailable',kit_why or 'no_reply')
            return result(groups=kit['groups'],items=kit['items'],read_at=kit.get('read_at'))
        if name == 'guild':
            return _guild(conn,status)
        raise ValueError('unknown_section')
    except Missing:
        return result('not_set_up','missing_source')
    except Budget:
        return result('unavailable','slow')
    except sqlite3.OperationalError as exc:
        if str(exc).startswith(('no such table','no such column')):
            return result('not_set_up','missing_source')
        return result('unavailable','slow' if str(exc)=='interrupted' else 'read_failed')
    except Exception:
        return result('unavailable','read_failed')


def tonight_view(conn,data_dir: Path,*,now: float,status: dict | None,kit: dict | None,kit_why: str | None,
                 outreach_enabled=False) -> dict:
    sections = {}
    for name in (*SECTIONS, *(('outreach',) if outreach_enabled else ())):
        deadline = time.monotonic()+1
        conn.set_progress_handler(lambda: time.monotonic()>=deadline,1000)
        try:
            sections[name] = section_view(name,conn,data_dir,now=now,status=status,kit=kit,kit_why=kit_why)
        finally:
            conn.set_progress_handler(None,0)
    return {'generated':now,'sections':sections}


class TonightView(OpsView):
    """Use the Ops single-flight daemon workers so a stalled read cannot accumulate."""
    def __init__(self,data,*,clock=time.time,outreach_enabled=False):
        self.sections = (*SECTIONS, *(('outreach',) if outreach_enabled else ()))
        super().__init__(data,data,budgets={n:2.8 if n=='kit' else 1.0 for n in self.sections},clock=clock)

    def _source(self,name,deadline,runtime):
        kit,why = (runtime['kit_read']() if name=='kit' else (None,None))
        with database(self.data,deadline) as conn:
            return section_view(name,conn,self.data,now=runtime['now'],status=runtime['status'],kit=kit,kit_why=why)

    async def snapshot(self,runtime):
        values = await asyncio.gather(*(self._one(n,runtime) for n in self.sections))
        sections = {}
        for name,value in zip(self.sections,values):
            if value['status'] in ('missing','error'):
                value = result('not_set_up' if value['status']=='missing' else 'unavailable',
                               'slow' if value.get('skipped')=='budget' else 'missing_source' if value['status']=='missing' else 'read_failed')
            if name=='guild' and value['status']=='ok':
                value = {**value,**_presence(runtime.get('status'))}
            sections[name] = value
        return {'generated':runtime['now'],'sections':sections}
