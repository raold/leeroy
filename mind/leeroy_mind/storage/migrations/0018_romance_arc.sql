-- The romance ladder becomes the long-distance arc (Track S; arc.ladder). relationship's
-- romance_stage CHECK lists the stages, so the table is rebuilt with the same columns: the old
-- vulnerable, invitation and acknowledged stages land on late_nights, planning and confession
-- (arc.ladder.LEGACY). Every such row's old stage is stashed in meta romance_arc:legacy first;
-- migrate's data step (storage.db.DATA_STEPS, arc.ladder.map_stored_stages) then lowers a
-- former invitation to the stage her scores support, keeps everyone but a drawn or lust woman
-- at whispers (arc.ladder.FOND_CAP), records every remapped row in meta romance_arc:mapped and
-- removes the stash, in this same transaction.
INSERT INTO meta(key, value)
SELECT 'romance_arc:legacy', json_group_array(json_array(a, b, romance_stage))
FROM (SELECT a, b, romance_stage FROM relationship
      WHERE romance_stage IN ('vulnerable', 'invitation', 'acknowledged') ORDER BY a, b);

CREATE TABLE relationship_new (
    a             INTEGER NOT NULL,
    b             INTEGER NOT NULL,
    affinity      REAL NOT NULL DEFAULT 0,
    trust         REAL NOT NULL DEFAULT 0,
    respect       REAL NOT NULL DEFAULT 0,
    attraction    REAL NOT NULL DEFAULT 0,
    familiarity   REAL NOT NULL DEFAULT 0,
    label         TEXT NOT NULL DEFAULT '',
    romance_stage TEXT NOT NULL DEFAULT 'none'
                  CHECK (romance_stage IN ('none','notices','banter','whispers','late_nights',
                                           'voice','picture','confession','planning','weekend',
                                           'after','ex')),
    notes         TEXT NOT NULL DEFAULT '',
    pinned        TEXT NOT NULL DEFAULT '[]',   -- JSON list of memory ids
    last_seen     INTEGER NOT NULL DEFAULT 0,   -- unix seconds
    interactions  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (a, b)
);
INSERT INTO relationship_new(a, b, affinity, trust, respect, attraction, familiarity, label,
                             romance_stage, notes, pinned, last_seen, interactions)
SELECT a, b, affinity, trust, respect, attraction, familiarity, label,
       CASE romance_stage WHEN 'vulnerable' THEN 'late_nights' WHEN 'invitation' THEN 'planning'
                          WHEN 'acknowledged' THEN 'confession' ELSE romance_stage END,
       notes, pinned, last_seen, interactions
FROM relationship;
DROP TABLE relationship;
ALTER TABLE relationship_new RENAME TO relationship;

-- The persona ledger gains the kind 'household': her card's household rewritten when an affair
-- ends her marriage (Task S6), undone through nightly.growth.undo like the night's other
-- persona writes. Rebuilt the same way, keeping every row's id, so undo refs still hold.
CREATE TABLE persona_ledger_new(id INTEGER PRIMARY KEY, guid INTEGER NOT NULL, ts REAL NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('note_added','note_dropped','note_rolled_back','drift_flag',
                                    'stance_band','undo','household')),
  text TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]', ref INTEGER, score_before REAL);
INSERT INTO persona_ledger_new(id, guid, ts, kind, text, evidence, ref, score_before)
SELECT id, guid, ts, kind, text, evidence, ref, score_before FROM persona_ledger;
DROP TABLE persona_ledger;
ALTER TABLE persona_ledger_new RENAME TO persona_ledger;

-- One row a woman on the arc: the plan she has floated and how far it got (hoped, talked about,
-- set, done), when she reached planning, her weekends with him and the last meetup, the
-- after-weekend milestones, her partner at home and how it ended. plan is a JSON object,
-- events a JSON list of [kind, ts], newest last. Times are mind-clock unix seconds.
CREATE TABLE romance_arc (
    guid           INTEGER PRIMARY KEY,
    human          INTEGER NOT NULL,
    plan           TEXT NOT NULL DEFAULT '{}',
    plan_state     TEXT NOT NULL DEFAULT 'none'
                   CHECK (plan_state IN ('none','hoped','talked','set','done')),
    plan_at        REAL,
    planning_since REAL,
    weekends       INTEGER NOT NULL DEFAULT 0,
    last_meetup    REAL,
    milestone      INTEGER NOT NULL DEFAULT 0,
    milestone_at   REAL,
    partner        TEXT NOT NULL DEFAULT 'none' CHECK (partner IN ('none','together','separated')),
    ended          TEXT CHECK (ended IN ('he_ended','found_out','owner')),
    events         TEXT NOT NULL DEFAULT '[]',
    updated        REAL NOT NULL
);

-- One meetup for each of his absences (absence: the session whose end opened the gap): the
-- woman who took it, her away row, a weekend or a later visit, and when the night wrote it.
CREATE TABLE arc_meetup (
    absence INTEGER PRIMARY KEY,
    guid    INTEGER NOT NULL,
    away_id INTEGER NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('weekend','visit')),
    written REAL NOT NULL
);
