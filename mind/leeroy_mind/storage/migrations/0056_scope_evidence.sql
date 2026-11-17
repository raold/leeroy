-- A memory's scope is never wider than its evidence (memory.privacy.resolve, memory.scopes).
-- derived_scope/derived_with: what the memory's evidence implied when it was last checked, to a
-- fixed point over memory: refs; derived_at: when. A row with no derived_at is unchecked: readers
-- derive it themselves and never trust the stored check. The triggers below clear derived_at on
-- every memory that rests, at any depth, on one whose scope or evidence changed, in the same
-- transaction as that change. migrate's data step (storage.db.DATA_STEPS, memory.scopes.backfill)
-- checks every stored memory once; it writes the derived columns only and changes no stored scope
-- (`memory rescope --go` does that, with a receipt).
ALTER TABLE memory ADD COLUMN derived_scope TEXT
    CHECK (derived_scope IS NULL OR derived_scope IN ('open', 'private', 'secret'));
ALTER TABLE memory ADD COLUMN derived_with INTEGER;
ALTER TABLE memory ADD COLUMN derived_at REAL;
CREATE INDEX memory_unchecked ON memory(id) WHERE derived_at IS NULL;

-- The memory: and chat: refs of each memory's evidence, so "what rests on this" is an index read
CREATE TABLE memory_cite (
    src  INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('chat', 'memory')),
    dst  INTEGER NOT NULL,
    PRIMARY KEY (src, kind, dst)
) WITHOUT ROWID;
CREATE INDEX memory_cite_dst ON memory_cite(kind, dst);

INSERT OR IGNORE INTO memory_cite(src, kind, dst)
SELECT m.id, substr(e.value, 1, instr(e.value, ':') - 1),
       CAST(substr(e.value, instr(e.value, ':') + 1) AS INTEGER)
FROM memory m, json_each(CASE WHEN json_valid(m.evidence) THEN m.evidence ELSE '[]' END) e
WHERE e.type = 'text' AND (e.value GLOB 'memory:[0-9]*' OR e.value GLOB 'chat:[0-9]*')
  AND substr(e.value, instr(e.value, ':') + 1) NOT GLOB '*[^0-9]*'
  AND length(e.value) <= 25;

CREATE TRIGGER memory_cite_ai AFTER INSERT ON memory BEGIN
    INSERT OR IGNORE INTO memory_cite(src, kind, dst)
    SELECT NEW.id, substr(value, 1, instr(value, ':') - 1),
           CAST(substr(value, instr(value, ':') + 1) AS INTEGER)
    FROM json_each(CASE WHEN json_valid(NEW.evidence) THEN NEW.evidence ELSE '[]' END)
    WHERE type = 'text' AND (value GLOB 'memory:[0-9]*' OR value GLOB 'chat:[0-9]*')
      AND substr(value, instr(value, ':') + 1) NOT GLOB '*[^0-9]*' AND length(value) <= 25;
    -- a memory that already cited this id (a ref older than its row) rests on it from now on
    UPDATE memory SET derived_at = NULL WHERE derived_at IS NOT NULL AND id IN (
        WITH RECURSIVE dep(id) AS (
            SELECT src FROM memory_cite WHERE kind = 'memory' AND dst = NEW.id
            UNION SELECT c.src FROM memory_cite c JOIN dep ON c.dst = dep.id WHERE c.kind = 'memory')
        SELECT id FROM dep);
END;

CREATE TRIGGER memory_cite_au AFTER UPDATE OF evidence ON memory
WHEN OLD.evidence IS NOT NEW.evidence BEGIN
    DELETE FROM memory_cite WHERE src = NEW.id;
    INSERT OR IGNORE INTO memory_cite(src, kind, dst)
    SELECT NEW.id, substr(value, 1, instr(value, ':') - 1),
           CAST(substr(value, instr(value, ':') + 1) AS INTEGER)
    FROM json_each(CASE WHEN json_valid(NEW.evidence) THEN NEW.evidence ELSE '[]' END)
    WHERE type = 'text' AND (value GLOB 'memory:[0-9]*' OR value GLOB 'chat:[0-9]*')
      AND substr(value, instr(value, ':') + 1) NOT GLOB '*[^0-9]*' AND length(value) <= 25;
    UPDATE memory SET derived_at = NULL WHERE derived_at IS NOT NULL AND (id = NEW.id OR id IN (
        WITH RECURSIVE dep(id) AS (
            SELECT src FROM memory_cite WHERE kind = 'memory' AND dst = NEW.id
            UNION SELECT c.src FROM memory_cite c JOIN dep ON c.dst = dep.id WHERE c.kind = 'memory')
        SELECT id FROM dep));
END;

CREATE TRIGGER memory_cite_ad AFTER DELETE ON memory BEGIN
    DELETE FROM memory_cite WHERE src = OLD.id;
END;

-- A scope made stricter (or changed at all), stored or derived: everything resting on it is
-- unchecked again
CREATE TRIGGER memory_scope_au AFTER UPDATE OF scope, scope_with, derived_scope, derived_with, owner
ON memory
WHEN OLD.scope IS NOT NEW.scope OR OLD.scope_with IS NOT NEW.scope_with
  OR OLD.owner IS NOT NEW.owner
  OR COALESCE(OLD.derived_scope, 'open') IS NOT COALESCE(NEW.derived_scope, 'open')
  OR COALESCE(OLD.derived_with, 0) IS NOT COALESCE(NEW.derived_with, 0) BEGIN
    UPDATE memory SET derived_at = NULL WHERE derived_at IS NOT NULL AND id IN (
        WITH RECURSIVE dep(id) AS (
            SELECT src FROM memory_cite WHERE kind = 'memory' AND dst = NEW.id
            UNION SELECT c.src FROM memory_cite c JOIN dep ON c.dst = dep.id WHERE c.kind = 'memory')
        SELECT id FROM dep);
END;

-- What a rescope changed, so it can be undone: [[id, scope, scope_with, new scope, new with], ...]
CREATE TABLE memory_rescope (
    id        INTEGER PRIMARY KEY,
    at        REAL NOT NULL,
    source    TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 40),
    changes   TEXT NOT NULL CHECK (json_valid(changes)),
    undone_at REAL
);
