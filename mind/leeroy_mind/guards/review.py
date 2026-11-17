"""Model-free candidate review. No commits, migrations, logging or send hooks."""
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta
from difflib import SequenceMatcher
import hashlib
import json
import math
import re
import sqlite3
import time

from leeroy_mind.claims.store import vector as checked_vector
from leeroy_mind.claims.visibility import claim_visible
from leeroy_mind.discord.channels import channel_id, discord_sql, is_discord, is_private, is_group
from leeroy_mind.discord.layout import dm_peer
from leeroy_mind.discord.text import is_emoji_message
from leeroy_mind.eval.talk import contradiction, nonsequitur, repetition, unawareness
from leeroy_mind.eval.talk.common import Line, day, evening, similarity, words
from leeroy_mind.memory.recall import whisper_ok
from leeroy_mind.personas.facts import (Facts, conflicts as persona_conflicts, facts_of_json,
                                        owner_texts)
from leeroy_mind.talk.everyday import everyday_reason
from leeroy_mind.talk.moves import topics

from . import owncopy, phrases, pronouns
from .identity import CODES as SLIP_CODES, code_for as slip_code, slips
from .opener import echo_opener
from .types import Draft, GuardStores, HistoryLine, Reference, Score, Verdict

OWN_LINES = 128
GUILD_LINES = 256
GUILD_SECONDS = 12 * 3600
CONTEXT_LINES = 16
MAX_TEXT = 4000
RECEIPT_DAYS = 14
REACTIONS = ('👍', '😄')
_CHANNELS = frozenset({'guild', 'officer', 'whisper', 'game', 'discord', 'dm', 'group', 'voice'})
_GAME = frozenset({'party', 'raid', 'raid_warning', 'say', 'yell', 'emote'})
DROP_CODES = frozenset({'empty', 'own_repeat_same_channel', 'own_repeat_cross_channel',
                        'repeated_topic', 'nothing_new', 'own_repeat_discord', 'channel_repeat', 'filler_opener',
                        'echo_parent', 'own_copy'})
# Never sent, and never an owed answer's fallback: his words said back as hers, her speaking
# with the other sex's body or role (identity.py), and her own previous message in this
# conversation sent again (owncopy.py). A slip is a rewrite, so it gets one retry.
HARD_CODES = frozenset({'echo_parent', 'own_copy', *SLIP_CODES.values()})
# A draft is never her candidate's "clean part" for these (Pipeline._short_answer cuts slips)
WHOLE_CODES = frozenset({'echo_parent', 'own_copy'})
OWN_COPY_SECONDS = 3 * 3600  # her messages of the conversation going on
ECHO_RUN, ECHO_RATIO, ECHO_MIN_WORDS = 8, .9, 6
ECHO_OPENING = 5  # his first words said back as hers, carried past his first sentence
ECHO_OPENING_FIRST = 3  # ...when that first sentence has this many words
# He asked for the words, so saying them is the answer ("repeat after me", "now say it"); never
# "i'd say that's fair".
_ASKED = re.compile(r"(?:^|[.!?,:;]\s*|\b(?:now|just|please|pls|plz|go on|go ahead|can you|"
                    r"could you|will you|wanna)\s+)(?:repeat|say)\b(?:\s+(?:after me|it|that|"
                    r"this|back|the words)\b|\s*[:\"“])", re.IGNORECASE)
_ASKING = re.compile(r'^(?:can|could|would|will|do|does|did|are|is|am|was|were|have|has|had|'
                     r'should|what|how|when|where|why|who|which|whats|hows|wanna)\b')
