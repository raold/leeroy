-- The Turing judge (turing/README.md): how distinguishable the members' lines are from the
-- owner's, per night and channel family. turing_night holds the stylometric classifier's AUC
-- with its interval and its top features (numbers and fixed labels), and the blind judge's
-- totals and tell codes, kept equal to its turing_judgement rows. A judgement row is one judged
-- window: its line references (never text), its score and the judge's tells; a tell's short
-- phrase is kept only for a window from a public channel. Only the night writes these; the
-- console and the CLI read them.
CREATE TABLE turing_night (
  night TEXT NOT NULL,
  family TEXT NOT NULL CHECK (family IN ('game','private','discord')),
  ts REAL NOT NULL,
  days INTEGER,
  status TEXT,                       -- ok, too_few or one_class
  human_lines INTEGER, member_lines INTEGER, sample INTEGER, groups INTEGER,
  auc REAL, auc_low REAL, auc_high REAL,
  features TEXT,                     -- JSON list: key, human, member, separation
  judge_windows INTEGER NOT NULL DEFAULT 0,
  judge_speakers INTEGER NOT NULL DEFAULT 0,
  judge_correct INTEGER NOT NULL DEFAULT 0,
  judge_his INTEGER NOT NULL DEFAULT 0,
  judge_his_correct INTEGER NOT NULL DEFAULT 0,
  tells TEXT,                        -- JSON: {caught|missed|him: {code: count}}
  PRIMARY KEY (night, family)
);
CREATE TABLE turing_judgement (
  night TEXT NOT NULL,
  unit TEXT NOT NULL,
  family TEXT NOT NULL CHECK (family IN ('game','private','discord')),
  refs TEXT NOT NULL,                -- JSON list of 'c:<chat_line id>' / 'd:<discord_message id>'
  status TEXT NOT NULL CHECK (status IN ('ok','invalid','missing')),
  speakers INTEGER NOT NULL DEFAULT 0,
  correct INTEGER NOT NULL DEFAULT 0,
  his INTEGER NOT NULL DEFAULT 0,
  his_correct INTEGER NOT NULL DEFAULT 0,
  tells TEXT NOT NULL DEFAULT '[]',  -- JSON list: code, judged, truth, phrase (public windows only)
  ts REAL NOT NULL,
  PRIMARY KEY (night, unit)
);
