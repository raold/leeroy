-- Observations only. No words, prompts, names or learned decisions live here.
CREATE TABLE talk_signal (
    id INTEGER PRIMARY KEY,
    line_id INTEGER REFERENCES chat_line(id) ON DELETE CASCADE,
    act_id INTEGER,
    guid INTEGER NOT NULL CHECK (guid > 0),
    kind TEXT NOT NULL CHECK (kind IN (
        'replied','reply_latency_s','reply_len','thread_continued','ignored',
        'emote_friendly','emote_romantic','emote_rude','muted','dial_down','nudged_against',
        'explicit','reason_funny','reason_in_character','reason_boring','reason_repetitive',
        'reason_weird','reason_too_much','reason_wrong_facts',
        'judge_repetition','judge_nonsequitur','judge_unawareness','judge_contradiction',
        'judge_guard_rewrite','judge_guard_drop','judge_guard_react')),
    value REAL NOT NULL CHECK (value BETWEEN -1 AND 1),
    ts REAL NOT NULL CHECK (ts >= 0),
    source TEXT NOT NULL CHECK (source IN ('implicit','explicit','judge')),
    CHECK ((line_id IS NOT NULL AND act_id IS NULL) OR
           (line_id IS NULL AND act_id IS NOT NULL AND act_id > 0))
);
CREATE INDEX talk_signal_guid_ts ON talk_signal(guid, ts);
CREATE INDEX talk_signal_line ON talk_signal(line_id);
CREATE INDEX talk_signal_ts ON talk_signal(ts);
CREATE UNIQUE INDEX talk_signal_line_kind ON talk_signal(line_id, kind, source)
    WHERE line_id IS NOT NULL;
CREATE UNIQUE INDEX talk_signal_act_kind ON talk_signal(act_id, kind, source)
    WHERE act_id IS NOT NULL;
