-- Profile photos (P2). Only the running mind writes these (its night and its control endpoint's
-- `selfie` op); the console reads them. Images live in mind-data/selfies/<guid>/, never here
-- (leeroy_mind/selfies/files.py); leeroy_mind/selfies/store.py is the one writer.
--
-- selfie_set: a set of four candidates for one member. 'queued' waits for the night's render
-- (at most one a member); 'ready' has its candidates and files. A ready set's `decided` is NULL
-- while it waits for his choice, 'picked' once a photo came from it, 'passed' when he asked for
-- four more instead. `reason` is why it was queued: a member's first photo, or his "4 more".
-- selfie_candidate: one rendered image of a ready set, with its seed, how many seeds the NSFW
-- check took and the score of the one kept, and the prompt it was rendered from.
-- selfie_pick: one row per pick or undo, never changed. An undo names the change it reverses in
-- `undoes`; NULL in before/after means no photo.
-- selfie_current: the photo in force, kept in the pick's transaction, so a reader never folds
-- the log; `pick` is the change that put it there.
CREATE TABLE selfie_set (
  guid INTEGER NOT NULL CHECK(typeof(guid) = 'integer' AND guid > 0),
  set_no INTEGER NOT NULL CHECK(typeof(set_no) = 'integer' AND set_no >= 1),
  reason TEXT NOT NULL CHECK(reason IN ('first','more')),
  status TEXT NOT NULL CHECK(status IN ('queued','ready')),
  requested REAL NOT NULL,
  rendered REAL,
  night TEXT,
  persona_version INTEGER,
  decided TEXT CHECK(decided IS NULL OR decided IN ('picked','passed')),
  PRIMARY KEY(guid, set_no),
  CHECK((status = 'ready') = (rendered IS NOT NULL))
);
CREATE UNIQUE INDEX selfie_set_one_queued ON selfie_set(guid) WHERE status = 'queued';

CREATE TABLE selfie_candidate (
  guid INTEGER NOT NULL,
  set_no INTEGER NOT NULL,
  idx INTEGER NOT NULL CHECK(idx BETWEEN 0 AND 3),
  seed INTEGER NOT NULL,
  tries INTEGER NOT NULL CHECK(tries BETWEEN 1 AND 3),
  nsfw REAL NOT NULL CHECK(nsfw BETWEEN 0 AND 1),
  prompt TEXT NOT NULL CHECK(length(prompt) <= 1000),
  PRIMARY KEY(guid, set_no, idx),
  FOREIGN KEY(guid, set_no) REFERENCES selfie_set(guid, set_no)
);

CREATE TABLE selfie_pick (  -- append-only: a pick or an undo; NULL before/after means no photo
  id INTEGER PRIMARY KEY CHECK(id >= 1),
  ts REAL NOT NULL,
  guid INTEGER NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('console','cli')),
  undoes INTEGER REFERENCES selfie_pick(id),
  before_set INTEGER,
  before_idx INTEGER,
  after_set INTEGER,
  after_idx INTEGER,
  CHECK((before_set IS NULL) = (before_idx IS NULL)),
  CHECK((after_set IS NULL) = (after_idx IS NULL)),
  CHECK(undoes IS NOT NULL OR after_set IS NOT NULL)
);

CREATE TABLE selfie_current (  -- the photo in force, kept in the pick's transaction
  guid INTEGER PRIMARY KEY,
  set_no INTEGER NOT NULL,
  idx INTEGER NOT NULL CHECK(idx BETWEEN 0 AND 3),
  pick INTEGER NOT NULL REFERENCES selfie_pick(id),
  FOREIGN KEY(guid, set_no) REFERENCES selfie_set(guid, set_no)
);
