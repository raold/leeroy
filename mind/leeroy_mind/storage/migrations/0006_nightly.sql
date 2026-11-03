-- memory.scope and scope_with start at 'open' and 0; migrate's data step (storage.db.DATA_STEPS,
-- privacy.backfill_scopes) then derives every existing memory's scope in this same transaction.
ALTER TABLE memory ADD COLUMN scope TEXT NOT NULL DEFAULT 'open' CHECK (scope IN ('open', 'private', 'secret'));
ALTER TABLE memory ADD COLUMN scope_with INTEGER NOT NULL DEFAULT 0;
-- night_run.status: running (also a run his login paused), interrupted (a paused run whose cleanup
-- raised), done, failed (retried after a back-off), abandoned (unfinished, older than two nights).
-- resumes: the run's resumes since its last committed unit, less those that ended in a requested
-- stop; the runner fails a night that stalls past three (a crash, or a cancel nobody asked for).
CREATE TABLE night_run(night TEXT PRIMARY KEY, window_start REAL NOT NULL, window_end REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','interrupted','done','failed','abandoned')),
  started REAL NOT NULL, finished REAL,
  attempts INTEGER NOT NULL DEFAULT 1, foreign_loads INTEGER NOT NULL DEFAULT 0, error TEXT,
  plan TEXT NOT NULL DEFAULT '{}', resumes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE night_stage(night TEXT NOT NULL, stage TEXT NOT NULL, started REAL NOT NULL, finished REAL NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, call_s REAL NOT NULL DEFAULT 0, PRIMARY KEY(night, stage));
CREATE TABLE night_step(night TEXT NOT NULL, stage TEXT NOT NULL, unit TEXT NOT NULL, finished REAL NOT NULL,
  PRIMARY KEY(night, stage, unit));
CREATE TABLE rel_cap(night TEXT, a INTEGER, b INTEGER, dim TEXT, used REAL NOT NULL, PRIMARY KEY(night, a, b, dim));
CREATE TABLE grounding_log(id INTEGER PRIMARY KEY, night TEXT, stage TEXT, owner INTEGER, kind TEXT, text TEXT,
  verdict TEXT CHECK(verdict IN ('accepted','rejected')), check_stage TEXT, reason TEXT, ts REAL);
CREATE TABLE review_queue(id INTEGER PRIMARY KEY, guid INTEGER, kind TEXT, text TEXT, evidence TEXT, reason TEXT,
  created REAL, resolved REAL);
CREATE TABLE jealousy(holder INTEGER, target INTEGER, kind TEXT CHECK(kind IN ('rivalry','envy')), level REAL,
  updated REAL, evidence TEXT, PRIMARY KEY(holder, target));
CREATE TABLE life_thread(id INTEGER PRIMARY KEY, guid INTEGER, title TEXT, beat_n INTEGER DEFAULT 0, beats_max INTEGER,
  next_beat_at REAL, status TEXT CHECK(status IN ('active','resolved')), started REAL, resolved REAL);
CREATE TABLE life_beat(id INTEGER PRIMARY KEY, thread_id INTEGER, night TEXT, text TEXT, ts REAL);
CREATE TABLE wow_life(guid INTEGER PRIMARY KEY, pressure REAL NOT NULL DEFAULT 0, guilt REAL NOT NULL DEFAULT 0,
  vow INTEGER NOT NULL DEFAULT 0, line TEXT NOT NULL DEFAULT '', line_night TEXT, updated REAL NOT NULL DEFAULT 0);
CREATE TABLE wow_hours(night TEXT NOT NULL, guid INTEGER NOT NULL, hours REAL NOT NULL, PRIMARY KEY(night, guid));
CREATE TABLE away(id INTEGER PRIMARY KEY, guid INTEGER, reason TEXT, left_at REAL, returns_at REAL, returned_at REAL);
CREATE UNIQUE INDEX away_one ON away(returned_at IS NULL) WHERE returned_at IS NULL;
CREATE TABLE encounter(id INTEGER PRIMARY KEY, night TEXT, idx INTEGER, a INTEGER, b INTEGER, setting TEXT,
  status TEXT CHECK(status IN ('planned','done','rejected')), scene TEXT, private INTEGER NOT NULL DEFAULT 0,
  volition TEXT, ref TEXT NOT NULL DEFAULT '', blowup INTEGER NOT NULL DEFAULT 0, UNIQUE(night, idx));
CREATE TABLE knows(root TEXT NOT NULL, guid INTEGER NOT NULL,
  how TEXT NOT NULL CHECK(how IN ('witnessed','overheard','told','confided','inferred')),
  via INTEGER, learned REAL NOT NULL, confidence REAL NOT NULL DEFAULT 1.0, secret INTEGER NOT NULL DEFAULT 0,
  may_tell INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(root, guid));
CREATE TABLE fact(id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('whisper_scene','confession','said_about','feud_cause','life_news','noticed','real_name')),
  root TEXT NOT NULL, about INTEGER, text TEXT NOT NULL DEFAULT '', secret INTEGER NOT NULL DEFAULT 0,
  holder INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, UNIQUE(kind, root, holder));
