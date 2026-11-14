# Decisions (D4)

What the guild settles in its Discord becomes policy in game. This package holds the decision
ledger and the officers' polls, members' data-built positions and votes, the discussion seeds for
the D2 engine, the Decisions card part, and his vote and overrule. Part A (this) records and shows
decisions; the kinds that carry them into the game are `apply.BUILTIN`: Part B's lineups and loot
rules (below), then Part C's bank, boss strategy and ranks. Each decisions service builds its own
instances of them (`apply.builtin_rules`), so no kind state is shared between apps; the console
loads a stateless set for `reversible()` only (`load_builtin`), and `apply.register` puts a
stand-in ahead of both (tests).

## The switch

`[decisions]` in `mind.toml`, read once at the start (restart the mind and the console):

| Key | Default | Range | Meaning |
|---|---|---|---|
| `enabled` | `false` | bool | Needs `[server] enabled`; without it the table logs `decisions_need_server` and stays off. |
| `bank_items_per_day` | 10 | 0-50 | Stacks leaving the bank a day (the bank kind, a later part). |
| `bank_restock_per_week` | 20 | 0-60 | Stacks the crafters restock a week; 0 turns the restock off (a later part). |
| `loot_poll_days` | 14 | 3-60 | Fewest days between loot-rule polls (Part B's loot rules). |

An invalid table is off (`decisions_config_invalid key=`). Off adds no task, seed or card part,
and the golden prompts are byte-identical (`tests/decisions/test_off.py`).

## The ledger (migration `00NN_decisions.sql`)

- `guild_decision`: kind (`lineup`, `loot_rule`, `bank`, `strategy`, `rank`), subject (`raid:<cal id>`,
  `loot`, `bank:<req>`, `strategy:<map:boss:diff>`, `rank:<guid>`), value (JSON), positions
  (`[{guid, side, why}]`, codes), who settled it (0: a poll), the poll and announcing message,
  status, times and `effect_ref` (what changed: direct-ledger refs, ledger ids, receipt keys).
  Per kind and subject at most one decision is pending (proposed or settled) and one is in
  effect. A newer proposal supersedes the pending one; the rule in force stays in force while its
  successor waits (a loot rule settled mid-raid waits for the next raid, a rank or strategy
  proposal leaves the standing overlay alone). `take_effect` supersedes the rule in force and
  names it in `supersedes`, in one transaction. An overruled, expired or talk-only successor
  leaves the rule in force standing.
- Status moves: `propose(..., poll=)` -> `settle` -> `take_effect`; `talk_only` (nothing in game
  carries it), `expire`, `fail`, his `overrule` (any live status; `overruled_by_owner = 1`), and
  `restore` (an undo puts the decision it replaced back in effect; `overrule(..., restore_previous=True)`
  does both in one transaction). `pending`, `current` and `live` (pending, else current) read them.
- The database holds the rules too: one pending and one in-effect row per kind and subject (two
  partial unique indexes), the owner's flag only on overruled rows, settled/in-effect times present,
  and a vote can never be updated or deleted (triggers).
- `guild_poll` and `guild_poll_vote`: a poll in a guild channel with 2-6 option codes, its voters
  fixed at open (he is added from `meta.human_guid`: one voter in every poll), and the positions
  the members' votes will follow. A vote is final. A tie or no votes closes with no result and
  the status quo stands. Polls close at their closing time. `attach_message` names the post
  that carries a poll (its card in Comms).
- A closed poll's result reaches its kind's `closed(app, poll, winner)` hook exactly once: the
  receipt is `guild_poll.handled`. A kind writes `polls.mark_handled` in the transaction that
  records its decision; otherwise the service writes it when the hook returns. A failing hook is
  tried again on later ticks (`hook_tries`, at most 5, then `decisions_poll_hook_gave_up`), and a
  restart between closing and the hook loses nothing. A poll whose kind has no hook yet waits.

## Positions and votes

`positions.py` builds each member's side and a `why` code from rows (DKP balance and rank,
30-day attendance, the bench in the last three raids, wipes) and the persona (agreeableness, the
argumentative and drama dials, guild role, how she thinks). Same inputs, same side; never parsed
from model text. Each officer votes once at a fixed draw 10 minutes to 6 hours after a poll opens,
while her Discord presence is online or idle (the service's minute tick); the vote is her
position's side with its `why`. Presence is computed from the presence inputs only (as
`server.state` does, without its message scan), for the members whose vote is due.

