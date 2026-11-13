"""Recheck source audiences for learned notes; absent provenance is not public proof."""
import json
from types import SimpleNamespace

from leeroy_mind.discord.channels import is_discord, is_private, is_officer_channel, channel_id
from leeroy_mind.memory.privacy import derive_scope, restricted_channels, OFFICERS
from leeroy_mind.memory.recall import scope_ok, hidden_ok
from leeroy_mind.storage.provenance import paired

PREFIX = 'learn:lesson:evidence:'
MAX_REFS = 128


def evidence(conn, line_ids):
    """Include memories carried by generated source lines, not only their channel."""
    refs = set()
    for ident in line_ids:
        row = conn.execute('SELECT * FROM chat_line WHERE id=?', (ident,)).fetchone()
        if row is None:
            return []
        refs.add(f'chat:{ident}')
        if row['origin'] == 'mind':
            utterance = paired(conn, row)
            if utterance is None or utterance['dropped'] is not None:
                return []
            try:
                ids = json.loads(utterance['memory_ids'])
                if type(ids) is not list or any(type(i) is not int or i <= 0 for i in ids):
                    return []
                refs.update(f'memory:{i}' for i in ids)
            except (ValueError, TypeError):
                return []
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
    if not refs or not fresh and not old:
        return
    combined = sorted(set(old) | set(refs))
    if len(combined) > MAX_REFS:
        combined = []  # bounded and fail closed, never discard a restriction
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (PREFIX+str(ident), json.dumps(dict(version=1, refs=combined,
                    forward_min=max(forward_floor(conn, ident), forward_min)))))


def visible(conn, refs, *, guid, channel, to_guid, readers=(), officer=False):
    """Complete bounded provenance plus the canonical memory audience gates.

    Historical groups additionally require every current reader in the old
    audience. Public knowledge may cross media; a public chat window may not.
    """
    if not refs or len(refs) > MAX_REFS:
        return False
    visited = set()
    def walk(ref, depth=0, stack=frozenset()):
        if len(ref) > 100:
            return False
        if ref in stack:
            return False
        if depth > 8 or len(visited) >= MAX_REFS:
            return False
        if ref in visited:
            return True
        visited.add(ref)
        kind, sep, ident = ref.partition(':')
        if not sep or kind not in ('chat', 'memory') or not ident.isdecimal() or not 0 < int(ident) < 2**63:
            return False
        row = conn.execute(f'SELECT * FROM {"chat_line" if kind == "chat" else "memory"} WHERE id=?',
                           (int(ident),)).fetchone()
        if row is None:
            return False
        if kind == 'memory':
            if row['scope'] != 'open' and row['owner'] != guid:
                return False
            if not scope_ok(SimpleNamespace(**dict(row)), turn_channel=channel, to_guid=to_guid):
                return False
            try:
                children = json.loads(row['evidence'])
                # Owner-authored canon has no restricted source, but unknown roots
                # are not evidence for an automatically learned note.
                return bool(children) and all(type(r) is str and walk(r, depth+1, stack | {ref}) for r in children)
            except (ValueError, TypeError):
                return False
        source = row['channel']
        if is_discord(source):
            # Deletion revokes retained text even if chat_line outlives its post.
            matches = conn.execute("SELECT m.deleted FROM meta r JOIN discord_message m "
                "ON m.id=CAST(substr(r.key,14) AS INTEGER) WHERE r.key GLOB 'discord:line:*' "
                "AND r.value=? AND m.channel=?", (str(row['id']), channel_id(source))).fetchall()
            if not matches or any(r[0] is not None for r in matches):
                return False
        if is_officer_channel(source) and not officer:
            return False
        if row['audience'] is not None:
            try:
                audience = json.loads(row['audience'])
                if guid not in audience:
                    return False
                if not is_private(source) and not is_officer_channel(source) and source in (
                        'party', 'raid', 'raid_warning') and (not readers or not set(readers).issubset(audience)):
                    return False
            except (ValueError, TypeError):
                return False
        elif source in ('party', 'raid', 'raid_warning'):
            return False
        return True
    if not all(type(r) is str and walk(r) for r in refs):
        return False
    # Re-derive every visited source: a mistakenly open nested memory cannot
    # launder its private evidence into a public style note.
    scope = derive_scope(conn, guid, visited)
    hidden = restricted_channels(conn, {0: visited}).get(0, ())
    return (scope_ok(SimpleNamespace(owner=guid, scope=scope.scope, scope_with=scope.scope_with),
                     turn_channel=channel, to_guid=to_guid)
            and (scope.scope_with != OFFICERS or officer)
            and (not hidden or hidden_ok(hidden, channel)))