CREATE TABLE thinks_knows(holder INTEGER NOT NULL, other INTEGER NOT NULL, root TEXT NOT NULL,
  belief TEXT NOT NULL CHECK(belief IN ('yes','no','unsure')), updated REAL NOT NULL, PRIMARY KEY(holder, other, root));
CREATE TABLE probe_run(id INTEGER PRIMARY KEY, ts REAL NOT NULL, model TEXT NOT NULL, n INTEGER NOT NULL,
  leaks INTEGER NOT NULL, rate REAL NOT NULL, details TEXT NOT NULL DEFAULT '[]');
CREATE TABLE disclosure(id INTEGER PRIMARY KEY, guid INTEGER NOT NULL, other INTEGER NOT NULL,
  depth INTEGER NOT NULL CHECK(depth BETWEEN 1 AND 3),
  response TEXT NOT NULL CHECK(response IN ('responsive','neutral','dismissive','none')), layer INTEGER,
  ts REAL NOT NULL, ref TEXT NOT NULL);
CREATE TABLE layer_open(guid INTEGER NOT NULL, other INTEGER NOT NULL, layer INTEGER NOT NULL, opened REAL NOT NULL,
  ref TEXT NOT NULL, PRIMARY KEY(guid, other, layer));
CREATE TABLE attraction_state(guid INTEGER PRIMARY KEY, stance TEXT NOT NULL, version INTEGER NOT NULL,
  hurt_until REAL NOT NULL DEFAULT 0, hurt_deep INTEGER NOT NULL DEFAULT 0, together_since REAL,
  rush_until REAL NOT NULL DEFAULT 0, answer_rate REAL, bids_7d INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
CREATE TABLE attraction_act(id INTEGER PRIMARY KEY, guid INTEGER NOT NULL,
  act TEXT NOT NULL CHECK(act IN ('flirt','public_compliment','remembered','answered','quest_together','noticed','hurt')),
  gain REAL NOT NULL, mult REAL NOT NULL, ref TEXT NOT NULL, ts REAL NOT NULL, live INTEGER NOT NULL);
CREATE INDEX attraction_act_guid ON attraction_act(guid, ts);
CREATE TABLE attraction_log(night TEXT NOT NULL, guid INTEGER NOT NULL, gained REAL NOT NULL, decayed REAL NOT NULL,
  neglected INTEGER NOT NULL, hurt INTEGER NOT NULL, half_life REAL NOT NULL, before REAL NOT NULL, after REAL NOT NULL,
  stance_before TEXT NOT NULL, stance_after TEXT NOT NULL, PRIMARY KEY(night, guid));
CREATE TABLE lands(guid INTEGER NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, laughs INTEGER NOT NULL DEFAULT 0,
  replies INTEGER NOT NULL DEFAULT 0, ignored INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(guid, kind));
CREATE TABLE bandit(guid INTEGER NOT NULL, arm TEXT NOT NULL, context TEXT NOT NULL, alpha REAL NOT NULL DEFAULT 0,
  beta REAL NOT NULL DEFAULT 0, n INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(guid, arm, context));
CREATE TABLE lands_profile(night TEXT PRIMARY KEY, text TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]');
CREATE TABLE lexicon(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('joke','nickname','slang')),
  term TEXT NOT NULL, target INTEGER NOT NULL DEFAULT 0, origin_ref TEXT NOT NULL, originator INTEGER NOT NULL,
  born REAL NOT NULL, uses INTEGER NOT NULL DEFAULT 0, heat REAL NOT NULL DEFAULT 0, last_used REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','dormant','vetoed')), UNIQUE(kind, term, target));
CREATE TABLE lexicon_adopter(term INTEGER NOT NULL, guid INTEGER NOT NULL, first_use REAL NOT NULL,
  uses INTEGER NOT NULL DEFAULT 0, via INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(term, guid));
CREATE TABLE vocab(word TEXT PRIMARY KEY, first_seen REAL NOT NULL, origin INTEGER NOT NULL,
  users TEXT NOT NULL DEFAULT '[]', slang INTEGER NOT NULL DEFAULT 0);
CREATE TABLE rumor(id INTEGER PRIMARY KEY, root TEXT NOT NULL UNIQUE, about INTEGER NOT NULL, canonical TEXT NOT NULL,
  source INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, last_told REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'live' CHECK(status IN ('live','faded')));
CREATE TABLE rumor_version(id INTEGER PRIMARY KEY, rumor INTEGER NOT NULL, teller INTEGER NOT NULL,
  hearer INTEGER NOT NULL, op TEXT NOT NULL CHECK(op IN ('level','sharpen','assimilate','correct')),
  text TEXT NOT NULL, ts REAL NOT NULL, source INTEGER NOT NULL DEFAULT 0);
CREATE TABLE rumor_state(rumor INTEGER NOT NULL, guid INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('spreader','stifler')), tells INTEGER NOT NULL DEFAULT 0, version INTEGER,
  shift REAL NOT NULL DEFAULT 0, updated REAL NOT NULL, PRIMARY KEY(rumor, guid));
