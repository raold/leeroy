-- A kill announcement may say "for the first time" (first = 'ever'). An 'ever' row keeps the
-- weekly slot key, so it also takes that week's slot. At most one 'ever' row exists per exact
-- mode, and an unknown mode (-1) is never 'ever'. SQLite cannot change a CHECK in place, so the
-- table is rebuilt; every existing row is kept as it is. No table references it.
ALTER TABLE realm_announcement RENAME TO realm_announcement_v12;

CREATE TABLE realm_announcement (
  command_id TEXT NOT NULL PRIMARY KEY CHECK(typeof(command_id) = 'text' AND length(CAST(command_id AS BLOB)) = length(command_id) AND length(command_id) BETWEEN 1 AND 128 AND command_id NOT GLOB '*[^A-Za-z0-9_-]*'),
  kill_id TEXT NOT NULL UNIQUE REFERENCES realm_kill(kill_id) ON DELETE NO ACTION ON UPDATE NO ACTION,
  guild INTEGER NOT NULL CHECK(typeof(guild) = 'integer' AND guild BETWEEN 1 AND 4294967295),
  human INTEGER NOT NULL CHECK(typeof(human) = 'integer' AND human BETWEEN 1 AND 4294967295),
  map INTEGER NOT NULL CHECK(typeof(map) = 'integer' AND map BETWEEN 0 AND 4294967295),
  boss_id INTEGER NOT NULL CHECK(typeof(boss_id) = 'integer' AND boss_id BETWEEN 0 AND 4294967295),
  difficulty INTEGER NOT NULL CHECK(typeof(difficulty) = 'integer' AND difficulty IN (-1,0,1,2,3)),
  cadence TEXT NOT NULL CHECK(cadence = 'product-tue04-america-new_york-v1'),
  period_key TEXT NOT NULL CHECK(length(period_key) = 10 AND length(CAST(period_key AS BLOB)) = 10
    AND period_key GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    AND substr(period_key,1,4) BETWEEN '0001' AND '9999' AND date(period_key,'+0 days') IS NOT NULL AND date(period_key,'+0 days') = period_key),
  first TEXT NOT NULL CHECK(first IN ('week','ever')),
  payload_json TEXT NOT NULL CHECK(CASE WHEN json_valid(payload_json) THEN json_type(payload_json) = 'object'
    AND json(payload_json) = payload_json AND length(CAST(payload_json AS BLOB)) <= 4096 ELSE 0 END),
  payload_sha256 TEXT NOT NULL CHECK(typeof(payload_sha256) = 'text' AND length(payload_sha256) = 64 AND length(CAST(payload_sha256 AS BLOB)) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  process_id TEXT NOT NULL CHECK(typeof(process_id) = 'text' AND length(CAST(process_id AS BLOB)) BETWEEN 1 AND 256),
  generation INTEGER NOT NULL CHECK(typeof(generation) = 'integer' AND generation >= 0),
  connection_epoch TEXT NOT NULL CHECK(length(CAST(connection_epoch AS BLOB)) = length(connection_epoch) AND length(connection_epoch) BETWEEN 1 AND 20
    AND connection_epoch NOT GLOB '*[^0-9]*' AND substr(connection_epoch,1,1) BETWEEN '1' AND '9'
    AND (length(connection_epoch) < 20 OR connection_epoch <= '18446744073709551615')),
  prepared_at REAL NOT NULL CHECK(typeof(prepared_at) IN ('integer','real') AND prepared_at BETWEEN 0 AND 253402300799.999),
  expires_at REAL NOT NULL CHECK(typeof(expires_at) IN ('integer','real') AND expires_at BETWEEN 0 AND 253402300799.999),
  dispatched_at REAL CHECK(dispatched_at IS NULL OR (typeof(dispatched_at) IN ('integer','real') AND dispatched_at BETWEEN 0 AND 253402300799.999)),
  settled_at REAL CHECK(settled_at IS NULL OR (typeof(settled_at) IN ('integer','real') AND settled_at BETWEEN 0 AND 253402300799.999)),
  state TEXT NOT NULL CHECK(state IN ('prepared','dispatched','sent','refused','unknown','expired','abandoned','conflict')),
  safe_reason TEXT NOT NULL CHECK(safe_reason IN ('none','expired','context_unavailable','capacity',
    'proof_unavailable','payload_mismatch','native_refused','transport_unknown','recovered_prepared',
    'recovered_dispatched','evidence_conflict','clock_conflict','pretransport_refused')),
  UNIQUE(guild,map,boss_id,difficulty,cadence,period_key),
  CHECK(first = 'week' OR difficulty != -1),
  CHECK(state != 'sent' OR (dispatched_at IS NOT NULL AND settled_at IS NOT NULL)),
  CHECK(state != 'dispatched' OR dispatched_at IS NOT NULL),
  CHECK(state NOT IN ('expired','abandoned') OR dispatched_at IS NULL)
);

INSERT INTO realm_announcement(command_id,kill_id,guild,human,map,boss_id,difficulty,cadence,period_key,
    first,payload_json,payload_sha256,process_id,generation,connection_epoch,prepared_at,expires_at,
    dispatched_at,settled_at,state,safe_reason)
  SELECT command_id,kill_id,guild,human,map,boss_id,difficulty,cadence,period_key,
    first,payload_json,payload_sha256,process_id,generation,connection_epoch,prepared_at,expires_at,
    dispatched_at,settled_at,state,safe_reason
  FROM realm_announcement_v12 ORDER BY rowid;

DROP TABLE realm_announcement_v12;

CREATE UNIQUE INDEX realm_announcement_ever ON realm_announcement(guild,map,boss_id,difficulty)
  WHERE first = 'ever';