## Talk

- Seeds (`seeds.py`, gathered by `server/seeds.py` only when on): one per stage (proposed, poll
  open, settled), key `decision:<id>:<stage>` (`decision:p<poll>:poll` for a poll), a fact built
  from rows ("benched 2 of the last 3", "DKP 340, rank 1 of 5"), its stakeholders as `about`. A
  stage is argued once: a used `decision` key never becomes fresh again.
- The card's Decisions part (after Your standing): decisions touching her, her vote or position
  on each, the open polls she votes in, at most five lines.
- Privacy follows the audiences of `memory/AUDIENCES.md`: bank requests, ranks and any decision
  announced in an officers' channel are the officers' business; votes and positions of a poll
  held in an officers' channel likewise, and the positions on a proposal of a kind the officers'
  poll settles (loot rules, ranks) until its poll is linked (`propose(..., poll=)`), so a proposal
  never seeds publicly before its audience is known. They reach officers' channels in either
  medium (game officer chat, the officers' voice room, officers' Discord channels) and nothing
  else: never a group DM, a whisper, a DM or a public room.

## His view and his overrule

- Console Guild tab, Decisions (hidden while off with nothing recorded): the ledger newest first
  with what each changed, and the polls. Poll cards in Comms on the message that carries a poll.
- Control op `decision`: `{action: "vote", poll, option}` or `{action: "overrule", id}`
  (`POST /api/decision`; the key, Origin and `--no-control` gates apply). Overrule of an in-effect
  decision calls its kind's `undo` only where `reversible(row)` says the game can undo it;
  otherwise it answers "can't be undone in game" and stops its future effect only. The page asks
  first; an overrule the game cannot undo also asks him to tick that he understands. After an
  undo the row is read again; if the ledger cannot record the overrule it says so (the game was
  changed) and the service keeps retrying the write. Overruling a pending successor leaves the
  rule in force. Logs `decision_op action= id= outcome=`, never text or names.
- Tonight counts the open polls still waiting for his vote, and reads "not set up yet" while
  `[decisions]` is off.

## Part B: the lineup (`lineup.py`)

- **Proposed** for each raid night the Realm's calendar desk posted (`realm_calendar` kind `raid`,
  so it needs `[realm]` on), once, between 30 h and 6 h before it (`LINEUP_AHEAD_S`), in the name of
  the night's raid lead (`comp.choose_leads`). `build_lineup` is `select_comp` over the cast, never
  him (he always raids, in a damage seat): `Candidate.ilvl` from the armory's equipment read and,
  among members of equal priority, the one adding a missing Bloodlust or Replenishment source
  first (`buff_ties`), then the better gear. Its `coverage` codes say which it has (`no_<buff>`
  when not). The size follows his newest raid of the last two weeks, else 25 with 22 available.
  The bench it posts is the next `BENCH_MAX` (10) raiders and officers by priority. Positions
  (Part A's `lineup_positions`) put the members who object first.
- **Posted** two hours later (outside the server's quiet window): the lead's post in #raid-planning
  is the code-built roster by role with the bench (and what it misses), and the decision is settled
  by her with that message. The D2 engine argues it at both stages (Part A's seeds).
- **Formed from**: `RaidNight._form` reads the settled (or in-effect) lineup of the night at hand
  (from 6 h before its time to 6 h after) through the guild ports (`lineup`, `lineup_formed`). Its
  available members go in as `must_include` within capacity and each role's seats (the leaders his
  pick or the rotation chose stay required); its bench is held out and used only for a role nobody
  else can fill; unavailable members are filled by `select_comp` as without a lineup
  (`lineup_partial id= missing=`). The lineup's bench stays the raid's bench (standby DKP). Once the
  raid has begun it is in effect (`effect_ref {"raid": id}`).
- **Followed by his add**: his raids are usually groups he built with `.playerbots bot addN`. When
  native's `guild_group` (`built` or `entered`) says `roster_followed: true` (the picker used the
  posted roster for at least one member it added), `RaidNight._adopt` puts tonight's lineup in effect
  (`{"raid": id, "via": "add"}`), and the lineup's bench (its available raiders and officers outside
  the raid) stands by with standby DKP, as a formed raid's bench does. `false` (or an older
  worldserver) leaves it settled, and talk only at the night's end.
