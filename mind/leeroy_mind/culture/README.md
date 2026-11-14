# Guild culture

The culture table defaults off. Enable both `[server] enabled = true` and
`[culture] enabled = true` in mind.toml to enable event-backed joke births and
permanent joke retirement. Invalid tables disable culture. Part B's `calendar` and
`loot` and Part C's `bots`, `emoji` and `memes` are their own switches inside it, each
false by default; `memes_per_week` defaults 3 (0–14).
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

## Part C: server bots, the guild's own emoji and original memes

Three more switches inside enabled culture, each false by default and read once at mind start
(restart the mind, and the console for `emoji`, after changing them). `bots` was reserved as
`true` and never read; it now defaults to `false` like the others, so enabling culture gains
nothing until a part is asked for:

```toml
[culture]
enabled = true
bots = false          # the server bots post the guild's real events
emoji = false         # the guild's own role and class emoji in the console
memes = false         # text memes, and image memes where the selfie worker renders
memes_per_week = 3    # image memes in any seven days (0-14; 0: text memes only)
```

As with Part B's keys, older code reading a mind.toml that sets one of these turns culture off:
remove them before rolling back. With all three false nothing below runs: no task, no post, no
reaction, no night stage, no render, no seed, no page change but the emoji files being served.

**Server bots** (`culture/bots.py`, `ServerBots`, ticking every 60 s) post as bots, never as
members: `discord_message` rows of origin `bot`, author 0 and the bot's id (the shape migration
0038 reserved), with no chat line, no turn and no model call. Every post is built by code from
rows:

