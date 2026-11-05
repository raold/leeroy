-- Persona tuning (console phase 3b): the owner's dials, as an overlay over the stored personas.
-- No persona row is ever rewritten by a dial: the mind reads persona_core and folds these values
-- over it at read time (leeroy_mind/tuning). Only the running mind writes these tables, through
-- its control endpoint's `tune` op, or the `tuning` CLI holding the instance lock.
--
-- tuning_change: one row per owner action (a dial set or cleared, a group saved or retired, an
-- undo, a revert). An undo is an ordinary change whose `undoes` names the change it reverses.
-- tuning_row: what one change did, value by value. NULL in before/after means unset. A group's
-- definition change is the row with dial 'definition', its JSON in before_def/after_def.
-- tuning_value: the current values, kept in the same transaction as the log, so a reader never
-- folds the log. A member's row is a pin (an absolute value); a group's is an offset that adds
-- for every member in it; a world row overrides mind.toml.
-- tuning_group: the saved group definitions. 'everyone' is built in and has no row here.
-- meta 'tuning_version' is the newest change id (written with each change).
CREATE TABLE tuning_group (
  id INTEGER PRIMARY KEY CHECK(typeof(id) = 'integer' AND id >= 1),
  name TEXT NOT NULL CHECK(typeof(name) = 'text' AND length(name) BETWEEN 1 AND 40),
  predicate TEXT NOT NULL CHECK(CASE WHEN json_valid(predicate) THEN json_type(predicate) = 'object'
    AND length(CAST(predicate AS BLOB)) <= 2048 ELSE 0 END),
  created REAL NOT NULL CHECK(typeof(created) IN ('integer','real')),
  retired REAL CHECK(retired IS NULL OR typeof(retired) IN ('integer','real'))
);
CREATE UNIQUE INDEX tuning_group_live_name ON tuning_group(name) WHERE retired IS NULL;

CREATE TABLE tuning_change (
  id INTEGER PRIMARY KEY CHECK(typeof(id) = 'integer' AND id >= 1),
  ts REAL NOT NULL CHECK(typeof(ts) IN ('integer','real')),
  source TEXT NOT NULL CHECK(source IN ('console','cli')),
  label TEXT NOT NULL DEFAULT '' CHECK(typeof(label) = 'text' AND length(label) <= 120),
  undoes INTEGER REFERENCES tuning_change(id),
  previewed INTEGER NOT NULL DEFAULT 0 CHECK(previewed IN (0,1))
);

CREATE TABLE tuning_row (
  change INTEGER NOT NULL REFERENCES tuning_change(id),
  scope TEXT NOT NULL CHECK(scope IN ('world','group','member')),
  target TEXT NOT NULL CHECK(typeof(target) = 'text' AND (
    (scope = 'world' AND target = '')
    OR (scope = 'group' AND (target = 'everyone' OR (length(target) BETWEEN 1 AND 10 AND target NOT GLOB '*[^0-9]*')))
    OR (scope = 'member' AND length(target) BETWEEN 1 AND 10 AND target NOT GLOB '*[^0-9]*'))),
  dial TEXT NOT NULL CHECK(typeof(dial) = 'text' AND length(dial) BETWEEN 1 AND 32
    AND dial NOT GLOB '*[^a-z_]*'),
  before INTEGER CHECK(before IS NULL OR (typeof(before) = 'integer' AND before BETWEEN -10 AND 10)),
  after INTEGER CHECK(after IS NULL OR (typeof(after) = 'integer' AND after BETWEEN -10 AND 10)),
  before_def TEXT CHECK(before_def IS NULL OR json_valid(before_def)),
  after_def TEXT CHECK(after_def IS NULL OR json_valid(after_def)),
  persona_version INTEGER CHECK(persona_version IS NULL OR typeof(persona_version) = 'integer'),
  PRIMARY KEY(change, scope, target, dial),
  CHECK(dial = 'definition' OR (before_def IS NULL AND after_def IS NULL)),
  CHECK(dial != 'definition' OR (scope = 'group' AND before IS NULL AND after IS NULL))
);

CREATE TABLE tuning_value (
  scope TEXT NOT NULL CHECK(scope IN ('world','group','member')),
  target TEXT NOT NULL CHECK(typeof(target) = 'text'),
  dial TEXT NOT NULL CHECK(typeof(dial) = 'text' AND dial NOT GLOB '*[^a-z_]*' AND dial != 'definition'),
  value INTEGER NOT NULL CHECK(typeof(value) = 'integer' AND value BETWEEN -10 AND 10),
  change INTEGER NOT NULL REFERENCES tuning_change(id),
  persona_version INTEGER CHECK(persona_version IS NULL OR typeof(persona_version) = 'integer'),
  PRIMARY KEY(scope, target, dial)
);
