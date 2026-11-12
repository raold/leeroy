"""Preference mining on an owned SQLite snapshot. No models, logs or source writes."""
from collections import defaultdict
from contextlib import contextmanager, closing
from hashlib import blake2b
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata

from leeroy_mind.discord.channels import is_discord, is_private, is_restricted
from leeroy_mind.personas.screens import names_real_name
from leeroy_mind.selfies.kind import EXPLICIT_RE
from leeroy_mind.eval.talk.common import similarity
from leeroy_mind.storage.provenance import paired
from leeroy_mind.voice.rooms import BY_ID,PRIVATE
from .capture import digest
from .reward import DAY, channel_kind

MAX_LINES=20000
MAX_PAIRS=5000
EXPLICIT=re.compile(r'\b(sex\w*|orgasm\w*|masturbat\w*|penetrat\w*|genitals?|erection\w*)\b',re.I)


def output_dir(path,*,data_dir=None):
    raw=Path(os.path.abspath(Path(path).expanduser()))
    linked=any(p.is_symlink() or p.is_junction() for p in (raw,*raw.parents))
    target=raw.resolve()
    scratch=(Path(data_dir).expanduser().resolve()/'scratch') if data_dir is not None else None
    own_scratch=scratch is not None and target.is_relative_to(scratch)
    requested_scratch=data_dir is not None and raw.is_relative_to(Path(os.path.abspath(Path(data_dir).expanduser()))/'scratch')
    if (own_scratch or requested_scratch) and linked:
        raise ValueError('learn_output_linked')
    # Resolve junctions/symlinks before checking every ancestor, including linked worktrees.
    if not own_scratch and any((parent/'.git').exists() for parent in (target,*target.parents)):
        raise ValueError('learn_output_in_repository')
    target.mkdir(parents=True,exist_ok=True)
    return target


def member_key(conn):
    row=conn.execute("SELECT value FROM meta WHERE key='learn:member-hash-key'").fetchone()
    try:
        key=bytes.fromhex(row[0]) if row else b''
    except (ValueError,TypeError):
        key=b''
    if len(key)!=32:
        raise ValueError('learn_member_key_unavailable')
    return key


@contextmanager
def snapshot(data_dir,work_dir):
    with tempfile.TemporaryDirectory(prefix='.learn-snapshot-',dir=work_dir) as temporary:
        path=Path(temporary)/'mind.db'
        with closing(sqlite3.connect((Path(data_dir)/'mind.db').resolve().as_uri()+'?mode=ro',uri=True)) as source:
            with closing(sqlite3.connect(path)) as copy:
                source.backup(copy)
        conn=sqlite3.connect(path)
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.row_factory=sqlite3.Row
            yield conn
        finally:
            conn.close()


def have(conn,name):
    return conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone() is not None


def readers(row):
    try:
        return tuple(sorted(json.loads(row['audience']))) if row['audience'] else ()
    except (ValueError,TypeError,KeyError,IndexError):
        return ()


def private(row):
    return is_private(row['channel']) or row['channel'].startswith('voice:') and (
        row['channel'].removeprefix('voice:') in (*PRIVATE,'interview','penalty_box') or len(readers(row))<=2)


def allowed(row,include_private):
    if row['channel'].startswith('voice:') and (row['channel'].removeprefix('voice:') not in BY_ID or
                                               row['channel']=='voice:officers'):
        return False
    return (row['channel']!='officer' and not is_restricted(row['channel'])
            and (include_private or not private(row)))


def room(row):
    channel=row['channel']
    # Keep exact Discord rooms, private pairs and captured group/voice audiences.
    audience=readers(row) if channel in ('party','raid','raid_warning') or channel.startswith('voice:') else ()
    return (channel if is_discord(channel) or channel.startswith('voice:') else channel_kind(channel),
            row['to_guid'] if private(row) else 0,audience)


def context_rows(conn,row,owner):
    rows=conn.execute("SELECT * FROM chat_line WHERE channel=? AND id<? AND ts BETWEEN ? AND ? "
                      "AND origin IN ('human','mind') ORDER BY ts DESC,id DESC LIMIT 100",
                      (row['channel'],row['id'],row['ts']-300,row['ts'])).fetchall()
    out=[]
    for other in rows:
        if is_private(row['channel']) and {other['from_guid'],other['to_guid']}!={row['from_guid'],row['to_guid']}:
            continue
        if row['channel'] in ('party','raid','raid_warning') or row['channel'].startswith('voice:'):
            if readers(row)!=readers(other):
                continue
        if row['channel'] in ('say','yell','emote') and (
                (row['map'],row['zone'])!=(other['map'],other['zone'])
                or row['from_guid'] not in readers(other)):
            continue
        out.append(other)
        if len(out)==6:
            break
    return list(reversed(out))


