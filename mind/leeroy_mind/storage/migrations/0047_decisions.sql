-- The guild's decisions (D4): one ledger of what the guild settled, and the officers' polls.
-- Only the running mind writes these; the console reads them.
CREATE TABLE guild_poll (
  id INTEGER PRIMARY KEY CHECK(id >= 1),
  channel TEXT NOT NULL CHECK (length(channel) BETWEEN 1 AND 40),   -- a layout channel id
  message INTEGER REFERENCES discord_message(id),                   -- the post that carries it
  question TEXT NOT NULL CHECK (length(question) BETWEEN 1 AND 200),
  options TEXT NOT NULL CHECK (json_valid(options) AND json_array_length(options) BETWEEN 2 AND 6),
  voters TEXT NOT NULL CHECK (json_valid(voters)),                  -- eligible guids at open, and him
  opened REAL NOT NULL, closes REAL NOT NULL CHECK (closes > opened),
  status TEXT NOT NULL CHECK (status IN ('open','closed','cancelled')),
  result TEXT,                                                      -- the winning option; NULL on a tie
  kind TEXT CHECK (kind IS NULL OR kind IN ('lineup','loot_rule','bank','strategy','rank')),
  subject TEXT CHECK (subject IS NULL OR length(subject) BETWEEN 1 AND 80),
  positions TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(positions)), -- [{"guid","side","why"}] fixed at open
  handled REAL,                                                     -- its kind took the closed result (once)
  hook_tries INTEGER NOT NULL DEFAULT 0 CHECK (hook_tries >= 0),
  CHECK (handled IS NULL OR status <> 'open')
);
CREATE INDEX guild_poll_open ON guild_poll(closes) WHERE status = 'open';
CREATE INDEX guild_poll_unhandled ON guild_poll(id) WHERE status = 'closed' AND handled IS NULL;
CREATE TABLE guild_poll_vote (
  poll INTEGER NOT NULL REFERENCES guild_poll(id), member INTEGER NOT NULL CHECK (member >= 1),
  option TEXT NOT NULL, why TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,
  PRIMARY KEY (poll, member)
);
-- A vote is final: never changed, never taken back
CREATE TRIGGER guild_poll_vote_final_update BEFORE UPDATE ON guild_poll_vote
BEGIN SELECT RAISE(ABORT, 'a vote is final'); END;
CREATE TRIGGER guild_poll_vote_final_delete BEFORE DELETE ON guild_poll_vote
BEGIN SELECT RAISE(ABORT, 'a vote is final'); END;
CREATE TABLE guild_decision (
  id INTEGER PRIMARY KEY CHECK(id >= 1),
  kind TEXT NOT NULL CHECK (kind IN ('lineup','loot_rule','bank','strategy','rank')),
  subject TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 80),   -- raid:<cal id>, loot, bank:<req>, strategy:<map:boss:diff>, rank:<guid>
  value TEXT NOT NULL CHECK (json_valid(value) AND length(value) <= 4000),
  settled_by INTEGER NOT NULL DEFAULT 0,                            -- the settling member's guid; 0 when a poll settled it
  poll INTEGER REFERENCES guild_poll(id),
  message INTEGER REFERENCES discord_message(id),                   -- the post that announced it
  positions TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(positions)), -- [{"guid","side","why"}] by code
  status TEXT NOT NULL CHECK (status IN ('proposed','settled','in_effect','talk_only','overruled',
                                         'superseded','expired','failed')),
  proposed REAL NOT NULL, settled REAL, effect REAL, ended REAL,
  effect_ref TEXT CHECK (effect_ref IS NULL OR (json_valid(effect_ref) AND length(effect_ref) <= 4000)),
  supersedes INTEGER REFERENCES guild_decision(id),                 -- the rule in effect it replaced
  overruled_by_owner INTEGER NOT NULL DEFAULT 0 CHECK (overruled_by_owner IN (0,1)),
  CHECK (overruled_by_owner = 0 OR status = 'overruled'),
  CHECK (status NOT IN ('settled','in_effect') OR settled IS NOT NULL),
  CHECK (status <> 'in_effect' OR effect IS NOT NULL)
);
-- One pending decision and one in effect per kind and subject: a settled successor waits beside
-- the rule still in force until it takes effect, which supersedes that rule in one transaction
CREATE UNIQUE INDEX guild_decision_pending ON guild_decision(kind, subject)
  WHERE status IN ('proposed','settled');
CREATE UNIQUE INDEX guild_decision_current ON guild_decision(kind, subject)
  WHERE status = 'in_effect';
CREATE INDEX guild_decision_poll ON guild_decision(poll) WHERE poll IS NOT NULL;
