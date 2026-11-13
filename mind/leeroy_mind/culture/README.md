# Guild culture

The culture table defaults off. Enable both `[server] enabled = true` and
`[culture] enabled = true` in mind.toml to enable event-backed joke births and
permanent joke retirement. Invalid tables disable culture. `bots` defaults true
but has no effect outside enabled culture; `memes_per_week` defaults 3 (0–14).
These reserved settings are consumed by later culture features.

Jokes keep their lexicon ID, butt, immutable origin references and lifetime use
count. The reflection model supplies only a screened label of at most 60
characters; origin facts are rebuilt from retained rows. A missing origin,
12 uses, or age over 60 days with heat below 1 retires a joke permanently.
Public laughed-line births retain the existing paired-output/laughter checks;
private, officer and unknown-channel lines cannot become public origins.

Loot roots use `loot:close_loot:<receipt-key>` or
`loot:his_claim:<receipt-key>`. Each requires the charged guild loot receipt and
its unique matching DKP debit. The typed identity keeps the original fact kind
when one receipt qualifies for both. Game death origins require two unambiguous
active pulls ending in wipes on one map/boss, with the same member first dead.
Neither death cause nor a personal trait is inferred.

Banter folds each old joke-use receipt into lifetime usage once when culture
first consults that joke or the nightly fade runs. Delivered banter consumes lifetime usage; dropped
threads do not. Its first delivered chat row is marked so nightly spread cannot
count that same delivery twice. With culture off, legacy banter receipts and
legacy lexicon birth/spread/fade behavior remain in use.

The culture migration adds the lexicon fields and reserves the discord_meme table for
later culture work. It does not publish memes or add a native key. Release is
mind-only. Parallel migration-number collisions must be renumbered at merge.

With culture enabled, targeted banter requires affinity at least 30 and refuses
cruel teasers, hurt moods or emotions, active feuds and current owner hurts.
A delivered thread consumes one target allowance per host-local calendar day;
the owner's existing evening allowance also lasts until the 06:00 boundary.
Nothing is charged on selection or a dropped send. Further lines in that same
bounded thread retain their allowance but recheck kindness before each send.

With both switches enabled, the Discord server gathers active, unretired jokes
after three days without use, for general and memes. Origins and every retained
source must still pass the public provenance and audience checks; private,
officer, frozen and missing sources cannot be widened into these channels.
Facts are rebuilt before speaking. The starter and each teasing participant
pass the kindness gate again before sending; the target may answer without an
instruction to tease themselves. Only a first delivered line spends the joke's
cooldown, lifetime use and target allowance. Dropped lines spend nothing.