def parent(conn,row,context,owner):
    if is_discord(row['channel']):
        receipt=conn.execute('SELECT value FROM meta WHERE key=?',(f"learn:discord-parent:{row['id']}",)).fetchone()
        if receipt is not None:
            return next((r for r in context if str(r['id'])==receipt[0] and r['from_guid']==owner),None)
    receipt=paired(conn,row)
    if receipt is not None and receipt['trigger_line']:
        return next((r for r in context if r['id']==receipt['trigger_line'] and r['from_guid']==owner),None)
    # Without an explicit receipt require reciprocal directed adjacency.
    latest=context[-1] if context else None
    return latest if latest and latest['from_guid']==owner and row['to_guid']==owner and latest['to_guid']==row['from_guid'] else None


def vector(conn,row):
    if row is None or not have(conn,'learn_line_vector'):
        return None
    found=conn.execute('SELECT model_hash,text_hash,embedding FROM learn_line_vector WHERE line_id=?',(row['id'],)).fetchone()
    return (found[0],found[2]) if found and found[1]==digest(row['text']) else None


def body(conn,row):
    if have(conn,'talk_recipe_line'):
        rows=conn.execute('SELECT c.text FROM talk_recipe_line m JOIN chat_line c ON c.id=m.line_id '
            'WHERE m.recipe_line_id=? AND c.from_guid=? AND c.channel=? AND c.to_guid=? ORDER BY c.id',
            (row['id'],row['from_guid'],row['channel'],row['to_guid'])).fetchall()
        if rows:
            return ' '.join(r[0] for r in rows)
    if have(conn,'learn_rewrite'):
        rows=conn.execute('SELECT c.text FROM learn_rewrite r JOIN learn_rewrite_line m ON m.generation=r.generation '
            'JOIN chat_line c ON c.id=m.line_id WHERE r.first_line_id=? AND c.from_guid=? AND c.channel=? '
            'AND c.to_guid=? ORDER BY c.id',(row['id'],row['from_guid'],row['channel'],row['to_guid'])).fetchall()
        if rows:
            return ' '.join(r[0] for r in rows)
    return row['text']


def summary(conn,guid):
    row=conn.execute('SELECT json FROM persona_core WHERE guid=?',(guid,)).fetchone()
    if row is None:
        return {}
    try:
        persona=json.loads(row[0])
        return {'age':persona.get('identity',{}).get('age'),
                'gender':persona.get('identity',{}).get('player_gender'),
                'typing':persona.get('typing',{}),
                'traits':{k:v.get('score') for k,v in persona.get('big_five',{}).items() if isinstance(v,dict)}}
    except (ValueError,TypeError,AttributeError):
        return {}


def safe(pair,ident,include_overlay):
    def strings(value):
        if isinstance(value,str):
            yield value
        elif isinstance(value,dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value,list):
            for item in value:
                yield from strings(item)
    text=' '.join(unicodedata.normalize('NFKC',' '.join(strings(pair))).split())
    # The ordinary name screen is supplemented with case-insensitive token matching
    # for export: even lower-case spelling must not escape into a training file.
    name=' '.join(ident.real_name.split())
    if names_real_name(text,ident) or name and re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',text,re.I):
        return False
    return include_overlay or not (EXPLICIT_RE.search(text) or EXPLICIT.search(text))


def context_hash(history):
    return digest(json.dumps([(r['id'],r['ts'],r['from_guid'],r['to_guid'],r['text'],readers(r),
                              r['channel'],r['origin'],r['thread_id'],r['map'],r['zone'])
                              for r in history],ensure_ascii=False))


def source_hash(row):
    return digest(json.dumps([row[key] for key in ('id','ts','from_guid','to_guid','channel',
                                                  'origin','thread_id','map','zone','audience')]))


