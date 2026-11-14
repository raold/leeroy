# Member awareness

`card(app_or_stores, guid, *, context=CardContext())` returns a structured `Card`.
`Card.render(budget_tokens=600)` returns deterministic plain text. This package
is read-only. The talk adapter includes one delimited card when `[talk] aware =
true`; it is off by default. The card never calls a model, probes a server,
migrates a database, commits, or logs.

```python
from leeroy_mind.awareness import CardContext, card

result = card(store, 101, context=CardContext(channel="guild", now=receipt_time))
text = result.render(600)
```

The source may be an app with `.store`, a `Store`, a borrowed SQLite connection,
or `CardSources`. The caller owns its connection and transaction. Use a read
transaction when several reads must share one snapshot; the reader leaves an
existing transaction untouched. A copied database is appropriate for inspection.

`CardSources` can additionally carry an already-observed `world`, per-guid `game`
mapping (`area`, `instance_id`, `combat`), fresh `bank_snapshot`, detached body
`holds`, and a clock. The app adapter reads only its stored connection, world,
clock, and cached control bank reply. It does not invoke world role lookups,
body-ledger settlement, or control callbacks. A detached hold snapshot can include
the body ledger's `ALL` sentinel. Private scope ids and payloads are never rendered.

## Sources and priority

Parts occur in this order; the renderer drops whole parts from the end until the
result fits. It uses the existing `text.estimate_tokens` character estimate,
including headings and separators. If even the first part cannot fit, it returns
an empty string. It does not trim a sentence or partially expose a part.

| Part | Source and limits |
| --- | --- |
| In game | Current WorldView presence/map/zone and owner group when supplied; otherwise `cast_member` and recent receipt-time `game_event` observations. `meta.human_guid` identifies the owner. Area and combat remain unknown unless explicitly supplied; a boss event can supply a labeled last-encounter instance. Group and event ids identify participants without fetching names. Durable holds use active root `realm_action` reservations and training/benchmark metadata, or supplied snapshots; only allowlisted codes or generic `realm` appear. |
| Conversation | Caller-supplied channel/purpose, reply, last eight thread items, first four pins, last four directed Discord events. |
| Recent events | Last 15 minutes, at most 80 candidate bridge events and five rendered emotes/loot/deaths/boss kills. Up to three owner whispers only in the matching private conversation. Receipt seconds, not server milliseconds, govern recency. |
| Your claims | At most eight own, live, unsuperseded claims. With a supplied query vector and vector table, use `claims.store.nearest`; otherwise use latest observation order. No embedding is generated. Chat claims must have visible, same-author provenance. |
| Guild raid | Current recorded or most recent `guild_raid`, with kills and wipes. A durable active row is labeled recorded active, rather than treated as a live server probe. |
| Upcoming calendar | First four posted events within 14 days, plus the next posted raid even when later. `realm_rsvp` joins through the native `event_id`, with up to four signups per event so later guild facts still fit. Pending/failed posts are omitted. |
| Your standing | `dkp_ledger` balance/rank and `guild_attendance` status counts. |
| Guild decisions | Only with `[decisions]` on (`CardContext.decisions`): up to five lines of live `guild_decision` rows that touch her (her lineup or bench, the loot rule, a strategy, her request or rank; a proposal only with her position), her vote or code-built position on each, and at most two open `guild_poll` rows she votes in. Bank, rank and officers'-channel polls (and positions on a proposal its officers' poll settles, until that poll is linked) reach only officers' channels (game officer chat, the officers' voice room, officers' Discord channels), never a group DM. See `decisions/README.md`. |
| Guild bank | Cached balance only when observed within 15 minutes; `realm_bank` movement counts over seven days. The ledger is not a current balance; without a fresh snapshot that balance is unknown. |
| Your recent guild drama | At most four active/recent `feud` rows involving this member, gated on visible direct chat/encounter evidence. Only counterpart/id/status, never the generated cause or evidence text. |

Missing tables or older tables without required columns omit affected facts.
`Card.missing` lists affected source names for callers; it is not rendered.
An empty/older database still supplies an explicit unknown game state. SQLite
errors other than missing schema/extension errors propagate to the API caller.
The renderer never treats missing facts as an instruction to invent them.

## Caller context and privacy

Every `CardContext` content field is empty by default:

- `channel`, `purpose`: the current channel identifier and description.
- `to_guid`: the private reply partner, required for matching whispers.
- `readers`: explicit member ids authorized to know a non-WoW channel's purpose.
- `thread`: chronological `ContextItem` messages visible to this turn.
- `reply_to`: the particular `ContextItem` being answered.
- `pins`: relevant pinned `ContextItem` messages.
- `discord_events`: `ContextItem` reactions, mentions, or replies directed at the member.
- `embedding`: optional already-computed 512-dimensional query bytes in the claims
  model's vector space; the reader does not derive it from the text slots.
- `now`: optional receipt-time seconds for deterministic replay and expiry.
- `names`: optional visible-member name map for model cards. This switches IDs
  and epochs to names, named places and relative ages; omitted maps retain the
  diagnostic format used by the CLI.

Each `ContextItem` carries text, its original channel, sender/recipient ids, and
optional `readers`. The Discord caller captures the correct audience
when obtaining the content. Non-WoW, party, raid, and local channel text requires
explicit reader membership and a matching current channel. Guild text is public
within the guild. An explicit audience always further restricts access.

The existing `whisper_ok` pair rule gates private chat and `scope_ok` gates
encounter privacy, including secret encounter/feud roots in the fact ledger.
Whispers stay with the same pair and private turn. Officer
content requires both the stored officer role and a current officer context.
This includes caller slots and claim evidence, not only recent chat reads.
All claims, including owner corrections, require visible cited provenance.
Discord cards omit all game-state sections. Text emotes at the member appear
only in game say or the matching whisper pair; romantic emotes appear only in
the matching pair's whisper card. They never enter guild, raid or Discord cards.
Uncited claims and drama with missing or indirect memory evidence are omitted
conservatively. These omissions can hide otherwise legitimate facts; storage alone
does not establish that a particular conversation may repeat them. Long text
(over 500 characters per item) is omitted, WoW markup removed, and controls folded.

## Live card fitting

Live cards retain their detached sections after the first rendering. If the full
prompt exceeds its budget, they render again into the remaining space, dropping
whole low-priority sections first. Wrappers count toward that allowance. Below
32 available tokens, or when even the highest-priority section cannot fit, the
optional card is omitted. The observation block stays before the instruction;
the switches-off prompts and the live prompt budget are unchanged.

## CLI and checks

From `mind/`, using its Python environment:

```sh
python -m leeroy_mind awareness card 101 --data-dir /path/to/copy
python -m leeroy_mind awareness card 101 --data-dir /path/to/copy --budget-tokens 400
python -m pytest -q tests/awareness tests/claims
```

The CLI opens an existing `mind.db` with `mode=ro`, never creates or migrates it,
and prints only to the terminal. It supplies empty conversation slots and no
query embedding. Errors expose exception classes only. It starts no model client
or service and does not configure logging. The root CLI registration is independent
of the existing `claims` and `eval` commands.

The reader uses a bounded number of SQL statements, with no per-claim/per-evidence
query loop. Row scans such as DKP aggregation still depend on database size.
Measurements belong in the PR with their dataset and warm-cache conditions;
they are not latency guarantees. Tests use invented members and cover missing
schemas, privacy, budget boundaries, deterministic reads, and transaction safety.
