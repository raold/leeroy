-- Situational and guild photos (P4). Only the running mind writes these (its night stages, and its
-- talk loop when a photo goes out); the console reads them. Images live in mind-data/selfies/.
CREATE TABLE selfie_snap (
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
  sent REAL, sender INTEGER, channel TEXT CHECK(channel IS NULL OR channel IN ('whisper','guild')),
  line_id INTEGER UNIQUE,
  UNIQUE(guid, ref), CHECK(expires > not_before),
  CHECK((kind = 'guild') = (guid = 0)), CHECK((kind = 'guild') = (members IS NOT NULL)),
  CHECK((kind = 'situation') = (pick IS NOT NULL AND looks IS NOT NULL)),
  CHECK((status = 'expired') = (ended IS NOT NULL)),
  CHECK(status NOT IN ('ready','sent') OR rendered IS NOT NULL),
  CHECK((status = 'sent') = (sent IS NOT NULL AND sender IS NOT NULL AND line_id IS NOT NULL)));
CREATE INDEX selfie_snap_open ON selfie_snap(status, expires) WHERE status IN ('planned','ready');
CREATE INDEX selfie_snap_sent ON selfie_snap(sent) WHERE status = 'sent';
