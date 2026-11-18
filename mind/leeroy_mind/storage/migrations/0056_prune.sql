-- Retention (storage/prune.py) and the reads of game_event that scanned it.
--
-- game_event had one index, on ts. "A type's rows in a window" and "her zone events" read every
-- row of the window, "the newest rows of a type" (ORDER BY id) and "the session's last event"
-- the whole table, all on the event loop. (type, ts) serves the windows, (type) the newest of a
-- type in id order, (session_id, ts) the session's (most rows have no session).
CREATE INDEX IF NOT EXISTS game_event_type_ts ON game_event(type, ts);
CREATE INDEX IF NOT EXISTS game_event_type ON game_event(type);
CREATE INDEX IF NOT EXISTS game_event_session ON game_event(session_id, ts)
  WHERE session_id IS NOT NULL;
-- A row can only be deleted once SQLite has looked for a child that points at it. Without these
-- two, every pruned game_event row scanned realm_source and every source scanned
-- realm_fact_origin.
CREATE INDEX IF NOT EXISTS realm_source_game_event ON realm_source(game_event_id)
  WHERE game_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS realm_fact_origin_source ON realm_fact_origin(source);
