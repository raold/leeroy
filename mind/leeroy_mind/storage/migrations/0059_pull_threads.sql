-- Romantic threads, phase 1 (drama/README.md "Threads (recording)"): what would pull two
-- members together, recorded and never applied. pull_event is the ledger: one row per recorded
-- fact of a night that would have moved the directed pull a -> b, with its kind, its weight
-- before and after the household and stance factors, the scope of its evidence and the
-- evidence's id (never text). pull_shadow is the would-be pull per directed pair with the
-- thread it would have opened; it is never relationship.attraction, and nothing reads it but
-- the report, the console panel and health. pull_night is one summary (counts only) per
-- recorded night. Only the night's `pulls` stage and the replay tool (on a scratch copy) write
-- these.
CREATE TABLE pull_event (
  id INTEGER PRIMARY KEY,
  night TEXT NOT NULL,
  a INTEGER NOT NULL,                  -- whose pull would move
  b INTEGER NOT NULL,                  -- toward whom
  kind TEXT NOT NULL,
  weight REAL NOT NULL CHECK (weight >= 0),   -- the kind's weight
  factor REAL NOT NULL CHECK (factor BETWEEN 0 AND 1),  -- household cost x stance damping
  amount REAL NOT NULL CHECK (amount >= 0),   -- what the shadow pull took, after the night cap
  scope TEXT NOT NULL CHECK (scope IN ('public','pair','restricted','private')),
  ref TEXT NOT NULL,                   -- raid:<id>, event:<id>, encounter:<id>, chat:<id>, ...
  ts REAL NOT NULL,
  CHECK (a != b),
  UNIQUE (night, a, b, kind, ref)
);
CREATE INDEX pull_event_night ON pull_event(night, kind);
CREATE INDEX pull_event_pair ON pull_event(a, b, night);
CREATE TABLE pull_shadow (
  a INTEGER NOT NULL,
  b INTEGER NOT NULL,
  pull REAL NOT NULL DEFAULT 0 CHECK (pull BETWEEN 0 AND 100),
  public_pull REAL NOT NULL DEFAULT 0 CHECK (public_pull BETWEEN 0 AND 100),  -- from public evidence only
  events INTEGER NOT NULL DEFAULT 0,
  first_night TEXT NOT NULL,
  last_event_night TEXT NOT NULL,
  updated_night TEXT NOT NULL,
  thread_state TEXT NOT NULL DEFAULT 'none'
               CHECK (thread_state IN ('none','waiting','open','faded')),
  thread_night TEXT,                   -- the night of the state in force
  thread_why TEXT NOT NULL DEFAULT '', -- why a waiting one was not admitted (a code)
  shape TEXT NOT NULL DEFAULT '',
  ceiling TEXT NOT NULL DEFAULT '',    -- 'acknowledged': her arc with the owner is at late_nights or later
  taken INTEGER NOT NULL DEFAULT 0 CHECK (taken IN (0, 1)),
  PRIMARY KEY (a, b),
  CHECK (a != b)
);
CREATE TABLE pull_night (
  night TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  summary TEXT NOT NULL CHECK (json_valid(summary) AND json_type(summary) = 'object')
);
