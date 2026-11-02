CREATE TABLE guild_raid (
  raid_id INTEGER PRIMARY KEY, started REAL NOT NULL, ended REAL,
  raid_key TEXT NOT NULL, size INTEGER NOT NULL, heroic INTEGER NOT NULL, offline INTEGER NOT NULL,
  lead_guid INTEGER NOT NULL, tank_guid INTEGER NOT NULL, loot_guid INTEGER NOT NULL,
  kills INTEGER NOT NULL DEFAULT 0, wipes INTEGER NOT NULL DEFAULT 0, outcome TEXT
);
CREATE TABLE guild_attendance (
  raid_id INTEGER NOT NULL REFERENCES guild_raid(raid_id), guid INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('raided','benched')), PRIMARY KEY (raid_id, guid)
);
CREATE TABLE dkp_ledger (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, guid INTEGER NOT NULL, delta REAL NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('kill','attendance','standby','item','decay','adjust')),
  raid_id INTEGER, item_id INTEGER, note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX dkp_ledger_guid ON dkp_ledger (guid);
CREATE TABLE guild_flag (
  kind TEXT NOT NULL CHECK (kind IN ('geared','friend','spec','tc_geared')), guid INTEGER NOT NULL,
  ts REAL NOT NULL, value REAL, PRIMARY KEY (kind, guid)
);
