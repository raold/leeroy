"""Recheck source audiences for learned notes; absent provenance is not public proof."""
import json
from types import SimpleNamespace

from leeroy_mind.discord.channels import is_discord, is_private, is_officer_channel, is_local_channel, channel_id
from leeroy_mind.memory.privacy import derive_scope, restricted_channels, OFFICERS
from leeroy_mind.memory.recall import scope_ok, hidden_ok, whisper_ok, OWNER_REF
from leeroy_mind.storage.provenance import paired

PREFIX = 'learn:lesson:evidence:'
MAX_REFS = 512


def evidence(conn, line_ids, *, cache=None):
    """Include memories carried by generated source lines, not only their channel."""
    refs = set()
    for ident in line_ids:
        if cache is not None and ident in cache:
            if cache[ident] is None:
                return []
            refs.update(cache[ident])
            continue
        row = conn.execute('SELECT * FROM chat_line WHERE id=?', (ident,)).fetchone()
        if row is None:
            return []
        direct = {f'chat:{ident}'}
        if row['origin'] == 'mind':
            utterance = paired(conn, row)
            if utterance is None or utterance['dropped'] is not None:
                return []
            try:
                ids = json.loads(utterance['memory_ids'])
                if type(ids) is not list or any(type(i) is not int or i <= 0 for i in ids):
                    return []
                direct.update(f'memory:{i}' for i in ids)
            except (ValueError, TypeError):
                return []
        refs.update(direct)
        if cache is not None:
            cache[ident] = direct
    return sorted(refs) if len(refs) <= MAX_REFS else []


def saved(conn, ident):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (PREFIX+str(ident),)).fetchone()
    try:
        data = json.loads(row[0]) if row else None
        if (type(data) is dict and data.get('version') == 1 and type(data.get('refs')) is list
                and 0 < len(data['refs']) <= MAX_REFS and all(type(r) is str for r in data['refs'])):
            return data['refs']
    except (ValueError, TypeError):
        pass
    return []


def forward_floor(conn, ident):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (PREFIX+str(ident),)).fetchone()
    try:
        value = json.loads(row[0]).get('forward_min', 0) if row else 0
        return value if type(value) is int and 0 <= value <= 10 else 10
    except (ValueError, TypeError, AttributeError):
        return 10


def retain(conn, ident, refs, *, fresh=False, forward_min=0):
    old = saved(conn, ident)
    # An old note without its sources cannot acquire a public audience by a
    # later reinforcement. A newly written note starts a new provenance chain.
    if not old and (not fresh or not refs):
        if not conn.execute('SELECT 1 FROM meta WHERE key=?', (PREFIX+str(ident),)).fetchone():
            return
        refs = ()  # an existing failed-closed receipt can still raise its floor
    combined = sorted(set(old) | set(refs))
    if len(combined) > MAX_REFS:
        combined = []  # bounded and fail closed, never discard a restriction
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (PREFIX+str(ident), json.dumps(dict(version=1, refs=combined,
                    forward_min=max(forward_floor(conn, ident), forward_min)))))


class Visibility:
    """One snapshot/audience's memo, shared by its lessons and 300 typing lines.

    Each source is admitted through the canonical gates, so checking every edge
    computes the intersection without repeatedly walking the whole graph.
    """
    def __init__(self, conn, *, guid, channel, to_guid, readers=(), officer=False, human_guid=0):
        self.conn, self.guid, self.channel, self.to_guid = conn, guid, channel, to_guid
        # Game recordings contain cast only; the owner is never a missing cast reader.
        self.readers, self.officer = frozenset(readers) - {human_guid}, officer
        self.cache, self.visited = {}, set()

    def visible(self, refs):
        return bool(refs) and len(refs) <= MAX_REFS and all(self.walk(r) for r in refs)

    def walk(self, ref):
        # Explicit frames keep a deep but bounded graph off Python's call stack.
        pending, active, edges = [(ref, False)], set(), {}
        while pending:
            current, finish = pending.pop()
            if not isinstance(current, str) or len(current) > 100:
                return False
            if finish:
                self.cache[current] = all(self.cache.get(c, False) for c in edges[current])
                active.remove(current)
                continue
            if current in active:
                return False
            if current in self.cache:
                continue
            if len(self.visited) >= MAX_REFS:
                return False
            self.visited.add(current)
            children = self._node(current)
            if type(children) is bool:
                self.cache[current] = children
                continue
            active.add(current)
            edges[current] = children
            pending.append((current, True))
            pending.extend((child, False) for child in reversed(children))
        return self.cache.get(ref, False)

    def _node(self, ref):
        if ref.startswith(OWNER_REF):
            return bool(ref[len(OWNER_REF):])
        kind, sep, ident = ref.partition(':')
        if not sep or kind not in ('chat', 'memory', 'event') or not ident.isdecimal() or not 0 < int(ident) < 2**63:
            return False
        if kind == 'event':
            return True  # non-chat evidence, as Recall._shareable treats it
        row = self.conn.execute(f'SELECT * FROM {"chat_line" if kind == "chat" else "memory"} WHERE id=?',
                                (int(ident),)).fetchone()
        if row is None:
            return False
        if kind == 'memory':
            if (row['scope'] != 'open' and row['owner'] != self.guid or
                    not scope_ok(SimpleNamespace(**dict(row)), turn_channel=self.channel, to_guid=self.to_guid)):
                return False
            try:
                children = json.loads(row['evidence'])
                return children if (isinstance(children, list) and 0 < len(children) <= MAX_REFS
                        and all(isinstance(r, str) for r in children)) else False
            except (ValueError, TypeError):
                return False
        source = row['channel']
        if is_discord(source):
            matches = self.conn.execute("SELECT m.deleted FROM meta r JOIN discord_message m "
                "ON m.id=CAST(substr(r.key,14) AS INTEGER) WHERE r.key GLOB 'discord:line:*' "
                "AND r.value=? AND m.channel=?", (str(row['id']), channel_id(source))).fetchall()
            if not matches or any(r[0] is not None for r in matches):
                return False
        if is_private(source) and not whisper_ok(source, row['from_guid'], row['to_guid'],
                speaker=self.guid, turn_channel=self.channel, to_guid=self.to_guid):
            return False
        if is_officer_channel(source) and not self.officer:
            return False
        try:
            readers = json.loads(row['audience']) if row['audience'] is not None else None
            if readers is not None and (not isinstance(readers, list)
                    or any(type(g) is not int or g <= 0 for g in readers) or self.guid not in readers):
                return False
            if is_local_channel(source) and not is_officer_channel(source) and (
                    not readers or not self.readers or not self.readers.issubset(readers)):
                return False
        except (ValueError, TypeError):
            return False
        scope = derive_scope(self.conn, self.guid, (ref,))
        hidden = restricted_channels(self.conn, {0: (ref,)}).get(0, ())
        if (not scope_ok(SimpleNamespace(owner=self.guid, scope=scope.scope, scope_with=scope.scope_with),
                         turn_channel=self.channel, to_guid=self.to_guid)
                or scope.scope_with == OFFICERS and not self.officer or hidden and not hidden_ok(hidden, self.channel)):
            return False
        if row['origin'] == 'mind':
            utterance = paired(self.conn, row)
            if utterance is None or utterance['dropped'] is not None:
                return False
            try:
                mids = json.loads(utterance['memory_ids'])
                if not isinstance(mids, list) or any(type(i) is not int or i <= 0 for i in mids):
                    return False
                return [f'memory:{i}' for i in mids]
            except (ValueError, TypeError):
                return False
        return True


def visible(conn, refs, **context):
    return Visibility(conn, **context).visible(refs)
