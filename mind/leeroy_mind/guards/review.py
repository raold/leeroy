"""Model-free candidate review. No commits, migrations, logging or send hooks."""
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
import json
import math
import re
import sqlite3
import time

from leeroy_mind.claims.store import vector as checked_vector
from leeroy_mind.discord.channels import channel_id, discord_sql, is_discord, is_private
from leeroy_mind.discord.layout import dm_peer
from leeroy_mind.discord.text import is_emoji_message
from leeroy_mind.eval.talk import contradiction, nonsequitur, repetition, unawareness
from leeroy_mind.eval.talk.common import Line, day, evening, similarity, words
from leeroy_mind.memory.recall import whisper_ok
from leeroy_mind.talk.moves import topics

from .types import Draft, GuardStores, HistoryLine, Reference, Score, Verdict

OWN_LINES = 128
GUILD_LINES = 256
GUILD_SECONDS = 12 * 3600
CONTEXT_LINES = 16
MAX_TEXT = 4000
RECEIPT_DAYS = 14
REACTIONS = ('👍', '😄')
_CHANNELS = frozenset({'guild', 'officer', 'whisper', 'game', 'discord', 'dm', 'voice'})
_GAME = frozenset({'party', 'raid', 'raid_warning', 'say', 'yell', 'emote'})
DROP_CODES = frozenset({'empty', 'own_repeat_same_channel', 'own_repeat_cross_channel',
                        'repeated_topic', 'nothing_new'})


def _action(codes):
    if any(code not in DROP_CODES and code != 'filler_after_question' for code in codes):
        return 'rewrite'
    if any(code in DROP_CODES for code in codes):
        return 'drop'
    return 'react' if 'filler_after_question' in codes else 'send'


def _old_schema(exc):
    return str(exc).startswith(('no such table:', 'no such column:', 'no such module:'))


class Reader:
    def __init__(self, conn):
        self.conn = conn
        self.missing = set()

    def rows(self, code, sql, args=()):
        try:
            cursor = self.conn.execute(sql, args)
            keys = [column[0] for column in cursor.description]
            return [dict(zip(keys, row)) for row in cursor.fetchall()]
        except sqlite3.OperationalError as exc:
            if not _old_schema(exc):
                raise
            self.missing.add(code)
            return []


def _json(raw, default=None):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _now(stores, draft):
    return draft.now if draft.now is not None else stores.clock.now() if stores.clock else time.time()


def _validate(draft, now):
    if type(draft.guid) is not int or not 0 < draft.guid <= 0xFFFFFFFF:
        raise ValueError('guard_guid_invalid')
    if draft.channel not in _CHANNELS or (draft.channel == 'game' and draft.room not in _GAME):
        raise ValueError('guard_channel_invalid')
    if draft.channel in {'discord', 'dm', 'voice'} and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', draft.room):
        raise ValueError('guard_room_invalid')
    if type(now) not in (float, int) or not math.isfinite(now):
        raise ValueError('guard_time_invalid')
    if not isinstance(draft.text, str) or len(draft.text) > MAX_TEXT:
        raise ValueError('guard_text_invalid')
    if len(draft.thread_ids) > CONTEXT_LINES or any(type(i) is not int or i <= 0 for i in draft.thread_ids):
        raise ValueError('guard_thread_invalid')
    if draft.parent_id is not None and (type(draft.parent_id) is not int or draft.parent_id <= 0):
        raise ValueError('guard_parent_invalid')
    if draft.line_id is not None and (type(draft.line_id) is not int or not 0 < draft.line_id < 2**63):
        raise ValueError('guard_line_invalid')
    if draft.embedding is not None:
        checked_vector(draft.embedding)


