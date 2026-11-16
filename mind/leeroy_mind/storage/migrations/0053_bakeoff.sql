-- The bake-off's captured turns (bakeoff/README.md). With [bakeoff] capture = true the talk
-- pipeline stores each sent model turn here, after the line is recorded, so `bakeoff run` can ask
-- other models the same thing with the mind stopped. One row per generation that became a line:
-- the call whose words were sent, and, when the pipeline asked more than once (a silence asked
-- again, a retry for its tells, a guard's rewrite, an owed fallback), the generation's first call
-- too, before any retry hint. A private answer's beat ([talk] think_private) is never stored: the
-- requests are kept as they were before its lines, with only whether it had one and its want
-- code. Private: the messages hold persona text and his private lines. Never exported; pruned
-- 14 days after the turn.
CREATE TABLE bakeoff_turn (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,                 -- unix seconds, when its first line was recorded
  line_id INTEGER NOT NULL,         -- chat_line id of its first line
  discord_id INTEGER,               -- that line's discord_message id, for a Discord turn
  refs TEXT NOT NULL,               -- JSON list, every line it became: 'c:<chat_line>' or 'd:<discord_message>'
  guid INTEGER NOT NULL,
  channel TEXT NOT NULL,            -- the talk channel id: guild, whisper, dm:<guid>, discord:<channel> ...
  kind TEXT NOT NULL,               -- the talk turn kind
  calls INTEGER NOT NULL,           -- the model calls the generation made before its line (1: no retry)
  attempt INTEGER NOT NULL,         -- which of them was sent: 0 the first draft, else a later call
  beat INTEGER NOT NULL DEFAULT 0,  -- 1: the live turn had a beat (its lines are not stored)
  want TEXT,                        -- the beat's want code, when it had one
  -- the call whose words were sent
  model TEXT NOT NULL,
  think INTEGER,                    -- the request's think flag: 0, 1, or NULL when not sent
  options TEXT NOT NULL,            -- JSON: the request's options (sampling, stop, num_predict, num_ctx)
  messages TEXT NOT NULL,           -- JSON list of {role, content}
  reply TEXT NOT NULL,              -- the model's reply as it came, before the pipeline's repairs
  done_reason TEXT,                 -- Ollama's done_reason ("length": cut by num_predict)
  -- the generation's first call, when a later one was sent (attempt > 0); all NULL otherwise
  first_model TEXT,
  first_think INTEGER,
  first_options TEXT,
  first_messages TEXT,
  first_reply TEXT,
  first_done_reason TEXT,
  shape TEXT NOT NULL               -- JSON: max_lines, words (a Discord turn's length), two_lines
);
CREATE INDEX bakeoff_turn_ts ON bakeoff_turn(ts);
