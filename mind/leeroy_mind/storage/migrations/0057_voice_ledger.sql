-- A member's voice sheet (personas.voices, `personas voices --go`): each import or removal is a
-- persona ledger row of kind 'voice' with a private receipt in meta, so the persona ledger's
-- undo can take it back.
CREATE TABLE persona_ledger_new(id INTEGER PRIMARY KEY, guid INTEGER NOT NULL, ts REAL NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('note_added','note_dropped','note_rolled_back','drift_flag',
                                    'stance_band','undo','household','variety','looks','home',
                                    'voice')),
  text TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]', ref INTEGER, score_before REAL);
INSERT INTO persona_ledger_new SELECT * FROM persona_ledger;
DROP TABLE persona_ledger;
ALTER TABLE persona_ledger_new RENAME TO persona_ledger;
