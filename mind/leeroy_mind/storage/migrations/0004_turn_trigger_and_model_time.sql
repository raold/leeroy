-- mind.db schema v4: what each turn answered and how long the model took (liveops M5), and
-- enough of each said line to read a member's own turns back after a restart (prompts minor 15).
-- utterance.trigger_line: the chat_line id of the line the turn answered
-- (talk.turns.Turn.trigger_line), as say_sent and say_dropped log it; NULL for a turn that
-- answers no line and for rows from before v4. Not a foreign key: a turn from outside the
-- engine may name a line the log does not hold.
-- utterance.llm_s: the model's seconds on the turn, its retry included
-- (talk.pipeline.TurnMeta.llm_s); NULL when no model was asked: a template line (a gz, a kill
-- cheer, say_as).
-- utterance.to_guid: whom a whisper went to (Turn.to_guid); 0 for a channel line.
-- utterance.turn_id: the turn the line belongs to (TurnMeta.turn_id, "t17"): a turn's lines
-- share it. Ids count from t1 again in each run of the mind.
-- utterance_said: each member's said lines, newest first (Store.recent_mind_lines).

ALTER TABLE utterance ADD COLUMN trigger_line INTEGER;
ALTER TABLE utterance ADD COLUMN llm_s REAL;
ALTER TABLE utterance ADD COLUMN to_guid INTEGER NOT NULL DEFAULT 0;
ALTER TABLE utterance ADD COLUMN turn_id TEXT;
CREATE INDEX utterance_said ON utterance(guid, id) WHERE dropped IS NULL;
