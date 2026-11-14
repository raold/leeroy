"""Read-only outreach counts. Topics and source references never enter the console."""
import sqlite3


KINDS = ('life', 'plan', 'followup', 'his_day', 'birthday', 'miss_you', 'dm_nudge')
STATES = ('open', 'told', 'dropped')


def _read(reader):
    try:
        return reader()
    except sqlite3.OperationalError as error:
        missing = str(error).startswith(('no such table:', 'no such column:'))
        return {'status': 'not_set_up' if missing else 'unavailable',
                'why': 'missing_source' if missing else 'read_failed'}


def member_view(conn, guid):
    """Stored states, including history; no migration or expiry writes on a view."""
    def read():
        kinds, totals = {}, dict.fromkeys(STATES, 0)
        for kind, state, count in conn.execute(
                "SELECT kind,state,count(*) FROM outreach_item WHERE guid=? "
                "AND kind IN ('life','plan','followup','his_day','birthday','miss_you','dm_nudge') "
                "AND state IN ('open','told','dropped') GROUP BY kind,state", (guid,)):
            kinds.setdefault(kind, dict.fromkeys(STATES, 0))[state] = count
            totals[state] += count
        return {'status': 'ok', 'kinds': kinds, 'totals': totals}
    return _read(read)


def tonight_view(conn, *, now):
    """Only open items ready now; an expired row awaiting cleanup is not ready."""
    def read():
        count = conn.execute("SELECT count(*) FROM outreach_item WHERE state='open' "
                             "AND not_before<=? AND expires>?", (now, now)).fetchone()[0]
        return {'status': 'ok', 'count': count}
    return _read(read)
