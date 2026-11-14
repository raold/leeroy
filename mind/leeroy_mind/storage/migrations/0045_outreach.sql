CREATE TABLE outreach_item (
    id INTEGER PRIMARY KEY,
    guid INTEGER NOT NULL REFERENCES persona_core(guid),
    kind TEXT NOT NULL CHECK (kind IN ('life','plan','followup','his_day','birthday','miss_you','dm_nudge')),
    ref TEXT NOT NULL CHECK (length(ref) > 0),
    about TEXT NOT NULL CHECK (length(about) BETWEEN 1 AND 200),
    audience TEXT NOT NULL CHECK (audience IN ('pair','guild')),
    weight REAL NOT NULL CHECK (weight BETWEEN 0.1 AND 10),
    created REAL NOT NULL,
    not_before REAL NOT NULL,
    expires REAL NOT NULL CHECK (expires > not_before),
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','told','dropped')),
    told_via TEXT CHECK (told_via IN ('whisper','dm','guild','party')),
    told_at REAL,
    line_id INTEGER UNIQUE REFERENCES chat_line(id),
    ended TEXT CHECK (ended IN ('told','expired','superseded','capped','he_raised')),
    UNIQUE(guid, ref),
    CHECK (kind <> 'dm_nudge' OR audience = 'pair'),
    CHECK (audience <> 'pair' OR told_via IS NULL OR told_via IN ('whisper','dm')),
    CHECK (kind <> 'dm_nudge' OR told_via IS NULL OR told_via = 'whisper'),
    CHECK (
        (state = 'open' AND ended IS NULL AND told_via IS NULL AND told_at IS NULL AND line_id IS NULL)
        OR (state = 'told' AND ended IS NOT NULL AND ended = 'told' AND told_via IS NOT NULL AND told_at IS NOT NULL AND line_id IS NOT NULL)
        OR (state = 'dropped' AND ended IS NOT NULL AND ended IN ('expired','superseded','capped','he_raised')
            AND told_via IS NULL AND told_at IS NULL AND line_id IS NULL)
    )
);
CREATE INDEX outreach_member_open ON outreach_item(guid, state, weight DESC, created, id);
CREATE INDEX outreach_due ON outreach_item(state, not_before, expires);
