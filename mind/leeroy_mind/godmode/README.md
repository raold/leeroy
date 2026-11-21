# Godmode, strings and member activities

He edits a member's mind, and he pulls strings that she carries out in her own way. Both are
off by default.

## Operator notes

```toml
[godmode]
enabled = true
```

Restart the mind and the console after changing it. Missing, `false` or invalid (a non-boolean
value or an unknown key) means off; an invalid table logs `godmode_config_invalid` once. Run
`db migrate` with the mind stopped first: migration `0046_strings.sql` adds `godmode_intent`.
Edits and Discord strings are mind-only. Strings in game (Part B) also need the native rebuild
and `Leeroy.Strings.Enable = 1` in `mod_leeroy.conf` (default 0, `.reload config`); without it
they are reported `off`, and an older worldserver's as `unsupported`.

With it off:

- the tune op's `edit` verbs `set` and `preview` and the `string` op answer "Godmode is off";
- nothing attaches: no tick, no intent seeds, no card lines, no openers or reactions;
- `edit list`, `edit retire`, `tuning undo N` and `tuning revert N` still work, so edits made
  while it was on can be taken back. A revert or a retire leaves a table edit the night has
  moved since (a feud it reopened, a memory it changed) as the night left it and says so; a
  single undo of one refuses in words.

Edits already made stay in force when it is turned off, as dials and nudges do: retire them, undo
them, or revert to the change before the first one. Strings still waiting simply wait; they
expire after a day when it is on again.

## Edits

Every edit is one `tuning_change` row with a receipt in `meta`
(`tuning:edit:value:<id>`, `tuning:edit:change:<change>`), shown in the Personas tab's tuning
history and in `python -m leeroy_mind tuning --data-dir D edits|history`. `tuning undo N` and
`revert N` (mind stopped) and the console's Undo and Revert (mind running) cover every kind.

