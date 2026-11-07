-- A member's own claims; never a source of facts about another speaker.
CREATE TABLE member_claim (
    id INTEGER PRIMARY KEY,
    guid INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fact','opinion','plan')),
    subject TEXT NOT NULL CHECK (length(trim(subject)) BETWEEN 1 AND 60),
    text TEXT NOT NULL CHECK (length(trim(text)) BETWEEN 1 AND 200),
    polarity INTEGER NOT NULL CHECK (polarity IN (-1,0,1)),
    source TEXT NOT NULL CHECK (source IN ('chat','owner')),
    source_line INTEGER REFERENCES chat_line(id),
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    seen INTEGER NOT NULL DEFAULT 1 CHECK (seen > 0),
    expires REAL,
    superseded_by INTEGER REFERENCES member_claim(id),
    embedding BLOB NOT NULL CHECK (length(embedding) = 2048),
    CHECK (expires IS NULL OR kind = 'plan')
);
CREATE INDEX member_claim_subject ON member_claim(guid, subject);
CREATE INDEX member_claim_live ON member_claim(guid, superseded_by, expires);
CREATE INDEX member_claim_expiry ON member_claim(expires) WHERE expires IS NOT NULL;
CREATE VIRTUAL TABLE member_claim_vec USING vec0(
    claim_id INTEGER PRIMARY KEY,
    guid INTEGER PARTITION KEY,
    embedding float[512] distance_metric=cosine,
    chunk_size=16
);
CREATE TABLE member_claim_cursor (
    guid INTEGER PRIMARY KEY,
    last_line INTEGER NOT NULL
);
CREATE INDEX chat_line_claims ON chat_line(from_guid, id) WHERE origin = 'mind';