- **One night**: a lineup settled only after a raid of his began that night (not an offline raid)
  steers the next night only: no formation that night forms from it and the picker never holds it.
- **Ended** with its night (6 h after its time): never posted, `expired`; posted but never formed
  from, `talk_only` (`not_formed`); formed from, `ledger.retire` (status `expired`, its effect kept).
- **The picker in game**: while a posted lineup's night is at hand and no raid of his has begun
  that night, native holds its roster (`guild_add_roster {members, bench, until_ms}`, `until_ms` its
  night's end, at most 12 h ahead), and the owner's `.playerbots bot addN` follows it when
  `Leeroy.GuildAdd.FollowRoster = 1`: within each role its members first, its bench only when nobody
  else free fits. The mind sends it once per change, again after every hello (a worldserver start
  clears it), and an empty roster once his raid began, the night ends or he overrules the lineup. A
  refusal logs `guild_add_roster_refused error=`.
- **The tick**: proposing, posting, ending and the roster are separate steps; one that fails is
  logged (`lineup_step_failed step= error=`) and the others still run. A night no lineup can be
  built for (no tank) is tried again hourly; a failed armory read backs off 15 minutes (lineups go
  without item levels meanwhile).
- **His overrule**: before it formed a raid, overruling stops it (and clears the picker's roster).
  Once formed from it moved bodies: the game can't undo it, so his overrule stops its future effect.

## Part B: loot rules (`loot_rules.py`)

- **The modes**: `dkp` is today's award. `gear_priority` ranks by the slot's role priority, then
  the biggest upgrade, then today's order, charged as DKP. `loot_council` gives the item to the
  highest gain x (0.5 + 30-day attendance share) x role weight (a role first on a slot weighs 1.25,
  1.5, ... by its place), charges nothing and writes the `item` row with `delta=0` and note
  `council:<receipt>`. A rule's `prices` (by slot) override `[dkp] prices` while it stands. His claim
  always wins, at the rule's price (none under a council); the member he beat remembers losing it
  (`loot_lost`) and the guild talks about it in #loot-and-dkp (the `his_loot` seed, a week): "who
  wanted it" only when she bid wanting it, else "who could use it". It is about him, so who talks
  passes culture's teasing gates for him (affinity, his hurts, one thread at him an evening), and a
  delivered thread spends his allowance; at most one such seed a member a local day.
- **The poll**: the loot officer (else the lowest-guid officer) opens it in #officer-chat no sooner
  than `loot_poll_days` after the last, or 3 days after it when a close award or his claim made
  drama since (within 7 days), once the guild has raided in 30 days, never beside an open poll or a
  rule waiting. Options are the three modes; a `role_first` proposal comes from the last 4 raids'
  receipts (the slots two or more members wanted most, roles in order of the upgrade they sought)
  and rides on gear priority or a council. It runs 24 h or until every officer has voted (his vote
  is optional: officers settle it, and he can overrule). A tie keeps the rule in force; the rule in
  force voted again is no new decision ("Loot rules stay: DKP."); another winner is settled by the
  poll at once. Its post in #loot-and-dkp waits out the server's quiet window (meta
  `decisions:loot_rule:announce:<poll>`, then `ledger.announce` names it on the decision).
- **Taking effect**: only while no raid is open or forming and his raid is in no fight, wipe silence
  or debrief. Each raid fixes the rule in force when it opens (meta `decisions:loot_rule:raid:<id>`,
  in the unit that opens it), so a rule settled or undone mid-raid applies from the next raid.
- **His undo** (reversible while in effect) puts the rule it replaced back in effect (the ledger's
  restore); the next raid reads it.
- Receipts record `mode`, `slot` and each bidder's `role` and `share` only while decisions are on;
  off, a receipt and every award are exactly today's.

## For Part C

- `strategy_positions` counts every `tg_pull` wipe on the context, benchmark and offline pulls
  included; Part C should count the guild's own pulls only (`kind` in `his`/`offline` as it rules).
- Officers' business never reaches the member it is about: she does not learn the outcome of her
  own bank request or rank change from her card, though the plan lists them as touching her.
  Decide in Part C how the outcome reaches her (an announcement in a public channel, or a
  pair-scoped line to her).
