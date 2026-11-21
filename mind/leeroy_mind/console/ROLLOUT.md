# Roll out bank decisions, day plans and member heroics

This is the operator runbook for a staged rollout **after** the owner releases
these features dormant. It is not an activation script and merging it changes no
configuration. Until the release identity and its dormant baseline are recorded,
all stages below are **NOT RUN**. A PR, green CI, a successful migration, or quiet
logs alone is not evidence that a feature works in the live realm.

## Establish the dormant baseline

Follow [RELEASE.md](RELEASE.md) for deployment. The release must include the bank,
day-plan and heroic implementations plus the guide's recovery corrections (#398,
#399, #401 and #402), their native build and migrations through 0065. Record the
installed commit, native binary/build identity, mind and console process starts,
schema version, data directory, fresh bridge boot identity and capabilities. Use
the installed checkout's CLI and existing operator connection; do not point a
development checkout at live data or create a second bridge client.

Preserve a private configuration backup and the release recovery instructions.
Record existing values before changing them; do not replace unrelated settings.
Verify these *new admission* gates are dormant:

| Mind | Native |
|---|---|
| `[decisions] bank_items_per_day = 0`, `bank_restock_per_week = 0` during the canary | `Leeroy.Decisions.BankMail.Enable = 0`, `Leeroy.Decisions.BankRestock.Enable = 0` |
| `[life] day_plans = false` | `Leeroy.DayPlans.Enable = 0` |
| `[life] dungeons = false` | `Leeroy.MemberDungeon.Enable = 0` |

Do not turn off an already established `[decisions] enabled`, `[server] enabled`
or `[realm] enabled` just to establish this baseline. Those gates also own older
features. Bank mail requires the existing Realm permissions, item whitelist and
armory read connection. Day plans need native Realm enabled too. Missing shared
prerequisites are a separate change to observe before advancing, never a bundle
of silent enabling edits. Leave strategy/rank, training and roster settings at
their approved values.

Take a read-only evidence baseline: decision/direct-job IDs and states, bank
outflows, day plans/permits/handoffs, member heroic IDs/phases and unresolved
native receipts. Use SQLite `mode=ro` or a consistent `backup()` copy; do not copy
a running WAL database with a plain file copy. Store logs and evidence outside
the repository. Preserve counts, IDs, codes and timestamps; no chat, names,
persona text or credentials belong in a PR.

Observe one ordinary operating interval with the feature gates still off:
normal chat, an owner request, a reconnect, and one scheduled mind cycle. Record
pre-existing errors rather than attributing them to the next switch. Confirm no
new feature admission or body movement. Log retention is a separate release;
until it ships, copy the relevant native rotations privately before restarts.

## Change and observation protocol

Change **one configuration key at a time**. Native keys use the operator's
`.reload config` and a fresh capability/greeting check. Bank config is read at
mind startup, so restart mind and console in the agreed window. Life config is
read on session/night reload; record the reload that actually consumed it. A
file edit is not proof that a running service has applied it.

After each edit, record old/new value, application time, process/boot identity,
the last log offset and resulting capability/state. Observe a normal poll and
reconnect before the next edit. Once admission is enabled, wait for the actual
end-to-end behavior listed below, **then one further normal scheduled cycle**.
No fixed sleep substitutes for that evidence. If there is no eligible bank
request, day block or heroic roster, mark **WAITING FOR EXERCISE**, not PASS, and
leave the next feature off. Never manufacture consent, items or completion rows.

Keep previously accepted features on while observing the next, so priority and
resource conflicts are visible. Stop advancement on duplicate dispatch, unknown
or contradictory body ownership, a stuck owner request, wrong recipient/copy,
false public success, unexpected errors, or degraded chat/voice responsiveness.

## 1. Bank decisions

1. Keep both bank caps at zero and restock native disabled. Enable only
   `Leeroy.Decisions.BankMail.Enable`; verify `member_bank_mail` in a fresh
   greeting and no withdrawal while the cap is zero.
2. If shared Decisions/Server/Realm prerequisites are not already active,
   establish them individually with the bank caps still zero. Do not raise the
   cap until the existing features and baseline remain healthy.
3. Set `bank_items_per_day = 1` for the first observed delivery. Restart mind and
   console to apply it. Wait for one naturally eligible request and its guarded
   officers-only post; inspect the settled bank decision and original direct
   withdrawal/mail command IDs.
4. Require exactly one confirmed withdrawal/debit and one matching positive mail
   terminal, for the intended guild member, with zero gold. The native mail value
   is the first attachment item-instance GUID, not a mail-header ID. Confirm the
   decision reaches `in_effect`; do not equate posting with the member collecting
   the mail. A delayed response/reconnect must reconcile the original command,
   never send a second withdrawal or letter.
5. Confirm the last stack and a nonempty tab survive, the owner/nonmember cannot
   receive this mail, and the daily cap prevents another outgoing stack. Validate
   those fences from the real request and stored inventory; do not intentionally
   drain the bank to test them.
6. After that delivery and the next normal cycle are clean, restore the owner's
   recorded cap (default 10; supported range 0–50), as its own change. Observe
   again. Leave restock at zero/native off unless it is included in this rollout.