| Bot | From | Channel | Post |
|---|---|---|---|
| signups | a raid night the calendar desk posted, three days ahead | #raid-schedule | "Raid night — Tuesday 20:00. React :tank: :healer: :dps:" |
| loot | a charged loot receipt with its unique DKP debit | #loot-and-dkp | raid chat's own award line, "[Item] -> Name, 47 dkp" |
| logs | a guild raid that ended after at least one pull | #logs | "Logs are up: Naxxramas 25 — 3 of 15 bosses, 2 wipes, 2h 00m" |
| birthday | a member's card birthday, from 09:00 local that day | #general | "Happy birthday Name! 🎂" |
| firsts | a first-ever kill the Realm announced (sent); a cast member's realm-first achievement fact | #announcements | "GUILD FIRST: ..." / "REALM FIRST: Name earned ..." |
| reminders | a posted raid night an hour ahead (an officer meeting in #officer-chat) | #raid-schedule | "Raid night in 1 hour" (the true minutes when less) |

Each post's receipt (meta `culture:bot:<bot>:<ref>`) is written in the post's own transaction,
so a restart can neither post twice nor lose a receipt. Inside the server's quiet window nothing
is posted; what fell due waits and goes out at the window's end, oldest first, at most 10 a
minute; a post whose subject passed meanwhile (a reminder after its raid night, a birthday after
its day) is never sent late. Events from before the bots were switched on (meta
`culture:bots:since`, forgotten at every start with the bots off, so switching them on again never
posts a backlog) are never posted. A post the real-name screen catches, or the store refuses,
keeps a `refused:<code>` receipt and is logged once, never retried. Members
react to bot posts as to any post (the server's reactions), except a signup post: its reactions
are the members' real calendar signups (`realm_rsvp`), their role for signed up, a shrug for
tentative, nothing for a decline or for him. Item names in bot posts are bracketed, so Part B's
tooltips show them. Logs: `culture_bot_posted bot= message=`, `culture_bot_reacted count=`.

**Custom emoji** (`discord/custom_emoji.py`, `console/static/emoji/*.svg`): thirteen original
32 x 32 SVGs made for this repo under its licence (a shield, a cross, crossed blades, and a
class-coloured disc with a plain glyph for each class; no third-party emoji set or emote art),
named `:tank:`, `:healer:`, `:dps:`, `:warrior:` ... `:deathknight:` ... `:druid:`. With `emoji` on, a
registered shortcode counts as one emoji for reactions (his, from the console, and the signup
bot's), the picker gains "The guild's own", and shortcodes in messages and reactions draw as
images (fetched once into `blob:` URLs: the page's CSP is unchanged). An unregistered shortcode
is refused exactly as a non-emoji is; with `emoji` off a shortcode is refused and shows as text.
The page's registry (`comms/emoji.mjs`) is checked against the Python one by tests. In-joke emoji
images are left for later.

**Memes** (`culture/memes.py`):
- *Text memes and copypastas*: a `meme` seed in #memes for each active, public running joke (the
  same rebuilt fact and butt as its joke seed) and for the guild's last raid with wipes; together
  they weigh about one subject. A joke's meme passes the kindness gate exactly as the joke does
  and spends its cooldown, lifetime use and the butt's allowance only on its first delivered line.
- *Image memes*, only with `[selfies] enabled` (else `culture_memes_need key=selfies.enabled`) and
  `memes_per_week` above 0: stage `meme_plan` (reflection model, after `language`) plans at most
  one a night, at most `memes_per_week` in seven days, spaced through the week, for a public
  joke with no meme in 30 days. The model writes only the caption (top and bottom, 60 characters
  each) and picks the picture: one of 20 fixed scenes of places and objects with no people, or the
  butt's own situational photo already posted in a public channel (plain style, under the NSFW
  limit, she 25 or older). "Posted publicly" is read from the photo's actual posting, never its
  flags: every message carrying it is hers, not deleted, in a layout channel that is not
  officers-only; a selfie-request photo posted only in #officer-chat, #applications or
  #bank-requests (or in a DM) never becomes a meme, and a planned meme whose photo stops
  qualifying is refused at render. A caption is refused by the photo screens (`EXPLICIT_RE`,
  `WEAR_RE`, `AGE_RE`), the real-name screen, a member named who is not the joke's butt or
  originator, or a character the bundled font cannot draw (`glyph`: printable ASCII, Latin-1,
  Latin Extended-A and common typographic marks only, so no emoji or other scripts draw as
  boxes; the worker refuses the same); a refused caption stores nothing. Stage `meme_render` (an image stage after the photo stages,
  taking no GPU when nothing is planned) rechecks the joke, the caption and the photo, renders a
  scene with a plain worker (no identity) from the scene's fixed words only, so no name ever
  reaches an image prompt, under the NSFW gate and the selfie GPU rules (never under a live lease;
  his login ends the worker's whole process tree; Free GPU's pause stops the night as his login
  does), then has the worker draw the caption (the selfie worker's `caption` op, Pillow and the
  bundled OFL font) and score the captioned image again. The file is
  `mind-data/discord/memes/<id>.jpg` (a scene keeps its `<id>.base.jpg`). A flagged or screened
  meme is `refused`; no meme file is ever deleted (a flagged image is never written).
- *Posting*: a ready meme is a `meme_image` seed for #memes for a week; its opener (cast through
  the kindness gate) carries it as a `meme` attachment with her own short line, and it is marked
  `posted` in the message's own transaction. Later turns and his D1 answers see the caption as a
  note ("[meme picture captioned: ...]"). Memes never reach DMs, group DMs or officer channels.
  Logs: `culture_meme_plan`, `culture_meme_refused codes=`, `culture_meme_ready`,
  `culture_meme_posted`; never a caption.

Both meme stages are extension stages in the night's budget (one call for the plan; about 40 s
of image work for each meme to render), counted only on a runner that has them.

No migration (the `discord_meme` table is 0043's) and no native change. Release: mind, console and
selfie worker (`bash scripts/ac-release.sh <commit>`); the selfie worker gains the bundled font and
Pillow use for captions (`.venv/Scripts/python -m leeroy_selfie doctor` after the release reads
the font). The console serves `/static/emoji/*.svg` from this release on.
