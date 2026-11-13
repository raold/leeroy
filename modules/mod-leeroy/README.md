# Offline raid stop and progress

`offline_raid_stop` is a bridge command with the normal `v`, `type`, `ts` and
unique `id` envelope plus `raid_id`, the original `start_offline_raid` command id.
For example: `{"v":1,"type":"offline_raid_stop","ts":0,"id":"stop1","raid_id":"raid1"}`.
Only the connected mind's bridge can dispatch it. It cannot stop a benchmark or
another raid. Native checks the exact instance's encounter state again at command
execution; an engaged boss or unavailable state refuses the stop. It uses the
same teardown as the native timeout, ending in the original raid's `job` event
with reason `stopped`. An ack alone is not a cleanup receipt.

A repeated stop while that raid tears down for `stopped` succeeds without another
mutation. An already finishing run for another reason returns `raid_finishing`;
the most recently finished offline id returns `raid_finished`; other unknown ids
return `unknown_raid`. This bounded diagnostic history is process-local. After
a native restart all old ids are unknown. Mind retries use new envelope ids and
the same raid id because its bridge forbids reuse of issued command ids.

Running offline `job` progress is published every 60 seconds and on entering
running. It adds `members_in_instance` (admitted members actually in the exact
map/copy, even if they left the group), `last_progress_ts` (Unix milliseconds,
initialised at encounter baseline and advanced by boss engage, kill or wipe),
and `boss_engaged` when the instance's encounter state is readable. Loading an
unvisited boss grid or repeatedly seeing the same state is not progress. Kills,
wipes, the participant manifest and instance id remain the existing fields.
Unknown encounter state omits `boss_engaged`; it never means disengaged.

The mind defaults to 30 minutes without progress (`[guild] offline_stall_minutes`,
10-360). Members leaving the instance is native's own too_few stop, which fires
before a heartbeat is sent, so the mind has no scatter rule. Stale,
malformed or engaged observations never authorize a stop. The run closes through
the existing terminal path as `stalled`; native cleanup errors keep
their reason. Raid history already displays this outcome. Native's earlier
leader/member loss, wipe limits and timeouts keep their existing priority.

Old native lacks these fields: the mind logs unsupported once per run and keeps
the existing timeout. A command-level unsupported response has the same fallback.
After mind restart no saved heartbeat authorizes a stop; fresh native evidence is
required. Lost-terminal handling (deadline plus 15 minutes) is unchanged. No schema
migration. **Deploying this feature requires a native rebuild and updated mind.**

# Offline raid ownership and stuck cleanup

The driver is a self-mastered bot outside the raid group. Stock playerbots'
`ResetAiAction` clears a master outside the bot's group and `FindNewMaster` finds
none in an all-bot raid, so a member could be left masterless: slow thinking, no
protection from an arena captain's gather (which skips only members with a game-client
master), and a failed teardown ownership proof. Each world tick native restores the
driver for a member it still holds whose master is empty (`master_reasserted`).

A restart drops a member still absent after 30 s from its gate when the present
members meet the minimum (`restart_dropped`), else stops `too_few`. Once cleanup has
failed, a member no longer held and without a training journal is abandoned
(`cleanup_abandoned`). Ten minutes after `cleanup_failed`, an ordinary offline raid
releases its slot (`cleanup_released`) unless a member is still held (`cleanup_held`);
journals and the cleanup receipt are retained. `Leeroy.CastArenaTeams.Leave` (default 1)
takes pinned cast members out of rated teams the owner does not captain
(`cast_arena_team_left`); a cast captain of a bot team is kept (`cast_arena_team_kept`).
Logs carry raid ids, guids, team ids and counts only. **Native rebuild required.**

# Rezzes in the owner's group

Stock playerbots leaves a member of a real player's group lying dead: it never releases
within 100 yards of him (or inside his instance), and its paladins, priests and shamans
rez only out of combat, unmounted, unmoving, within 57 yards and in sight. While he rides,
his bots ride too, and a mounted player cannot cast, so nobody rezzes. The pinned bots'
revive-in-place timer skips every grouped bot.