HINTS = {
    'echo_parent': 'it repeats the message you are answering back instead of answering it',
    'echo_opener': ('it opens by saying back the message you are answering, its words repeated, '
                    'quoted or restated; answer it without saying it back'),
    'member_misgender': 'it calls a guild member by the wrong pronoun',
    'nothing_new': 'it only repeats what was already said',
    'own_copy': ('it says your own previous message again; say something new that answers the '
                 'message in front of you'),
    'own_repeat_same_channel': 'it repeats one of your own recent lines',
    'own_repeat_cross_channel': 'it repeats one of your own recent lines',
    'own_repeat_discord': 'it repeats one of your own recent messages',
    'channel_repeat': 'it repeats what someone here already said today',
    'repeated_topic': 'it goes back over a topic you already covered',
    'gag_ration': 'it reuses a running joke you have used enough lately',
    'stock_phrase_ration': 'it leans on a stock phrase you have used enough lately',
    'guild_evening_phrase': 'it reuses a phrase the guild has heard enough tonight',
    'low_parent_and_thread_relevance': 'it does not answer what was said',
    'off_anchor': 'it wanders off the subject',
    'claim_conflict': 'it contradicts something already known',
    'phrase_conversation': 'it reuses a phrase you already said in this conversation',
    'phrase_day': 'it leans on a phrase you keep using today',
    'phrase_cast': 'it leans on a worn-out phrase',
    'stock_member': 'it leans on a stock phrase you used lately',
    'stock_cast': 'it leans on a worn-out stock phrase',
    'self_male_slip': ("it speaks as if you were a man, with a man's body or a man's role; you "
                       'are a woman, so say it as yourself, about your own body'),
    'self_female_slip': ("it speaks as if you were a woman, with a woman's body or a woman's "
                         'role; you are a man, so say it as yourself, about your own body'),
    'persona_conflict': ('it gets your own life wrong: keep to who you live with, your kids, your '
                         'pets, your job and where you live as your card says'),
}
FILLER = {'agree': ({'same','agreed','true','this','yeah','yep','ya','+1','facts','fr'}, '👍'),
          'laugh': ({'lol','lmao','lulz','rofl','haha','hahaha','hehe','kek'}, '😂'),
          'grats': ({'gz','grats','gratz','congrats','nice','gg'}, '🎉'),
          'thanks': ({'ty','thx','thanks','tyty'}, '❤️'),
          'sympathy': ({'rip','oof','f','ouch'}, '🫡')}
_EMOTICONS = re.compile(r':[pd](?!\w)|:[()]|<3(?!\w)|(?<!\w)xd(?!\w)', re.IGNORECASE)
# A server reply that is only filler ("lol", "same", "rip") becomes a reaction this share of the
# time; the rest are posted as the short messages a guild Discord is full of (2026-11-15).
FILLER_REACT_SHARE = .4


def echoes(text: str, parent: str) -> bool:
    """True when `text` says `parent` back instead of answering it: the whole of a parent of at
    least ECHO_MIN_WORDS words in the same mood (a question as a question, a statement as a
    statement; their letters ECHO_RATIO alike), a run of ECHO_RUN words copied from one of his
    statements, a run of ECHO_RUN words copied across his sentences (a question among them
    too), or her line opening with his first ECHO_OPENING words or more, carried past the end
    of his first sentence ("are you sure? its kinda..." said back as hers). Turning his question
    into its answer ("Are the flasks ready?" / "The flasks are ready.") is an answer, and a
    short line said back ("i love you") is not an echo, nor is one that opens with his whole
    first sentence and stops there ("you drive me wild" / "you drive me wild too").
    Apostrophes are dropped first, so "you're" and "youre" match."""
    def bare(value):
        return words(re.sub(r"['\u2019]", "", value or ""))

    def asking(sentence):
        return sentence.rstrip().endswith('?') or bool(_ASKING.match(' '.join(bare(sentence))))
    mine, his = bare(text), bare(parent)
    if not mine or not his or _ASKED.search(parent or ''):
        return False  # nothing to compare, or he asked her to say it
    if (len(his) >= ECHO_MIN_WORDS and asking(text) == asking(parent)
            and SequenceMatcher(None, ' '.join(his), ' '.join(mine), autojunk=False).ratio() >= ECHO_RATIO):
        return True
    sentences = [bare(s) for s in re.split(r'(?<=[.!?])\s+|\n+', parent or '')]
    for sentence in re.split(r'(?<=[.!?])\s+|\n+', parent or ''):
        said = bare(sentence)
        if len(said) >= ECHO_RUN and not asking(sentence):
            blocks = SequenceMatcher(None, said, mine, autojunk=False).get_matching_blocks()
            if max(block.size for block in blocks) >= ECHO_RUN:
                return True
    # Which of his sentences each of his words is in, so a copy can be seen to cross one.
    where = [i for i, said in enumerate(s for s in sentences if s) for _ in said]
    if len(where) != len(his):  # markup split by a sentence break: read it as one sentence
        where = [0] * len(his)
    for block in SequenceMatcher(None, his, mine, autojunk=False).get_matching_blocks():
        if block.size >= ECHO_RUN and where[block.a] != where[block.a + block.size - 1]:
            return True
    # His opening needs a first sentence of ECHO_OPENING_FIRST words or more: "haha. you drive
    # me wild" / "haha you drive me wild too" is an answer.
    first = next((len(s) for s in sentences if s), 0)
    opening = next((i for i, (a, b) in enumerate(zip(his, mine)) if a != b), min(len(his), len(mine)))
    return (first >= ECHO_OPENING_FIRST and opening >= ECHO_OPENING
            and where[opening - 1] != where[0])


