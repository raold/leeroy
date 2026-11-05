-- Durable Realm action ownership, separate from bounded diagnostic receipts.
-- Unsigned native epochs/values live in validated lossless JSON decimal numbers;
-- connection_epoch is decimal TEXT, never a signed SQLite integer conversion.
CREATE TABLE realm_action (
  seq INTEGER PRIMARY KEY,
  command_id TEXT NOT NULL UNIQUE,
  parent_id TEXT REFERENCES realm_action(command_id),
  role TEXT NOT NULL CHECK(role IN ('root','query','cleanup')),
  policy TEXT NOT NULL CHECK(policy IN ('ordinary','defaults','wg','ack','result','query','cleanup')),
  owner TEXT NOT NULL,
  rank INTEGER NOT NULL CHECK(rank BETWEEN 0 AND 4),
  kind TEXT NOT NULL,
  payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB)) <= 65536),
  reserved_manifest TEXT NOT NULL,
  native_manifest TEXT,
  guild INTEGER NOT NULL,
  human INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','dispatched','unknown','conflict','completed','not_sent')),
  transport_state TEXT NOT NULL CHECK(transport_state IN ('not_entered','inflight','settled')),
  process_id TEXT NOT NULL DEFAULT '',
  connection_epoch TEXT NOT NULL DEFAULT '0',
  send_error TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '{}' CHECK(length(CAST(evidence AS BLOB)) <= 32768),
  result_ok INTEGER CHECK(result_ok IN (0,1)),
  result_reason TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL,
  updated REAL NOT NULL,
  dispatched REAL
);
CREATE INDEX realm_action_parent ON realm_action(parent_id,role,seq);
CREATE UNIQUE INDEX realm_action_query ON realm_action(parent_id) WHERE role='query';
CREATE INDEX realm_action_state ON realm_action(state,role,seq);