| Kind | What it changes | Undo |
| --- | --- | --- |
| `trait` | her persona as read, at one whitelisted path (Big Five scores and behaviour, attachment, humour, likes, dislikes, values, opinions, speech habits, game wants, motivation, thinks-as, favourite spot) | the overlay is dropped |
| `feeling` | offsets on affinity, trust and attraction toward you or a member; a romance stage on the ladder (none to planning) toward you | the overlay is dropped |
| `mood` | a mood offset (-1 to 1) for 1 hour to 14 days | the overlay is dropped |
| `life_event` | a new life thread with a screened title, the thread it replaces resolved | the thread is removed (resolved, its beats' memories archived, once the night wrote beats) and the old one is active again; a later life event that replaced it is undone first |
| `memory_implant` | a memory she simply has (`source owner`, key `owner:godmode:<id>`, dated `days_ago` back) | the memory is rejected and the memories built on it archived |
| `memory_erase` | one active memory archived | its old status comes back |
| `feud_start` / `feud_end` | a feud between two members opened (cause `owner`) or ended, with its story arc | the feud is removed (ended, once the night cited it) or reopened |
| `hangouts` | weights 0 (avoids) to 3 on Discord channels and voice rooms | the overlay is dropped |
| `skill` | star, middle or fire: play style's potions and wait, outdoors | the overlay is dropped |
| `activities` | day-plan activities and partners, for Part C's planner | the overlay is dropped |

**An overlay edit is a lens, never a write.** The stored rows stay the night's truth:
`Store.get_relationship(a, b)` and `Store.get_state(guid)` return the stored row, and every
writer and the night read that (the romance ladder, attraction and its stance bands, regression,
gains, breakups, weekends, the mood stage, notes, re-seeds). Only the readers that shape what a
member says or who speaks see the lens: `get_relationship(..., lens=True)` and
`get_state(..., lens=True)` in talk (speaker weights, relationship cards, the mood line, bits,
reactions, banter, photo and voice-room choices), `server.state.tie_kind`, the server snapshot,
`culture.tease`, `arc.live` (her stage and rival line) and the console's Personas and Social
views. Both mood folds add the edits in force to the stored mood and clamp once (`fold_mood`). A lens row
(`EffectiveRelationship`, `EffectiveState`) is never written: `put_relationship` and `put_state`
refuse one. Traits, hangouts, skill and activities ride on the effective persona for talk
(`tuning.effective.edits_of`); the night reads her persona as stored and dialled
(`NightContext.persona` and `ctx.personas`, a view of the cache without the edits, and
`load_effective`). So undoing an overlay edit leaves exactly what the
night would have produced without it (`tests/nightly/test_night_godmode_lens.py`, one test per
writer). Feelings sum over the edits in force; each mood edit counts until its own end.

Content rules: age, looks, names, identity and the romance stance are never edited (codes
`age`, `looks`, `name`, `identity`, `romance`); trait values must still make a valid persona
(`invalid`); text passes Board cleaning, the real-name screen (`real_name`), the explicit
screens (`explicit`), the age words (`age`) and romance between members (`romance_members`);
men stay friends (`men_friends`); a fond woman stops at the fond cap (`stance`). An implant
never widens its audience: its scope is `open`, `private` or `secret` with you, never lowered by
what it cites. Logs carry kinds, ids and codes, never text.

The tune op takes `{"action": "edit", "verb": "set"|"preview", "kind", "args"}`,
`{"verb": "retire", "id"}` and `{"verb": "list"}`. Preview is a dry run of the checks: no
model and no write.

## Strings (intents)

He asks; she means to. The card shows a waiting string to her as her own plan ("You meant to
..."), never an order, and only where she would carry it out. One thing to carry out a turn: a
turn that carries an outreach item or a string's own turn shows no further string.

| Kind | How it is carried out | Taken when |
| --- | --- | --- |
| `post` {channel, about} | an `intent` seed in the server engine, her as the opener | her opener is posted, or a line of hers there names it |
| `thread` {channel, name, about} | the same, and a thread named as he asked on her opener | her opener is posted |
| `bring_up` {topic[, channel]} | the same seed in its one channel (his, else general or irl by her guid) | a Discord line of hers there, or in her DM with you, names one of the topic's words (`moves.topics`); an opener that does not name it is not taken |
| `react` {message, emoji} | the server's Reactor, after her presence's delay; her presence and the string's fit are checked again before it is written | the reaction is written |
| `dm_him` {about} | a DM opener on D2's DM path, no outreach item, never within 10 minutes of an opener of her own | it is sent, or her DM to you names it |

An attempt that comes to nothing (a dropped draft, an opener that never named the topic) backs
the string off: 5 minutes, doubling, and after 4 it is `not_taken` (`dropped`,
`not_mentioned`).

Each tick (30 s) settles a string `expired` after a day, or `not_taken` with a reason when it
no longer fits: `away`, `gone`, `feud` (a feud with whom it touches), `stance` (warm words or a
loving reaction toward a rival, a DM when she dislikes you), `claims` (it reverses a claim of
hers), `deleted` (the message is gone), `reactions_off`, `dms_off` (`[server] dms_per_day = 0`),
`chattiness_off`, or the store's own refusal code. He can cancel a
waiting one. Group DMs and game lines never carry out or show a Discord string. Discord strings
need the guild Discord's engine (`[server] enabled`, `server_off` otherwise).

### In game (Part B)

| Kind | What she does | Native refuses (besides the shared codes) |
| --- | --- | --- |
| `come_to_me` | comes to his side; outside his group she then stays there 10 minutes, fenced from the Realm, training and offline raids (`busy:string`) | `bad_area` (he is not in a city or rest area), `grouped` |
| `join_group` | joins the party he leads (never a raid) and comes to him | `in_group`, `lfg_group`, `not_leader`, `in_raid`, `group_full`, the add command's own fences |
| `follow` {} | follows him; the same string again stops it and she waits | `not_in_group`, `busy:raid_owner` (his raid) |
| `duel_me` | challenges him to a duel (accepting is his) | `in_group`, `busy:duel`, `too_far`, `bad_area` |
| `emote` {emote} | one of 16 friendly emotes, at him | `bad_emote`, `too_far` |
| `sit` | sits near him for up to 5 minutes while he stays near; a later string that moves her stands her up | `too_far` |

Shared codes: `off`, `human_offline`, `not_cast`, `not_online`, a fence (`busy:realm_owner`,
`busy:training_owner`, ...), `dead`, `combat` (hers, his or his group's: never inside his fight), `in_instance`
(never in or onto an instance copy), `in_bg`, `queued`, `travelling`, `human_dead`; native's own
switch at 0 is stored as `strings_off` (the console names `mod_leeroy.conf`). The mind settles
some before or instead of native: `human_offline` (he is not in game), `no_link` (nothing was
sent), `combat` and `raid_quiet` (his raid fights, keeps its wipe silence or holds its debrief),
`unsupported` (an older worldserver), `away`, `gone` and `stance` (a duel from one who dislikes
him, affinity -30 or below; a kiss or a flirt only from a woman drawn to him or in lust, a hug or a
wink from a fond one too).

A game string goes at once, in the console's request: the mind checks her fit, him in game and the
bridge, sends one `string_do` and settles the string `taken` with what she did (native's done job:
`came`, `joined`, `following`, `stopped`, `challenged`, `emoted`, `sat`) or `not_taken` with the
code. It is never retried: one refused in his fight stays refused. A lost ack or link
(`AckTimeout`, `ConnectionLost`) may hide what she did, so it is never `not_taken`: native's
done job (sent before its ack, or with the outbox at the next greeting) settles it `taken`, and
without one it stays pending and expires after 10 minutes as `expired`/`unknown`. For the next
half hour her card (game channels with him: party, raid, say, yell, emote, a whisper with him; and
her DM with him) says what she did, as her own past ("You joined his group because he asked."),
or that she could not ("He asked you to come over, but you couldn't just then."): only her newest
string of each kind (a stop replaces the follow it ended), and a follow's line goes once she is no
longer in his group. Never in guild or officer chat, a server channel or a group DM, and nothing
for a refusal she would not have known of (the server, the link, his own state, her being offline,
a join while she is already with him). The card needs `[talk] aware = true`, as Part A's lines do. Nothing here takes a Realm
body-ledger rank; native's fences and journal are in `modules/mod-leeroy/README.md`.

The console's Personas member detail has a Strings panel (her strings, their state and reason,
Cancel, a form for the Discord kinds, and "In game": a button for each game kind, the emote with
its list; the answer says what she did or why not) and, beside her dials, her godmode edits with
Retire and a form for every kind. `POST /api/string` relays to the mind's `string` op under the same
key, Origin and `--no-control` gates as Board replies.

## Planned days in game

Enable `[life] day_plans = true` for the nightly reflection stage and `[realm] enabled = true` for execution.
The native worldserver also needs `Leeroy.DayPlans.Enable = 1` and `Leeroy.Realm.Enable = 1`. The day switches
default off and work independently of godmode edits. The mind applies `life.day_plans` on its normal nightly
and presence configuration reload. Disabling it stops new schedules, body commands and day projections.
Previously journaled work still reconciles. The native key reloads with `.reload config`. A fresh
bridge greeting advertises `day_plan_handoff_v1` for admission.

The night selects two to four blocks for the following server-calendar day between 08:00 and 02:00 in
America/New_York. Posted raids reserve the rest of the window from their start. Shared blocks have reciprocal
participant rows and one `day:<leader plan id>` claim at rank `realm_day`. At most twenty members may be away.
Offline blocks change only the schedule. Public cards and participant-led openers describe proved visits and
activity starts. Each arrival creates one individual keyed event memory. The activity receipt updates it.
Company requires a current native witness at the cohort's fixed destination. Fishing describes only the member
whose native operation succeeded. RPG status is a one-shot request.

An owner add, validated raid formation, offline raid or game string can interrupt the whole block. For a day
teleport already in flight, the original request waits up to 30 seconds for native to confirm release. Native
observes its own destination, cleans its exact duel or fishing operation, and closes the permit. Ordinary join,
combat, group, instance and ownership checks run again before the original request proceeds. Timeout or changed
identity ends the request. An unresolved operation stays in the Realm journal for the existing operator workflow.

The handoff entry point for a higher-priority mind caller is `app.realm.parts["day"].handoff(command, members,
reason, valid, send)`. Supply the original typed command with its reserved ID, its full intended roster, a
synchronous revalidation callback and the async original send. The send accepts the command and the optional
`expected_epoch` writer guard. The day runner journals the owner request. Overlapping permits require a
native admission check for the original command type. Successful admission stores the request and revokes
its overlapping day permits. A cold offline-raid driver gets a separate, bounded preparation ticket.
That ticket authorizes only driver preparation. Once the driver is ready, the raid must pass a fresh
admission check. Execution starts only when the caller sends the original command after handback.
Its request ID, check ID, arguments, native session, body lifetimes, bridge writer and deadline are
revalidated. Only the original executor's ordinary ACK and job receipts establish its result. If an
admission ACK is lost, the original command remains unsent while the day runner reconciles the permits.

A mind restart expires the old process's waiting requests. The current process can query or revoke an old permit
on the same native boot, including when new planning is off. The original boot, serial, token, begin ID, guild,
human and participant set reconcile its physical steps. After a native reboot, an intent that never crossed its
durable writer barrier can close without an activity claim. Dispatched unknown operations retain their holds.
The existing explicit operator abandonment for an exact root can resolve its old-boot journal. A new incarnation
alone gives no evidence of release. Recovery never replays a body command.

Migration `0064_day_plans.sql` adds the plan, permit, step and handoff tables. Deploy with
`ac-release.sh --native` to install the changed worldserver and mind together.

## Members' heroics

Enable `[life] dungeons = true` and `Leeroy.MemberDungeon.Enable = 1`. The Discord conversation uses the server and its guards. Apply the native key with `.reload config`. The mind rereads its switch on session start and nightly configuration reload. Day planning and godmode edits have their own switches.

While the owner is away, a tank can invite four level-80 guild members to Halls of Lightning heroic in `#lfg-and-alts`. The party needs one tank, one healer and three damage dealers. The proposal prefers the tank's friends and avoids rivals and open feuds in either direction. It expires in an hour. Each local calendar day has at most one proposal for this guild and owner.

Four different members of the final five must post through the guarded Discord conversation. A dropped turn can bring in a matching bench member. The new roster must agree again and every replacement must post. A deleted post loses its vote. If the conversation ends early, the proposal ends `talk_only`.

Normal entry, lockouts, independent ownership and the dedicated DC driver's eligibility are checked before anyone leaves a day activity. A cold driver may take up to eight preparations, spaced at least 60 seconds while login is pending. Each uses the same proposal and a short-lived native ticket. The owner handoff has its own 30-second deadline. A confirmed start uses the party tank as leader and normal heroic access and difficulty.

His login leaves an accepted run active. An admitted add-from-guild, raid summon, join or come request for its members can stop it. The original request waits for its own day and dungeon handbacks before continuing. A full, fresh native heartbeat puts the five in play and shows “Playing — Halls of Lightning.” Kills and wipes from that party's instance can reach Discord. A verified terminal with encounter results gives its participants an event memory.

The mind migration is `0065_member_heroics.sql`. Native creates `leeroy_member_dungeon_receipt` in the characters database on the first admitted start. A restart queries the original start ID. Cleanup records the party and instance before disband, preserves the first terminal and retains unresolved bodies. At 600 seconds after a cleanup-failed report, native can release its job slot when it finds no member still under that job’s control. A later party can use disjoint bodies as soon as status confirms the slot was released. The original roster stays fenced until its handback is proved. External GM teleports can leave an unresolved receipt.

Ship mind and worldserver together with `ac-release.sh --native`. The initial rotation contains map 602, whose four encounter slots in route order are 0, 3, 1 and 2. The automated rehearsal uses a synthetic worldserver.

## Not reachable here

- An arena queue and "go to a place" strings.
- A string that brings a member into his instance, or anything inside one: refused by design.
- "Stands in fire" inside instances waits for the dojo's policy work; `skill` changes only what
  the play-style map can say outdoors.
- Romance between members: the persona content rule allows romance only toward his character, so
  a "couple" edit between members is refused.

## Isolated validation

```sh
python -m pytest -q tests/tuning/test_edits.py tests/tuning/test_behaviour_edits.py \
  tests/tuning/test_edits_invariant.py tests/godmode tests/console/test_console_godmode.py
node --test tests/console/js/godmode.test.mjs tests/console/js/strings.test.mjs
python -m pytest -q tests/console/e2e/test_flows.py -k strings   # needs Playwright
```
