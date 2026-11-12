"""Generation membership and pooled numeric observations; no content leaves SQLite."""
import sqlite3


def lines(conn, line_id):
    try:
        row = conn.execute("SELECT recipe_line_id FROM talk_recipe_line WHERE line_id=?", (line_id,)).fetchone()
        if row:
            return [r[0] for r in conn.execute("SELECT line_id FROM talk_recipe_line "
                                              "WHERE recipe_line_id=? ORDER BY line_id", (row[0],))]
    except sqlite3.OperationalError:
        pass  # inspection of a pre-generation database is still read-only
    return [line_id]


def observations(conn, ids, guid, now):
    rows = list(conn.execute("SELECT kind,value,source FROM talk_signal WHERE line_id IN (" +
                             ",".join("?" for _ in ids) + ") AND guid=? AND ts<=?", (*ids, guid, now)))
    raw = {}
    for kind, value, source in rows:
        key = kind, source
        # A veto on any fragment wins; repeated observations are one component.
        combine = min if kind in ("explicit", "reply_latency_s") else max
        raw[key] = combine(raw[key], value) if key in raw else value
    if ("replied", "implicit") in raw:
        raw.pop(("ignored", "implicit"), None)
    return raw, len(rows)