If restocking is also desired, treat it as a separate bank substage: with its
weekly cap zero, enable `Leeroy.Decisions.BankRestock.Enable`, check
`bank_stock_target`, then set `bank_restock_per_week = 1` separately. Wait for an
eligible weekly top-up and a single matching maker/item stock receipt. Only then
restore the owner's recorded weekly cap (default 20, range 0–60). A week with no
eligible maker/deficit is not a tested top-up. Do not delay unrelated accepted
features on a claim that restocking was tested when it was not.

Rollback: stop new bank admission by setting the outgoing/weekly caps back to
zero and applying the mind configuration. Preserve and reconcile already
submitted original jobs. Disable the affected native admission key only after
in-flight evidence is captured; a withdrawal already committed cannot be undone
by flipping a flag. Never replay it or rewrite the bank ledger. Escalate an item
in the officer's bags or uncertain mail outcome by its original decision/job ID.
See [Decisions](../decisions/README.md#release-and-switches).

## 2. Day plans

1. With `[life] day_plans = false`, enable only `Leeroy.DayPlans.Enable`. Apply
   native config and verify `day_plan_handoff_v1`; confirm no day admission.
2. Enable `[life] day_plans = true` separately and record its actual reload.
   Observe the next scheduled reflection and the next eligible day block. A
   planned row is not evidence of arrival. Check reciprocal friend cohorts,
   current rival/feud exclusions, raid reservations and the twenty-member limit.
3. Follow one cohort's plan -> permit -> dispatched step -> native arrival ->
   activity receipt -> completion/return. Require the exact boot, token, serial,
   member and command identity. Cards and memories must describe only confirmed
   arrivals/activities. Fishing success belongs only to the successful actor.
4. During an owner's normal validated add/raid/string request, verify the cohort
   yields and native closes the exact permit before the original command runs.
   It keeps its original 30-second deadline; a timeout must not secretly run the
   request later. Login alone may coexist with day activity.
5. Check the next scheduled block with bank decisions still enabled and ordinary
   chat/voice functioning. Preserve unresolved dispatched operations over a
   reconnect; a new boot alone is not proof that a body has returned.

Rollback: disable `[life] day_plans` and apply the reload to stop new plans/body
commands. Let the existing journal reconcile/revoke permits and verify return
before disabling the native gate. Do not clear Realm holds, delete permits or
manually abandon a root to make the dashboard look healthy. Unknown operations
remain fenced for the documented operator investigation.
See [Day plans](../godmode/README.md).

## 3. Member heroics

1. With `[life] dungeons = false`, enable only `Leeroy.MemberDungeon.Enable`.
   Reload native config and verify `member_dungeon_v1` and enabled capability.
   Verify DungeonClear and ordinary level-80 heroic eligibility. The native
   receipt table may be absent until the first admitted start; that is expected.
2. Enable `[life] dungeons = true` separately and record its reload. Wait for a
   natural proposal while the owner is away. Require four distinct, actually
   delivered guarded Discord posts from the final roster before admission;
   replacement members need their own posts. Do not create synthetic agreement.
3. Follow the sole original start ID through any bounded driver preparation,
   admission, the tank/healer/three-damage party, map **602** (Halls of Lightning),
   difficulty and instance copy. Require a fresh heartbeat naming all five
   before counting them as playing. Preserve earned binds; no progression grants
   or unbinding belong to this feature.
4. Observe real encounter progress (script slots in route order **0, 3, 1, 2**),
   kills/wipes, terminal and each member's verified handback. Inspect the native
   receipt's revision and original immutable terminal, not only the console
   phase. Partial witnesses get lower-bound personal results; no invented
   party-wide speaker or unobserved clear may be posted.
5. On the owner's next ordinary login, confirm an already accepted run continues.
   A validated request for its members must instead obtain positive handback
   before the original request resumes, inside its original 30-second deadline.
   Observe this handoff separately if it did not occur during the first run.
6. Require the next normal scheduled cycle to show no duplicate proposal/start,
   no unresolved new body hold, and no interference with bank decisions, day
   plans, offline raids or chat/voice. Without real progress/return evidence the
   live heroic stage stays unverified even when all automated tests pass.

Rollback: disable `[life] dungeons` to stop new proposals/admission, preserve the
original start and receipts, and allow active-job cleanup/status reconciliation.
Use the existing typed stop/handoff workflow for that exact run if stopping is
needed, subject to its combat fences. Do not kill the server to clear ownership.
Disable the native admission gate after inspecting outstanding jobs; disabling
never proves return. At 600 seconds a durably failed run can release its job slot
without proving every body returned. Unresolved bodies stay fenced. Preserve the
per-member handbacks and inspect them with the
[unresolved-return procedure](../godmode/README.md#inspecting-an-unresolved-return).

## Private observation record

Create one record per key change, and a separate end-to-end acceptance record for
each feature. Keep it with the release evidence, outside Git:

```text
Stage / feature:
Status: NOT RUN | WAITING FOR EXERCISE | OBSERVING | PASS | STOPPED
Installed commit / native build / schema:
Mind process / bridge boot / config reload evidence:
Key / previous value / new value / applied at:
Baseline log range / observation log range:
Original decision, plan, permit or start IDs:
Observed dispatch, result, handback and next-cycle evidence:
Expected fence/refusal codes versus new unexpected errors:
Chat/voice and owner-request observations:
Rollback performed / remaining unknowns:
Next stage permitted (only after evidence above):
```

The PR implementing this runbook makes no claim that any live stage has run.
Runtime activation waits for the owner's next release to ship the features dormant.
