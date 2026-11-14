"""The officers' polls. A poll holds its options (codes), its voters (fixed when it opens, and him:
he counts as one voter in every poll) and the positions its member votes will follow. A vote is
final: a second vote by the same member is refused and the first never changes. A tie or no votes
closes with no result, and the status quo stands."""
from collections.abc import Iterable, Sequence
from collections import Counter
import json
import logging

from leeroy_mind.discord import layout
from .ledger import CODE, KINDS, SUBJECTS, DecisionError, _json, _time, positions_json

log = logging.getLogger(__name__)
MAX_OPTIONS = 6


def _clean(text) -> str:
    if not isinstance(text, str):
        return ''
    return ' '.join(''.join(c for c in text if c.isprintable() or c.isspace()).split())


def _human(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    value = row[0] if row else ''
    return int(value) if isinstance(value, str) and value.isdecimal() and 0 < int(value) <= 0xFFFFFFFF else 0


def get_poll(conn, poll: int):
    return conn.execute('SELECT * FROM guild_poll WHERE id=?', (poll,)).fetchone()


def voters_of(row) -> frozenset[int]:
    try:
        value = json.loads(row['voters'])
    except (TypeError, ValueError):
        return frozenset()
    return frozenset(v for v in value if type(v) is int) if isinstance(value, list) else frozenset()


def options_of(row) -> tuple[str, ...]:
    try:
        value = json.loads(row['options'])
    except (TypeError, ValueError):
        return ()
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def positions_of(row) -> list[dict]:
    try:
        return positions_json(json.loads(row['positions']))
    except (TypeError, ValueError):
        return []


def open_poll(store, *, channel, question, options: Sequence[str], voters: Iterable[int], closes, now,
              kind: str | None = None, subject: str | None = None, positions=()) -> int:
    """A new open poll in a guild channel. `positions` (one per voting member, each side one of the
    options) are what the members' votes will follow; he joins the voters from meta human_guid."""
    if layout.channel(channel) is None:
        raise DecisionError('poll_channel_invalid')
    words = _clean(question)
    if not 1 <= len(words) <= 200:
        raise DecisionError('poll_question_invalid')
    options = tuple(options)
    if (not 2 <= len(options) <= MAX_OPTIONS or len(set(options)) != len(options)
            or not all(isinstance(o, str) and CODE.fullmatch(o) for o in options)):
        raise DecisionError('poll_options_invalid')
    if (kind is None) != (subject is None) or kind is not None and (
            kind not in KINDS or not isinstance(subject, str) or not SUBJECTS[kind].match(subject)):
        raise DecisionError('poll_subject_invalid')
    opened, ending = _time(now), _time(closes)
    if ending <= opened:
        raise DecisionError('poll_closes_invalid')
    eligible = set()
    for guid in voters:
        if type(guid) is not int or not 0 < guid <= 0xFFFFFFFF:
            raise DecisionError('poll_voters_invalid')
        eligible.add(guid)
    stored = positions_json(positions)
    if any(p['side'] not in options or p['guid'] not in eligible for p in stored):
        raise DecisionError('poll_positions_invalid')
    with store.transaction():
        human = _human(store.conn)
        if human:
            eligible.add(human)
        if not eligible:
            raise DecisionError('poll_voters_invalid')
        ident = store.conn.execute(
            'INSERT INTO guild_poll(channel,question,options,voters,opened,closes,status,kind,subject,positions) '
            "VALUES (?,?,?,?,?,?,'open',?,?,?)",
            (channel, words, _json(list(options)), _json(sorted(eligible)), opened, ending, kind, subject,
             _json(stored))).lastrowid
    log.info('poll_opened id=%d kind=%s channel=%s options=%d voters=%d', ident, kind or '-', channel,
             len(options), len(eligible))
    return ident


def vote(store, poll, member, option, *, why='', now) -> bool:
    """True when this member's vote is recorded; False when the poll is not open, it has closed, the
    member is no voter, the option is not the poll's, or the member already voted (never changed)."""
    if type(poll) is not int or type(member) is not int or not isinstance(option, str):
        return False
    if not isinstance(why, str) or why and not CODE.fullmatch(why):
        raise DecisionError('poll_why_invalid')
    ts = _time(now)
    with store.transaction():
        row = get_poll(store.conn, poll)
        if (row is None or row['status'] != 'open' or ts >= row['closes'] or member not in voters_of(row)
                or option not in options_of(row)):
            return False
        added = store.conn.execute('INSERT OR IGNORE INTO guild_poll_vote(poll,member,option,why,ts) '
                                   'VALUES (?,?,?,?,?)', (poll, member, option, why, ts)).rowcount == 1
    if added:
        log.info('poll_vote poll=%d member=%d', poll, member)
    return added


def tally(conn, poll: int) -> dict[str, int]:
    row = get_poll(conn, poll)
    counts = dict.fromkeys(options_of(row) if row is not None else (), 0)
    for option, n in conn.execute('SELECT option,count(*) FROM guild_poll_vote WHERE poll=? GROUP BY option', (poll,)):
        if option in counts:
            counts[option] = n
    return counts


def winner(counts: dict[str, int]) -> str | None:
    """The one option with the most votes; None on a tie or with no votes."""
    ranked = Counter({k: v for k, v in counts.items() if v > 0}).most_common(2)
    if not ranked or len(ranked) == 2 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def close_due(store, *, now) -> list[tuple[int, str | None]]:
    """Close every open poll whose closing time has come: (poll, the winning option or None)."""
    ts = _time(now)
    out = []
    with store.transaction():
        for row in store.conn.execute("SELECT id FROM guild_poll WHERE status='open' AND closes<=? ORDER BY id",
                                      (ts,)).fetchall():
            counts = tally(store.conn, row['id'])
            result = winner(counts)
            store.conn.execute("UPDATE guild_poll SET status='closed', result=? WHERE id=? AND status='open'",
                               (result, row['id']))
            out.append((row['id'], result))
    for poll, result in out:
        log.info('poll_closed id=%d result=%s votes=%d', poll, result or '-', sum(tally(store.conn, poll).values()))
    return out


def close_early(store, poll: int, *, now) -> tuple[int, str | None] | None:
    """Close one open poll before its closing time (every voter a kind waits for has voted): (poll,
    the winning option or None), or None when it was not open."""
    ts = _time(now)
    with store.transaction():
        row = get_poll(store.conn, poll)
        if row is None or row['status'] != 'open':
            return None
        result = winner(tally(store.conn, poll))
        store.conn.execute("UPDATE guild_poll SET status='closed', result=? WHERE id=? AND status='open'",
                           (result, poll))
    log.info('poll_closed id=%d result=%s votes=%d early=1', poll, result or '-', sum(tally(store.conn, poll).values()))
    return poll, result


def open_polls(conn) -> list:
    return conn.execute("SELECT * FROM guild_poll WHERE status='open' ORDER BY id").fetchall()



def attach_message(store, poll: int, message: int) -> bool:
    """The Discord post that carries the poll (its card in Comms); set once."""
    if type(poll) is not int or type(message) is not int or message < 1:
        raise DecisionError('poll_message_invalid')
    with store.transaction():
        return store.conn.execute('UPDATE guild_poll SET message=? WHERE id=? AND message IS NULL',
                                  (message, poll)).rowcount == 1


def unhandled(conn) -> list:
    """Closed polls of a kind whose result that kind has not taken yet."""
    return conn.execute("SELECT * FROM guild_poll WHERE status='closed' AND handled IS NULL AND kind IS NOT NULL "
                        'ORDER BY id').fetchall()


def mark_handled(store, poll: int, *, now) -> bool:
    """The receipt that a closed poll's result reached its kind, once. A kind may write it in the
    transaction that records its decision; the service writes it after a hook returns."""
    ts = _time(now)
    with store.transaction():
        return store.conn.execute("UPDATE guild_poll SET handled=? WHERE id=? AND status<>'open' AND handled IS NULL",
                                  (ts, poll)).rowcount == 1


def hook_failed(store, poll: int) -> int:
    """One more failed attempt to hand this poll's result to its kind; the attempts so far."""
    with store.transaction():
        store.conn.execute('UPDATE guild_poll SET hook_tries=hook_tries+1 WHERE id=?', (poll,))
        row = store.conn.execute('SELECT hook_tries FROM guild_poll WHERE id=?', (poll,)).fetchone()
    return row[0] if row else 0
