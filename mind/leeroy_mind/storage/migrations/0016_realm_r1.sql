-- Realm R1 (plan Tasks 13-19). Three things the realm's light minds, steps, canon, calendar and
-- mail need before they can send or remember anything:
--
-- realm_direct: one exactly-once intent per command that moves no body (actor sets, speech,
-- renames, bank money and tabs, grants, calendar posts and mail). An intent is prepared, then
-- committed 'dispatched' just before the wire, then settled by native: acked (with its job's
-- value when it has one), refused (never done; the ref may be tried again), unknown (it may
-- have been done: never sent again) or abandoned (it never reached the wire). One live intent
-- per (owner, ref) at a time: an acked or unknown one stays live for good.
--
-- realm_rule_origin: the origin of a fact the realm derives by rule (a rival's rule kill, a lost
-- benchmark, a poach offer, a night's beat or forum post). It has no game_event or chat_line row
-- behind it, so it cannot use realm_fact_origin; its inputs are kept canonical instead.
--
-- realm_line, realm_mail and realm_calendar each gain a command binding and an outcome. SQLite
-- cannot change a CHECK in place, so the three are rebuilt and every existing row is kept, its
-- old columns as they were; the new columns of a row from before this migration are filled as
-- each copy says. No table references them.
--
-- realm_benchmark_seen: each training-ground benchmark the race has already counted.
CREATE TABLE realm_direct (
  command_id TEXT NOT NULL PRIMARY KEY CHECK(typeof(command_id) = 'text' AND length(CAST(command_id AS BLOB)) = 32
    AND length(command_id) = 32 AND command_id NOT GLOB '*[^0-9a-f]*'),
  owner TEXT NOT NULL CHECK(owner IN ('actors','speech','step','canon','calendar','mail')),
  ref TEXT NOT NULL CHECK(typeof(ref) = 'text' AND length(CAST(ref AS BLOB)) = length(ref)
    AND length(ref) BETWEEN 1 AND 128 AND ref NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  kind TEXT NOT NULL CHECK(typeof(kind) = 'text' AND length(CAST(kind AS BLOB)) = length(kind)
    AND length(kind) BETWEEN 1 AND 64 AND kind NOT GLOB '*[^a-z_]*'),
  payload_sha256 TEXT NOT NULL CHECK(typeof(payload_sha256) = 'text' AND length(payload_sha256) = 64
    AND length(CAST(payload_sha256 AS BLOB)) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  guild INTEGER NOT NULL CHECK(typeof(guild) = 'integer' AND guild BETWEEN 1 AND 4294967295),
  human INTEGER NOT NULL CHECK(typeof(human) = 'integer' AND human BETWEEN 1 AND 4294967295),
  process_id TEXT NOT NULL CHECK(typeof(process_id) = 'text' AND length(CAST(process_id AS BLOB)) BETWEEN 1 AND 256),
  expects_job INTEGER NOT NULL CHECK(typeof(expects_job) = 'integer' AND expects_job IN (0,1)),
  state TEXT NOT NULL CHECK(state IN ('prepared','dispatched','acked','refused','unknown','abandoned')),
  reason TEXT CHECK(reason IS NULL OR (typeof(reason) = 'text' AND length(CAST(reason AS BLOB)) = length(reason)
    AND length(reason) BETWEEN 1 AND 64 AND reason NOT GLOB '*[^a-z0-9_]*')),
  -- A job's unsigned 64-bit value, as decimal TEXT so it stays exact.
  value TEXT CHECK(value IS NULL OR (typeof(value) = 'text' AND length(CAST(value AS BLOB)) = length(value)
    AND length(value) BETWEEN 1 AND 20 AND value NOT GLOB '*[^0-9]*'
    AND (value = '0' OR substr(value,1,1) BETWEEN '1' AND '9')
    AND (length(value) < 20 OR value <= '18446744073709551615'))),
  prepared_ms INTEGER NOT NULL CHECK(typeof(prepared_ms) = 'integer' AND prepared_ms BETWEEN 1 AND 253402300799999),
  dispatched_ms INTEGER CHECK(dispatched_ms IS NULL OR (typeof(dispatched_ms) = 'integer'
    AND dispatched_ms BETWEEN 1 AND 253402300799999)),
  settled_ms INTEGER CHECK(settled_ms IS NULL OR (typeof(settled_ms) = 'integer'
    AND settled_ms BETWEEN 1 AND 253402300799999)),
  CHECK(dispatched_ms IS NULL OR dispatched_ms >= prepared_ms),
  CHECK(settled_ms IS NULL OR (settled_ms >= prepared_ms AND (dispatched_ms IS NULL OR settled_ms >= dispatched_ms))),
  CHECK(state != 'prepared' OR (dispatched_ms IS NULL AND settled_ms IS NULL AND reason IS NULL AND value IS NULL)),
  CHECK(state != 'dispatched' OR (dispatched_ms IS NOT NULL AND settled_ms IS NULL AND reason IS NULL AND value IS NULL)),
  -- Acked: without a job it is settled at once; with one it is settled exactly when the job's value is in.
  CHECK(state != 'acked' OR (dispatched_ms IS NOT NULL AND reason IS NULL
    AND ((expects_job = 0 AND value IS NULL AND settled_ms IS NOT NULL)
      OR (expects_job = 1 AND (settled_ms IS NULL) = (value IS NULL))))),
  CHECK(state != 'refused' OR (reason IS NOT NULL AND settled_ms IS NOT NULL AND value IS NULL)),
  CHECK(state != 'unknown' OR (reason IS NOT NULL AND dispatched_ms IS NOT NULL AND settled_ms IS NOT NULL
    AND value IS NULL)),
  CHECK(state != 'abandoned' OR (reason IS NOT NULL AND dispatched_ms IS NULL AND settled_ms IS NOT NULL
    AND value IS NULL))
);
CREATE UNIQUE INDEX realm_direct_live ON realm_direct(owner,ref)
  WHERE state IN ('prepared','dispatched','acked','unknown');

CREATE TABLE realm_rule_origin (
  fact INTEGER NOT NULL PRIMARY KEY REFERENCES realm_fact(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(fact) = 'integer' AND fact > 0),
  producer_key TEXT NOT NULL UNIQUE,
  policy TEXT NOT NULL CHECK(policy IN ('race_rule_kill_v1','benchmark_loss_v1','poach_offer_v1',
    'night_beat_v1','forum_post_v1')),
  inputs_json TEXT NOT NULL CHECK(CASE WHEN json_valid(inputs_json) THEN json_type(inputs_json) = 'object'
    AND json(inputs_json) = inputs_json AND length(CAST(inputs_json AS BLOB)) <= 8192 ELSE 0 END),
  -- Who knows the fact, as [[guild, how], ...] by guild: realm_knows must say exactly this.
  known_json TEXT NOT NULL CHECK(CASE WHEN json_valid(known_json) THEN json_type(known_json) = 'array'
    AND json(known_json) = known_json AND json_array_length(known_json) BETWEEN 1 AND 129
    AND length(CAST(known_json AS BLOB)) <= 4096 ELSE 0 END),
  text_sha256 TEXT NOT NULL CHECK(typeof(text_sha256) = 'text' AND length(text_sha256) = 64
    AND length(CAST(text_sha256 AS BLOB)) = 64 AND text_sha256 NOT GLOB '*[^0-9a-f]*'),
  revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision > 0),
  state TEXT NOT NULL CHECK(state IN ('active','revoked')),
  CHECK(typeof(producer_key) = 'text' AND length(CAST(producer_key AS BLOB)) = length(producer_key)
    AND length(producer_key) <= 128 AND producer_key NOT GLOB '*[^A-Za-z0-9_.:-]*'
    AND substr(producer_key,1,5) = 'rule:'
    AND substr(producer_key,6,length(policy) + 1) = policy || ':'
    AND length(producer_key) > length(policy) + 6)
) WITHOUT ROWID;

-- realm_line: a row from before this migration has no kind, command or reason. Its old drop
-- reason stays in `dropped`, and it reads 'dropped' when it has one, else 'unknown' (whether it
-- was ever sent is not recorded). A new row keeps its drop reason in `reason`, never `dropped`.
ALTER TABLE realm_line RENAME TO realm_line_v15;

CREATE TABLE realm_line (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  guid INTEGER NOT NULL,
  guild INTEGER NOT NULL,
  channel TEXT NOT NULL,
  text TEXT NOT NULL,
  dropped TEXT,
  command_id TEXT UNIQUE CHECK(command_id IS NULL OR (typeof(command_id) = 'text'
    AND length(CAST(command_id AS BLOB)) = 32 AND length(command_id) = 32 AND command_id NOT GLOB '*[^0-9a-f]*')),
  kind TEXT CHECK(kind IS NULL OR kind IN ('rival','gz','answer','member')),
  state TEXT NOT NULL CHECK(state IN ('dropped','prepared','dispatched','sent','refused','unknown')),
  reason TEXT CHECK(reason IS NULL OR (typeof(reason) = 'text' AND length(CAST(reason AS BLOB)) = length(reason)
    AND length(reason) BETWEEN 1 AND 64 AND reason NOT GLOB '*[^a-z0-9_]*')),
  CHECK(kind IS NOT NULL OR (command_id IS NULL AND reason IS NULL
    AND state = CASE WHEN dropped IS NULL THEN 'unknown' ELSE 'dropped' END)),
  CHECK(kind IS NULL OR dropped IS NULL),
  CHECK(kind IS NULL OR state != 'dropped' OR (command_id IS NULL AND reason IS NOT NULL)),
  CHECK(kind IS NULL OR state NOT IN ('prepared','dispatched','sent') OR (command_id IS NOT NULL AND reason IS NULL)),
  CHECK(kind IS NULL OR state NOT IN ('refused','unknown') OR reason IS NOT NULL)
);

INSERT INTO realm_line(id,ts,guid,guild,channel,text,dropped,command_id,kind,state,reason)
  SELECT id,ts,guid,guild,channel,text,dropped,NULL,NULL,
    CASE WHEN dropped IS NULL THEN 'unknown' ELSE 'dropped' END,NULL
  FROM realm_line_v15 ORDER BY id;

DROP TABLE realm_line_v15;

-- realm_mail: 'queued' (a written mail waiting to go) becomes 'ready'; every other state stays.
ALTER TABLE realm_mail RENAME TO realm_mail_v15;

CREATE TABLE realm_mail (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  sender INTEGER NOT NULL,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  items TEXT NOT NULL DEFAULT '[]',
  item_guid INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'awaiting_text'
    CHECK(state IN ('awaiting_text','ready','dispatched','sent','taken','failed','skipped')),
  taken REAL,
  command_id TEXT UNIQUE CHECK(command_id IS NULL OR (typeof(command_id) = 'text'
    AND length(CAST(command_id AS BLOB)) = 32 AND length(command_id) = 32 AND command_id NOT GLOB '*[^0-9a-f]*')),
  reason TEXT CHECK(reason IS NULL OR (typeof(reason) = 'text' AND length(CAST(reason AS BLOB)) = length(reason)
    AND length(reason) BETWEEN 1 AND 64 AND reason NOT GLOB '*[^a-z0-9_]*')),
  CHECK(state NOT IN ('awaiting_text','ready') OR command_id IS NULL),
  CHECK(state != 'dispatched' OR command_id IS NOT NULL),
  CHECK(state != 'skipped' OR reason IS NOT NULL)
);

INSERT INTO realm_mail(id,ts,sender,kind,subject,body,items,item_guid,state,taken,command_id,reason)
  SELECT id,ts,sender,kind,subject,body,items,item_guid,
    CASE WHEN state = 'queued' THEN 'ready' ELSE state END,taken,NULL,NULL
  FROM realm_mail_v15 ORDER BY id;

DROP TABLE realm_mail_v15;

-- realm_calendar: a row from before this migration recorded an event the realm had posted, but
-- not its native id; it reads 'posted', unbound, so it is never posted again.
ALTER TABLE realm_calendar RENAME TO realm_calendar_v15;

CREATE TABLE realm_calendar (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  creator INTEGER NOT NULL,
  at REAL NOT NULL,
  title TEXT NOT NULL,
  ref TEXT NOT NULL DEFAULT '',
  updated REAL NOT NULL,
  event_id INTEGER UNIQUE CHECK(event_id IS NULL OR (typeof(event_id) = 'integer' AND event_id > 0)),
  command_id TEXT UNIQUE CHECK(command_id IS NULL OR (typeof(command_id) = 'text'
    AND length(CAST(command_id AS BLOB)) = 32 AND length(command_id) = 32 AND command_id NOT GLOB '*[^0-9a-f]*')),
  state TEXT NOT NULL CHECK(state IN ('pending','posted','failed')),
  CHECK(event_id IS NULL OR state = 'posted')
);

INSERT INTO realm_calendar(id,kind,creator,at,title,ref,updated,event_id,command_id,state)
  SELECT id,kind,creator,at,title,ref,updated,NULL,NULL,'posted'
  FROM realm_calendar_v15 ORDER BY id;

DROP TABLE realm_calendar_v15;

CREATE TABLE realm_benchmark_seen (
  benchmark_id INTEGER NOT NULL PRIMARY KEY CHECK(typeof(benchmark_id) = 'integer' AND benchmark_id > 0),
  guild INTEGER NOT NULL CHECK(typeof(guild) = 'integer' AND guild BETWEEN 1 AND 4294967295),
  ts REAL NOT NULL CHECK(typeof(ts) IN ('integer','real') AND ts BETWEEN 0 AND 253402300799.999)
);
