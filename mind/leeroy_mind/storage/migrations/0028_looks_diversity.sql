-- Keep explicit owner appearance choices even after a generated persona update.
-- Old persona versions were not retained: protect legacy looks with unknown provenance.
CREATE TABLE persona_core_new (
    guid INTEGER PRIMARY KEY, json TEXT NOT NULL, card TEXT NOT NULL,
    card_tokens INTEGER NOT NULL, version INTEGER NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('seed','generated','owner','variety')),
    updated INTEGER NOT NULL
);
INSERT INTO persona_core_new SELECT * FROM persona_core;
DROP TABLE persona_core;
ALTER TABLE persona_core_new RENAME TO persona_core;

CREATE TABLE persona_looks_protection (
    guid INTEGER PRIMARY KEY,
    reason TEXT NOT NULL CHECK(reason IN ('owner','legacy_unknown')),
    version INTEGER NOT NULL CHECK(version >= 1)
);
INSERT INTO persona_looks_protection
SELECT guid, CASE WHEN source='owner' THEN 'owner' ELSE 'legacy_unknown' END, version
FROM persona_core WHERE source='owner'
    OR CASE WHEN json_valid(json) THEN json_type(json,'$.appearance')='object' ELSE 1 END;

CREATE TRIGGER protect_owner_looks_insert BEFORE INSERT ON persona_core
WHEN NEW.source='owner' AND json_valid(NEW.json)
 AND COALESCE(json_extract(NEW.json,'$.appearance'),'null') != COALESCE(
    (SELECT CASE WHEN json_valid(json) THEN json_extract(json,'$.appearance') END
     FROM persona_core WHERE guid=NEW.guid), 'null')
BEGIN
    INSERT OR REPLACE INTO persona_looks_protection VALUES(NEW.guid,'owner',NEW.version);
END;
CREATE TRIGGER protect_owner_looks_update AFTER UPDATE ON persona_core
WHEN NEW.source='owner' AND json_valid(NEW.json)
 AND COALESCE(json_extract(NEW.json,'$.appearance'),'null') != COALESCE(
    CASE WHEN json_valid(OLD.json) THEN json_extract(OLD.json,'$.appearance') END, 'null')
BEGIN
    INSERT OR REPLACE INTO persona_looks_protection VALUES(NEW.guid,'owner',NEW.version);
END;

CREATE TABLE selfie_embedding (
    guid INTEGER NOT NULL,
    pick INTEGER NOT NULL REFERENCES selfie_pick(id),
    model TEXT NOT NULL CHECK(model='ipadapter-vith-head-v1'),
    vector BLOB NOT NULL CHECK(typeof(vector)='blob' AND length(vector)=4096),
    created REAL NOT NULL,
    PRIMARY KEY(guid,pick,model)
);