def _visible(line, draft, officer):
    if (line.channel in {'discord', 'dm'}) != (draft.channel in {'discord', 'dm'}):
        return False
    if line.readers is not None and line.guid != draft.guid and draft.guid not in line.readers:
        return False
    if line.channel == 'whisper':
        return draft.channel == 'whisper' and draft.guid in (line.guid, line.to_guid) and whisper_ok(
            'whisper', line.guid, line.to_guid, speaker=draft.guid,
            turn_channel='whisper', to_guid=draft.to_guid)
    if line.channel == 'dm':
        return (draft.channel == 'dm' and line.room == draft.room and line.readers is not None
                and draft.guid in line.readers and {line.guid, line.to_guid} == {draft.guid, draft.to_guid})
    if line.channel == 'officer':
        return officer and draft.channel == 'officer'
    if line.channel == 'voice':
        return (draft.channel == 'voice' and line.room == draft.room and line.readers is not None
                and draft.guid in line.readers and (line.room != 'officers' or officer))
    if line.channel == 'guild':
        return True
    if line.channel == 'discord':
        return (draft.channel == 'discord' and line.room == draft.room and line.readers is not None
                and draft.guid in line.readers)
    return line.channel == 'game' and (line.guid == draft.guid or
        line.readers is not None and draft.guid in line.readers)


def _same_room(line, draft):
    return line.channel == draft.channel and line.room == draft.room


def _line(row, vectors):
    channel = row['channel']
    room = channel if channel in _GAME else ''
    if is_discord(channel):
        private = is_private(channel)
        room = str(dm_peer(channel)) if private else channel_id(channel)
        channel = 'dm' if private else 'discord'
    elif channel.startswith('voice:'):
        room, channel = channel.removeprefix('voice:'), 'voice'
    readers = _json(row.get('audience'))
    readers = tuple(readers) if isinstance(readers, list) and all(type(g) is int for g in readers) else None
    return HistoryLine(row['id'], row['ts'], row['from_guid'], row['text'],
        'game' if channel in _GAME else channel, row['to_guid'], room, row.get('thread_id'),
        vectors.get(row['id']), readers, row['origin'])


def _history(reader, stores, draft, now, officer):
    columns = 'id,ts,from_guid,to_guid,channel,thread_id,origin,text,audience'
    ids = tuple(dict.fromkeys((*draft.thread_ids, *((draft.parent_id,) if draft.parent_id else ()))))
    cutoff = draft.line_id if draft.line_id is not None else 2**63-1
    groups = [reader.rows('history_unavailable', f'SELECT {columns} FROM chat_line '
        'WHERE from_guid=? AND NOT (' + discord_sql('channel') + ') '
        'AND ts BETWEEN ? AND ? AND id<? ORDER BY ts DESC,id DESC LIMIT ?',
        (draft.guid, now-stores.config.repeat_seconds, now, cutoff, OWN_LINES)),
        reader.rows('history_unavailable', f'SELECT {columns} FROM chat_line '
        "WHERE channel='guild' AND origin='mind' AND ts BETWEEN ? AND ? AND id<? ORDER BY ts DESC,id DESC LIMIT ?",
        (now-GUILD_SECONDS, now, cutoff, GUILD_LINES))] if draft.channel not in {'discord', 'dm'} else []
    if ids and draft.channel not in {'discord', 'dm'}:
        groups.append(reader.rows('context_unavailable', f'SELECT {columns} FROM chat_line '
            'WHERE id IN (SELECT value FROM json_each(?)) AND ts BETWEEN ? AND ? AND id<?',
            (json.dumps(ids), now-nonsequitur.CONTEXT_SECONDS, now, cutoff)))
    found = {('db', row['id']): _line(row, stores.vectors) for rows in groups for row in rows}
    for line in stores.history[-(OWN_LINES+GUILD_LINES+CONTEXT_LINES):]:
        found[(line.channel, line.room, line.id)] = line
    # Keep source windows independent. Each consumer below applies its own lower
    # bound; a short own-repeat window must not erase valid parent/evening evidence.
    return sorted((line for line in found.values() if line.ts <= now
                   and (draft.line_id is None or line.id < draft.line_id)
                   and len(line.text) <= MAX_TEXT and _visible(line, draft, officer)), key=lambda x: (x.ts, x.id))