def rewrite_hint(codes, named=(), misgendered=()) -> str:
    """The screened retry's instruction in words; a code without words is named as it is.
    `named` are the phrase cooldown's words from the draft itself (Verdict.phrases);
    `misgendered` the member and pronouns of `member_misgender` (Verdict.pronouns)."""
    reasons = list(dict.fromkeys(HINTS.get(code, code) for code in codes))
    said = ' '.join(x for x in (phrases.hint(named), pronouns.hint(misgendered)) if x)
    if not reasons:
        return 'Rewrite your reply once.' + (' ' + said if said else '')
    hint = 'Rewrite your reply once in your own words. Your draft has a problem: ' + '; '.join(reasons)
    return hint + '. ' + said if said else hint


def fresh_hint(codes) -> str:
    """rewrite_hint for a new draft that never sees the failed one (a hard problem: his words
    said back, her as a man, must not be the text the model goes on from)."""
    reasons = list(dict.fromkeys(HINTS.get(code, code) for code in codes))
    return ('Write your reply in your own words, as yourself. A draft of it had a problem to '
            'avoid: ' + '; '.join(reasons)) if reasons else 'Write your reply in your own words.'


def _action(codes):
    if any(code not in DROP_CODES and code not in ('filler_after_question','server_filler') for code in codes):
        return 'rewrite'
    if any(code in DROP_CODES for code in codes):
        return 'drop'
    return 'react' if any(c in codes for c in ('filler_after_question','server_filler')) else 'send'


def _server_filler(text):
    tokens = re.findall(r'\+1|\w+', _EMOTICONS.sub(' ',text).casefold())
    if 0 < len(tokens) <= 4:
        return next((emoji for tokenset,emoji in FILLER.values() if set(tokens) <= tokenset), None)
    return None


def _filler_posts(draft) -> bool:
    """A server reply of only filler is posted as its short message, not turned into a reaction,
    for 1 - FILLER_REACT_SHARE of drafts. The draw is fixed by the speaker, the message answered
    and the words, never the clock, so the recheck before sending gives the same answer."""
    key = f'{draft.guid}:{draft.parent_id}:{" ".join(draft.text.casefold().split())}'
    draw = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big') / 2**64
    return draw >= FILLER_REACT_SHARE


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
    if draft.channel in {'discord', 'dm', 'group', 'voice'} and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', draft.room):
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
    if line.channel == 'group':
        from leeroy_mind.groups.audience import visible
        target = ('gdm:' + draft.room if draft.channel == 'group' else
                  'dm:' + draft.room if draft.channel == 'dm' else draft.channel)
        return visible(line.readers,target,speaker=draft.guid,to_guid=draft.to_guid,
                       readers=draft.readers)
    if line.channel in {'whisper', 'dm'} and draft.channel in {'whisper', 'dm'}:
        # One mind per member: a whisper and a DM between the same two are one conversation.
        if line.readers is not None and line.guid != draft.guid and draft.guid not in line.readers:
            return False
        return ((line.channel != 'dm' or line.readers is not None)
                and draft.guid in (line.guid, line.to_guid)
                and {line.guid, line.to_guid} == {draft.guid, draft.to_guid})
    if (line.channel in {'discord', 'dm', 'group'}) != (draft.channel in {'discord', 'dm', 'group'}):
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
        group = is_group(channel)
        room = channel.removeprefix('gdm:') if group else str(dm_peer(channel)) if private else channel_id(channel)
        channel = 'group' if group else 'dm' if private else 'discord'
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
        (now-GUILD_SECONDS, now, cutoff, GUILD_LINES))] if draft.channel not in {'discord', 'dm', 'group'} else []
    if draft.channel in {'whisper', 'dm'}:
        # One mind per member: her own lines in their whispers and in her DM with him count
        # toward her repeats in either (_visible keeps them to the same two).
        groups.append(reader.rows('history_unavailable', f'SELECT {columns} FROM chat_line '
            "WHERE from_guid=? AND (channel='whisper' OR channel=?) AND ts BETWEEN ? AND ? "
            'AND id<? ORDER BY ts DESC,id DESC LIMIT ?',
            (draft.guid, f'dm:{draft.guid}', now-stores.config.repeat_seconds, now, cutoff,
             OWN_LINES)))
    if ids and draft.channel not in {'discord', 'dm', 'group'}:
        groups.append(reader.rows('context_unavailable', f'SELECT {columns} FROM chat_line '
            'WHERE id IN (SELECT value FROM json_each(?)) AND ts BETWEEN ? AND ? AND id<?',
            (json.dumps(ids), now-nonsequitur.CONTEXT_SECONDS, now, cutoff)))
    found = {('db', row['id']): _line(row, stores.vectors) for rows in groups for row in rows}
    for line in stores.history[-(OWN_LINES+GUILD_LINES+CONTEXT_LINES):]:
        if not line.own_repeat_only:
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


