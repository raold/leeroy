# Godmode (D6 Part A)

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
`culture.tease`, `arc.live`'s rival line and the console's Personas and Social views. A lens row
(`EffectiveRelationship`, `EffectiveState`) is never written: `put_relationship` and `put_state`
refuse one. Traits, hangouts, skill and activities ride on the effective persona for talk
(`tuning.effective.edits_of`); the night reads her persona as stored and dialled
(`NightContext.persona`, `load_effective`). So undoing an overlay edit leaves exactly what the
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
| `come_to_me` | comes to his side; outside his group she then stays there 10 minutes | `bad_area` (he is not in a city or rest area), `grouped` |
| `join_group` | joins the party he leads (never a raid) and comes to him | `in_group`, `lfg_group`, `not_leader`, `in_raid`, `group_full`, the add command's own fences |
| `follow` {} | follows him; the same string again stops it and she waits | `not_in_group`, `busy:raid_owner` (his raid) |
| `duel_me` | challenges him to a duel (accepting is his) | `in_group`, `busy:duel`, `too_far`, `bad_area` |
| `emote` {emote} | one of 16 friendly emotes, at him | `bad_emote`, `too_far` |
| `sit` | sits near him for up to 5 minutes while he stays near | `too_far` |

Shared codes: `off`, `human_offline`, `not_cast`, `not_online`, a fence (`busy:realm_owner`,
`busy:training_owner`, ...), `dead`, `combat` (hers, his or his group's: never inside his fight), `in_instance`
(never in or onto an instance copy), `in_bg`, `queued`, `travelling`, `human_dead`. The mind
settles some before or instead of native: `human_offline` (he is not in game), `no_link`,
`no_answer`, `unsupported` (an older worldserver), `away`, `gone` and `stance` (a duel from one
who dislikes him, affinity -30 or below; a kiss or a flirt only from a woman drawn to him or in
lust, a hug or a wink from a fond one too).

A game string goes at once, in the console's request: the mind checks her fit, him in game and the
bridge, sends one `string_do` and settles the string `taken` with what she did (native's done job:
`came`, `joined`, `following`, `stopped`, `challenged`, `emoted`, `sat`) or `not_taken` with the
code. It is never retried: one refused in his fight stays refused. One a crash left in flight
expires after 10 minutes. For the next half hour her card (game channels with him: party, raid,
say, yell, emote, a whisper with him; and her DM with him) says what she did, as her own past
("You joined his group because he asked."), or that she could not ("He asked you to come over, but
you couldn't just then."); never in guild or officer chat, a server channel or a group DM, and
nothing for a refusal she would not have known of (the server, the link, his own state, her being
offline). The card needs `[talk] aware = true`, as Part A's lines do. Nothing here takes a Realm
body-ledger rank; native's fences and journal are in `modules/mod-leeroy/README.md`.

The console's Personas member detail has a Strings panel (her strings, their state and reason,
Cancel, a form for the Discord kinds, and "In game": a button for each game kind, the emote with
its list; the answer says what she did or why not) and, beside her dials, her godmode edits with
Retire and a form for every kind. `POST /api/string` relays to the mind's `string` op under the same
key, Origin and `--no-control` gates as Board replies.

## Not reachable here

- Day plans (Part C), members' heroics (Part D); an arena queue and "go to a place" strings.
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
