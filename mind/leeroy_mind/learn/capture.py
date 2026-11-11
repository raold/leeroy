"""Optional reuse of guard evidence, with no additional model call or content log."""
from hashlib import sha256
from leeroy_mind.claims.store import vector
from leeroy_mind.discord.channels import is_restricted


def digest(text):
    return sha256(text.encode('utf-8')).hexdigest()


def vectors(conn, history, cached, model):
    for item in history:
        if item.id<=0 or item.id not in cached:
            continue
        row=conn.execute('SELECT text,channel FROM chat_line WHERE id=?',(item.id,)).fetchone()
        if row is None or row[0]!=item.text or row[1] in ('officer','voice:officers') or is_restricted(row[1]):
            continue
        conn.execute('INSERT INTO learn_line_vector VALUES (?,?,?,?) ON CONFLICT(line_id) DO UPDATE SET '
            'model_hash=excluded.model_hash,text_hash=excluded.text_hash,embedding=excluded.embedding',
            (item.id,digest(model),digest(item.text),vector(cached[item.id])))


def delivered(conn,line_id,meta):
    original=getattr(meta,'preference_original','')
    if (not original or not meta.guard_rewritten or meta.guard_verdict!='rewrite'
            or len(original)>4000):
        return
    row=conn.execute('SELECT channel,from_guid,to_guid FROM chat_line WHERE id=?',(line_id,)).fetchone()
    if row is None or row[0] in ('officer','voice:officers') or is_restricted(row[0]):
        return
    generation=getattr(meta,'preference_generation','')
    prior=conn.execute('SELECT c.channel,c.from_guid,c.to_guid FROM learn_rewrite r '
                       'JOIN chat_line c ON c.id=r.first_line_id WHERE r.generation=?',(generation,)).fetchone()
    if prior is None or tuple(prior)!=tuple(row):
        generation=str(line_id)  # durable IDs do not reset with the engine's turn counter
        conn.execute('INSERT INTO learn_rewrite VALUES (?,?,?)',(generation,line_id,original))
    conn.execute('INSERT INTO learn_rewrite_line VALUES (?,?)',(line_id,generation))
    meta.preference_generation=generation
