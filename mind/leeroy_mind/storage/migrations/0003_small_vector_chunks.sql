-- mind.db schema v3: memory_vec in chunks of 16 vectors.
-- vec0 allocates storage a chunk at a time for each PARTITION KEY value, and its default chunk
-- holds 1,024 vectors (2 MB at 512 float32): one memory per owner made mind.db about 215 MB
-- for the cast of 106. Measured with sqlite-vec 0.1.9 for 107 owners: 3 vectors each take
-- 3.6 MB with 16 a chunk (14.2 MB with 64), 30 each 7.2 MB, 300 each 68.5 MB (64: 71.2 MB),
-- and a k=50 search stays under 1 ms. The rebuild keeps every vector under its memory id.
-- Space the old chunks held is reused, not returned: VACUUM with the mind stopped returns it.
--
-- memory.evidence is a JSON list of refs: "chat:<chat_line.id>", "event:<game_event.id>",
-- "memory:<memory.id>", and "owner:<moment>" for the owner's own memories (source 'owner',
-- seeded by `memories seed`; no chat line behind them).

CREATE TEMP TABLE memory_vec_copy AS SELECT memory_id, owner, embedding FROM memory_vec;
DROP TABLE memory_vec;
CREATE VIRTUAL TABLE memory_vec USING vec0(
    memory_id INTEGER PRIMARY KEY,
    owner INTEGER PARTITION KEY,
    embedding float[512] distance_metric=cosine,
    chunk_size=16
);
INSERT INTO memory_vec(memory_id, owner, embedding)
    SELECT memory_id, owner, embedding FROM memory_vec_copy ORDER BY memory_id;
DROP TABLE memory_vec_copy;
