-- The conversation lab (lab/README.md): a nightly battery of invented owner-style lines run in a
-- sandbox copy of the guild, scored, and prompt variants tested against the built-in wording.
-- lab_run is one night's (or one `lab run`'s) run: the piece it varied, its seeded plan (scripts,
-- member guids and channels, never text), the seconds of its budget it used, its status and a
-- summary of numbers per arm. prompt_variant is a proposed wording of one mutable prompt piece
-- (talk/pieces.py): its text (instruction wording written from the repo's own, never persona text
-- or his lines), a word diff against the built-in wording, its outcome code and, once promoted,
-- the learn_arm it became; its scoresheet (numbers only) is kept for good. lab_score is one of her
-- answers in one arm: rule flags, word counts, the 0-3 rubric grade and the blind judge's verdict;
-- never a line's words. Only the night and `lab run` write these; the console and CLI read them.
CREATE TABLE lab_run (
  id INTEGER PRIMARY KEY,
  night TEXT NOT NULL,               -- the night it belongs to (a `lab run`: its local date)
  source TEXT NOT NULL CHECK (source IN ('night', 'cli')),
  piece TEXT NOT NULL,               -- the mutable piece varied (talk/pieces.py)
  seed TEXT NOT NULL,
  plan TEXT NOT NULL,                -- JSON: {"pairs": [{pair, script, kind, guid, channel}], ...}
  started REAL NOT NULL,
  finished REAL,
  status TEXT NOT NULL CHECK (status IN ('running', 'done', 'budget', 'failed', 'skipped')),
  reason TEXT,                       -- a code, never text
  spent REAL NOT NULL DEFAULT 0,     -- seconds of [lab] minutes used so far
  summary TEXT                       -- JSON: numbers per arm, the comparison, the promotion
);
CREATE INDEX lab_run_night ON lab_run(night);
CREATE TABLE prompt_variant (
  id INTEGER PRIMARY KEY,
  run INTEGER NOT NULL REFERENCES lab_run(id),
  piece TEXT NOT NULL,
  text TEXT NOT NULL,
  diff TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('proposed', 'promoted', 'retired')),
  outcome TEXT,                      -- won, lost, regressed, untested, frozen, budget, too_few
  arm_id TEXT,                       -- learn_arm id once promoted
  scores TEXT,                       -- JSON scoresheet: numbers only
  created REAL NOT NULL
);
CREATE INDEX prompt_variant_piece ON prompt_variant(piece, status);
CREATE TABLE lab_score (
  run INTEGER NOT NULL REFERENCES lab_run(id),
  arm TEXT NOT NULL,                 -- 'control' or 'v<prompt_variant id>'
  pair INTEGER NOT NULL,
  turn INTEGER NOT NULL,
  guid INTEGER NOT NULL,
  kind TEXT NOT NULL,                -- the script's kind
  channel TEXT NOT NULL,             -- dm, whisper, guild or party
  answered INTEGER NOT NULL,         -- 1: she said something to that line
  applied INTEGER NOT NULL DEFAULT 0,  -- 1: the variant's piece was in her prompt
  rules TEXT NOT NULL,               -- JSON {code: 0|1}: the rule checks (lab/rules.py)
  words INTEGER NOT NULL DEFAULT 0,
  his_words INTEGER NOT NULL DEFAULT 0,
  rubric INTEGER CHECK (rubric BETWEEN 0 AND 3),   -- NULL until graded
  rubric_code TEXT,
  judged_human INTEGER,              -- the blind judge on her (1 human, 0 AI), NULL: not judged
  his_judged_human INTEGER,
  PRIMARY KEY (run, arm, pair, turn)
);
