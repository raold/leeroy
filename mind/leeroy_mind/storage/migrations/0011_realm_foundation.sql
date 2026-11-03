-- Source-bound foundation storage. Existing notable rows remain permanent cap tombstones.
-- Canonical semantic JSON/key sets and cross-row identity are rechecked by typed helpers.
CREATE TABLE realm_source (
  id INTEGER NOT NULL PRIMARY KEY CHECK(id > 0),
  source_key TEXT NOT NULL UNIQUE CHECK(typeof(source_key) = 'text' AND length(source_key) = 64 AND length(CAST(source_key AS BLOB)) = 64 AND source_key NOT GLOB '*[^0-9a-f]*'),
  protocol_version INTEGER NOT NULL CHECK(typeof(protocol_version) = 'integer' AND protocol_version = 1),
  event_type TEXT NOT NULL CHECK(typeof(event_type) = 'text' AND length(CAST(event_type AS BLOB)) = length(event_type) AND length(event_type) BETWEEN 1 AND 128 AND event_type NOT GLOB '*[^a-z0-9_]*'),
  game_event_id INTEGER REFERENCES game_event(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(game_event_id IS NULL OR (typeof(game_event_id) = 'integer' AND game_event_id > 0)),
  chat_line_id INTEGER REFERENCES chat_line(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(chat_line_id IS NULL OR (typeof(chat_line_id) = 'integer' AND chat_line_id > 0)),
  row_sha256 TEXT NOT NULL CHECK(typeof(row_sha256) = 'text' AND length(row_sha256) = 64 AND length(CAST(row_sha256 AS BLOB)) = 64 AND row_sha256 NOT GLOB '*[^0-9a-f]*'),
  server_ms INTEGER NOT NULL CHECK(typeof(server_ms) = 'integer' AND server_ms BETWEEN 1 AND 253402300799999),
  received_at REAL NOT NULL CHECK(typeof(received_at) IN ('integer','real') AND received_at BETWEEN 0 AND 253402300799.999),
  guild INTEGER NOT NULL CHECK(typeof(guild) = 'integer' AND guild BETWEEN 1 AND 4294967295),
  human INTEGER NOT NULL CHECK(typeof(human) = 'integer' AND human BETWEEN 1 AND 4294967295),
  top_keys_json TEXT NOT NULL CHECK(CASE WHEN json_valid(top_keys_json) THEN json_type(top_keys_json) = 'array'
    AND json(top_keys_json) = top_keys_json AND json_array_length(top_keys_json) <= 64
    AND length(CAST(top_keys_json AS BLOB)) <= 8192 ELSE 0 END),
  CHECK((game_event_id IS NOT NULL) + (chat_line_id IS NOT NULL) = 1)
);

CREATE TABLE realm_fact_origin (
  fact INTEGER NOT NULL PRIMARY KEY REFERENCES realm_fact(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(fact) = 'integer' AND fact > 0),
  producer_key TEXT NOT NULL UNIQUE,
  source INTEGER NOT NULL REFERENCES realm_source(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(source) = 'integer' AND source > 0),
  policy TEXT NOT NULL CHECK(policy IN ('verified_raid_kill_v1','bg_result_v1','arena_result_v1',
    'wg_result_v1','pvp_result_v1','channel_raid_topic_v1')),
  assurance TEXT NOT NULL CHECK((policy = 'verified_raid_kill_v1' AND assurance = 'verified_event')
    OR (policy IN ('bg_result_v1','arena_result_v1','wg_result_v1','pvp_result_v1') AND assurance = 'observed_event')
    OR (policy = 'channel_raid_topic_v1' AND assurance = 'observed_topic')),
  proof_json TEXT NOT NULL CHECK(CASE WHEN json_valid(proof_json) THEN json_type(proof_json) = 'object'
    AND json(proof_json) = proof_json AND length(CAST(proof_json AS BLOB)) <= 32768 ELSE 0 END),
  qualification_json TEXT NOT NULL CHECK(CASE WHEN json_valid(qualification_json) THEN json_type(qualification_json) = 'object'
    AND json(qualification_json) = qualification_json AND length(CAST(qualification_json AS BLOB)) <= 8192 ELSE 0 END),
  text_sha256 TEXT NOT NULL CHECK(typeof(text_sha256) = 'text' AND length(text_sha256) = 64 AND length(CAST(text_sha256 AS BLOB)) = 64 AND text_sha256 NOT GLOB '*[^0-9a-f]*'),
  revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision > 0),
  state TEXT NOT NULL CHECK(state IN ('active','revoked')),
  CHECK(length(CAST(producer_key AS BLOB)) = length(producer_key)
    AND substr(producer_key,1,6) = 'event:' AND substr(producer_key,71,1) = ':'
    AND length(substr(producer_key,7,64)) = 64 AND substr(producer_key,7,64) NOT GLOB '*[^0-9a-f]*'
    AND substr(producer_key,72) = policy)
) WITHOUT ROWID;

CREATE TABLE realm_kill (
  kill_id TEXT NOT NULL PRIMARY KEY,
  source INTEGER NOT NULL UNIQUE REFERENCES realm_source(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(source) = 'integer' AND source > 0),
  native_boot TEXT NOT NULL CHECK(typeof(native_boot) = 'text' AND length(native_boot) = 32 AND length(CAST(native_boot AS BLOB)) = 32 AND native_boot NOT GLOB '*[^0-9a-f]*'),
  native_epoch TEXT NOT NULL CHECK(length(CAST(native_epoch AS BLOB)) = length(native_epoch) AND length(native_epoch) BETWEEN 1 AND 20
    AND native_epoch NOT GLOB '*[^0-9]*' AND substr(native_epoch,1,1) BETWEEN '1' AND '9'
    AND (length(native_epoch) < 20 OR native_epoch <= '18446744073709551615')),
  guild INTEGER NOT NULL CHECK(typeof(guild) = 'integer' AND guild BETWEEN 1 AND 4294967295),
  human INTEGER NOT NULL CHECK(typeof(human) = 'integer' AND human BETWEEN 1 AND 4294967295),
  map INTEGER NOT NULL CHECK(typeof(map) = 'integer' AND map BETWEEN 0 AND 4294967295),
  instance_id INTEGER NOT NULL CHECK(typeof(instance_id) = 'integer' AND instance_id > 0),
  boss_id INTEGER NOT NULL CHECK(typeof(boss_id) = 'integer' AND boss_id BETWEEN 0 AND 4294967295),
  difficulty INTEGER NOT NULL CHECK(typeof(difficulty) = 'integer' AND difficulty IN (-1,0,1,2,3)),
  size INTEGER NOT NULL CHECK(typeof(size) = 'integer' AND size IN (10,25)),
  heroic INTEGER NOT NULL CHECK(typeof(heroic) = 'integer' AND heroic IN (0,1)),
  killed_ms INTEGER NOT NULL CHECK(typeof(killed_ms) = 'integer' AND killed_ms BETWEEN 1 AND 253402300799999),
  verified_ms INTEGER NOT NULL CHECK(typeof(verified_ms) = 'integer' AND verified_ms BETWEEN 1 AND 253402300799999),
  published_ms INTEGER NOT NULL CHECK(typeof(published_ms) = 'integer' AND published_ms BETWEEN 1 AND 253402300799999),
  native_cast_json TEXT NOT NULL CHECK(CASE WHEN json_valid(native_cast_json) THEN json_type(native_cast_json) = 'array'
    AND json(native_cast_json) = native_cast_json AND json_array_length(native_cast_json) <= 128
    AND length(CAST(native_cast_json AS BLOB)) <= 2048 ELSE 0 END),
  human_present INTEGER NOT NULL CHECK(typeof(human_present) = 'integer' AND human_present IN (0,1)),
  qualified_cast_json TEXT NOT NULL CHECK(CASE WHEN json_valid(qualified_cast_json) THEN json_type(qualified_cast_json) = 'array'
    AND json(qualified_cast_json) = qualified_cast_json AND json_array_length(qualified_cast_json) <= 128
    AND length(CAST(qualified_cast_json AS BLOB)) <= 2048 ELSE 0 END),
  qualification_sha256 TEXT NOT NULL CHECK(typeof(qualification_sha256) = 'text' AND length(qualification_sha256) = 64 AND length(CAST(qualification_sha256 AS BLOB)) = 64 AND qualification_sha256 NOT GLOB '*[^0-9a-f]*'),
  fact INTEGER UNIQUE REFERENCES realm_fact(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(fact IS NULL OR (typeof(fact) = 'integer' AND fact > 0)),
  CHECK(length(CAST(kill_id AS BLOB)) = length(kill_id)
    AND length(kill_id) BETWEEN 34 AND 53 AND substr(kill_id,1,33) = native_boot || ':'
    AND substr(kill_id,34) NOT GLOB '*[^0-9]*' AND substr(kill_id,34,1) BETWEEN '1' AND '9'
    AND (length(substr(kill_id,34)) < 20 OR substr(kill_id,34) <= '18446744073709551615')),
  CHECK(killed_ms <= verified_ms AND verified_ms <= published_ms),
  CHECK(difficulty = -1 OR (difficulty = 0 AND size = 10 AND heroic = 0)
    OR (difficulty = 1 AND size = 25 AND heroic = 0) OR (difficulty = 2 AND size = 10 AND heroic = 1)
    OR (difficulty = 3 AND size = 25 AND heroic = 1))
);

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
  first TEXT NOT NULL CHECK(first = 'week'),
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
  CHECK(state != 'sent' OR (dispatched_at IS NOT NULL AND settled_at IS NOT NULL)),
  CHECK(state != 'dispatched' OR dispatched_at IS NOT NULL),
  CHECK(state NOT IN ('expired','abandoned') OR dispatched_at IS NULL)
);

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
  admission_command_id TEXT UNIQUE CHECK(admission_command_id IS NULL),
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
      AND ((answer = 'yes' AND consent_state = 'pending_native') OR (answer = 'no' AND consent_state = 'declined'))))
);
CREATE UNIQUE INDEX realm_kick_episode_active_question ON realm_kick_episode(guild,human,lead)
  WHERE answer IS NULL AND ask_state IN ('reserved','delivered','unknown');

CREATE TABLE realm_memory_dependency (
  memory INTEGER NOT NULL REFERENCES memory(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(memory) = 'integer' AND memory > 0),
  fact INTEGER NOT NULL REFERENCES realm_fact(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(fact) = 'integer' AND fact > 0),
  PRIMARY KEY(memory,fact)
);

CREATE TABLE realm_speech_line (
  chat_line INTEGER NOT NULL PRIMARY KEY REFERENCES chat_line(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(chat_line) = 'integer' AND chat_line > 0),
  utterance INTEGER NOT NULL UNIQUE REFERENCES utterance(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(utterance) = 'integer' AND utterance > 0)
) WITHOUT ROWID;