def mine(conn,ident,*,since,until,include_private=False,include_overlay=False,salt=None):
    salt=salt or member_key(conn)
    rows=conn.execute("SELECT c.*,CASE WHEN r.scored_at<? THEN r.value END AS value FROM chat_line c LEFT JOIN talk_reward r ON r.line_id=c.id "
        "WHERE c.origin='mind' AND c.from_guid!=? AND c.ts>=? AND c.ts<? ORDER BY c.ts,c.id LIMIT ?",
        (until,ident.guid,since,until,MAX_LINES+1)).fetchall()
    truncated=len(rows)>MAX_LINES
    rows=[r for r in rows[:MAX_LINES] if allowed(r,include_private)]
    # Only first fragments are training candidates when generation attribution exists.
    membership={}
    if have(conn,'talk_recipe_line'):
        membership=dict(conn.execute('SELECT line_id,recipe_line_id FROM talk_recipe_line'))
        fragments={lid for lid,anchor in membership.items() if lid!=anchor}
        rows=[r for r in rows if r['id'] not in fragments]
    if have(conn,'learn_rewrite_line'):
        for lid,anchor in conn.execute('SELECT m.line_id,r.first_line_id FROM learn_rewrite_line m '
                                      'JOIN learn_rewrite r ON r.generation=m.generation'):
            membership.setdefault(lid,anchor)
        rows=[r for r in rows if membership.get(r['id'],r['id'])==r['id']]
    crossing=set()
    for lid,ts in conn.execute('SELECT id,ts FROM chat_line WHERE ts<? OR ts>=?',(since,until)):
        if lid in membership:
            crossing.add(membership[lid])
    rows=[r for r in rows if r['id'] not in crossing]
    by_id={r['id']:r for r in rows}
    output,indices,seen=[],[],set()
    def add(chosen,rejected,text,source,*,plain=None):
        if len(output)>=MAX_PAIRS:
            return
        chosen_text=body(conn,chosen) if plain is None else plain
        if not chosen_text or not text or ' '.join(chosen_text.split())==' '.join(text.split()) or max(len(chosen_text),len(text))>4000:
            return
        history=context_rows(conn,chosen,ident.guid)
        payload={'context':{'channel_kind':channel_kind(chosen['channel']),
                'persona':summary(conn,chosen['from_guid']),
                'lines':[{'role':'owner' if r['from_guid']==ident.guid else 'member','text':r['text']}
                         for r in history]},
            'chosen':chosen_text,'rejected':text,'source':source,
            'member_guid_hash':blake2b(str(chosen['from_guid']).encode(),key=salt,digest_size=16).hexdigest()}
        if not safe(payload,ident,include_overlay):
            return
        key=(chosen['id'],digest(text))
        if key in seen:
            return
        seen.add(key)
        case_id=digest(str(chosen['id'])+':'+str(len(output)))[:20]
        output.append(payload)
        indices.append({'id':case_id,'line_id':chosen['id'],'text_hash':digest(body(conn,chosen)),
                        'source_hash':source_hash(chosen),
                        'context_hash':context_hash(history),
                        'parent_id':(p['id'] if (p:=parent(conn,chosen,history,ident.guid)) is not None else None)})
    if have(conn,'learn_rewrite'):
        for receipt in conn.execute('SELECT * FROM learn_rewrite ORDER BY first_line_id'):
            if chosen:=by_id.get(receipt['first_line_id']):
                # Require a confirmed stored rewritten utterance, never a refused draft.
                sent=paired(conn,chosen)
                if sent is not None and sent['dropped'] is None and sent['guard_rewritten'] and sent['guard_verdict']=='rewrite':
                    plain=' '.join(r[0] for r in conn.execute(
                        'SELECT plain FROM learn_rewrite_line WHERE generation=? ORDER BY line_id',(receipt['generation'],)))
                    add(chosen,None,' '.join(receipt['original'].split()),'guard_rewrite',plain=plain)
    votes={}
    for lid,value in conn.execute("SELECT line_id,value FROM talk_signal WHERE source='explicit' "
                                  "AND kind='explicit' AND ts<?",(until,)):
        anchor=membership.get(lid,lid)
        votes[anchor]=min(votes.get(anchor,value),value)
    grouped=defaultdict(list)
    for row in rows:
        grouped[(row['from_guid'],room(row))].append(row)
    for group in grouped.values():
        bad=[r for r in group if votes.get(r['id'])==-1]
        lows=[r for r in group if r['value'] is not None and r['value']<=0]
        for good in group:
            if votes.get(good['id'])==1:
                near=[r for r in bad if r['id']!=good['id'] and abs(r['ts']-good['ts'])<=7*DAY]
                if near:
                    other=min(near,key=lambda r:(abs(r['ts']-good['ts']),r['id']))
                    add(good,other,body(conn,other),'thumbs')
            if good['value'] is None or good['value']<.5:
                continue
            pv=vector(conn,parent(conn,good,context_rows(conn,good,ident.guid),ident.guid))
            if pv is None:
                continue
            for other in sorted(lows,key=lambda r:abs(r['ts']-good['ts']))[:128]:
                if other['id']==good['id'] or abs(other['ts']-good['ts'])>7*DAY:
                    continue
                ov=vector(conn,parent(conn,other,context_rows(conn,other,ident.guid),ident.guid))
                if ov and pv[0]==ov[0] and similarity(pv[1],ov[1])>=.8:
                    add(good,other,body(conn,other),'reward')
                    break
    return output,indices,{'pairs':len(output),'candidate_limit':MAX_LINES,'pair_limit':MAX_PAIRS,
                          'truncated':truncated or len(output)>=MAX_PAIRS}


def write_jsonl(path,rows):
    # Exclusive creation refuses accidental replacement of an earlier private dataset.
    with Path(path).open('x',encoding='utf-8',newline='\n') as stream:
        for row in rows:
            stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
