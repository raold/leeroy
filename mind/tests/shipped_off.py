"""The switches that default on since 2026-11-25 (owner: "if it's shipped it should be on by
default"), and a mind.toml with each of them off.

A test written while these features defaulted off, which exercises only its own feature, passes
its mind.toml through `off()`: every shipped switch it does not set is written false, so the rest
of the mind stays as quiet as the test expects (no voice service probed, no Discord server, no
night stages it did not ask for). tests/test_shipped_defaults.py checks the defaults themselves."""
import tomllib

from leeroy_mind.sandbox.prepare import dump_toml

# table -> its switches that default on since 2026-11-25: every switch the live mind.toml set true
# then, but [training] lab (on before) and [selfies] descriptive_prompts (not on main)
SHIPPED = {
    "bakeoff": ("capture",), "banter": ("enabled",), "callbacks": ("enabled",), "claims": ("enabled",),
    "control": ("ops_actions",),
    "culture": ("enabled", "bots", "calendar", "emoji", "loot", "memes"),
    "decisions": ("enabled", "bank_requests", "boss_strategy", "rank_reviews"),
    "drama": ("member_romance",), "fingerprint": ("enabled",), "godmode": ("enabled",),
    "groups": ("enabled",), "guards": ("enabled", "semantic_enabled"), "guild": ("loot_receipts",),
    "heat": ("enabled",), "lab": ("enabled",),
    "learn": ("enabled", "apply", "auto_lessons", "bandit", "capture_preferences", "coach", "cycle"),
    "life": ("day_plans", "dungeons", "state"), "looks": ("diversity",), "outreach": ("enabled",),
    "personas": ("auto_newcomers",),
    "photos": ("enabled", "request_priority", "requests", "scene_from_words", "send", "unsolicited"),
    "playstyle": ("enabled",), "prune": ("enabled",), "raidlead": ("enabled", "briefs"),
    "raidplay": ("respond",), "realm": ("enabled",), "romance": ("activities", "realism"),
    "selfies": ("enabled", "booth", "critic", "detailer", "detailer_hands", "identity_sets",
                "param_bandit", "romance_styling", "snaps", "varied_sets", "variety"),
    "server": ("enabled",), "signals": ("enabled",),
    "talk": ("aware", "coherence", "combat_quiet", "intent", "realism", "recall_diversity", "shape",
             "situation", "texture"),
    "training": ("enabled", "playbooks"), "turing": ("enabled",), "videos": ("enabled",),
    "voice": ("enabled", "autostart"), "voice_notes": ("enabled",), "watchdog": ("enabled",),
    "world": ("enabled", "realm", "selfies"),
}
# [drama] threads: a mode, "record" by default since 2026-11-25
MODES = {("drama", "threads"): "off"}


def off(text: str = "", *, keep=()) -> str:
    """`text` (a mind.toml) with every shipped switch it does not set written off, but those of the
    tables in `keep`. A table it sets to something other than a table is left as it is."""
    doc = tomllib.loads(text)
    for table, keys in SHIPPED.items():
        if table in keep:
            continue
        current = doc.setdefault(table, {})
        if isinstance(current, dict):
            for key in keys:
                current.setdefault(key, False)
    for (table, key), value in MODES.items():
        if table not in keep and isinstance(doc.get(table), dict):
            doc[table].setdefault(key, value)
    return dump_toml(doc)


def keep_pins(monkeypatch) -> None:
    """Put back, when the test ends, the process-wide pins a mind's start sets ([drama]
    member_romance, [life], the heat dial): a mind with the shipped features on would otherwise
    leave them on for the tests after it."""
    from leeroy_mind.drama import config as drama
    from leeroy_mind.heat import live as heat
    from leeroy_mind.life import config as life
    for module, names in ((drama, ("_PINNED",)), (life, ("_availability", "_state", "_dm_wait_cap")),
                          (heat, ("_LIVE",))):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))


def merge(path, tables: dict) -> None:
    """Set `tables` ({table: {key: value}}) in the mind.toml at `path`, key by key, keeping the
    rest of it (a test that turns one more feature on after its harness wrote the file)."""
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    for table, keys in tables.items():
        doc[table] = {**doc.get(table, {}), **keys}
    path.write_text(dump_toml(doc), encoding="utf-8")
