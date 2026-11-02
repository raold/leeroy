-- Training ground, after voice 0005, nightly 0006 and guild life 0007.
-- Tables only: existing schemas, rows and vectors are unchanged.
CREATE TABLE tg_pull (id TEXT PRIMARY KEY, context TEXT NOT NULL, map INTEGER NOT NULL, instance_id INTEGER NOT NULL,
  boss_id INTEGER NOT NULL, difficulty INTEGER NOT NULL, size INTEGER NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('killed','wiped','aborted')), start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
  members INTEGER NOT NULL, assisted INTEGER NOT NULL, boss_entry INTEGER NOT NULL, upd_mean_ms INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('his','offline','benchmark','other')), lead_guid INTEGER NOT NULL DEFAULT 0,
  raid_id INTEGER, card_id INTEGER, arm_id INTEGER, change TEXT NOT NULL DEFAULT '',
  complete INTEGER NOT NULL, scoreable INTEGER NOT NULL, deaths INTEGER NOT NULL DEFAULT 0,
  failed_mechanics INTEGER NOT NULL DEFAULT 0, score REAL, covariates TEXT NOT NULL DEFAULT '{}',
  checks_version INTEGER NOT NULL DEFAULT 0, checks_complete INTEGER NOT NULL DEFAULT 0,
  checks_coverage TEXT NOT NULL DEFAULT '[]', score_basis TEXT NOT NULL DEFAULT 'unknown', received REAL NOT NULL,
  damage_basis TEXT NOT NULL DEFAULT 'unknown', healing_basis TEXT NOT NULL DEFAULT 'unknown',
  spell_basis TEXT NOT NULL DEFAULT 'unknown', interrupt_basis TEXT NOT NULL DEFAULT 'unknown');
CREATE INDEX tg_pull_context ON tg_pull (context, start_ms);
CREATE TABLE tg_member (pull TEXT NOT NULL REFERENCES tg_pull(id), guid INTEGER NOT NULL, class INTEGER NOT NULL,
  spec_tab INTEGER NOT NULL, role TEXT NOT NULL CHECK (role IN ('tank','healer','melee','ranged')), ilvl INTEGER NOT NULL,
  damage INTEGER NOT NULL, damage_boss INTEGER NOT NULL, healing INTEGER NOT NULL, overheal INTEGER NOT NULL,
  taken INTEGER NOT NULL, deaths INTEGER NOT NULL, interrupts INTEGER NOT NULL, active_ms INTEGER NOT NULL,
  detail TEXT NOT NULL, PRIMARY KEY (pull, guid));
