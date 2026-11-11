CREATE TABLE persona_tier (
    guid INTEGER PRIMARY KEY,
    tier TEXT NOT NULL CHECK(tier IN ('cast','world'))
);
INSERT INTO persona_tier SELECT guid,'cast' FROM persona_core;
CREATE TABLE world_member (
    guid INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL CHECK(guild_id>0),
    guild_name TEXT NOT NULL,
    account INTEGER NOT NULL CHECK(account>0),
    name TEXT NOT NULL,
    race INTEGER NOT NULL,
    class INTEGER NOT NULL,
    gender INTEGER NOT NULL,
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 255),
    rank INTEGER NOT NULL,
    online INTEGER NOT NULL CHECK(online IN (0,1)),
    map INTEGER NOT NULL,
    zone INTEGER NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0,1)),
    observed REAL NOT NULL
);
CREATE INDEX world_member_guild ON world_member(guild_id,retired,guid);
