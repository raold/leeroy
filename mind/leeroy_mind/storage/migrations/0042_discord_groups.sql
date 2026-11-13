CREATE TABLE discord_group (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 40),
    created_by INTEGER NOT NULL,
    created REAL NOT NULL,
    closed REAL
);
CREATE TABLE discord_group_member (
    grp INTEGER NOT NULL REFERENCES discord_group(id),
    guid INTEGER NOT NULL,
    joined REAL NOT NULL,
    left REAL,
    PRIMARY KEY(grp, guid, joined)
);
CREATE UNIQUE INDEX discord_group_current_member ON discord_group_member(grp,guid) WHERE left IS NULL;
