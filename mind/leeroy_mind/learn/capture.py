"""Optional reuse of guard evidence, with no additional model call or content log."""
from hashlib import sha256
from leeroy_mind.claims.store import vector
from leeroy_mind.discord.channels import is_restricted, is_discord, channel_id
from .control import frozen


def digest(text):
    return sha256(text.encode('utf-8')).hexdigest()


def vectors(conn, history, cached, model,*,channel=None,settings=None):
    if frozen(conn,settings):
        return
    for item in history:
        if item.id<=0 or item.id not in cached:
            continue
        line_id=item.id
        if is_discord(channel):
            message=conn.execute('SELECT text,channel,author,origin,deleted FROM discord_message WHERE id=?',(item.id,)).fetchone()
            if message is None or message[0]!=item.text or message[1]!=channel_id(channel) or message[4] is not None:
                continue
            mapped=conn.execute("SELECT value FROM meta WHERE key=?",(f'discord:line:{item.id}',)).fetchone()
            if mapped is None:
                continue
            line_id=int(mapped[0])
        row=conn.execute('SELECT text,channel,from_guid,origin FROM chat_line WHERE id=?',(line_id,)).fetchone()
        if row is None or row[0]!=item.text or row[1] in ('officer','voice:officers') or is_restricted(row[1]):
            continue
        if channel is not None and row[1]!=channel:
            continue
        if is_discord(channel) and (row[2],row[3])!=(message[2],message[3]):
            continue
        conn.execute('INSERT INTO learn_line_vector VALUES (?,?,?,?) ON CONFLICT(line_id) DO UPDATE SET '
            'model_hash=excluded.model_hash,text_hash=excluded.text_hash,embedding=excluded.embedding',
            (line_id,digest(model),digest(item.text),vector(cached[item.id])))


def delivered(conn,line_id,meta,*,settings=None):
    if frozen(conn,settings):
        return
    target=getattr(meta,'discord',None)
    if target is not None and target.reply_to is not None:
        parent=conn.execute("SELECT value FROM meta WHERE key=?",(f'discord:line:{target.reply_to}',)).fetchone()
        if parent is not None:
            conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                         (f'learn:discord-parent:{line_id}',parent[0]))
    original=' '.join(getattr(meta,'preference_original','').split())
    plain=' '.join(getattr(meta,'words','').split())
    if (not original or not meta.guard_rewritten or meta.guard_verdict!='rewrite'
            or not plain or len(original)>4000):
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
    conn.execute('INSERT INTO learn_rewrite_line VALUES (?,?,?)',(line_id,generation,plain))
    meta.preference_generation=generation


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='learn_rewrite'").fetchone() is not None


def prune(conn,now,*,schema_available=False):
    """The reward night's retention pass; raw private drafts/vectors last at most 90 days."""
    if not schema_available and not available(conn):
        return
    cutoff=now-90*86400
    conn.execute('DELETE FROM learn_rewrite WHERE first_line_id IN (SELECT id FROM chat_line WHERE ts<?)',(cutoff,))
    conn.execute('DELETE FROM learn_line_vector WHERE line_id IN (SELECT id FROM chat_line WHERE ts<?)',(cutoff,))
    conn.execute("DELETE FROM meta WHERE key IN (SELECT 'learn:discord-parent:' || id FROM chat_line WHERE ts<?)",(cutoff,))
    conn.execute("DELETE FROM meta WHERE key GLOB 'learn:recipe:*' AND NOT EXISTS "
                 "(SELECT 1 FROM chat_line WHERE id=CAST(substr(meta.key,14) AS INTEGER) AND ts>=?)", (cutoff,))
