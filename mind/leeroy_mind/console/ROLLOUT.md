# Switch a feature off safely

Shipped features are on (see [AGENTS.md](../../../AGENTS.md#feature-switches)). Use
this guide when one must be turned off: it misbehaves, the owner asks, or a
rollback needs it. Turning a switch off stops new work. It does not undo work
already started, and it never proves a body has returned or a command has not
run. A file edit is not proof either: check that the running service consumed it.

An off switch the owner wants kept off is left as an explicit `false` (or `0`)
with a comment saying who ruled it and when. Never leave it unset.

## Before you change anything

- Record the key, its old value and the time. Keep a private copy of the config.
- Note the process starts, bridge boot identity and the last log offset, so
  later lines can be tied to the change.
- Read, do not edit, the state first: unresolved decision, direct-job, permit,
  start and receipt IDs. Use SQLite `mode=ro` or a consistent `backup()` copy,
  never a plain copy of a running WAL database.
- Keep evidence outside the repository. Counts, IDs, codes and timestamps only:
  no chat, names, persona text or credentials.
- Change one key at a time and check it before the next. If the cause is not
  yet clear, switch off the narrowest switch that stops it.

## How each kind of switch is applied

| Switch | Applied by |
|---|---|
| Native `Leeroy.*` keys | `.reload config` in the worldserver, then restart the mind so it reconnects and reads the new bridge hello (the native hello is sent only on a new connection), then check the hello and the Ops tab |
| `[decisions]` and `[server]` | Restart mind and console in the agreed window |
| `[life]` | Read on session or night reload; record the reload that consumed it |
| Most other `mind.toml` sections | Read at mind start; restart the mind |

Check the Ops tab afterwards: it shows each switch's state as the running
service read it. Before a restart, check the owner is not online and has no
recent console Discord posts waiting.

## Rollback order

Stop new admission first, let in-flight work settle, then close the native gate.

1. Switch off the mind-side admission key (for example `[life] day_plans`,
   `[life] dungeons`, `[decisions] bank_requests`, `[decisions] boss_strategy`)
   and apply it as the table says. This stops new plans, proposals, requests
   and strategy use.
2. Verify the running service consumed it. A cap that is also a throttle
   (`bank_items_per_day`) goes to zero as its own, later change.
3. Let active jobs reconcile. Preserve original IDs, journals and receipts. Use
   the feature's typed stop or handoff workflow if a run must end, subject to
   its combat fences.
4. Inspect what is outstanding, then switch off the native gate
   (`Leeroy.DayPlans.Enable`, `Leeroy.MemberDungeon.Enable`,
   `Leeroy.Decisions.BankMail.Enable` and the like) and `.reload config`, then restart the mind so it reconnects and reads the new
   hello.
5. Observe one normal scheduled cycle and a reconnect before calling it done.

Never kill the server to clear ownership, clear a journal, delete permits,
abandon a root, replay a committed command or rewrite a ledger to make a
dashboard look healthy. An unknown operation stays unknown and fenced for the
documented investigation.

## What to check afterwards

- No new dispatch, admission or body movement for the feature.
- No duplicate send, no stuck owner request, no wrong recipient, no false public
  success.
- In-flight operations reconcile by their original IDs, including across a
  reconnect. A new boot alone is not proof a body has returned.
- Chat and voice still answer. Older features that share the gate (an
  established `[decisions] enabled`, `[server] enabled` or `[realm] enabled`) are
  left on unless the cause is in them.
- The switch reads off in the Ops tab and the record says why.

## Feature notes

- **Day plans.** Switch off `[life] day_plans` and apply the reload. Let the
  journal reconcile and revoke permits and verify return before disabling
  `Leeroy.DayPlans.Enable`. Do not clear Realm holds. See
  [Day plans](../godmode/README.md).
- **Member heroics.** Switch off `[life] dungeons` to stop proposals and
  admission; keep the original start and receipts. Inspect outstanding jobs,
  then disable `Leeroy.MemberDungeon.Enable`. Disabling never proves return: at
  600 seconds a durably failed run releases its job slot without proving every
  body returned, and unresolved bodies stay fenced. See the
  [unresolved-return procedure](../godmode/README.md#inspecting-an-unresolved-return).
- **Bank requests.** Set `bank_requests = false` and apply it, then separately
  return the outgoing cap to zero. A withdrawal already committed cannot be
  undone by a flag: never replay it. Escalate an item in the officer's bags or an
  uncertain mail outcome by its original decision/job ID. Disable
  `Leeroy.Decisions.BankMail.Enable` only after in-flight evidence is captured.
  See [Decisions](../decisions/README.md#release-and-switches).
- **Boss strategy.** Set `boss_strategy = false` and apply the Decisions reload.
  Keep the decision and training journal; removal is verified at the next safe
  muster or run cleanup. Never change levers during combat to make the rollback
  look immediate. See [Boss strategy](../decisions/README.md#boss-strategy).
- **Rank reviews.** They can demote members in game; switch off first when in
  doubt.
- **Training and telemetry.** `Leeroy.Training.Enable = 0` stops additions and
  requests verified cleanup, which may remain pending; disabling a flag is not
  proof that everything was restored. Do not change this shared gate for another
  feature's rollback.

## Private record

One record per key change, kept with the release evidence and outside Git:

```text
Feature / key:
Previous value / new value / applied at:
Reload or restart that consumed it:
Mind process / bridge boot / log offset before and after:
Outstanding IDs at the time (decision, plan, permit, start, job):
Observed reconcile, handback and next-cycle evidence:
Unexpected errors:
Chat/voice and owner-request observations:
Remaining unknowns:
```
