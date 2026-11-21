CREATE TABLE day_plan (
  id INTEGER PRIMARY KEY, night TEXT NOT NULL, member INTEGER NOT NULL,
  starts REAL NOT NULL, ends REAL NOT NULL,
  activity TEXT NOT NULL CHECK (activity IN ('city','inn','fishing','gathering','camp','duel_practice',
                                             'holiday','offline','questing')),
  place TEXT, partners TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(partners)),
  status TEXT NOT NULL CHECK (status IN ('planned','running','done','skipped','preempted')),
  reason TEXT, action_ref TEXT, UNIQUE(night, member, starts));
CREATE INDEX day_plan_due ON day_plan(status,starts,ends);
CREATE TABLE day_permit (
  plan INTEGER PRIMARY KEY REFERENCES day_plan(id), begin_id TEXT UNIQUE NOT NULL,
  token TEXT, boot INTEGER, serial TEXT, guild INTEGER, human INTEGER, process TEXT, epoch TEXT,
  state TEXT NOT NULL, reason TEXT, release_ids TEXT CHECK(json_valid(release_ids)),
  succeeded TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(succeeded)), phase TEXT,
  evidence INTEGER REFERENCES game_event(id));
CREATE TABLE day_step (
  id TEXT PRIMARY KEY, plan INTEGER NOT NULL REFERENCES day_permit(plan),
  kind TEXT NOT NULL, member INTEGER NOT NULL, UNIQUE(plan,kind,member));
CREATE TABLE day_handoff (
  request_id TEXT PRIMARY KEY, reason TEXT NOT NULL, members TEXT NOT NULL CHECK(json_valid(members)),
  process TEXT NOT NULL, epoch TEXT NOT NULL, guild INTEGER NOT NULL, human INTEGER NOT NULL,
  expires REAL NOT NULL, state TEXT NOT NULL CHECK(state IN ('waiting','sent','expired')));
