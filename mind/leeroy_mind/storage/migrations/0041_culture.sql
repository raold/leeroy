ALTER TABLE lexicon ADD COLUMN butt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE lexicon ADD COLUMN origin_kind TEXT NOT NULL DEFAULT 'line' CHECK (origin_kind IN ('line','event'));
ALTER TABLE lexicon ADD COLUMN origin_refs TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(origin_refs));
ALTER TABLE lexicon ADD COLUMN uses_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE lexicon ADD COLUMN retired REAL;
CREATE TABLE discord_meme (
  id INTEGER PRIMARY KEY, night TEXT NOT NULL, joke INTEGER REFERENCES lexicon(id),
  base TEXT NOT NULL CHECK (base IN ('scene','snap')), snap INTEGER REFERENCES selfie_snap(id),
  scene TEXT, top TEXT NOT NULL CHECK (length(top) <= 60), bottom TEXT NOT NULL CHECK (length(bottom) <= 60),
  status TEXT NOT NULL CHECK (status IN ('planned','ready','posted','refused')),
  path TEXT, nsfw REAL, created REAL NOT NULL, posted_message INTEGER REFERENCES discord_message(id));
