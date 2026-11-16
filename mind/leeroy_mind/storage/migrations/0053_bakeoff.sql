-- The bake-off's captured turns (bakeoff/README.md). With [bakeoff] capture = true the talk
-- pipeline stores each sent model turn's exact request here, after the line is recorded, so
-- `bakeoff run` can ask other models the same thing with the mind stopped. Private: the messages
-- hold persona text and his private lines. Never exported; pruned 14 days after the turn.
CREATE TABLE bakeoff_turn (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,                 -- unix seconds, when its first line was recorded
  line_id INTEGER NOT NULL,         -- chat_line id of its first line
  discord_id INTEGER,               -- that line's discord_message id, for a Discord turn
  refs TEXT NOT NULL,               -- JSON list, every line it became: 'c:<chat_line>' or 'd:<discord_message>'
  guid INTEGER NOT NULL,
  channel TEXT NOT NULL,            -- the talk channel id: guild, whisper, dm:<guid>, discord:<channel> ...
  kind TEXT NOT NULL,               -- the talk turn kind
  model TEXT NOT NULL,
  think INTEGER,                    -- the request's think flag: 0, 1, or NULL when not sent
  options TEXT NOT NULL,            -- JSON: the request's options (sampling, stop, num_predict, num_ctx)
  messages TEXT NOT NULL,           -- JSON list of {role, content}
  reply TEXT NOT NULL,              -- the model's reply as it came, before the pipeline's repairs
  shape TEXT NOT NULL               -- JSON: max_lines, words (a Discord turn's length), two_lines
);
CREATE INDEX bakeoff_turn_ts ON bakeoff_turn(ts);
