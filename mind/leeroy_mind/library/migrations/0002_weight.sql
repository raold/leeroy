-- library.db schema v2: a per-chunk search weight in (0, 1], set by the source. 1 for what the
-- guild asks about; less for trade goods, recipes, grey items and quote sections, so a short
-- chunk that names the query once cannot crowd out the passages that answer it.

ALTER TABLE chunks ADD COLUMN weight REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0 AND weight <= 1);
