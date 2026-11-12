-- Discord attachments and the bot identity reserved for later server posts.
PRAGMA defer_foreign_keys=ON;
CREATE TABLE discord_message_new (
  id INTEGER PRIMARY KEY CHECK(id >= 1),
  channel TEXT NOT NULL CHECK(length(channel) BETWEEN 1 AND 40),   -- a layout id, or dm:<guid>
  thread INTEGER REFERENCES discord_thread(id),
  author INTEGER NOT NULL CHECK(typeof(author) = 'integer' AND author >= 0),
  origin TEXT NOT NULL CHECK(origin IN ('human','mind','bot')),
  text TEXT NOT NULL CHECK(length(text) <= 2000),
  reply_to INTEGER REFERENCES discord_message(id),
  ts REAL NOT NULL, edited REAL, deleted REAL, pinned REAL,
  bot TEXT CHECK(bot IS NULL OR length(bot) BETWEEN 1 AND 20),
  turn TEXT,                                                          -- the talk turn id for a mind line
  CHECK ((origin = 'bot') = (bot IS NOT NULL) AND (origin = 'bot') = (author = 0))
);
INSERT INTO discord_message_new(id,channel,thread,author,origin,text,reply_to,ts,edited,deleted,pinned,turn) SELECT id,channel,thread,author,origin,text,reply_to,ts,edited,deleted,pinned,turn FROM discord_message;
DROP TABLE discord_message;
ALTER TABLE discord_message_new RENAME TO discord_message;
CREATE INDEX discord_message_channel ON discord_message(channel,id);
CREATE INDEX discord_message_thread ON discord_message(thread,id) WHERE thread IS NOT NULL;
CREATE TABLE selfie_snap_new (
  id INTEGER PRIMARY KEY CHECK(id >= 1),
  kind TEXT NOT NULL CHECK(kind IN ('situation','guild')),
  guid INTEGER NOT NULL CHECK(typeof(guid) = 'integer' AND guid >= 0),  -- whose; 0 for a guild photo
  source TEXT NOT NULL CHECK(source IN ('life','birthday','everyday','raid')),
  ref TEXT NOT NULL CHECK(length(ref) BETWEEN 1 AND 40),  -- beat:<id>, birthday:<night>, everyday:<night>, raid:<raid_id>
  night TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('planned','ready','sent','expired')),
  ended TEXT CHECK(ended IS NULL OR ended IN ('passed','photo_changed','looks_changed','flagged','missing')),
  scene TEXT NOT NULL CHECK(json_valid(scene) AND length(scene) <= 2000),
  audience TEXT NOT NULL CHECK(audience IN ('him','anyone')),
  phases TEXT NOT NULL CHECK(json_valid(phases)),
  members TEXT CHECK(members IS NULL OR json_valid(members)),  -- a guild photo: [[guid, pick], ...] in tile order
  pick INTEGER, looks TEXT,  -- a situation: the chosen photo's pick and the looks' hash it was planned from
  created REAL NOT NULL, not_before REAL NOT NULL, expires REAL NOT NULL,
  rendered REAL, width INTEGER, height INTEGER, seed INTEGER, tries INTEGER, nsfw REAL,
  prompt TEXT CHECK(prompt IS NULL OR length(prompt) <= 1000),
  sent REAL, sender INTEGER, channel TEXT CHECK(channel IS NULL OR channel IN ('whisper','guild','discord','dm')),
  line_id INTEGER UNIQUE,
  UNIQUE(guid, ref), CHECK(expires > not_before),
  CHECK((kind = 'guild') = (guid = 0)), CHECK((kind = 'guild') = (members IS NOT NULL)),
  CHECK((kind = 'situation') = (pick IS NOT NULL AND looks IS NOT NULL)),
  CHECK((status = 'expired') = (ended IS NOT NULL)),
  CHECK(status NOT IN ('ready','sent') OR rendered IS NOT NULL),
  CHECK((status = 'sent') = (sent IS NOT NULL AND sender IS NOT NULL AND line_id IS NOT NULL)));
INSERT INTO selfie_snap_new SELECT * FROM selfie_snap;
DROP TABLE selfie_snap;
ALTER TABLE selfie_snap_new RENAME TO selfie_snap;
CREATE INDEX selfie_snap_open ON selfie_snap(status,expires) WHERE status IN ('planned','ready');
CREATE INDEX selfie_snap_sent ON selfie_snap(sent) WHERE status='sent';
CREATE TABLE discord_attachment (
  message INTEGER PRIMARY KEY REFERENCES discord_message(id),
  kind TEXT NOT NULL CHECK(kind IN ('upload','snap','meme')),
  path TEXT NOT NULL CHECK(length(path) BETWEEN 1 AND 200),
  width INTEGER NOT NULL CHECK(width BETWEEN 16 AND 8192),
  height INTEGER NOT NULL CHECK(height BETWEEN 16 AND 8192),
  bytes INTEGER NOT NULL CHECK(bytes BETWEEN 1 AND 4194304),
  snap INTEGER REFERENCES selfie_snap(id),
  look TEXT CHECK(look IS NULL OR length(look) <= 600),
  look_ts REAL,
  CHECK ((kind = 'snap') = (snap IS NOT NULL))
);
-- The rebuild runs inside migrate's transaction with FK enforcement on. Verify
-- the restored graph before clearing the deferred DROP TABLE constraint counters.
CREATE TEMP TABLE discord_photos_fk_check(ok INTEGER CHECK(ok=1));
INSERT INTO discord_photos_fk_check SELECT 0 FROM pragma_foreign_key_check;
DROP TABLE discord_photos_fk_check;
PRAGMA defer_foreign_keys=OFF;
