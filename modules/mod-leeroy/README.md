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
that proof forward, so a later Wintergrasp refusal can safely end a large leave;
an earlier changed or unproved member prevents it. Rearm's unchanged refusals
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

Old native omits the field, preserving the reason-based rules. Old minds ignore
unknown wire fields and retain their existing settlement behaviour (an older mind would rearm
and default a skipped member with the rest, sending `nc -bg,-lfg` to a bot still in its
battleground). An older mind also does not know the four group ack reasons: it settles such a
refused leave `unknown` and holds its manifest, although native holds nobody (so the owner's
`addN` is unaffected); the new mind's startup recheck of ack-only unknown roots settles it.
Ship both together (`ac-release.sh --native`). Rolling the mind back past
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
accounts. Missing character/account evidence refuses the guild. Existing Realm
actors and their `realm_say` path retain their behavior. The mind also requires
its separate `[world]` opt-ins; world photos need `[selfies] enabled = true`.
