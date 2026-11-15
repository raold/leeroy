-- Member drama ([drama] member_romance): a woman of the cast who left a member partner for the
-- owner. The couple's stages live on their relationship rows; this keeps who left whom, for whom
-- and when, which the Social tab shows. `public`: 1 when some of his pursuit was public, so the
-- guild was told why (else only that they split). Ids and codes only, no text.
CREATE TABLE breakup (
    id INTEGER PRIMARY KEY,
    woman INTEGER NOT NULL,
    partner INTEGER NOT NULL,
    for_guid INTEGER NOT NULL,
    stage TEXT NOT NULL,
    night TEXT NOT NULL,
    ts REAL NOT NULL,
    public INTEGER NOT NULL DEFAULT 0 CHECK (public IN (0, 1)),
    feud INTEGER,
    rumor INTEGER,
    CHECK (woman != partner)
);
CREATE INDEX breakup_ts ON breakup(ts);
