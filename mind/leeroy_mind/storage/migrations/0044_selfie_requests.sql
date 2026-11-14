-- Requested photos retain the owner's post and their delivery receipts across restart.
PRAGMA defer_foreign_keys=ON;
CREATE TABLE selfie_snap_new (
  id INTEGER PRIMARY KEY CHECK(id >= 1),
  kind TEXT NOT NULL CHECK(kind IN ('situation','guild')),
  guid INTEGER NOT NULL CHECK(typeof(guid) = 'integer' AND guid >= 0),
  source TEXT NOT NULL CHECK(source IN ('life','birthday','everyday','raid','request')),
  ref TEXT NOT NULL CHECK(length(ref) BETWEEN 1 AND 40),
  night TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('planned','ready','sent','expired')),
  ended TEXT CHECK(ended IS NULL OR ended IN ('passed','photo_changed','looks_changed','flagged','missing')),
  scene TEXT NOT NULL CHECK(json_valid(scene) AND length(scene) <= 2000),
  audience TEXT NOT NULL CHECK(audience IN ('him','anyone')),
  phases TEXT NOT NULL CHECK(json_valid(phases)),
  members TEXT CHECK(members IS NULL OR json_valid(members)),
  pick INTEGER, looks TEXT,
  created REAL NOT NULL, not_before REAL NOT NULL, expires REAL NOT NULL,
  rendered REAL, width INTEGER, height INTEGER, seed INTEGER, tries INTEGER, nsfw REAL,
  prompt TEXT CHECK(prompt IS NULL OR length(prompt) <= 1000),
  sent REAL, sender INTEGER, channel TEXT CHECK(channel IS NULL OR channel IN ('whisper','guild','discord','dm')),
  line_id INTEGER UNIQUE,
  request INTEGER REFERENCES discord_message(id),
  UNIQUE(guid, ref), CHECK(expires > not_before),
  CHECK((kind = 'guild') = (guid = 0)), CHECK((kind = 'guild') = (members IS NOT NULL)),
  CHECK((kind = 'situation') = (pick IS NOT NULL AND looks IS NOT NULL)),
  CHECK((status = 'expired') = (ended IS NOT NULL)),
  CHECK(status NOT IN ('ready','sent') OR rendered IS NOT NULL),
  CHECK((status = 'sent') = (sent IS NOT NULL AND sender IS NOT NULL AND line_id IS NOT NULL)),
  CHECK((source = 'request') = (request IS NOT NULL)),
  CHECK(source != 'request' OR kind = 'situation'));
INSERT INTO selfie_snap_new SELECT *, NULL FROM selfie_snap;
DROP TABLE selfie_snap;
ALTER TABLE selfie_snap_new RENAME TO selfie_snap;
CREATE INDEX selfie_snap_open ON selfie_snap(status,expires) WHERE status IN ('planned','ready');
CREATE INDEX selfie_snap_sent ON selfie_snap(sent) WHERE status='sent';
CREATE INDEX selfie_snap_request ON selfie_snap(request) WHERE request IS NOT NULL;

CREATE TABLE selfie_request (
  message INTEGER PRIMARY KEY REFERENCES discord_message(id),
  guid INTEGER NOT NULL CHECK(typeof(guid)='integer' AND guid>0),
  asked_channel TEXT NOT NULL CHECK(length(asked_channel) BETWEEN 1 AND 40),
  thread INTEGER REFERENCES discord_thread(id),
  photo_channel TEXT NOT NULL CHECK(length(photo_channel) BETWEEN 1 AND 40),
  audience TEXT NOT NULL CHECK(audience IN ('him','anyone')),
  created REAL NOT NULL, expires REAL NOT NULL CHECK(expires>created),
  fresh_snap INTEGER UNIQUE REFERENCES selfie_snap(id),
  fallback_snap INTEGER UNIQUE REFERENCES selfie_snap(id),
  initial_message INTEGER UNIQUE REFERENCES discord_message(id),
  fresh_message INTEGER UNIQUE REFERENCES discord_message(id),
  fallback_message INTEGER UNIQUE REFERENCES discord_message(id),
  followup_message INTEGER UNIQUE REFERENCES discord_message(id),
  failure_message INTEGER UNIQUE REFERENCES discord_message(id),
  failure TEXT CHECK(failure IS NULL OR length(failure) BETWEEN 1 AND 60),
  ended TEXT CHECK(ended IS NULL OR ended IN ('passed','cancelled')),
  CHECK(audience!='him' OR photo_channel='dm:'||guid),
  CHECK(audience!='anyone' OR substr(photo_channel,1,3)!='dm:'),
  CHECK(fresh_snap IS NULL OR fallback_snap IS NULL OR fresh_snap!=fallback_snap));
CREATE INDEX selfie_request_member ON selfie_request(guid,created);
CREATE INDEX selfie_request_pending ON selfie_request(created) WHERE fresh_message IS NULL;
CREATE TABLE selfie_request_borrow (
  id INTEGER PRIMARY KEY,
  request INTEGER NOT NULL REFERENCES selfie_request(message),
  started REAL NOT NULL,
  local_date TEXT NOT NULL CHECK(length(local_date)=10));
CREATE INDEX selfie_request_borrow_time ON selfie_request_borrow(started);
CREATE INDEX selfie_request_borrow_day ON selfie_request_borrow(local_date);
-- Every copied fallback keeps its original image identity, independent of the copy's id.
CREATE TABLE selfie_request_image (
  snap INTEGER PRIMARY KEY REFERENCES selfie_snap(id),
  identity TEXT NOT NULL CHECK(length(identity) BETWEEN 1 AND 100),
  digest TEXT NOT NULL CHECK(length(digest)=64));
CREATE INDEX selfie_request_image_identity ON selfie_request_image(identity);
CREATE INDEX selfie_request_image_digest ON selfie_request_image(digest);

CREATE TEMP TABLE selfie_requests_fk_check(ok INTEGER CHECK(ok=1));
INSERT INTO selfie_requests_fk_check SELECT 0 FROM pragma_foreign_key_check;
DROP TABLE selfie_requests_fk_check;
PRAGMA defer_foreign_keys=OFF;
