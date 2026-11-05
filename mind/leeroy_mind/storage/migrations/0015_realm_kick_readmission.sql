-- His yes to the raid lead's question may now lead to one guild_readmit. 0011 allowed no
-- command at all (admission_command_id IS NULL). A yes now takes the id of its one command,
-- and the row keeps that command's outcome: prepared (decided, not yet at the barrier),
-- dispatched (committed just before transport), admitted (native acked it), refused (never
-- sent, or native said no; the reason is kept) or unknown (it may have reached native; never
-- sent again). Every other column and check is 0011's. SQLite cannot change a CHECK in place,
-- so the table is rebuilt; every existing row is kept as it is, without an admission. No
-- table references it.
ALTER TABLE realm_kick_episode RENAME TO realm_kick_episode_v14;

CREATE TABLE realm_kick_episode (
  episode_id TEXT NOT NULL PRIMARY KEY CHECK(length(CAST(episode_id AS BLOB)) = length(episode_id) AND length(episode_id) = 69 AND substr(episode_id,1,5) = 'kick:'
    AND substr(episode_id,6) NOT GLOB '*[^0-9a-f]*'),
  source INTEGER NOT NULL UNIQUE REFERENCES realm_source(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(source) = 'integer' AND source > 0),
  guid INTEGER NOT NULL CHECK(typeof(guid) = 'integer' AND guid BETWEEN 1 AND 4294967295),
  guild INTEGER NOT NULL CHECK(typeof(guild) = 'integer' AND guild BETWEEN 1 AND 4294967295),
  human INTEGER NOT NULL CHECK(typeof(human) = 'integer' AND human BETWEEN 1 AND 4294967295),
  kick_ms INTEGER NOT NULL CHECK(typeof(kick_ms) = 'integer' AND kick_ms BETWEEN 1 AND 253402300799999),
  old_rank INTEGER NOT NULL CHECK(typeof(old_rank) = 'integer' AND old_rank BETWEEN 0 AND 255),
  lead INTEGER CHECK(lead IS NULL OR (typeof(lead) = 'integer' AND lead BETWEEN 1 AND 4294967295 AND lead != human AND lead != guid)),
  ask_id TEXT UNIQUE CHECK(ask_id IS NULL OR (typeof(ask_id) = 'text' AND length(CAST(ask_id AS BLOB)) = length(ask_id)
    AND length(ask_id) BETWEEN 1 AND 128 AND ask_id NOT GLOB '*[^A-Za-z0-9_-]*')),
  ask_line INTEGER REFERENCES chat_line(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(ask_line IS NULL OR (typeof(ask_line) = 'integer' AND ask_line > 0)),
  ask_text_sha256 TEXT CHECK(ask_text_sha256 IS NULL OR (typeof(ask_text_sha256) = 'text' AND length(ask_text_sha256) = 64 AND length(CAST(ask_text_sha256 AS BLOB)) = 64 AND ask_text_sha256 NOT GLOB '*[^0-9a-f]*')),
  ask_state TEXT NOT NULL CHECK(ask_state IN ('unasked','reserved','delivered','refused','unknown','expired')),
  ask_expires_ms INTEGER NOT NULL CHECK(typeof(ask_expires_ms) = 'integer' AND ask_expires_ms BETWEEN 1 AND 253402300799999),
  ask_process_id TEXT CHECK(ask_process_id IS NULL OR (typeof(ask_process_id) = 'text' AND length(CAST(ask_process_id AS BLOB)) BETWEEN 1 AND 256)),
  ask_generation INTEGER CHECK(ask_generation IS NULL OR (typeof(ask_generation) = 'integer' AND ask_generation >= 0)),
  ask_connection_epoch TEXT CHECK(ask_connection_epoch IS NULL OR (length(CAST(ask_connection_epoch AS BLOB)) = length(ask_connection_epoch)
    AND length(ask_connection_epoch) BETWEEN 1 AND 20
    AND ask_connection_epoch NOT GLOB '*[^0-9]*' AND substr(ask_connection_epoch,1,1) BETWEEN '1' AND '9'
    AND (length(ask_connection_epoch) < 20 OR ask_connection_epoch <= '18446744073709551615'))),
  attempted_ms INTEGER CHECK(attempted_ms IS NULL OR (typeof(attempted_ms) = 'integer' AND attempted_ms BETWEEN 0 AND 253402300799999)),
  dispatched_ms INTEGER CHECK(dispatched_ms IS NULL OR (typeof(dispatched_ms) = 'integer' AND dispatched_ms BETWEEN 0 AND 253402300799999)),
  ack_received_ms INTEGER CHECK(ack_received_ms IS NULL OR (typeof(ack_received_ms) = 'integer' AND ack_received_ms BETWEEN 1 AND 253402300799999)),
  ask_ack_ms INTEGER CHECK(ask_ack_ms IS NULL OR (typeof(ask_ack_ms) = 'integer' AND ask_ack_ms BETWEEN 1 AND 253402300799999)),
  delivered_ms INTEGER CHECK(delivered_ms IS NULL OR (typeof(delivered_ms) = 'integer' AND delivered_ms BETWEEN 1 AND 253402300799999)),
  reply_expires_ms INTEGER CHECK(reply_expires_ms IS NULL OR (typeof(reply_expires_ms) = 'integer' AND reply_expires_ms BETWEEN 1 AND 253402300799999)),
  answer_source INTEGER UNIQUE REFERENCES realm_source(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(answer_source IS NULL OR (typeof(answer_source) = 'integer' AND answer_source > 0)),
  answer TEXT CHECK(answer IS NULL OR answer IN ('yes','no')),
  answered_ms INTEGER CHECK(answered_ms IS NULL OR (typeof(answered_ms) = 'integer' AND answered_ms BETWEEN 0 AND 253402300799999)),
  consent_state TEXT NOT NULL CHECK(consent_state IN ('none','declined','expired','pending_native')),
  admission_command_id TEXT UNIQUE CHECK(admission_command_id IS NULL OR (typeof(admission_command_id) = 'text'
    AND length(CAST(admission_command_id AS BLOB)) = length(admission_command_id)
    AND length(admission_command_id) BETWEEN 1 AND 128 AND admission_command_id NOT GLOB '*[^A-Za-z0-9_-]*')),
  admission_state TEXT NOT NULL DEFAULT 'none'
    CHECK(admission_state IN ('none','prepared','dispatched','admitted','refused','unknown')),
  admission_reason TEXT CHECK(admission_reason IS NULL OR (typeof(admission_reason) = 'text'
    AND length(CAST(admission_reason AS BLOB)) = length(admission_reason)
    AND length(admission_reason) BETWEEN 1 AND 64 AND admission_reason NOT GLOB '*[^a-z0-9_]*')),
  admission_dispatched_ms INTEGER CHECK(admission_dispatched_ms IS NULL OR (typeof(admission_dispatched_ms) = 'integer'
    AND admission_dispatched_ms BETWEEN 1 AND 253402300799999)),
  admission_settled_ms INTEGER CHECK(admission_settled_ms IS NULL OR (typeof(admission_settled_ms) = 'integer'
    AND admission_settled_ms BETWEEN 1 AND 253402300799999)),
  CHECK(ask_expires_ms = kick_ms + 259200000),
  CHECK((lead IS NULL AND ask_id IS NULL AND ask_text_sha256 IS NULL AND ask_process_id IS NULL
      AND ask_generation IS NULL AND ask_connection_epoch IS NULL AND attempted_ms IS NULL)
    OR (lead IS NOT NULL AND ask_id IS NOT NULL AND ask_text_sha256 IS NOT NULL AND ask_process_id IS NOT NULL
      AND ask_generation IS NOT NULL AND ask_connection_epoch IS NOT NULL AND attempted_ms IS NOT NULL)),
  CHECK(ask_state NOT IN ('reserved','delivered','refused','unknown') OR lead IS NOT NULL),
  CHECK(ask_state != 'unasked' OR (lead IS NULL AND ask_line IS NULL AND dispatched_ms IS NULL
    AND ack_received_ms IS NULL AND ask_ack_ms IS NULL AND delivered_ms IS NULL AND reply_expires_ms IS NULL
    AND answer_source IS NULL AND answer IS NULL AND answered_ms IS NULL)),
  CHECK(lead IS NOT NULL OR (ask_line IS NULL AND dispatched_ms IS NULL AND ack_received_ms IS NULL
    AND ask_ack_ms IS NULL AND delivered_ms IS NULL AND reply_expires_ms IS NULL)),
  CHECK(dispatched_ms IS NULL OR (attempted_ms IS NOT NULL AND dispatched_ms >= attempted_ms AND dispatched_ms < ask_expires_ms)),
  CHECK(ask_ack_ms IS NULL OR (dispatched_ms IS NOT NULL AND dispatched_ms <= ask_ack_ms)),
  CHECK(ack_received_ms IS NULL OR (dispatched_ms IS NOT NULL AND dispatched_ms <= ack_received_ms)),
  CHECK(ask_ack_ms IS NULL OR ack_received_ms IS NULL OR ask_ack_ms <= ack_received_ms),
  CHECK((ask_line IS NULL AND delivered_ms IS NULL AND reply_expires_ms IS NULL)
    OR (ask_line IS NOT NULL AND delivered_ms IS NOT NULL AND reply_expires_ms IS NOT NULL
      AND dispatched_ms IS NOT NULL AND ask_ack_ms IS NOT NULL AND ack_received_ms IS NOT NULL
      AND dispatched_ms <= ask_ack_ms AND ask_ack_ms <= ack_received_ms
      AND delivered_ms = ask_ack_ms AND reply_expires_ms = delivered_ms + 259200000)),
  CHECK(ask_state != 'delivered' OR delivered_ms IS NOT NULL),
  CHECK(ask_state IN ('delivered','expired') OR delivered_ms IS NULL),
  CHECK((answer IS NULL AND answer_source IS NULL AND answered_ms IS NULL AND consent_state IN ('none','expired'))
    OR (answer IS NOT NULL AND answer_source IS NOT NULL AND answered_ms IS NOT NULL
      AND delivered_ms IS NOT NULL AND answered_ms >= delivered_ms AND answered_ms < reply_expires_ms
      AND ((answer = 'yes' AND consent_state = 'pending_native') OR (answer = 'no' AND consent_state = 'declined')))),
  -- Only his yes takes a command; a yes from before this migration keeps none.
  CHECK(admission_command_id IS NULL OR (answer IS NOT NULL AND answer = 'yes')),
  CHECK((admission_command_id IS NULL AND admission_state = 'none')
    OR (admission_command_id IS NOT NULL AND admission_state != 'none')),
  CHECK(admission_state NOT IN ('none','prepared') OR (admission_reason IS NULL
    AND admission_dispatched_ms IS NULL AND admission_settled_ms IS NULL)),
  CHECK(admission_state != 'dispatched' OR (admission_reason IS NULL
    AND admission_dispatched_ms IS NOT NULL AND admission_settled_ms IS NULL)),
  CHECK(admission_state != 'admitted' OR (admission_reason IS NULL
    AND admission_dispatched_ms IS NOT NULL AND admission_settled_ms IS NOT NULL)),
  CHECK(admission_state != 'refused' OR (admission_reason IS NOT NULL AND admission_settled_ms IS NOT NULL)),
  CHECK(admission_state != 'unknown' OR (admission_reason IS NOT NULL
    AND admission_dispatched_ms IS NOT NULL AND admission_settled_ms IS NOT NULL)),
  CHECK(admission_dispatched_ms IS NULL OR admission_settled_ms IS NULL
    OR admission_dispatched_ms <= admission_settled_ms)
);

INSERT INTO realm_kick_episode(episode_id,source,guid,guild,human,kick_ms,old_rank,lead,ask_id,ask_line,
    ask_text_sha256,ask_state,ask_expires_ms,ask_process_id,ask_generation,ask_connection_epoch,
    attempted_ms,dispatched_ms,ack_received_ms,ask_ack_ms,delivered_ms,reply_expires_ms,answer_source,
    answer,answered_ms,consent_state,admission_command_id)
  SELECT episode_id,source,guid,guild,human,kick_ms,old_rank,lead,ask_id,ask_line,
    ask_text_sha256,ask_state,ask_expires_ms,ask_process_id,ask_generation,ask_connection_epoch,
    attempted_ms,dispatched_ms,ack_received_ms,ask_ack_ms,delivered_ms,reply_expires_ms,answer_source,
    answer,answered_ms,consent_state,admission_command_id
  FROM realm_kick_episode_v14 ORDER BY rowid;

DROP TABLE realm_kick_episode_v14;

CREATE UNIQUE INDEX realm_kick_episode_active_question ON realm_kick_episode(guild,human,lead)
  WHERE answer IS NULL AND ask_state IN ('reserved','delivered','unknown');
