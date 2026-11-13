-- Deletion checks resolve a chat line through its durable Discord receipt.
CREATE INDEX meta_discord_line_value ON meta(value) WHERE key GLOB 'discord:line:*';