def _pairs(text, vector, history, cfg, *, minimum=repetition.MIN_WORDS):
    tokens = words(text)
    if len(tokens) < minimum:
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
    for row in reader.rows('gags_unavailable', 'SELECT l.id,l.term,l.kind FROM lexicon l '
            'JOIN lexicon_adopter a ON a.term=l.id WHERE a.guid=? AND a.active=1 '
            "AND l.status='active' AND l.kind IN ('joke','slang') ORDER BY l.id LIMIT 100", (draft.guid,)):
        phrase = words(row['term'])
        if row['kind'] == 'slang' and len(phrase) == 1 and everyday_reason(phrase[0]):
            continue  # an everyday word is never slang, even when one is still stored
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
            raw = ('discord:' + draft.room if draft.channel == 'discord' else
                   'gdm:' + draft.room if draft.channel == 'group' else
                   'dm:' + draft.room if draft.channel == 'dm' else
                   'voice:' + draft.room if draft.channel == 'voice' else
                   draft.room if draft.channel == 'game' else draft.channel)
            line = _line(dict(row, id=row['line_id'], text=''), {})
            if claim_visible(source_channel=row['channel'], source_from=row['from_guid'],
                    source_to=row['to_guid'], source_readers=line.readers, draft_channel=raw,
                    speaker=draft.guid, to_guid=draft.to_guid, officer=officer, cross_medium=stores.cross_medium,
                    draft_readers=draft.readers):
                visible.add(row['id'])
    return [hit for hit in found if hit.claim_id in visible]


def _misgendered(reader, stores, draft, parent):
    """pronouns.misgendered for the draft, with the roster (character names and the players'
    genders, the speaker left out; guild members without a persona as other people) read only
    when it calls someone "he" or "she". His line it answers names whom a pronoun may mean."""
    if not pronouns.calls_someone(draft.text):
        return ()
    from leeroy_mind.talk.words import COMMON_WORDS
    roster = {}
    for row in reader.rows('roster_unavailable', "SELECT guid,json_extract(json,'$.character_name') "
            "AS name,json_extract(json,'$.identity.player_gender') AS gender FROM persona_core "
            'WHERE json_valid(json) LIMIT 2000'):
        name = row['name']
        if row['guid'] != draft.guid and isinstance(name, str) and row['gender'] in pronouns.FORMS:
            key = name.casefold()
            roster[key] = (name, row['gender'], key in COMMON_WORDS or bool(everyday_reason(key)))
    others = frozenset(row['name'].casefold() for row in reader.rows('roster_unavailable',
        'SELECT name FROM cast_member LIMIT 2000') if isinstance(row['name'], str)
        and row['name'].casefold() not in roster)
    his = [*stores.his_names, *(row['value'] for row in reader.rows('roster_unavailable',
           "SELECT value FROM meta WHERE key='human_name'") if isinstance(row['value'], str))]
    his_line = (parent.text if parent is not None and parent.guid != draft.guid
                and parent.origin == 'human' else '')
    return pronouns.misgendered(draft.text, his_line, roster, his_names=his, others=others)


