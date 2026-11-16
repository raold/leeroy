-- The photo critic ([selfies] critic, selfies/critic.py). Only the running mind writes these
-- (its night, booth and photo requests); the console and `selfies critic-report` read them.
--
-- selfie_critic: one row per rendered candidate the critic measured: a profile set's candidate
-- (set_no, idx), or one seed of a situational or request photo (snap_id; best_of renders several
-- and sends the best). `file` is the image under mind-data, built from integers only; `ref` her
-- reference photo's, when it had one. framing, outfit and prop are the scene's enum codes, never
-- free text. scale and steps are what it rendered with; `arm` 1 when the parameter bandit drew
-- them (params.py). The worker's detectors fill faces..identity; the vision judge fills
-- judge_model..overall after the worker has exited (NULL: not judged). `verdict` is 'pending'
-- until the judge's pass, then 'clean' or 'flagged' with `codes` (comma-separated defect codes)
-- and `score` (0-1, what best-of-N ranks by); `chosen` 1 for the seed that was sent.
CREATE TABLE selfie_critic (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  run TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('set','snap','request')),
  guid INTEGER NOT NULL CHECK(typeof(guid) = 'integer' AND guid > 0),
  set_no INTEGER,
  idx INTEGER CHECK(idx IS NULL OR idx BETWEEN 0 AND 3),
  snap_id INTEGER,
  seed INTEGER NOT NULL,
  file TEXT NOT NULL CHECK(length(file) <= 200),
  ref TEXT CHECK(ref IS NULL OR length(ref) <= 200),
  crop TEXT,
  framing TEXT CHECK(framing IS NULL OR length(framing) <= 40),
  outfit TEXT CHECK(outfit IS NULL OR length(outfit) <= 40),
  prop TEXT CHECK(prop IS NULL OR length(prop) <= 40),
  scale REAL,
  steps INTEGER,
  arm INTEGER NOT NULL DEFAULT 0 CHECK(arm IN (0, 1)),
  face_expected INTEGER NOT NULL DEFAULT 1 CHECK(face_expected IN (0, 1)),
  faces INTEGER,
  hands INTEGER,
  fingers TEXT,
  hands_odd INTEGER,
  eye_tilt REAL,
  eye_level REAL,
  gaze_gap REAL,
  identity REAL,
  detectors TEXT,
  judge_model TEXT,
  limbs TEXT CHECK(limbs IS NULL OR limbs IN ('ok','defect')),
  hands_judged TEXT CHECK(hands_judged IS NULL OR hands_judged IN ('ok','defect','not_visible')),
  fingers_ok INTEGER,
  eyes_ok INTEGER,
  extra_people INTEGER,
  same_person INTEGER,
  overall INTEGER CHECK(overall IS NULL OR overall BETWEEN 0 AND 3),
  verdict TEXT NOT NULL DEFAULT 'pending' CHECK(verdict IN ('pending','clean','flagged')),
  codes TEXT NOT NULL DEFAULT '',
  score REAL,
  chosen INTEGER NOT NULL DEFAULT 0 CHECK(chosen IN (0, 1)),
  CHECK((purpose = 'set') = (set_no IS NOT NULL AND idx IS NOT NULL)),
  CHECK((purpose = 'set') = (snap_id IS NULL))
);
CREATE INDEX selfie_critic_set ON selfie_critic(guid, set_no, idx);
CREATE INDEX selfie_critic_snap ON selfie_critic(snap_id);
CREATE INDEX selfie_critic_pending ON selfie_critic(verdict) WHERE verdict = 'pending';

-- The render-parameter bandit ([selfies] param_bandit, selfies/params.py): identity scale x
-- steps per context ('<shape>:<framing>'), Thompson sampling over Beta(1, 1) priors with the
-- learn bandit's 21-day decay. selfie_param_arm counts the draws; selfie_param_credit holds one
-- replaceable reward per source: 'critic:<selfie_critic id>' (the critic's overall) or
-- 'pick:<selfie_pick id>:<idx>' (his choice among a set's candidates; an undo removes it).
CREATE TABLE selfie_param_arm (
  context TEXT NOT NULL,
  scale REAL NOT NULL,
  steps INTEGER NOT NULL,
  drawn INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(context, scale, steps)
);
CREATE TABLE selfie_param_credit (
  source TEXT PRIMARY KEY,
  context TEXT NOT NULL,
  scale REAL NOT NULL,
  steps INTEGER NOT NULL,
  ts REAL NOT NULL,
  success REAL NOT NULL CHECK(success >= 0),
  failure REAL NOT NULL CHECK(failure >= 0)
);
CREATE INDEX selfie_param_credit_context ON selfie_param_credit(context);
