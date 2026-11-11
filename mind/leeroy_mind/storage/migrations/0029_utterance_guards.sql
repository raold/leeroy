-- Content-free guard provenance on existing sent/dropped utterances.
ALTER TABLE utterance ADD COLUMN guard_verdict TEXT NOT NULL DEFAULT 'not_run'
    CHECK(guard_verdict IN ('not_run','send','rewrite','react','drop','failed_open'));
ALTER TABLE utterance ADD COLUMN guard_rewritten INTEGER NOT NULL DEFAULT 0 CHECK(guard_rewritten IN (0,1));
CREATE INDEX chat_line_channel_ts ON chat_line(channel,ts DESC,id DESC);
