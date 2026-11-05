-- Native evidence for three more public facts: realm-first achievements, duels outside cities
-- and verified 5-man guild kills. Their origins need three new policies. SQLite cannot change
-- a CHECK in place, so realm_fact_origin is rebuilt and every existing row is kept as it is;
-- no table references it.
ALTER TABLE realm_fact_origin RENAME TO realm_fact_origin_v13;

CREATE TABLE realm_fact_origin (
  fact INTEGER NOT NULL PRIMARY KEY REFERENCES realm_fact(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(fact) = 'integer' AND fact > 0),
  producer_key TEXT NOT NULL UNIQUE,
  source INTEGER NOT NULL REFERENCES realm_source(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(source) = 'integer' AND source > 0),
  policy TEXT NOT NULL CHECK(policy IN ('verified_raid_kill_v1','bg_result_v1','arena_result_v1',
    'wg_result_v1','pvp_result_v1','channel_raid_topic_v1','realm_first_achievement_v1',
    'duel_result_v1','verified_dungeon_kill_v1')),
  assurance TEXT NOT NULL CHECK((policy IN ('verified_raid_kill_v1','verified_dungeon_kill_v1') AND assurance = 'verified_event')
    OR (policy IN ('bg_result_v1','arena_result_v1','wg_result_v1','pvp_result_v1',
      'realm_first_achievement_v1','duel_result_v1') AND assurance = 'observed_event')
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

INSERT INTO realm_fact_origin(fact,producer_key,source,policy,assurance,proof_json,qualification_json,
    text_sha256,revision,state)
  SELECT fact,producer_key,source,policy,assurance,proof_json,qualification_json,
    text_sha256,revision,state
  FROM realm_fact_origin_v13 ORDER BY fact;

DROP TABLE realm_fact_origin_v13;

-- A verified 5-man guild kill: him present, or at least five qualified cast members. It is
-- stored only with its public fact, and it is kept apart from realm_kill, so it is never an
-- announcement's kill and never suppresses or decides a raid announcement.
CREATE TABLE realm_dungeon_kill (
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
  difficulty INTEGER NOT NULL CHECK(typeof(difficulty) = 'integer' AND difficulty IN (0,1)),
  size INTEGER NOT NULL CHECK(typeof(size) = 'integer' AND size = 5),
  heroic INTEGER NOT NULL CHECK(typeof(heroic) = 'integer' AND heroic = difficulty),
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
  fact INTEGER NOT NULL UNIQUE REFERENCES realm_fact(id) ON DELETE NO ACTION ON UPDATE NO ACTION
    CHECK(typeof(fact) = 'integer' AND fact > 0),
  CHECK(length(CAST(kill_id AS BLOB)) = length(kill_id)
    AND length(kill_id) BETWEEN 34 AND 53 AND substr(kill_id,1,33) = native_boot || ':'
    AND substr(kill_id,34) NOT GLOB '*[^0-9]*' AND substr(kill_id,34,1) BETWEEN '1' AND '9'
    AND (length(substr(kill_id,34)) < 20 OR substr(kill_id,34) <= '18446744073709551615')),
  CHECK(killed_ms <= verified_ms AND verified_ms <= published_ms),
  CHECK(human_present = 1 OR CASE WHEN json_valid(qualified_cast_json)
    THEN json_array_length(qualified_cast_json) >= 5 ELSE 0 END)
);
