CREATE TABLE bond_moment (
  guid INTEGER NOT NULL, human INTEGER NOT NULL, kind TEXT NOT NULL,
  source TEXT NOT NULL, ts REAL NOT NULL,
  memory_id INTEGER NOT NULL UNIQUE REFERENCES memory(id) ON DELETE CASCADE,
  PRIMARY KEY(guid,human,kind,source),
  CHECK(kind IN ('first_kill','late_whisper','gift'))
);
CREATE TABLE bond_mood (
  guid INTEGER NOT NULL, human INTEGER NOT NULL, night TEXT NOT NULL,
  mood TEXT NOT NULL CHECK(mood IN ('missing','jealous','playful','quiet')),
  strength REAL NOT NULL CHECK(strength BETWEEN 0 AND 1),
  updated REAL NOT NULL, expires REAL NOT NULL,
  PRIMARY KEY(guid,human)
);
CREATE TABLE bond_invite (
  id TEXT PRIMARY KEY, guid INTEGER NOT NULL, human INTEGER NOT NULL,
  day TEXT NOT NULL, place TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','offered','accepted','moving','seated',
    'declined','expired','closed','unknown')),
  offered REAL, move_id TEXT UNIQUE, emote_id TEXT UNIQUE,
  UNIQUE(guid,human,day)
);
CREATE INDEX bond_invite_day ON bond_invite(day);