CREATE TABLE volition(night TEXT NOT NULL, guid INTEGER NOT NULL, kind TEXT NOT NULL, target INTEGER NOT NULL,
  weight REAL NOT NULL, ref TEXT NOT NULL DEFAULT '', chosen INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(night, guid, kind, target));
CREATE TABLE story_arc(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('feud','rumor','life','mentor')),
  key TEXT NOT NULL UNIQUE, tension REAL NOT NULL DEFAULT 0, opened REAL NOT NULL, last_beat REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')));
CREATE TABLE feud(id INTEGER PRIMARY KEY, a INTEGER NOT NULL, b INTEGER NOT NULL CHECK(a < b), started REAL NOT NULL,
  ended REAL, cause TEXT NOT NULL, sides TEXT NOT NULL DEFAULT '{}', evidence TEXT NOT NULL DEFAULT '[]');
CREATE UNIQUE INDEX feud_open ON feud(a, b) WHERE ended IS NULL;
CREATE TABLE mentor(id INTEGER PRIMARY KEY, mentor INTEGER NOT NULL, mentee INTEGER NOT NULL, started REAL NOT NULL,
  ends REAL NOT NULL, ended REAL);
CREATE UNIQUE INDEX mentor_one ON mentor(mentee) WHERE ended IS NULL;
CREATE TABLE guild_mood(night TEXT PRIMARY KEY, value REAL NOT NULL, reasons TEXT NOT NULL DEFAULT '[]');
CREATE TABLE pad_mood(guid INTEGER PRIMARY KEY, p REAL NOT NULL, a REAL NOT NULL, d REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE emotion(id INTEGER PRIMARY KEY, guid INTEGER NOT NULL, night TEXT NOT NULL, type TEXT NOT NULL,
  target INTEGER NOT NULL DEFAULT 0, intensity INTEGER NOT NULL CHECK(intensity BETWEEN 1 AND 3),
  why TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]', ts REAL NOT NULL);
CREATE INDEX emotion_night ON emotion(night, guid);
CREATE TABLE motive(guid INTEGER NOT NULL, motive TEXT NOT NULL, weight REAL NOT NULL, deficit REAL NOT NULL DEFAULT 0,
  updated REAL NOT NULL, PRIMARY KEY(guid, motive));
CREATE TABLE trait_state(night TEXT NOT NULL, guid INTEGER NOT NULL, trait TEXT NOT NULL, value REAL NOT NULL,
  PRIMARY KEY(night, guid, trait));
CREATE TABLE chronicle(id INTEGER PRIMARY KEY, week TEXT NOT NULL UNIQUE, era TEXT NOT NULL, author INTEGER NOT NULL,
  text TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL);
CREATE TABLE award(id INTEGER PRIMARY KEY, season TEXT NOT NULL, title TEXT NOT NULL, guid INTEGER NOT NULL,
  value REAL NOT NULL, UNIQUE(season, title));
CREATE TABLE critic_score(night TEXT NOT NULL, guid INTEGER NOT NULL, n INTEGER NOT NULL, mean REAL NOT NULL,
  PRIMARY KEY(night, guid));
CREATE TABLE persona_ledger(id INTEGER PRIMARY KEY, guid INTEGER NOT NULL, ts REAL NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('note_added','note_dropped','note_rolled_back','drift_flag','stance_band','undo')),
  text TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]', ref INTEGER, score_before REAL);
CREATE TABLE eval_score(week TEXT, guid INTEGER, metric TEXT, value REAL, PRIMARY KEY(week, guid, metric));
CREATE TABLE eval_answer(id INTEGER PRIMARY KEY, week TEXT, kind TEXT, guid INTEGER, qkey TEXT, category TEXT,
  question TEXT, truth TEXT, answer TEXT, memory_ids TEXT, verdict TEXT, UNIQUE(week, kind, guid, qkey));
