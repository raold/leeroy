# Roll out day plans, member heroics and decisions

This is the operator runbook for a staged rollout **after** the owner releases
these features dormant. It is not an activation script and merging it changes no
configuration. Wait for the owner to report the release complete; a scheduled
release time is not that confirmation. Until the release identity and its dormant
baseline are recorded, all stages below are **NOT RUN**. A PR, green CI, a successful migration, or quiet
logs alone is not evidence that a feature works in the live realm.

## Establish the dormant baseline

Follow [RELEASE.md](RELEASE.md) for deployment. The release must include the bank,
day-plan and heroic implementations plus the guide's recovery corrections (#398,
#399, #401 and #402), the #411 per-kind decision switches, their native build and
migrations through 0065. Member heroics additionally require the
[#401 receipt follow-up, #415](https://github.com/raold/leeroy/pull/415) to be
reviewed, merged **and included in the installed native build**:
tolerant offline handback, retryable receipt loading and asynchronous receipt
checkpoints. A PR that is only open or merged is insufficient. Record the
installed commit, native binary/build identity, mind and console process starts,
schema version, data directory, fresh bridge boot identity and capabilities. Use
the installed checkout's CLI and existing operator connection; do not point a
development checkout at live data or create a second bridge client.

Preserve a private configuration backup and the release recovery instructions.
Record existing values before changing them; do not replace unrelated settings.
Verify these *new admission* gates are dormant:

| Mind | Native |
|---|---|
| `[life] day_plans = false` | `Leeroy.DayPlans.Enable = 0` |
| `[life] dungeons = false` | `Leeroy.MemberDungeon.Enable = 0` |
| `[decisions] bank_requests = false`, `bank_items_per_day = 0`, `bank_restock_per_week = 0` during the canary | `Leeroy.Decisions.BankMail.Enable = 0`, `Leeroy.Decisions.BankRestock.Enable = 0` |
| `[decisions] boss_strategy = false` | Preserve existing `Leeroy.Training.Enable`; do not silently change a shared training gate |
| `[decisions] rank_reviews = false` throughout this rollout | No rank-review activation: reviews can demote members in game |

Do not turn off an already established `[decisions] enabled`, `[server] enabled`
or `[realm] enabled` just to establish this baseline. Those gates also own older
features. Bank mail requires the existing Realm permissions, item whitelist and
armory read connection. Day plans need native Realm enabled too. Missing shared
prerequisites are a separate change to observe before advancing, never a bundle
of silent enabling edits. The rollout order is **day plans, member heroics, bank
requests, boss strategy**, each accepted before the next. Leave training and
roster settings at their approved values. Rank reviews and bank restock are not
part of this rollout and stay off unless the owner explicitly requests them.

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
`.reload config` and a fresh capability/greeting check. Decisions config is read at
mind startup, so restart mind and console in the agreed window. Life config is
read on session/night reload; record the reload that actually consumed it. A
file edit is not proof that a running service has applied it.

After each edit, record old/new value, application time, process/boot identity,
the last log offset and resulting capability/state. Observe a normal poll and
reconnect before the next edit. Once admission is enabled, wait for the actual
end-to-end behavior listed below, **then one further normal scheduled cycle**.
No fixed sleep substitutes for that evidence. If there is no eligible bank
request, day block, heroic roster or strategy trial, mark **WAITING FOR EXERCISE**,
not PASS, and leave the next feature off. Never manufacture consent, items or completion rows.

Keep previously accepted features on while observing the next, so priority and
resource conflicts are visible. Stop advancement on duplicate dispatch, unknown
or contradictory body ownership, a stuck owner request, wrong recipient/copy,
false public success, unexpected errors, or degraded chat/voice responsiveness.

## 1. Day plans

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
5. Check the next scheduled block with later rollout features still off and
   ordinary chat/voice functioning. Preserve unresolved dispatched operations over a
   reconnect; a new boot alone is not proof that a body has returned.

Rollback: disable `[life] day_plans` and apply the reload to stop new plans/body
commands. Let the existing journal reconcile/revoke permits and verify return
before disabling the native gate. Do not clear Realm holds, delete permits or
manually abandon a root to make the dashboard look healthy. Unknown operations
remain fenced for the documented operator investigation.
See [Day plans](../godmode/README.md).

## 2. Member heroics

Do not start this stage until the installed native build includes the reviewed
#401 receipt follow-up described above. Inspect the Characters-database load and
checkpoint results: a transient first load must recover, world updates must stay
responsive while writes complete, and an offline member's handback must have a
confirmed stored result. Never interrupt the production database to test this;
the failure cases need fake-database tests before release and healthy live evidence.

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
   no unresolved new body hold, and no interference with accepted day plans,
   offline raids or chat/voice. Bank requests and boss strategy remain off.
   Without real progress/return evidence the live heroic stage stays unverified
   even when all automated tests pass.

Rollback: disable `[life] dungeons` to stop new proposals/admission, preserve the
original start and receipts, and allow active-job cleanup/status reconciliation.
Use the existing typed stop/handoff workflow for that exact run if stopping is
needed, subject to its combat fences. Do not kill the server to clear ownership.
Disable the native admission gate after inspecting outstanding jobs; disabling
never proves return. At 600 seconds a durably failed run can release its job slot
without proving every body returned. Unresolved bodies stay fenced. Preserve the
per-member handbacks and inspect them with the
[unresolved-return procedure](../godmode/README.md#inspecting-an-unresolved-return).

## 3. Bank requests

1. Keep `bank_requests = false`, both bank caps at zero and restock native disabled.
   Enable only `Leeroy.Decisions.BankMail.Enable`; verify `member_bank_mail` in a fresh
   greeting and no withdrawal while the cap is zero.
2. If shared Decisions/Server/Realm prerequisites are not already active,
   establish them individually with the bank caps still zero. Do not raise the
   cap until the existing features and baseline remain healthy.
3. Set `bank_items_per_day = 1` while `bank_requests` remains false. Apply and
   observe that edit, then separately set `bank_requests = true`. Restart mind
   and console for each Decisions config change. Wait for one naturally eligible
   request and its guarded officers-only post; inspect the settled bank decision and original direct
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
   again with accepted day plans and heroics still running. Leave restock at
   zero/native off throughout this rollout.

Rollback: set `bank_requests = false`, apply it, then separately return the
outgoing cap to zero. Preserve and reconcile already submitted original jobs.
Disable the affected native admission key only after
in-flight evidence is captured; a withdrawal already committed cannot be undone
by flipping a flag. Never replay it or rewrite the bank ledger. Escalate an item
in the officer's bags or uncertain mail outcome by its original decision/job ID.
See [Decisions](../decisions/README.md#release-and-switches).

## 4. Boss strategy

1. Keep `[decisions] boss_strategy = false` while confirming the existing
   training service, telemetry/action settings and `Leeroy.Training.Enable`
   provide `training_enabled`. Missing shared prerequisites need separate
   observed changes; never silently turn training on as part of this stage.
2. Set only `boss_strategy = true`, restart mind and console, and verify the
   consumed config. Keep `rank_reviews = false` and bank restock disabled.
3. Wait for two real, complete guild wipes in the same boss/difficulty context
   within seven days. Inspect their pull IDs, the resulting decision and the
   lead's settled strategy post after its normal two-hour discussion delay.
   Do not manufacture wipe rows or treat a `training_off` talk-only result as
   applied behavior.
4. Observe the next eligible resting muster: the guild layer belongs above
   standing instructions and below the boss champion, with the decision ID in
   the in-play/pull receipt. Combat, wipe silence and debrief must hold changes.
   A dungeon-clear lever needs a bot-led run; a human-led run can carry lust
   timing. No mid-fight action change is an acceptance test.
5. Require real pull/cleanup evidence and separate scoring for that guild
   decision ID. Strategy-bearing pulls must not enter ordinary benchmark
   comparisons, trial promotions or the promotion-capable coach. Observe the
   next scheduled cycle with day plans, heroics and bank requests still healthy.

Rollback: set `boss_strategy = false` and apply the Decisions reload. Preserve
the decision and training journal; verify removal at the next safe muster or
run cleanup. The switch stops future use but does not prove an active layer has
already been removed. Never clear a journal or change levers during combat to
make the rollback appear immediate. See [Boss strategy](../decisions/README.md#boss-strategy).

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
Runtime activation waits for the owner's explicit release-complete report and
the recorded dormant baseline. Rank reviews stay off unless separately authorized.
