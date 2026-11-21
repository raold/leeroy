-- One proposal per guild, owner and local day. Its ID is also the sole native start ID.
CREATE TABLE member_heroic (
    id TEXT PRIMARY KEY, day TEXT NOT NULL, guild INTEGER NOT NULL, human INTEGER NOT NULL,
    created REAL NOT NULL, expires REAL NOT NULL, map INTEGER NOT NULL CHECK(map=602),
    roster TEXT NOT NULL, roles TEXT NOT NULL, bench TEXT NOT NULL, replaced TEXT NOT NULL DEFAULT '[]',
    conversation TEXT, generation INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL CHECK(phase IN ('offered','chat','agreed','sent','running','terminal','talk_only')),
    reason TEXT NOT NULL DEFAULT '', tries INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
    sent REAL, instance INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
    terminal TEXT, returned INTEGER NOT NULL DEFAULT 0, result_at REAL,
    kills INTEGER NOT NULL DEFAULT 0, wipes INTEGER NOT NULL DEFAULT 0,
    UNIQUE(guild,human,day)
);
CREATE TABLE member_heroic_post (
    start TEXT NOT NULL REFERENCES member_heroic(id), generation INTEGER NOT NULL,
    member INTEGER NOT NULL, message INTEGER PRIMARY KEY REFERENCES discord_message(id) ON DELETE CASCADE
);
CREATE INDEX member_heroic_consent ON member_heroic_post(start,generation);
CREATE TABLE member_heroic_encounter (
    start TEXT NOT NULL REFERENCES member_heroic(id), boss INTEGER NOT NULL, state TEXT NOT NULL,
    ts INTEGER NOT NULL, witnesses TEXT NOT NULL, PRIMARY KEY(start,boss,state,ts)
);