def _context(history, draft, now):
    ids = set(draft.thread_ids)
    if draft.parent_id:
        ids.add(draft.parent_id)
    thread = [line for line in history if line.id in ids and _same_room(line, draft)
              and 0 <= now-line.ts <= nonsequitur.CONTEXT_SECONDS]
    parent = next((line for line in thread if line.id == draft.parent_id), None)
    # The shared relevance helper expects the parent in its thread vectors. Keep
    # that evidence even when all sixteen caller thread slots are other lines.
    recent = thread[-CONTEXT_LINES:]
    if parent is not None and parent not in recent:
        recent = [parent, *recent[-(CONTEXT_LINES-1):]]
    return parent, recent


def _pairs(text, vector, history, cfg):
    tokens = words(text)
    if len(tokens) < repetition.MIN_WORDS:
        return []
    mine = repetition.ngrams(tokens)
    hits = []
    for line in history:
        cosine = similarity(vector, checked_vector(line.embedding)) \
            if cfg.semantic_enabled and vector is not None and line.embedding is not None else 0.0
        overlap, score, _ = repetition.pair_score(mine, repetition.ngrams(words(line.text)), cosine)
        if overlap >= cfg.repeat_overlap or cfg.semantic_enabled and vector is not None \
                and line.embedding is not None and cosine >= cfg.repeat_cosine:
            hits.append((line, score))
    return sorted(hits, key=lambda item: (-item[1], item[0].id))


def _gags(reader, draft):
    tokens = words(draft.text)
    keys = {f'{kind}:{hashlib.sha256(" ".join(key).encode()).hexdigest()}'
            for kind, key in repetition.stock_keys(tokens)}
    for row in reader.rows('gags_unavailable', 'SELECT l.id,l.term FROM lexicon l '
            'JOIN lexicon_adopter a ON a.term=l.id WHERE a.guid=? AND a.active=1 '
            "AND l.status='active' AND l.kind IN ('joke','slang') ORDER BY l.id LIMIT 100", (draft.guid,)):
        phrase = words(row['term'])
        if phrase and phrase in [tokens[i:i+len(phrase)] for i in range(len(tokens)-len(phrase)+1)]:
            keys.add(f'gag:{row["id"]}')
    return keys


def _receipt_key(draft, now):
    return f'guards:sent:{day(now)}:{draft.guid}'


def _receipts(reader, draft, now):
    rows = reader.rows('ration_unavailable', 'SELECT value FROM meta WHERE key=?', (_receipt_key(draft, now),))
    receipts = _json(rows[0]['value']) if rows else {}
    if (not isinstance(receipts, dict) or any(not re.fullmatch(r'[0-9a-f]{64}', key)
            or not isinstance(value, list) or any(not isinstance(v, str) or not re.fullmatch(
                r'(?:gag:[1-9][0-9]*|stock_(?:opening|closing):[0-9a-f]{64})', v) for v in value)
            for key, value in receipts.items())):
        raise ValueError('guard_receipt_invalid')
    return receipts


