-- The guild's Discord (spec 2026-11-07). Only the running mind writes these; the console reads them.
CREATE TABLE discord_message (
  id INTEGER PRIMARY KEY CHECK(id >= 1),
  channel TEXT NOT NULL CHECK(length(channel) BETWEEN 1 AND 40),   -- a layout id, or dm:<guid>
  thread INTEGER REFERENCES discord_thread(id),
  author INTEGER NOT NULL CHECK(typeof(author) = 'integer' AND author >= 1),
  origin TEXT NOT NULL CHECK(origin IN ('human','mind')),
  text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 2000),
  reply_to INTEGER REFERENCES discord_message(id),
  ts REAL NOT NULL, edited REAL, deleted REAL, pinned REAL,
  turn TEXT                                                           -- the talk turn id for a mind line
);
CREATE INDEX discord_message_channel ON discord_message(channel, id);
CREATE INDEX discord_message_thread ON discord_message(thread, id) WHERE thread IS NOT NULL;
CREATE TABLE discord_thread (
  id INTEGER PRIMARY KEY CHECK(id >= 1),
  channel TEXT NOT NULL, parent INTEGER NOT NULL UNIQUE REFERENCES discord_message(id),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 60), created REAL NOT NULL);
CREATE TABLE discord_reaction (
  message INTEGER NOT NULL REFERENCES discord_message(id), emoji TEXT NOT NULL CHECK(length(emoji) BETWEEN 1 AND 16),
  member INTEGER NOT NULL, ts REAL NOT NULL, PRIMARY KEY (message, emoji, member));
