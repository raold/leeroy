-- One mind per member (2026-11-12): the officers' lines in either medium are one audience.
-- No schema change. migrate's data step (storage.db.DATA_STEPS,
-- memory.privacy.backfill_officer_scopes) gives each memory that rests on officer lines only
-- the officers' scope (private, scope_with -1), in this same transaction.
SELECT 1;