def record_sent(stores: GuardStores, draft: Draft, *, receipt_id: str) -> bool:
    """Record confirmed delivery once. Join the caller's transaction; never commit.

    The future engine serializes review/send/record per member, and calls this only
    after delivery. Draft.now must be the fixed delivery timestamp, preserved on
    retries across midnight. Retrying review or rejecting a draft spends nothing.
    """
    if not stores.config.enabled:
        return False
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(stores.conn,draft.guid):
        return False
    if draft.now is None:
        raise ValueError('guard_delivery_time_required')
    now = _now(stores, draft)
    _validate(draft, now)
    if not isinstance(receipt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', receipt_id):
        raise ValueError('guard_receipt_id_invalid')
    reader = Reader(stores.conn)
    # Keep cleanup in the caller's delivery transaction, even when this line has no gag.
    if stores.conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta' AND type='table'").fetchone():
        cutoff = (date.fromisoformat(day(now)) - timedelta(days=RECEIPT_DAYS)).isoformat()
        stores.conn.execute("DELETE FROM meta WHERE key GLOB 'guards:sent:????-??-??:*' AND key<?",
                            ('guards:sent:' + cutoff + ':',))
    keys = sorted(_gags(reader, draft))
    if not keys:
        return False
    receipts = _receipts(reader, draft, now)
    if 'ration_unavailable' in reader.missing:
        raise ValueError('guard_receipt_store_unavailable')
    receipt = hashlib.sha256(receipt_id.encode()).hexdigest()
    if receipt in receipts:
        if receipts[receipt] != keys:
            raise ValueError('guard_receipt_conflict')
        return False
    stores.conn.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET '
        'value=json_insert(meta.value,?,json(?))', (_receipt_key(draft, now), json.dumps({receipt: keys}),
        f'$."{receipt}"', json.dumps(keys)))
    return True


def _claim_hits(reader, stores, draft, now, officer):
    if not stores.claims_enabled or draft.embedding is None:
        return []
    try:
        found = contradiction.check_line(stores.conn, draft.guid, draft.text, draft.embedding, now=now)
    except sqlite3.OperationalError as exc:
        if not _old_schema(exc):
            raise
        reader.missing.add('claims_unavailable')
        return []
    if not found:
        return []
    rows = reader.rows('claims_unavailable', 'SELECT c.id,c.source,c.first_seen,l.id AS line_id,l.ts,l.from_guid,'
        'l.to_guid,l.channel,l.thread_id,l.origin,l.audience FROM member_claim c '
        'LEFT JOIN chat_line l ON l.id=c.source_line WHERE c.id IN (SELECT value FROM json_each(?))',
        (json.dumps([hit.claim_id for hit in found]),))
    visible = set()
    for row in rows:
        if row['first_seen'] > now:
            continue
        if (row['line_id'] is not None and row['ts'] <= now
                and (row['source'] == 'owner' or row['source'] == 'chat' and row['from_guid'] == draft.guid)):
            if _visible(_line(dict(row, id=row['line_id'], text=''), {}), draft, officer):
                visible.add(row['id'])
    return [hit for hit in found if hit.claim_id in visible]


