# Guild culture

The culture table defaults off. Enable both `[server] enabled = true` and
`[culture] enabled = true` in mind.toml to enable event-backed joke births and
permanent joke retirement. Invalid tables disable culture. `bots` defaults true
but has no effect outside enabled culture; `memes_per_week` defaults 3 (0–14).
These reserved settings are consumed by later culture features.
Culture is read once when the mind starts; restart after changing either switch.
Live turns and nightly reloads retain the same startup choice.

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
Wipe rechecks read only indexed game events in the recorded span, with a bounded
event count. They never rediscover laughed lines. Item and boss names come from
screened library/receipt and encounter facts; unknown names stay generic. Each
coin attempt is a separate nightly unit: a nonfatal model failure stays retryable
and does not prevent later stages from running.

Banter folds each old joke-use receipt into lifetime usage once when culture
first consults that joke or the nightly fade runs. Delivered banter consumes lifetime usage; dropped
threads do not. Its first delivered chat row is marked so nightly spread cannot
count that same delivery twice. With culture off, legacy banter receipts and
legacy lexicon birth/spread/fade behavior remain in use.
Rewarded public lines and screened candidate jokes still create line-origin terms
with culture enabled. Historical terms with restricted origins remain frozen,
including their lifetime counters and holders' memories.

The culture migration adds the lexicon fields and reserves the discord_meme table for
later culture work. It does not publish memes or add a native key. Release is
mind-only. Parallel migration-number collisions must be renumbered at merge.

With culture enabled, targeted joke banter requires affinity at least 30 and refuses
cruel teasers, hurt moods or emotions, active feuds and current owner hurts.
The emotion gate checks tonight and the previous reporting night, and only
negative targeted feelings count as hurt. Birthday and loot congratulations do
not consume the daily teasing allowance.
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
Discord records these receipts in the same transaction as the delivered message
and chat mirror. Banter and the server share joke cooldowns and pending banter
reservations, so two clocks cannot reuse a freshly delivered joke.

## Part B: the server's calendar and loot talk

Two more switches inside enabled culture, both false by default and read once at mind start
(restart the mind, and the console for tooltips, after changing them):

```toml
[culture]
enabled = true
calendar = false   # the server's own events in Discord talk and the awareness card
loot = false       # close awards, raiders' wishes, and item tooltips in the console
```

A wrong type disables the whole culture table, Part A included. Older code reading a
mind.toml that sets either key treats it as an unknown key and turns culture off: remove
the keys before rolling back. With both false nothing below runs: no handler, no task, no
MySQL read, no seed, no card line and no tooltip.

**Calendar.** Once a day after 08:05 local (and at once with nothing cached) the mind reads
`game_event` from the world database through `library/mysql.query`: a read-only session,
one SELECT, a 5-second server limit, credentials from `WorldDatabaseInfo` in
worldserver.conf, never logged. It keeps holidays and the Darkmoon Faire and fishing
contests (`culture:wowcal`). A failed read logs only its exception class and retries an
hour later. The core gives holiday stages (`holidayStage > 0`) their dates from the
client's Holidays.dbc and its own date rules; their world rows carry no start time, so the
mind never dates them ahead. Rows the core schedules from the table (stage 0 with a real
start, such as the weekly fishing contests) get their occurrences by the core's own rule.
The bridge's `game_event` start/stop (native publishes it while `Leeroy.Realm.Enable = 1`;
no native change) goes into `culture:wowcal:active` and `culture:wowcal:stopped` for the
cached entries only (events seen before the first read wait in memory), and wins over the
table: a start means on now (a holiday's end is then unknown and never stated), a stop after
an occurrence began ends it. The core starts every running event again at each worldserver
boot, so a run keeps its first start (a start within its length only moves its latest one)
and a boot is no new subject. Each hello's `started_ms` is kept in `culture:wowcal:boot`:
five minutes after a hello from a new worldserver process (its replay is in by then), a run
that boot did not start again is over, since an event that ended while the server was down
gets no stop. A run never outlives its length plus six hours from its first start. Discord
gets one `wow_event` subject an occurrence (its "starts" and "on" share the key) in
#achievements-and-mounts and #general (fishing in #professions-and-gold and
#achievements-and-mounts, Call to Arms in #pvp-arena and #general): "<name> is on until
<day>", "<name> is on now" or "<name> starts <day>" within three days, in the mind's local
time, plus the library event page's first sentence when it has one of at most 140
characters that names no time or date (the computed local time is the only one stated).
Event names and page sentences pass the real-name screen. The awareness card's Upcoming
calendar lists up to four server events within a week. Synthetic apps (an injected model
client) never read the game database.

**Loot.** `loot_close` seeds come from charged guild loot receipts with their unique matching
DKP debit (the rule Part A's origins use) whose award was close, within seven days, as raid
chat announced them: "<winner> beat <runner-up> to [<item>] by <n> DKP", where n is the price
paid over the runner-up's bid, in #loot-and-dkp. That drop's plain `loot` seed is then
dropped; a drop already talked about as plain loot, or one Part A made a `close_loot` joke,
is not raised again; a close award is held for its whole week. The runner-up is the subject's
sore point, so the conversation passes Part A's kindness gate with the runner-up as target
(`tease_ok` for the opener and each speaker; the runner-up may answer), and a delivered
thread spends the runner-up's teasing allowance for the day once (receipt
`culture:server-close:<conversation>`). `loot_wish` seeds need a raid night the calendar desk
posted within three days. Raid nights name no instance, so the target raid is his newest raid
within 14 days (the members' own offline raids only when he has none); the raiders are the
members who raided in his last four ended raids (likewise). Off the event loop and only while
he is out of game, the mind reads that raid's bosses' drops of its size and difficulty from
library.db (a chunk counts only when its title names the boss), then the items and the
raiders' equipment through the guild armory, and keeps for each raider the largest
`upgrade_gain` (the loot system's own rule; ties to the lower item id) in
`culture:loot:wish`, recomputed every 12 hours or when the raid night changes. A raider
whose equipment cannot be read wishes for nothing. Each wish seeds "<name> needs [<item>]
from <boss>" in #loot-and-dkp and the member's class channel, opened by that member; all
wishes together weigh about three ordinary subjects. Item names pass the real-name screen.
Legendary progress and BiS lists are not reachable: no data for either exists. Loot from
native `loot_won` events is still not used (they name no members). None of these seeds
reach group DMs (they are not public kinds there), DMs or officer channels.

**Tooltips.** With `loot` on, the console's Discord view gives each message the bracketed
names that the library's item table holds exactly (any case, at most eight a message; junk,
test items and boss, raid, dungeon, class and race names never), read-only; the page shows
them quality-coloured with a popover of name, quality, item level and slot. A miss stays
plain text. Several items share many names (10/25, normal/heroic): the guild's own item wins
(an id its loot receipts and awards of the last 30 days or its wishes name), else the copy
that drops at its raid's size and difficulty by the library's loot chunks; failing both the
popover lists the levels ("Item level 264 / 277") rather than guessing one.

No migration, no native change and no model call. Release is mind and console only
(`bash scripts/ac-release.sh <commit>`).
