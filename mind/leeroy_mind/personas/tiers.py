"""Cast is the default on current and older databases. World access is explicit."""
def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_tier'").fetchone() is not None


def tier(conn, guid):
    if not available(conn):
        return "cast"
    row = conn.execute("SELECT tier FROM persona_tier WHERE guid=?",(guid,)).fetchone()
    return row[0] if row else "cast"


def cast_sql(conn, column="guid"):
    """Only callers' fixed column identifiers are accepted, never request text."""
    if column not in {"guid","p.guid","c.guid","a.guid","persona_core.guid"}:
        raise ValueError("tier_column")
    return f"{column} NOT IN (SELECT guid FROM persona_tier WHERE tier='world')" if available(conn) else "1"


def is_cast(conn, guid):
    return tier(conn,guid) == "cast"