def review(stores: GuardStores, draft: Draft) -> Verdict:
    if not stores.config.enabled:
        return Verdict('send')
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(stores.conn,draft.guid):
        return Verdict('drop', ('world_excluded',))
    now = _now(stores, draft)
    _validate(draft, now)
    if not words(draft.text) and not (draft.channel in {'discord', 'dm'} and is_emoji_message(draft.text)):
        return Verdict('drop', ('empty',))
    cfg, reader = stores.config, Reader(stores.conn)
    role = reader.rows('role_unavailable', "SELECT json_extract(json,'$.play.guild_role') AS role "
                       'FROM persona_core WHERE guid=? AND json_valid(json)', (draft.guid,))
    officer = bool(role and role[0]['role'] == 'officer')
    history = _history(reader, stores, draft, now, officer)
    parent, thread = _context(history, draft, now)
    scores, refs, rewrite, drop = [], [], [], []
    own = [line for line in history if line.guid == draft.guid and line.origin == 'mind'
           and now-line.ts <= cfg.repeat_seconds][-OWN_LINES:]
    repeats = _pairs(draft.text, draft.embedding, own, cfg)
    if repeats:
        previous, score = repeats[0]
        drop.append('own_repeat_same_channel' if _same_room(previous, draft) else 'own_repeat_cross_channel')
        scores.append(Score('repeat', round(score, 6)))
        refs.append(Reference('line', previous.id))
    shared = defaultdict(set)
    bucket = evening(now)
    if draft.channel in {'guild', 'discord'} and bucket is not None:
        for line in history:
            if _same_room(line, draft) and line.origin == 'mind' and now-line.ts <= GUILD_SECONDS \
                    and evening(line.ts) == bucket:
                for phrase in repetition.phrase_keys(words(line.text)):
                    shared[phrase].add(line.guid)
        if repetition.shared_by_others(words(draft.text), draft.guid, shared):
            rewrite.append('guild_evening_phrase')
    # Reuse the variety ledger without its Store.transaction()/pruning writers.
    seen_topics = Counter()
    for row in reader.rows('topics_unavailable', 'SELECT channel,to_guid,topics FROM talk_move '
            'WHERE guid=? AND ts BETWEEN ? AND ? ORDER BY ts DESC,id DESC LIMIT 64',
            (draft.guid, now-86400, now)):
        line = _line(dict(id=0,ts=now,from_guid=draft.guid,text='',origin='mind',**row), {})
        if _visible(line, draft, officer):
            values = _json(row['topics'], [])
            if isinstance(values, list):
                seen_topics.update(v for v in values if isinstance(v, str))
    topic_uses = max((seen_topics[t] for t in topics(draft.text)), default=0)
    if topic_uses:
        scores.append(Score('recent_topic_uses', topic_uses))
        if repeats:
            drop.append('repeated_topic')
    keys = _gags(reader, draft)
    try:
        counts = Counter(key for value in _receipts(reader, draft, now).values() for key in set(value))
    except ValueError:
        counts = Counter()
        rewrite.append('ration_unavailable')
    for key in sorted(keys):
        if key.startswith('gag:'):
            ident = int(key.split(':')[1])
            if counts[key] >= cfg.gag_caps.get(ident, cfg.gag_daily_cap):
                rewrite.append('gag_ration')
                refs.append(Reference('gag', ident))
        elif counts[key] >= cfg.stock_daily_cap:
            rewrite.append('stock_phrase_ration')
    conflicts = _claim_hits(reader, stores, draft, now, officer)
    if conflicts:
        rewrite.append('claim_conflict')
        refs.extend(Reference('claim', hit.claim_id) for hit in conflicts)
        scores.append(Score('claim', round(max(hit.score for hit in conflicts), 6)))
    if stores.facts is not None and draft.channel not in {'discord','dm'}:
        line = Line(0, now, draft.guid, draft.to_guid, None, draft.channel, 'mind', draft.map, draft.zone, draft.text)
        world = unawareness.check_line(stores.facts, line)
        rewrite.extend(flag.rule for flag in world)
        refs.extend(Reference('world', flag.related_id) for flag in world if type(flag.related_id) is int)
    filler = bool((draft.real_answer_due or parent and parent.text.rstrip().endswith('?'))
                  and nonsequitur.is_filler(words(draft.text)))
    if parent and cfg.semantic_enabled and len(words(draft.text)) >= nonsequitur.MIN_REPLY_WORDS \
            and draft.embedding is not None and parent.embedding is not None:
        parent_score, thread_score = nonsequitur.relevance_scores(draft.embedding,
            checked_vector(parent.embedding), [checked_vector(line.embedding) for line in thread if line.embedding is not None])
        scores.extend((Score('parent_relevance', round(parent_score,6)), Score('thread_relevance', round(thread_score,6))))
        if parent_score < cfg.parent_cosine and thread_score < cfg.thread_cosine:
            rewrite.append('low_parent_and_thread_relevance')
    if not filler and _pairs(draft.text, draft.embedding, [line for line in thread if line.guid != draft.guid], cfg):
        drop.append('nothing_new')
    codes = tuple(dict.fromkeys([*rewrite, *drop, *(['filler_after_question'] if filler else [])]))
    action = _action(codes)
    if reader.missing:
        scores.append(Score('missing_sources', len(reader.missing)))
    reaction = '👍' if parent and parent.text.rstrip().endswith('?') else '😄' if set(words(draft.text)) & {'lol','haha','hehe'} else '👍'
    return Verdict(action, codes, tuple(scores), tuple(dict.fromkeys(refs)), reaction if action == 'react' else None)