CREATE TABLE tg_death (id INTEGER PRIMARY KEY, pull TEXT NOT NULL REFERENCES tg_pull(id), guid INTEGER NOT NULL,
  at_ms INTEGER NOT NULL, spell INTEGER NOT NULL, source INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
  hits TEXT NOT NULL, cause TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE tg_track (pull TEXT NOT NULL REFERENCES tg_pull(id), who INTEGER NOT NULL, entry INTEGER NOT NULL,
  data TEXT NOT NULL, PRIMARY KEY (pull, who, entry));
CREATE TABLE tg_failure (id INTEGER PRIMARY KEY, pull TEXT NOT NULL REFERENCES tg_pull(id), kind TEXT NOT NULL,
  key TEXT NOT NULL, guid INTEGER NOT NULL, count INTEGER NOT NULL, deaths REAL NOT NULL, damage_share REAL NOT NULL);
CREATE TABLE tg_card (id INTEGER PRIMARY KEY, context TEXT NOT NULL, version INTEGER NOT NULL, lineage INTEGER,
  status TEXT NOT NULL CHECK (status IN ('candidate','trial','champion','retired','suspended')),
  origin TEXT NOT NULL CHECK (origin IN ('stock','coach','progression','lab')), directives TEXT NOT NULL,
  call TEXT NOT NULL DEFAULT '' CHECK (length(call) <= 60), assignment TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '[]', vetoes TEXT NOT NULL DEFAULT '[]',
  prev_champion INTEGER, embedding BLOB, created REAL NOT NULL, changed REAL NOT NULL);
CREATE UNIQUE INDEX tg_card_champion ON tg_card (context) WHERE status = 'champion';
CREATE TABLE tg_lesson (context TEXT NOT NULL, night TEXT NOT NULL, text TEXT NOT NULL CHECK (length(text) <= 200),
  evidence TEXT NOT NULL, PRIMARY KEY (context, night));
CREATE TABLE tg_point (id INTEGER PRIMARY KEY, context TEXT NOT NULL, night TEXT NOT NULL, fact TEXT NOT NULL,
  text TEXT NOT NULL, used REAL);
CREATE TABLE tg_trial (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, lever TEXT NOT NULL,
  metric TEXT NOT NULL CHECK (metric IN ('raid','spec')),
  status TEXT NOT NULL CHECK (status IN ('open','promoted','retired','gave_up','drift')),
  opened REAL NOT NULL, closed REAL, verdict TEXT NOT NULL DEFAULT '');
CREATE TABLE tg_arm (id INTEGER PRIMARY KEY, trial INTEGER NOT NULL REFERENCES tg_trial(id),
  card INTEGER REFERENCES tg_card(id), champion INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('live','retired','won','lost')), wipes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE tg_obs (arm INTEGER NOT NULL REFERENCES tg_arm(id), pull TEXT NOT NULL REFERENCES tg_pull(id),
  context TEXT NOT NULL, value REAL NOT NULL, fails REAL NOT NULL DEFAULT 0, PRIMARY KEY (arm, pull));
CREATE TABLE tg_applied (id INTEGER PRIMARY KEY, ts REAL NOT NULL, run TEXT NOT NULL,
  layer TEXT NOT NULL CHECK (layer IN ('standing','pooled','boss','arm')), context TEXT NOT NULL,
  command TEXT NOT NULL, inverse TEXT NOT NULL, undone REAL);
CREATE INDEX tg_applied_open ON tg_applied (undone);
CREATE TABLE tg_rotation (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, class INTEGER NOT NULL,
  spec_tab INTEGER NOT NULL, lines TEXT NOT NULL, origin TEXT NOT NULL, parent INTEGER,
  status TEXT NOT NULL CHECK (status IN ('candidate','dummy','field','champion','retired')), created REAL NOT NULL);
CREATE TABLE tg_dummy (id INTEGER PRIMARY KEY, rotation INTEGER REFERENCES tg_rotation(id), guid INTEGER NOT NULL,
  round INTEGER NOT NULL, seconds INTEGER NOT NULL, dps INTEGER NOT NULL, stock_dps INTEGER NOT NULL,
  dropped TEXT NOT NULL DEFAULT '', ts REAL NOT NULL, damage_basis TEXT NOT NULL DEFAULT 'unknown',
  spell_basis TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE tg_setup (id INTEGER PRIMARY KEY, guid INTEGER NOT NULL, since REAL NOT NULL,
  rotation INTEGER REFERENCES tg_rotation(id), toggles TEXT NOT NULL DEFAULT '[]', vals TEXT NOT NULL DEFAULT '{}',
  premade INTEGER, copied_from INTEGER, explored TEXT NOT NULL DEFAULT '', applied REAL);
CREATE INDEX tg_setup_guid ON tg_setup (guid, since);
CREATE TABLE tg_benchmark (id INTEGER PRIMARY KEY, week TEXT NOT NULL UNIQUE, raid_key TEXT NOT NULL,
  guild_id INTEGER NOT NULL, members TEXT NOT NULL, bosses TEXT NOT NULL, outcome TEXT NOT NULL,
  kills INTEGER NOT NULL, wipes INTEGER NOT NULL, ts REAL NOT NULL);
CREATE TABLE tg_perf (ts REAL NOT NULL, fight TEXT NOT NULL, context TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('his','offline')), armed INTEGER NOT NULL, mean_ms INTEGER NOT NULL);
CREATE INDEX tg_perf_context ON tg_perf (context, fight);
