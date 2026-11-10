-- Callback receipts contain only IDs and receipt-time seconds, never memory text.
CREATE TABLE callback_use (
    id INTEGER PRIMARY KEY,
    memory_id INTEGER NOT NULL REFERENCES memory(id),
    guid INTEGER NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX callback_use_memory_time ON callback_use(memory_id,ts);
CREATE INDEX callback_use_time ON callback_use(ts,guid);