`Leeroy.GroupRez.Enable` (default 1) checks his group once a second (`GroupRezJob`, pure
policy `LeeroyGroupRezPolicy`). For a member lying dead (not released, not in a
battleground) while nobody in his group fights within 100 yards of the body, the nearest
free rezzer bot of the group (alive, out of combat, not casting, not in a vehicle or on a
taxi, no Realm or training owner) within 57 yards and in sight is used: within its spell's
range it is put down from its mount (only within 3 yards of the ground), out of Shadowform
or Ghost Wolf, stopped and made to cast its highest-rank rez (`group_rez_cast`, refusals
once per code as `group_rez_cast_refused`); farther out a mounted one is put down so
playerbots walks it in (`group_rez_dismount`). His own body is offered a rez the same way.
A bot outside a dungeon or raid that has lain dead through `Leeroy.GroupRez.ReleaseSeconds`
(default 60, at most 600, 0 never) of quiet with no rez under way or waiting releases as
playerbots' own auto-release does, and runs back from the graveyard (`group_rez_released`);
fighting nearby restarts that clock. Inside an instance it waits for a rez, as before.
Reloadable with `.reload config`. Logs carry guids, spell ids and result codes only.
**Native rebuild required.**

# Realm job receipts

The bridge's existing `realm_job` event may include `"changed": false` on a
failed body job. It proves that **the entire job** changed no body and made no
cleanup mutation, and that native released its reservation. The `reason`, `guid`
and `value` fields retain their meanings. An absent field means unknown; `true`
also provides no refusal proof. A successful job does not carry the field.

`RealmModule::Tick` combines an executor's explicit `RealmStep.unchanged` proof
with every previously completed member's proof. Before an executor has run,
the step is unchanged only if it has no pending work. Retention, declared scopes,
a continuation, any guild-join attempt, or shared cleanup mutation disqualifies
the receipt. An exception or unproved post-step identity never provides proof.

The ordinary leave executor proves an unchanged refusal only before its saved
`changed` flag or `cleanup_mutated` flag is set. Completed no-op members carry
that proof forward, so a later refusal can safely end a large leave (a refusal of
a member's own now skips that member instead, below); an earlier changed or
unproved member prevents it. Rearm's unchanged refusals (now mostly skips, below)
precede all weapon swaps; its completed no-op steps also carry explicit proof.
Other executors keep the default (unproved) unless rejected before execution.

The mind requires a positive ack, a correlated failed receipt with zero value,
a member in the reserved manifest, no PvP owner token, and an ordinary root
without children. It settles that root as `step_refused`, holding nobody. An
uncontested stored unknown with these receipts is reconsidered at startup.
Later contradictory evidence still becomes `conflict` and holds the manifest.
WG continuation, ack-only and result roots keep their existing rules.

An ordinary leave refuses at its ack, naming them, the members its step would find unable to
leave their battleground or battlefield group alone (`shared_group_leader`, `shared_group_cleanup`,
`shared_lfg_queue`, `shared_lfg_cleanup`), after the Wintergrasp and cleanup-closure refusals.
A member that becomes such a case only at its own step, before its step or an earlier member's
shared cleanup changed it, is skipped: `RealmModule::Tick` checks the body is as admitted and that
nothing declared, expected or awaits a transition for it (`AcceptsMemberSkip`), ends that member's
lease alone and goes on with the rest; the last step's readback covers only the members still held.
The terminal `realm_job` then carries `"skipped": [guid, ...]` (only when nonempty, done or failed);
a member whose own step already changed something keeps the old rule (unproved, retained).

The same skip covers every other refusal of a leave member's own (`SkipsLeaveMember`,
`MemberSkipReason`, an allow-list in `LeeroyRealmFailure.h` of the member's own fences, state and
closure; anything else, such as `busy:training_cleanup`, which is pending for every body at once,
`realm_off`, `busy:operation_identity`, `busy:realm_owner`, `lfg_cleanup_disabled` or a reason added
later, keeps the old rule), only while its step's `changed` flag is unset, its step recorded no LFG group
receipt and no shared cleanup touched it: its original raid group (`busy:raid_owner`),
`entry_point_unavailable`, `battleground_group_changed`, `battleground_receipt_unavailable`, an LFG
proposal, Wintergrasp, an expired step that changed nothing, its own recheck (`OrdinaryCleanupFault`
names the member), and the cleanup producers' refusals before they charge or declare anything
(`LeaveNativeQueue`, `LeaveObservedLfgApplicants`, and `LeaveOnlineLfgGroup` before its receipt).
Tick's own pre-step fence (its holder and lease checks, its admission) also skips a member whose
step never ran: no pending entry at all, not declared, not touched (`DecideFence`); a
battleground that ends under a member still waiting its turn is this case. The readback a skipped
last member makes runs under a `try`: a throw finishes `operation_outcome_unknown`, retained. A step
rechecks its current member and earlier/later members the job may have changed (`RecheckedMembers`), so such
a member no longer fails an earlier member's step; each producer rechecks its whole closure in full
(`ClosureFault`) before it charges it, and the last step still reads back every member held. Kept
retained: anything after a step set `changed`, a member a shared cleanup declared or changed,
producer refusals after declaring or after an LFG group receipt, and faults in a body the job may
have changed. The failed ordinary leave releases provably untouched members as described below.

