-- SQLite requires a table rebuild to extend the direct owner's CHECK. Preserve receipt order.
ALTER TABLE realm_direct RENAME TO prior_direct;
DROP INDEX realm_direct_live;
CREATE TABLE realm_direct (
  command_id TEXT NOT NULL PRIMARY KEY CHECK(typeof(command_id) = 'text' AND length(CAST(command_id AS BLOB)) = 32
    AND length(command_id) = 32 AND command_id NOT GLOB '*[^0-9a-f]*'),
  owner TEXT NOT NULL CHECK(owner IN ('actors','speech','step','canon','calendar','mail','decision')),
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

INSERT INTO realm_direct(rowid,command_id,owner,ref,kind,payload_sha256,guild,human,process_id,expects_job,state,reason,value,prepared_ms,dispatched_ms,settled_ms)
SELECT rowid,* FROM prior_direct;
DROP TABLE prior_direct;

-- Keep pending native cleanup journals when admitting the guild layer.
ALTER TABLE tg_applied RENAME TO prior_applied;
DROP INDEX tg_applied_open;
CREATE TABLE tg_applied (id INTEGER PRIMARY KEY, ts REAL NOT NULL, run TEXT NOT NULL,
  layer TEXT NOT NULL CHECK (layer IN ('standing','pooled','guild','boss','arm')), context TEXT NOT NULL,
  command TEXT NOT NULL, inverse TEXT NOT NULL, undone REAL);
INSERT INTO tg_applied SELECT * FROM prior_applied;
DROP TABLE prior_applied;
CREATE INDEX tg_applied_open ON tg_applied(undone);
