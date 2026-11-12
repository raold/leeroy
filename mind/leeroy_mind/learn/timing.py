"""Reward wall costs, independent of generation-model call accounting."""
from statistics import median
from leeroy_mind.signals import stage as screening

DEFAULT_SECONDS = {"lexical": .02, "embedding": 2.}


def record_timing(conn, night, mode, seconds):
    conn.execute("INSERT INTO reward_timing VALUES (?,?,1,?) ON CONFLICT(night,mode) "
                 "DO UPDATE SET units=units+1,seconds=seconds+excluded.seconds",
                 (night, mode, max(0., seconds)))
    conn.execute("DELETE FROM reward_timing WHERE night NOT IN "
                 "(SELECT DISTINCT night FROM reward_timing ORDER BY night DESC LIMIT 7)")


def planning_seconds(ctx):
    path = ctx.settings.data_dir / "mind.toml"
    guards = screening.load_guards_config(path)
    try:
        claims = screening.load_claims_config(path).enabled
    except screening.ConfigError:
        claims = False
    mode = "embedding" if claims or guards.semantic_enabled else "lexical"
    rows = ctx.store.conn.execute("SELECT seconds/units FROM reward_timing WHERE mode=? "
                                  "ORDER BY night DESC LIMIT 7", (mode,)).fetchall()
    # One completed night is sufficient; later nights smooth noise. The two
    # paths never borrow each other's observations, including after a restart.
    return max(.001, median(r[0] for r in rows)) if rows else DEFAULT_SECONDS[mode]
