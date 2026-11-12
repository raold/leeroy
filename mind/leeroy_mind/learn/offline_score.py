"""Replay candidate outputs through the existing four-detector guard adapter, offline."""
import json
from contextlib import contextmanager
from leeroy_mind.guards.review import day
from leeroy_mind.discord.channels import is_discord,is_private,channel_id
from leeroy_mind.discord.layout import dm_peer
from leeroy_mind.guards import Draft,GuardStores,HistoryLine,review
from leeroy_mind.guards.config import GuardsCfg
from leeroy_mind.eval.talk import unawareness
from leeroy_mind.claims.store import vector as check_vector
from leeroy_mind.storage.rows import pack_vec
from . import dataset
from .capture import digest


@contextmanager
def prior_receipts(conn,row):
    """Temporarily fence the owned snapshot's ledger to confirmed earlier deliveries.

    Live receipts hash line-<id>. Unmapped receipts cannot prove a historical use.
    The savepoint restores even on a failed detector and never changes the source DB.
    """
    key=f"guards:sent:{day(row['ts'])}:{row['from_guid']}"
    saved=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    known={digest(f'line-{r[0]}') for r in conn.execute(
        "SELECT id FROM chat_line WHERE from_guid=? AND origin='mind' AND "
        '(ts<? OR (ts=? AND id<?))',(row['from_guid'],row['ts'],row['ts'],row['id']))}
    receipts=json.loads(saved[0]) if saved else {}
    earlier={k:v for k,v in receipts.items() if k in known}
    conn.execute('SAVEPOINT learn_historical_rations')
    try:
        conn.execute('UPDATE meta SET value=? WHERE key=?',(json.dumps(earlier),key))
        yield
    finally:
        conn.execute('ROLLBACK TO learn_historical_rations')
        conn.execute('RELEASE learn_historical_rations')


def read_jsonl(path):
    if path.stat().st_size>64*1024*1024:
        raise ValueError('learn_input_too_large')
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if len(line)>100000:
                raise ValueError('learn_input_line_too_large')
            if line.strip():
                yield json.loads(line)


def score(conn,indices,candidates,*,owner,model_hash,include_private=False):
    by_id={r['id']:r for r in indices}
    facts=unawareness.load_facts(conn)
    results=[]
    seen=set()
    for candidate in candidates:
        if (type(candidate) is not dict or set(candidate)-{'id','text','embedding','model_hash'}
                or type(candidate.get('id')) is not str or candidate['id'] not in by_id
                or candidate['id'] in seen or type(candidate.get('text')) is not str
                or len(candidate['text'])>4000):
            raise ValueError('learn_candidate_invalid')
        seen.add(candidate['id'])
        case=by_id[candidate['id']]
        row=conn.execute('SELECT * FROM chat_line WHERE id=?',(case['line_id'],)).fetchone()
        if (row is None or digest(dataset.body(conn,row))!=case['text_hash']
                or dataset.source_hash(row)!=case['source_hash'] or not dataset.allowed(row,include_private)):
            raise ValueError('learn_eval_source_changed')
        history=dataset.context_rows(conn,row,owner)
        if dataset.context_hash(history)!=case['context_hash']:
            raise ValueError('learn_eval_source_changed')
        vectors={}
        for item in history:
            stored=dataset.vector(conn,item)
            if stored and stored[0]==model_hash:
                vectors[item['id']]=stored[1]
        embedded=None
        if 'embedding' in candidate:
            values=candidate['embedding']
            if (candidate.get('model_hash')!=model_hash or type(values) is not list or len(values)!=512
                    or any(type(v) not in (int,float) for v in values)):
                raise ValueError('learn_candidate_embedding_invalid')
            embedded=check_vector(pack_vec(values))
        channel=row['channel']
        if is_discord(channel):
            kind='dm' if is_private(channel) else 'discord'
            room=str(dm_peer(channel)) if kind=='dm' else channel_id(channel)
        elif channel.startswith('voice:'):
            kind,room='voice',channel.removeprefix('voice:')
        else:
            kind,room=(channel,'') if channel in ('guild','whisper') else ('game',channel)
        external=tuple(HistoryLine(r['id'],r['ts'],r['from_guid'],r['text'],kind,r['to_guid'],room,
                         r['thread_id'],vectors.get(r['id']),dataset.readers(r) or None,r['origin']) for r in history)
        draft=Draft(row['from_guid'],kind,candidate['text'],parent_id=case['parent_id'],
                    thread_ids=tuple(r['id'] for r in history),embedding=embedded,to_guid=row['to_guid'],
                    room=room,now=row['ts'],map=row['map'],zone=row['zone'],line_id=row['id'])
        with prior_receipts(conn,row):
            verdict=review(GuardStores(conn,GuardsCfg(enabled=True,semantic_enabled=embedded is not None),
                                      claims_enabled=embedded is not None,history=external if is_discord(channel) else (),
                                      vectors=vectors,facts=facts),draft)
        results.append({'id':candidate['id'],'action':verdict.action,'codes':list(verdict.codes),
            'scores':{s.code:s.value for s in verdict.scores},
            'semantic_embedding':embedded is not None,
            'parent_embedding':case['parent_id'] in vectors})
    return {'cases':len(results),'missing_candidates':len(by_id)-len(results),'results':results,
            'coverage':{'candidate_embeddings':sum(r['semantic_embedding'] for r in results),
                        'parent_embeddings':sum(r['parent_embedding'] for r in results)},
            'note':'Lexical checks always run; semantic relevance and contradiction need supplied matching embeddings. Rations use only mapped earlier delivery receipts; expired or unmapped receipts have no coverage. World checks have narrow coverage.'}
