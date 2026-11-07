-- Conversation variety, topic memory: what each member did with each counterpart, as moves and
-- topics (never the raw line, bar the final clause), so a later prompt can tell her what she
-- already did with him tonight. A whisper row (channel 'whisper', to_guid the addressee) is read
-- back only into a whisper between the same two (memory.recall.whisper_ok).
-- talk_move: one row per turn. moves is a sorted JSON list of move names, topics a JSON list.
-- pair_note: short scene notes per pair, at most 80 characters.
CREATE TABLE talk_move (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  guid INTEGER NOT NULL,
  counterpart INTEGER NOT NULL,
  channel TEXT NOT NULL,
  to_guid INTEGER NOT NULL DEFAULT 0,
  moves TEXT NOT NULL DEFAULT '[]',
  main TEXT NOT NULL DEFAULT '',
  topics TEXT NOT NULL DEFAULT '[]',
  clause TEXT NOT NULL DEFAULT '',
  angle TEXT NOT NULL DEFAULT ''
);
CREATE INDEX talk_move_pair ON talk_move(guid, counterpart, ts);

CREATE TABLE pair_note (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  guid INTEGER NOT NULL,
  other INTEGER NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL CHECK (length(text) <= 80),
  channel TEXT NOT NULL,
  to_guid INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX pair_note_pair ON pair_note(guid, other, ts);
