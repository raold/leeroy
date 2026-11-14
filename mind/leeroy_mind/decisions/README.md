# Decisions (D4)

What the guild settles in its Discord becomes policy in game. This package holds the decision
ledger and the officers' polls, members' data-built positions and votes, the discussion seeds for
the D2 engine, the Decisions card part, and his vote and overrule. Part A (this) records and shows
decisions; the kinds that carry them into the game (lineups and loot rules, then the bank, boss
strategy and ranks) register with `apply.register` in later parts.

## The switch

`[decisions]` in `mind.toml`, read once at the start (restart the mind and the console):

| Key | Default | Range | Meaning |
|---|---|---|---|
| `enabled` | `false` | bool | Needs `[server] enabled`; without it the table logs `decisions_need_server` and stays off. |
| `bank_items_per_day` | 10 | 0-50 | Stacks leaving the bank a day (the bank kind, a later part). |
| `bank_restock_per_week` | 20 | 0-60 | Stacks the crafters restock a week; 0 turns the restock off (a later part). |
| `loot_poll_days` | 14 | 3-60 | Fewest days between loot-rule polls (the loot kind, a later part). |

An invalid table is off (`decisions_config_invalid key=`). Off adds no task, seed or card part,
and the golden prompts are byte-identical (`tests/decisions/test_off.py`).

## The ledger (migration `00NN_decisions.sql`)

- `guild_decision`: kind (`lineup`, `loot_rule`, `bank`, `strategy`, `rank`), subject (`raid:<cal id>`,
  `loot`, `bank:<req>`, `strategy:<map:boss:diff>`, `rank:<guid>`), value (JSON), positions
  (`[{guid, side, why}]`, codes), who settled it (0: a poll), the poll and announcing message,
  status, times and `effect_ref` (what changed: direct-ledger refs, ledger ids, receipt keys).
  One live decision (proposed, settled, in effect) per kind and subject: a newer proposal
  supersedes the old in the same transaction (`supersedes` names it).
- Status moves: `propose` -> `settle` -> `take_effect`; `talk_only` (nothing in game carries it),
  `expire`, `fail`, and his `overrule` (any live status; `overruled_by_owner = 1`).
- `guild_poll` and `guild_poll_vote`: a poll in a guild channel with 2-6 option codes, its voters
  fixed at open (he is added from `meta.human_guid`: one voter in every poll), and the positions
  the members' votes will follow. A vote is final. A tie or no votes closes with no result and
  the status quo stands. Polls close at their closing time.

## Positions and votes

`positions.py` builds each member's side and a `why` code from rows (DKP balance and rank,
30-day attendance, the bench in the last three raids, wipes) and the persona (agreeableness, the
argumentative and drama dials, guild role, how she thinks). Same inputs, same side; never parsed
from model text. Each officer votes once at a fixed draw 10 minutes to 6 hours after a poll opens,
while her Discord presence is online or idle (the service's minute tick); the vote is her
position's side with its `why`. A kind may define `closed(app, poll, winner)` to act on a result.

## Talk

- Seeds (`seeds.py`, gathered by `server/seeds.py` only when on): one per stage (proposed, poll
  open, settled), key `decision:<id>:<stage>` (`decision:p<poll>:poll` for a poll), a fact built
  from rows ("benched 2 of the last 3", "DKP 340, rank 1 of 5"), its stakeholders as `about`.
- The card's Decisions part (after Your standing): decisions touching her, her vote or position
  on each, the open polls she votes in, at most five lines.
- Privacy follows the audiences of `memory/AUDIENCES.md`: bank requests, ranks and any decision
  announced in an officers' channel are the officers' business; votes and positions of a poll
  held in an officers' channel likewise. They reach officers' rooms in either medium, and a group
  DM only when every reader but him is an officer; never a whisper, a DM or a public room.

## His view and his overrule

- Console Guild tab, Decisions (hidden while off with nothing recorded): the ledger newest first
  with what each changed, and the polls. Poll cards in Comms on the message that carries a poll.
- Control op `decision`: `{action: "vote", poll, option}` or `{action: "overrule", id}`
  (`POST /api/decision`; the key, Origin and `--no-control` gates apply). Overrule of an in-effect
  decision calls its kind's `undo` only where `reversible(row)` says the game can undo it;
  otherwise it answers "can't be undone in game" and stops its future effect only. Logs
  `decision_op action= id= outcome=`, never text or names.
