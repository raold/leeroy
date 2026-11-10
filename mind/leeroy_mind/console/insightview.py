"""Read-only Personas insight. Each source can fail without hiding the others."""
import sqlite3
import time

from leeroy_mind.awareness import CardContext, card
from . import insightreports, personaview

LIMIT = 100
BUDGETS = {'small': 200, 'medium': 600, 'large': 1200}


def _part(conn, reader):
    deadline = time.monotonic() + .3
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        return reader()
    except sqlite3.OperationalError as exc:
        old = str(exc).startswith(('no such table:', 'no such column:', 'no such module:'))
        return {'state': 'missing' if old else 'error',
                'why': 'Not set up yet in this mind.db.' if old else 'This part could not be read within its budget.'}
    except (ValueError, TypeError, KeyError):
        return {'state': 'error', 'why': 'This part contains data this console cannot read.'}
    finally:
        conn.set_progress_handler(None, 0)


def claims(conn, guid, now):
    fields = 'id,kind,subject,polarity,seen,last_seen,expires,superseded_by'
    rows = lambda where: [dict(row) for row in conn.execute(
        f'SELECT {fields} FROM member_claim WHERE guid=? AND {where} '
        'ORDER BY last_seen DESC,id DESC LIMIT ?', (guid, now, LIMIT + 1))]
    live = rows('superseded_by IS NULL AND (expires IS NULL OR expires>?)')
    history = rows('superseded_by IS NOT NULL AND last_seen<=?')
    return {'state': 'ok', 'live': live[:LIMIT], 'history': history[:LIMIT],
            'truncated': len(live) > LIMIT or len(history) > LIMIT,
            'why': 'Newest 100 rows in each list.' if len(live) > LIMIT or len(history) > LIMIT else None}


def awareness(conn, guid, now):
    value = card(conn, guid, context=CardContext(channel='guild', now=now))
    variants = {}
    for name, budget in BUDGETS.items():
        rendered = value.render(budget)
        variants[name] = {'tokens': budget, 'text': rendered,
                          'dropped': [p.title for p in value.parts if p.render() not in rendered]}
    return {'state': 'missing' if 'cast_member' in value.missing else 'ok', 'variants': variants,
            'missing': list(value.missing),
            'why': 'Guild context, using stored observations. Live game state is unavailable here.'}


def conflict_badges(conn, now):
    # Claims has no adjudicated-conflict journal. Count potential unresolved subjects,
    # never expired/superseded rows; label the heuristic explicitly in the UI.
    rows = conn.execute('SELECT guid,count(*) FROM (SELECT guid,kind,subject FROM member_claim '
        'WHERE superseded_by IS NULL AND (expires IS NULL OR expires>?) '
        'GROUP BY guid,kind,subject HAVING min(polarity)<0 AND max(polarity)>0) '
        'GROUP BY guid LIMIT 10000', (now,))
    return {str(guid): count for guid, count in rows}


def badges(conn, runs, now):
    conflicts = _part(conn, lambda: conflict_badges(conn, now))
    why = None
    if 'state' in conflicts:
        why, conflicts = conflicts['why'], {}
    repetitions = insightreports.repetition_badges(runs, now=now)
    return {g: {'conflicts': conflicts.get(g, 0), 'repetition': repetitions.get(g, 0)}
            for g in conflicts.keys() | repetitions.keys()}, why


def roster_view(conn, *, data_dir, now):
    view = personaview.roster_view(conn, now=now)
    runs, why = insightreports.reports(data_dir, now=now)
    view['insight_badges'], view['insight_badges_why'] = badges(conn, runs, now)
    if why and not view['insight_badges_why']:
        view['insight_badges_why'] = why
    return view


def insight_view(conn, guid, *, data_dir, now):
    runs, why = insightreports.reports(data_dir, now=now)
    return {'guid': guid, 'at': now,
            'claims': _part(conn, lambda: claims(conn, guid, now)),
            'awareness': _part(conn, lambda: awareness(conn, guid, now)),
            'quality': insightreports.quality(runs, guid, now=now, why=why),
            'guards': {'state': 'missing', 'why': 'Guards are installed, but verdict history is not recorded yet. '
                       'Send, rewrite, react and drop counts are unavailable.'}}
