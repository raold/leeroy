"""Recheck source audiences for learned notes; absent provenance is not public proof."""
import json
from types import SimpleNamespace

from leeroy_mind.discord.channels import is_discord, is_private, is_officer_channel, is_local_channel, channel_id
from leeroy_mind.groups.audience import GroupSource
from leeroy_mind.memory.privacy import (derive_scope, restricted_channels, stricter, OFFICERS, GROUP, SCOPES,
    _derive as derive_rows, _lines as privacy_lines, _secret_threads as secret_threads,
    _Memory as privacy_memory)
from leeroy_mind.memory.recall import scope_ok, hidden_ok, whisper_ok, OWNER_REF
from leeroy_mind.storage.provenance import paired

PREFIX = 'learn:lesson:evidence:'
MAX_REFS = 512
SCOPE_NODES = 4096  # memories one turn's effective-scope reads may touch; past it fails closed


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

    A note's own sources (its lines, and the memories her generated lines carried) are
    admitted one by one through the strict gates. A memory is admitted by the gate that lets
    it into a prompt (Recall._shareable), and its evidence is opened one level: the lines it
    cites must be the turn's to hear, and what a generated line among them carried must be
    recallable here too. Deeper than that the stored scopes stand, as they do for recall:
    the whole graph under one line reaches most of the guild's history (live, 2026-11-17:
    100-1,200 nodes, 12 levels), so walking all of it refused every note on every turn.
    """
    def __init__(self, conn, *, guid, channel, to_guid, readers=(), officer=False, human_guid=0):
        self.conn, self.guid, self.channel, self.to_guid = conn, guid, channel, to_guid
        # Game recordings contain cast only; the owner is never a missing cast reader.
        self.readers, self.officer = frozenset(readers) - {human_guid}, officer
        self.all_readers = tuple(readers)
        self.cache, self.visited, self.scopes = {}, set(), {}

    def visible(self, refs):
        return bool(refs) and len(refs) <= MAX_REFS and all(self.walk(r) for r in refs)

    def walk(self, ref):
        # Explicit frames keep the walk off Python's call stack. A node is (ref, under):
        # under 0 is a source of the note itself, 1 sits below one memory, 2 is a memory
        # below that (gated, never opened).
        root = (ref, 0)
        pending, active, edges = [(root, False)], set(), {}
        while pending:
            current, finish = pending.pop()
            if not isinstance(current[0], str) or len(current[0]) > 100:
                return False
            if finish:
                self.cache[current] = all(self.cache.get(c, False) for c in edges[current])
                active.remove(current)
                continue
            if any(a[0] == current[0] for a in active):
                return False
            if current in self.cache:
                continue
            if len(self.visited) >= MAX_REFS:
                return False
            self.visited.add(current)
            children = self._node(*current)
            if type(children) is bool:
                self.cache[current] = children
                continue
            active.add(current)
            edges[current] = children
            pending.append((current, True))
            pending.extend((child, False) for child in reversed(children))
        return self.cache.get(root, False)

    def _effective(self, ident):
        """A memory's scope as its evidence makes it: the stricter of what is stored and what
        derives from its evidence, with every memory it cites, at any depth, read the same way.
        Stored scopes are not always as strict as their evidence (live, 2026-11-17: 281 open
        memories cite a non-open one, 434 reach a private line through memory refs), so a gate
        that stops at a memory must not take its stored scope alone. Only memory refs are
        followed, never the lines' own recalled memories. None: unknown, cyclic or past the
        node budget, which no caller may read as open.

        The stored check is the one source of it (memory.privacy.resolve; the scope_evidence
        migration's derived_scope, derived_with, derived_at): a memory that carries one is
        read from it and not opened. Only a memory without one (unchecked since something it
        rests on changed, or a database from before that migration) is walked here."""
        done = self.scopes
        if ident in done:
            return done[ident]
        rows, order, todo, seen = {}, [], [ident], set()
        while todo:
            current = todo.pop()
            if current in seen or current in done:
                continue
            seen.add(current)
            if len(seen) + len(done) > SCOPE_NODES:
                done[ident] = None
                return None
            row = self.conn.execute('SELECT * FROM memory WHERE id=?', (current,)).fetchone()
            if (row is not None and 'derived_at' in row.keys() and row['derived_at'] is not None
                    and row['scope'] in SCOPES and row['derived_scope'] in SCOPES):
                # derived_complete 0: its evidence, at some depth, is malformed or cites a
                # memory with no row. Unknown is never open here, checked or not
                whole = 'derived_complete' not in row.keys() or row['derived_complete'] != 0
                done[current] = stricter((row['scope'], row['scope_with']),
                                         (row['derived_scope'], row['derived_with'] or 0)) if whole else None
                continue
            try:
                refs = json.loads(row['evidence']) if row is not None else None
            except (ValueError, TypeError):
                refs = None
            if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
                rows[current] = None
                continue
            rows[current] = (row, refs)
            order.append(current)
            todo.extend(int(r[7:]) for r in refs if r.startswith('memory:') and r[7:].isdecimal())
        # Scopes only ever tighten, so passes over the found rows reach a fixed point; a
        # cycle settles at the strictest scope anything on it derives.
        current = {i: (None if rows[i] is None else (rows[i][0]['scope'], rows[i][0]['scope_with']))
                   for i in rows}
        chats = {i: [int(r[5:]) for r in rows[i][1] if r.startswith('chat:') and r[5:].isdecimal()]
                 for i in order}
        lines = privacy_lines(self.conn, {c for ids in chats.values() for c in ids})
        secret = secret_threads(self.conn, lines)
        for _ in range(len(order) + 1):
            changed = False
            # leaves first: a walk finds a memory before what it cites, and a scoped leaf under
            # a deep chain then settles in one pass instead of one pass a level
            for i in reversed(order):
                row, refs = rows[i]
                cited, broken = {}, False
                for r in refs:
                    if r.startswith('memory:') and r[7:].isdecimal():
                        other = current.get(int(r[7:]), done.get(int(r[7:])))
                        if other is None:
                            broken = True
                            break
                        child = rows.get(int(r[7:]))
                        owner = child[0]['owner'] if child else self.conn.execute(
                            'SELECT owner FROM memory WHERE id=?', (int(r[7:]),)).fetchone()[0]
                        cited[int(r[7:])] = privacy_memory(owner, other[0], other[1])
                if broken:
                    new = None
                else:
                    derived = derive_rows(row['owner'], refs, [], lines, secret, cited)
                    new = None if current[i] is None else stricter(current[i], (derived.scope, derived.scope_with))
                if new != current[i]:
                    current[i], changed = new, True
            if not changed:
                break
        done.update(current)
        return done.get(ident)

    def _recallable(self, row):
        """Recall._shareable for one memory row on this turn, over plain SELECTs, on its
        effective scope (_effective), never the stored one alone. Its evidence list when it
        may come up here, else False; malformed or missing evidence fails closed."""
        scope = self._effective(row['id'])
        if scope is None:
            return False
        if scope[0] != 'open' and row['owner'] != self.guid:
            return False
        if not scope_ok(SimpleNamespace(**(dict(row) | dict(scope=scope[0], scope_with=scope[1]))),
                        turn_channel=self.channel, to_guid=self.to_guid):
            return False
        if (row['key'] or '').startswith('bond:') and row['owner'] != self.guid:
            return False
        if scope[1] == OFFICERS and not self.officer:
            return False
        try:
            refs = json.loads(row['evidence'])
        except (ValueError, TypeError):
            return False
        if not (isinstance(refs, list) and len(refs) <= MAX_REFS and all(isinstance(r, str) for r in refs)):
            return False
        if not refs and scope[0] == 'open':
            return False  # absent provenance is no public proof; a scoped one stays with its pair
        hidden = restricted_channels(self.conn, {0: refs}).get(0, ())
        if scope[1] == GROUP and not any(isinstance(c, GroupSource) for c in hidden):
            return False
        if hidden and not hidden_ok(hidden, self.channel, speaker=self.guid, to_guid=self.to_guid,
                                    readers=self.all_readers):
            return False
        if not self.officer and any(not isinstance(c, GroupSource) and is_officer_channel(c) for c in hidden):
            return False
        ids = sorted({int(r[5:]) for r in refs if r.startswith('chat:') and r[5:].isdecimal()})
        cited = self.conn.execute('SELECT channel,from_guid,to_guid FROM chat_line WHERE id IN '
            '(SELECT value FROM json_each(?))', (json.dumps(ids),)).fetchall() if ids else []
        if len(cited) != len(ids):
            return False  # a cited line that is gone proves no audience
        if (not any(r.startswith(OWNER_REF) for r in refs) and cited
                and all(is_private(c[0]) for c in cited) and not all(
                    whisper_ok(c[0], c[1], c[2], speaker=self.guid, turn_channel=self.channel,
                               to_guid=self.to_guid) for c in cited)):
            return False
        return refs

    def _node(self, ref, under=0):
        if ref.startswith(OWNER_REF):
            return bool(ref[len(OWNER_REF):])
        kind, sep, ident = ref.partition(':')
        if not sep or kind not in ('chat', 'memory', 'event') or not ident.isdecimal() or not 0 < int(ident) < 2**63:
            # Below a memory the night's own refs (joke:, beat:, encounter:, arc:, rule:fact:)
            # are non-chat evidence that claims nobody, as privacy.derive_scope reads them.
            return bool(under and sep and kind and ident)
        if kind == 'event':
            return True  # non-chat evidence, as Recall._shareable treats it
        row = self.conn.execute(f'SELECT * FROM {"chat_line" if kind == "chat" else "memory"} WHERE id=?',
                                (int(ident),)).fetchone()
        if row is None:
            return False
        if kind == 'memory':
            children = self._recallable(row)
            if children is False or under or not children:
                return children is not False
            return [(r, 1) for r in children]
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
                    or any(type(g) is not int or g <= 0 for g in readers)):
                return False
            # Who read a line matters for the note's own sources. A line under a memory was
            # somebody else's to hear (she was told); the memory's scope carries that.
            if not under and readers is not None and self.guid not in readers:
                return False
            if not under and is_local_channel(source) and not is_officer_channel(source) and (
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
                return [(f'memory:{i}', 2 if under else 0) for i in mids]
            except (ValueError, TypeError):
                return False
        return True


def visible(conn, refs, **context):
    return Visibility(conn, **context).visible(refs)