An ordinary leave records each completed member's own change in `changed_guids`, beside shared
`cleanup_changed_guids`. An in-progress changed flag, an LFG receipt, declared scopes, or a
continuation also prevents a member being called untouched; an executor exception conservatively
marks its current member changed. Final readback runs after the last step or skip, without
replaying that step. Teleport and delayed exit restoration wait up to `OperationWaitMs` (60 s).
After that window, any bad readback of a provably untouched member skips that member alone.
The log names `readback_changed guid=<guid> check=<code>`, then the skip reason
`readback_changed:<code>`. Codes are `offline`, `owner`, `teleport`, `exit`, `battleground`,
`original_group`, `lfg`, `group`, and `queue`. The final readback still rechecks its last member's
cleanup state (`MemberCleanupFault`) even when untouched; a fault there the codes do not see (a
Wintergrasp queue, a training fence) fails the job named by its reason's code (`CleanupFaultCheck`:
`offline`, `queue`, `lfg` or `owner`). A readback without one of the nine codes, or naming a
member outside the manifest, is never sent (`LeaveReadbackFault`, `MakeRealmJob`, Tick's finish),
since the mind refuses the whole event over one. Tick reads the readback fields and its
`final_readback` shortcut only for `leave_pvp`, with typed reads: another kind's payload can be
the wire message itself.

A changed member still fails the readback. A failed ordinary leave keeps only members it may
have changed (including scopes/continuations) and ends the others' leases. Its `realm_job`
contains distinct manifest members in `released`, plus `readback_guid` and `check` when known.
The existing `guid` remains the current step, which can be different. The mind holds the original
manifest minus that exact release proof and backs off skipped members for 600 s. Release proof is
not proof a body left PvP (members after the failing step are released untouched too), so the mind
never rearms or defaults on it: the released members it did not skip, when fewer than the
manifest, get a new, strictly smaller leave, and only its success leads to rearm and defaults.
Missing fields retain the old conservative behavior.
Neither a contradictory receipt nor another kind/policy can borrow this proof.

After an operator's `realm release` abandonment receipt, a supporting mind sends `realm_forget`
with a **fresh envelope `id`** and the abandoned root as `origin_id`. Reusing the root's envelope
would collide with its original ACK; this command never replays or rewrites the original action.
Native emits `realm_forgotten` with those two ids, `outcome`, and `members`. Only a retained ordinary
`leave_pvp` or `rearm`, without declared scopes, continuation or guild joins, may be released.
`released` and `not_held` settle the mind's `realm:action:forgotten:<root>` receipt; `running` and
`kind` refuse without changing anything. The mind asks again after `busy:teleport` in 5 s,
doubling to 60 s, and after `running` every 60 s; `kind` and unanswered requests wait for a new
connection. IDs/codes/counts are the only logged data.

Deploy mind and native together; Claude performs the native rebuild/release with the owner offline.
New mind with old native keeps full failed manifests when `released` is absent, records an exact
unknown-command reply as `unsupported` once, and still needs the reviewed worldserver restart plus
operator release for native fences. Old mind with new native ignores the new wire fields, retains
its larger failed hold, and never sends `realm_forget`; the same manual recovery remains available.
Rolling an old mind over a database that already contains new release/readback evidence is different:
its strict journal reader cannot read the new keys. Use the pre-deployment snapshot for rollback;
do not strip exact receipts. No migration is required. See the mind Realm README for receipt details.

A rearm skips the same way (`SkipsMembers`): every refusal of its step before a swap
(`MemberRefusal`: the `busy:` fences such as `busy:combat` and `busy:dead`,
`weapon_record_unavailable`, `recorded_weapon_unavailable`, `equipment_changed`,
`cannot_equip_recorded_weapon`, `offhand_occupied`) and Tick's pre-step fence; a refusal after a
swap stays `Unknown`. At its ack a rearm also leaves out a member admission fences for a reason of
its own (his group's, `busy:raid_owner`) instead of refusing whole (`PartitionAtAck`): it reserves the
rest, lists the fenced in the job's `skipped`, and is refused whole at a fence that is the job's own
(`busy:training_cleanup`) or when every member is fenced. A one-member job still names its member
(`guid`) by the admitted manifest (`DoneJobGuid`). When every member is skipped the job
ends done, holding nobody. `changed: false` stays whole-job proof: skipped members leave the
completed members' proof as it was.

Old native omits the field, preserving the reason-based rules. Old minds ignore
unknown wire fields and retain their existing settlement behaviour (an older mind would rearm
and default a skipped member with the rest, sending `nc -bg,-lfg` to a bot still in its
battleground). An older mind also does not know the four group ack reasons: it settles such a
refused leave `unknown` and holds its manifest, although native holds nobody (so the owner's
`addN` is unaffected); the new mind's startup recheck of ack-only unknown roots settles it.
Ship both together (`ac-release.sh --native`). That holds again for the rearm skip: the mind
live before it rejects `skipped` on a rearm's evidence (the event matches nothing, so the rearm
stays dispatched, holding its members), so native and mind ship in one `ac-release.sh --native`. Rolling the mind back past
this change is different: the new mind stores `changed` (and `skipped`, `skipped_at`) in
`realm_action` evidence, and an older mind cannot read such a row (its journal reads fail, so the Realm stops moving bodies until the
key is stripped from those rows). Take a snapshot before deploying and roll back with it. Both a
native rebuild and an updated mind are needed for the new proof to take effect.

# World persona roster and speech

A native rebuild adds `get_world_roster` (complete, bounded roster pages carrying
an independent `request` token) and `world_say` (Trade/General for an eligible
member of a current rival guild). Both require `Leeroy.Realm.Enable = 1` and
`Leeroy.World.Enable = 1`. World defaults to 0; `.reload config` applies the switch.
The roster excludes the owner, our guild and every character on auction-bot
accounts. Missing character/account evidence refuses the guild. A member row that breaks
the page rules (or repeats a guid) is left out of the roster instead of refusing all of it,
and Server.log names it once per member and rule: `world roster skipped guid <n> (<rule>)`.
Before this, one such row answered every roster request `world_roster_invalid`. Existing Realm
actors and their `realm_say` path retain their behavior. The mind also requires
its separate `[world]` opt-ins; world photos need `[selfies] enabled = true`.
