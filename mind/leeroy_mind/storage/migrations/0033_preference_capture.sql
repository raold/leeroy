-- Private optional evidence, never logged or enabled by export commands.
CREATE TABLE learn_rewrite (
    generation TEXT PRIMARY KEY,
    first_line_id INTEGER NOT NULL UNIQUE REFERENCES chat_line(id) ON DELETE CASCADE,
    original TEXT NOT NULL
);
CREATE TABLE learn_rewrite_line (
    line_id INTEGER PRIMARY KEY REFERENCES chat_line(id) ON DELETE CASCADE,
    generation TEXT NOT NULL REFERENCES learn_rewrite(generation) ON DELETE CASCADE
);
CREATE INDEX learn_rewrite_generation ON learn_rewrite_line(generation,line_id);
CREATE TABLE learn_line_vector (
    line_id INTEGER PRIMARY KEY REFERENCES chat_line(id) ON DELETE CASCADE,
    model_hash TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    embedding BLOB NOT NULL CHECK(length(embedding)=2048)
);
