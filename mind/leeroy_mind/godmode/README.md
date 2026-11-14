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
This part is mind-only (no native rebuild).

With it off:

- the tune op's `edit` verbs `set` and `preview` and the `string` op answer "Godmode is off";
- nothing attaches: no tick, no intent seeds, no card lines, no openers or reactions;
- `edit list`, `edit retire`, `tuning undo N` and `tuning revert N` still work, so edits made
  while it was on can be taken back.

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
| `life_event` | a new life thread with a screened title, the thread it replaces resolved | the thread is removed (resolved, its beats' memories archived, once the night wrote beats) and the old one is active again |
| `memory_implant` | a memory she simply has (`source owner`, key `owner:godmode:<id>`, dated `days_ago` back) | the memory is rejected |
| `memory_erase` | one active memory archived | its old status comes back |
| `feud_start` / `feud_end` | a feud between two members opened (cause `owner`) or ended, with its story arc | the feud is removed (ended, once the night cited it) or reopened |
| `hangouts` | weights 0 (avoids) to 3 on Discord channels and voice rooms | the overlay is dropped |
| `skill` | star, middle or fire: play style's potions and wait, outdoors | the overlay is dropped |
| `activities` | day-plan activities and partners, for Part C's planner | the overlay is dropped |

Where they are read: traits, hangouts, skill and activities ride on the effective persona
(`tuning.effective.edits_of`); feelings and moods are folded in by `Store.get_relationship` and
`Store.get_state`, and by the raw readers (`server.state.tie_kind`, the server snapshot's ties,
`culture.tease`, the night's encounter partners) through `tuning.edits.feeling_overlay` and
`mood_overlay`. A write through an overlay goes back into the stored row's own terms: a moved
score by the writer's own amount, an untouched one as stored, so dropping the overlay leaves no
offset behind. What the night did meanwhile (a memory, a stance band, a mood it computed) is
the guild's history, as with dials.

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
| `bring_up` {topic[, channel]} | the same seed (general or irl without a channel) | a Discord line of hers in its scope names one of the topic's words (`moves.topics`) |
| `react` {message, emoji} | the server's Reactor, after her presence's delay | the reaction is written |
| `dm_him` {about} | a DM opener on D2's DM path, no outreach item | it is sent, or her DM to you names it |

Each tick (30 s) settles a string `expired` after a day, or `not_taken` with a reason when it
no longer fits: `away`, `gone`, `feud` (a feud with whom it touches), `stance` (warm words or a
loving reaction toward a rival, a DM when she dislikes you), `claims` (it reverses a claim of
hers), `deleted` (the message is gone), or the store's own refusal code. He can cancel a
waiting one. Group DMs and game lines never carry out or show a Discord string. The game kinds
(`come_to_me`, `join_group`, `follow`, `duel_me`, `emote`, `sit`) are Part B's native strings
and are refused `not_yet` here. Discord strings need the guild Discord's engine
(`[server] enabled`, `server_off` otherwise).

The console's Personas member detail has a Strings panel (her strings, their state and reason,
Cancel, and a form for the Discord kinds) and, beside her dials, her godmode edits with Retire
and a form for every kind. `POST /api/string` relays to the mind's `string` op under the same
key, Origin and `--no-control` gates as Board replies.

## Not reachable here

- Strings in the game (Part B, a native rebuild), day plans (Part C), members' heroics (Part D).
- "Stands in fire" inside instances waits for the dojo's policy work; `skill` changes only what
  the play-style map can say outdoors.
- Romance between members: the persona content rule allows romance only toward his character, so
  a "couple" edit between members is refused.

## Isolated validation

```sh
python -m pytest -q tests/tuning/test_edits.py tests/tuning/test_behaviour_edits.py \
  tests/tuning/test_edits_invariant.py tests/godmode tests/console/test_console_godmode.py
node --test tests/console/js/godmode.test.mjs
```