def review(stores: GuardStores, draft: Draft) -> Verdict:
    if not stores.config.enabled:
        return Verdict('send')
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(stores.conn,draft.guid):
        return Verdict('drop', ('world_excluded',))
    now = _now(stores, draft)
    _validate(draft, now)
    if not words(draft.text) and not (draft.channel in {'discord', 'dm', 'group'} and is_emoji_message(draft.text)):
        return Verdict('drop', ('empty',))
    cfg, reader = stores.config, Reader(stores.conn)
    if draft.server:
        cfg = replace(cfg, semantic_enabled=True)
    role = reader.rows('role_unavailable', "SELECT json_extract(json,'$.play.guild_role') AS role,"
                       'json FROM persona_core WHERE guid=? AND json_valid(json)', (draft.guid,))
    officer = bool(role and role[0]['role'] == 'officer')
    stored = _json(role[0]['json'], None) if role else None
    facts = facts_of_json(stored) if isinstance(stored, dict) else Facts()  # none: settles nothing
    history = _history(reader, stores, draft, now, officer)
    parent, thread = _context(history, draft, now)
    scores, refs, rewrite, drop = [], [], [], []
    server_reaction = _server_filler(draft.text) if draft.server else None
    # A reply of only filler is posted as it is for a share of drafts (_filler_posts): a "lol"
    # or "same" is no repeat, nothing new or off its parent, as its reaction would not be.
    # An opener of filler still drops (filler_opener).
    filler_post = bool(server_reaction and draft.parent_id is not None and _filler_posts(draft))
    if filler_post:
        server_reaction = None
        scores.append(Score('filler_post', 1.0))
    if draft.server:
        # The all-channel shelf is only for her own repeats. It never enters
        # context, cross-member comparisons, relevance or a rewrite hint.
        own_discord = [h for h in stores.history if h.guid == draft.guid and h.origin == 'mind'
            and h.channel in {'discord','dm','group'} and 0 <= now-h.ts <= cfg.repeat_seconds
            and (draft.line_id is None or h.id < draft.line_id)]
        day = [h for h in history if _same_room(h,draft) and h.origin == 'mind' and 0 <= now-h.ts <= 86400]
        for code, shelf in (('own_repeat_discord',own_discord), ('channel_repeat',day)):
            matches = [] if server_reaction or filler_post else _pairs(draft.text,draft.embedding,shelf,cfg,minimum=1)
            if matches:
                drop.append(code)
                scores.append(Score(code,round(matches[0][1],6)))
                refs.append(Reference('line',matches[0][0].id))
        if draft.parent_id is None and draft.anchor and draft.embedding is not None and draft.anchor_embedding is not None:
            cosine = similarity(checked_vector(draft.embedding),checked_vector(draft.anchor_embedding))
            scores.append(Score('anchor_relevance',round(cosine,6)))
            if cosine < cfg.anchor_cosine:
                rewrite.append('off_anchor')
    own = [line for line in history if line.guid == draft.guid and line.origin == 'mind'
           and now-line.ts <= cfg.repeat_seconds][-OWN_LINES:]
    repeats = [] if filler_post else _pairs(draft.text, draft.embedding, own, cfg)
    if repeats:
        previous, score = repeats[0]
        drop.append('own_repeat_same_channel' if _same_room(previous, draft) else 'own_repeat_cross_channel')
        scores.append(Score('repeat', round(score, 6)))
        refs.append(Reference('line', previous.id))
    # Her own previous message in this conversation, said again whole or as one piece of it
    # (owncopy.py): never when he asked her to say it again, never a short line.
    if not filler_post and not (parent and parent.guid != draft.guid
                                and owncopy.asked_again(parent.text)):
        here = [line for line in own if now - line.ts <= OWN_COPY_SECONDS
                and (_same_room(line, draft) or {line.channel, draft.channel} <= {'whisper', 'dm'})]
        if owncopy.copies_any(draft.text, [line.text for line in here]):
            drop.append('own_copy')
            refs.append(Reference('line', here[-1].id))
    shared = defaultdict(set)
    bucket = evening(now)
    if draft.channel in {'guild', 'discord', 'group'} and bucket is not None:
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
    # Her own persona (personas.facts), read with her role above.
    if slips(draft.text, facts.gender):
        rewrite.append(slip_code(facts.gender))
    misgendered = _misgendered(reader, stores, draft, parent)
    if misgendered:
        rewrite.append('member_misgender')
    hits = _claim_hits(reader, stores, draft, now, officer)
    wrong = persona_conflicts(draft.text, facts)
    if wrong or any(persona_conflicts(hit.claim_text, facts, claim=True) for hit in hits):
        # His own edits of her life (godmode implants and life events) win: read them only
        # when the persona is about to hold something against her.
        facts = facts_of_json(stored, owner=owner_texts(stores.conn, draft.guid))
        wrong = persona_conflicts(draft.text, facts)
    if wrong:
        rewrite.append('persona_conflict')
        refs.append(Reference('persona', draft.guid))
    # The persona outranks what she said in chat: a stored claim that contradicts it (an echo
    # of his words, an invented family) never holds a draft back.
    conflicts = [hit for hit in hits if not persona_conflicts(hit.claim_text, facts, claim=True)]
    if conflicts:
        rewrite.append('claim_conflict')
        refs.extend(Reference('claim', hit.claim_id) for hit in conflicts)
        scores.append(Score('claim', round(max(hit.score for hit in conflicts), 6)))
    if stores.facts is not None and draft.channel not in {'discord','dm','group'}:
        line = Line(0, now, draft.guid, draft.to_guid, None, draft.channel, 'mind', draft.map, draft.zone, draft.text)
        world = unawareness.check_line(stores.facts, line)
        rewrite.extend(flag.rule for flag in world)
        refs.extend(Reference('world', flag.related_id) for flag in world if type(flag.related_id) is int)
    filler = bool((draft.real_answer_due or parent and parent.text.rstrip().endswith('?'))
                  and nonsequitur.is_filler(words(draft.text)))
    if draft.server:
        filler = server_reaction is not None
        if filler and draft.parent_id is None:
            drop.append('filler_opener')
    if parent and not filler_post and cfg.semantic_enabled and len(words(draft.text)) >= nonsequitur.MIN_REPLY_WORDS \
            and draft.embedding is not None and parent.embedding is not None:
        parent_score, thread_score = nonsequitur.relevance_scores(draft.embedding,
            checked_vector(parent.embedding), [checked_vector(line.embedding) for line in thread if line.embedding is not None])
        scores.extend((Score('parent_relevance', round(parent_score,6)), Score('thread_relevance', round(thread_score,6))))
        if parent_score < cfg.parent_cosine and thread_score < cfg.thread_cosine:
            rewrite.append('low_parent_and_thread_relevance')
    echo_of = ''
    if parent and parent.guid != draft.guid and echoes(draft.text, parent.text):
        drop.append('echo_parent')
    elif parent and parent.guid != draft.guid and echo_opener(draft.text, parent.text):
        rewrite.append('echo_opener')  # his line said back as her opener (guards/opener.py)
        echo_of = parent.text
    if not filler and not filler_post and _pairs(draft.text, draft.embedding,
                                                 [line for line in thread if line.guid != draft.guid], cfg):
        drop.append('nothing_new')
    # A line already dropped as a repeat or echo stays dropped; phrases only add a rewrite.
    cooled = phrases.check(reader, draft, now) if cfg.phrase_cooldown and not drop else phrases.Found()
    rewrite.extend(cooled.codes)
    filler_code = 'server_filler' if draft.server else 'filler_after_question'
    codes = tuple(dict.fromkeys([*rewrite, *drop, *([filler_code] if filler else [])]))
    action = 'drop' if draft.server and drop else _action(codes)
    if reader.missing:
        scores.append(Score('missing_sources', len(reader.missing)))
    reaction = '👍' if parent and parent.text.rstrip().endswith('?') else '😄' if set(words(draft.text)) & {'lol','haha','hehe'} else '👍'
    return Verdict(action, codes, tuple(scores), tuple(dict.fromkeys(refs)),
                   (server_reaction or reaction) if action == 'react' else None, cooled.phrases,
                   cooled.pending, echo_of if 'echo_opener' in codes else '', misgendered)
