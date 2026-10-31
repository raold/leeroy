-- mind.db schema v2: what later phases learn from and cannot rebuild afterwards.
-- chat_line.audience: JSON list of the cast guids who could see the line when it was said
-- (talk.world.WorldView.audience); NULL for rows stored before v2.
-- utterance.bit: the BitScheduler bit id the turn brought out, NULL for none.
-- utterance.why: how the turn came about and how its speaker was chosen, e.g.
-- "reply:named", "greet:reason_pool", "burst:weighted_draw" (talk.pipeline.TurnMeta.why).

ALTER TABLE chat_line ADD COLUMN audience TEXT;
ALTER TABLE utterance ADD COLUMN bit TEXT;
ALTER TABLE utterance ADD COLUMN why TEXT;
