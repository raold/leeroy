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
the existing terminal path as `stalled`, with its cause in the log (below); native cleanup
errors keep their reason. Raid history already displays this outcome. Native's earlier
leader/member loss, wipe limits and timeouts keep their existing priority.

Old native lacks these fields: the mind logs unsupported once per run and keeps
the existing timeout. A command-level unsupported response has the same fallback.
After mind restart no saved heartbeat authorizes a stop; fresh native evidence is
required. Lost-terminal handling (deadline plus 15 minutes) is unchanged. No schema
migration. **Deploying this feature requires a native rebuild and updated mind.**

# Offline raids dungeon-clear cannot run, and named stalls

Dungeon-clear drives the offline raid's tank, so a raid it cannot route is a raid the guild
cannot do without the owner. Two rotation raids were such raids:

- **Trial of the Crusader.** Dungeon-clear builds its boss list from static spawns of the map's
  kill-credit encounters; the herald's event summons every ToC boss and none is spawned, so
  `dc on` is refused ("No bosses found for this map") on every try. On 2026-11-11 a 25-man run
  waited out the 60 s starting timer (`setup:starting:timeout`). Starting now checks the
  leader's roster once (`DungeonBossesValue::AllWings`) and fails at once with
  `setup:starting:dc_no_bosses` (pure policy `DcRosterRefusal`).
- **Ulduar.** The first boss on its route, Flame Leviathan, is a siege-vehicle fight behind a
  gate (194630) only that event opens. On 2026-11-13 a 25-man run fought courtyard trash for
  21 minutes, then dungeon-clear logged `door-blocked: can't open ... -> auto-pausing` and held
  there until the mind's 30-minute watchdog stopped it `stalled` with no kill.

Every offline Running tick now reads dungeon-clear's leader run as a phase (`DcPhase`: off, on,
paused, door; `dc_phase=<to> from=<from>` is logged on change, first 20 then every 50th). The
heartbeat carries it as `dc_state`; entering Running (a start or a restart) resets the phase to
`unknown` and the hold, so the first heartbeat never repeats a pre-restart `off`. A pause out of
every boss fight (an encounter known to be disengaged) for `OfflineDcHoldStopMs` (5 minutes)
stops the run under its own name, `dc_door_blocked` (paused at a door) or `dc_paused` (any other
pause). Nobody types `dc pause` in an offline raid, so `paused` is dungeon-clear's null-door
fallback or, once the Training Ground acts (`[training] act` and 28 days of pulls), its boss hold:
the training listener sets WaitAtBoss on every offline run's tank and releases each hold, native
resuming it after 20 s, so `dc_phase=paused` then appears at every boss and only a stuck hold
reaches the 5 minutes. An engaged or unreadable encounter, or any other phase, restarts the clock;
every existing stop and restart rule still wins. Benchmarks keep their own rules (the hold stop,
the reachable clear and the phase are offline only; the roster check applies to both).

Naxxramas's Sapphiron and Kel'Thuzad (instance bosses 13 and 14) are reached only by the hub's
teleporter after the four wings, which dungeon-clear does not use: its route stalls there with
the run still enabled. When every other boss is done and one of those is not (and neither is
engaged, failed or mid-event, as a manual `dc skip` could make it), native ends the run
`cleared_reachable` (pure `DcUnreachableBosses`, `ClearedReachable`; logged
`cleared_reachable kills= out_of_reach=13,14`) instead of idling into the mind's stall stop or
the timeout.

The mind never plans `toc` or `ulduar` (`guild/offline.py` `UNSUPPORTED`, logged once per
process as `offline_raid_unsupported raid= reason=`), whatever `[guild] offline_rotation` says,
and the default rotation is now `icc25, icc10, naxx10` (Naxxramas bosses all have static
spawns; none sits behind a vehicle event before it; `naxx25` is left out because he raids it
himself). A rotation of only unsupported raids holds the day. The planner also passes over, for
the day, any raid at a size he holds a permanent save for this lockout week (heroic or not;
`armory.saved_sizes`, the saves his add command reads; `offline_raid_skipped raid=
reason=owner_saved`): an offline run would save its members to another copy and his add command
would leave them out (`locked`) until the reset. `setup:starting:dc_no_bosses`, like
`no_fresh_encounters`, voids the run and passes the raid over for the rest of the day; so does a
native `dc_door_blocked` stop with no kill and no wipe, which also takes back the attendance and
its DKP recorded when the run reached running (`RaidSession.retract_attendance`; bench memories
already written stay) and marks the run not raided. A stall the mind stops names its cause from
the last fresh heartbeat: `offline_raid_stop raid_id= reason=stalled cause= idle_s= inside=
members=`, with `cause` one of `dc_door_blocked`, `dc_paused`, `dc_off`, `members_out`,
`no_boss_reached` (dungeon-clear on, no boss engaged) or `unknown` (an older worldserver sends
no `dc_state`); an operator stop is `cause=operator`. The cause is kept in the owned stop
receipt and the day receipt (`stall_cause`); the raid row keeps the stable outcome `stalled`.
Every finished run logs `offline_raid_ended raid_id= outcome= cause= kills= wipes= raided=`.
No outcome code reaches the wrap-up line's prompt: `guild/lines.py` `OUTCOME_WORDS` gives native
stops, the mind's own and a called raid night's ends plain words (`dc_door_blocked` reads "got
stuck at a door we couldn't open"), and any other code reads "the raid is over".

Every offline raid enters a new copy: Teleporting unbinds every member from the map's four
difficulties before the leader's worldport, so a leader's earlier saves can never leave a
planned raid with nothing fresh to kill (members saved to the owner's copy are already left
out by the mind). `no_fresh_encounters` on 2026-11-05 was the TO_BE_DECIDED misreading fixed
that day. **Native rebuild and updated mind required; no migration.**

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
within 100 yards of him (or inside his instance), and its paladins, priests, shamans and druids
rez only out of combat, unmounted, unmoving, within 57 yards and in sight. While he rides,
his bots ride too, and a mounted player cannot cast, so nobody rezzes. The pinned bots'
revive-in-place timer skips every grouped bot.

`Leeroy.GroupRez.Enable` (default 1) checks his group once a second (`GroupRezJob`, pure
policy `LeeroyGroupRezPolicy`). For a member lying dead (not released, not in a
battleground) while nobody in his group fights within 100 yards of the body, the nearest
free rezzer bot of the group (alive, out of combat, not casting, not in a vehicle or on a
taxi, no Realm or training owner) within 57 yards and in sight is used: within its spell's
range it is put down from its mount or Flight Form (only within 3 yards of the ground), out of
Shadowform, Ghost Wolf or a druid form, stopped and made to cast its highest-rank rez (`group_rez_cast`, refusals
once per code as `group_rez_cast_refused`); farther out a mounted one is put down so
playerbots walks it in (`group_rez_dismount`). His own body is offered a rez the same way.
A bot outside a dungeon or raid that has lain dead through `Leeroy.GroupRez.ReleaseSeconds`
(default 60, at most 600, 0 never) of quiet with no rez under way or waiting releases as
playerbots' own auto-release does, and runs back from the graveyard (`group_rez_released`);
fighting nearby restarts that clock. Inside an instance it waits for a rez, as before.
Reloadable with `.reload config`. Logs carry guids, spell ids and result codes only.
**Native rebuild required.**

# The one-time regear (the cast down to an item level)

Gear-up (`gear_up`, `GearUpJob`) runs playerbots' `autogear bis <ilvl>` on each bot, else
`PlayerbotFactory::AutoGear(bot, EPIC, ilvl, false)`. Neither keeps gear above a lower cap as a
rule: `autogear bis` destroys everything equipped and every weapon and armour piece in the bags,
gears the empty bot at the cap, then puts on the nearest BiS tier at or below it (a tier's list
holds items above it: 27 of the 204 entries of the 245 list are 264-284); the plain autogear swaps each slot for its
best capped pick and stores the old item in the bags, but keeps the old item when the bags are full
or nothing capped fits the slot. So every `gear_up` (role, under-level, theorycrafter, regear; hello
lists `regear`) runs a cap pass after it: weapons and armour above the cap left in the bags are destroyed
(bots never trade them, and an upgrade pass could equip them again; never the bank, the mail or
the guild bank), anything still equipped above the cap is stripped, emptied slots are refilled from
the capped search (`AutoGear`, incremental), and a slot nothing capped can fill gets its old item
back, never left empty, counted as `kept` in the progress event. Shirt and tabard are never
touched. Log `gear_up_member guid= ilvl= avg= stripped= restored= empty= kept= destroyed=`.

`gear_up` with `"regear": true` (ilvl 200-300) takes only the cast's bots (pure policy
`RegearRefusal`): the owner (`owner:<guid>`), anyone
outside the cast (`not_cast:<guid>`) or on a player's account (`not_bot:<guid>`) refuses the whole
command before anything moves; a regear while he is online is refused `owner_online`, and his login
mid-job ends it `owner_online`. Each member is skipped for the gear-up body checks and for every
Realm/training fence (`RealmModule::MutationReason`). Its members log `regear_member` (same fields).
**Native rebuild required.**

The mind sends it once per value of `[guild] regear_to` (0 = never), meta `guild:regear:<ilvl>`:
the cast's members at 80 and above at its start, theorycrafters at `theorycrafter_ilvl` when that is
higher, 40 a batch two minutes apart, only in gear-up's windows (he has been offline 10 minutes, no
raid night or offline raid). A member lands when its average item level is within 20 under and 6
over its target; its `geared`/`tc_geared` flags are replaced. With a native listing `regear`, every
gear-up's receipt uses that band (role, under-level and theorycrafter passes too), so a capped
theorycrafter pass cannot miss its old `ilvl - 6` floor and retry hourly. Skipped members wait 30 minutes; three
landings out of bounds (or failures) give a member up; members who leave the cast are dropped.
Gear-up and the theorycrafter pass wait until the regear is done. A hello from a restarted
worldserver releases any gear-up batch the old process held. Health's `regear` line shows members
done of the total. Logs: `regear_started`, `regear_batch`, `regear_member`, `regear_out_of_bounds`,
`regear_dropped`, `regear_gave_up`, `regear_batch_done`, `regear_done ilvl= guids= avg= gave_up=
dropped= kept=`.

# The raid lead (his orders to his own group)

`Leeroy.RaidLead.Enable` (default 0, reloadable with `.reload config`) is the native half of the
mind's `[raidlead]` (`mind/leeroy_mind/raidlead/README.md`). Pure policy is
`Lib/LeeroyRaidLeadPolicy.*` (unit tests `TestRaidLeadPolicy.cpp`); the world-thread side is
`Guild/RaidLead.cpp`. Hello lists `features: [..., "raidlead"]` so the mind knows the build has it.
With the key at 0 every raid lead action is refused `raidlead_off` (Bloodlust included) and every
hold is lifted at once; only `lift` (undoing a hold) still runs. **No raid assistant flag is ever
set**, and the leader flag is never touched.

- **`bot_do` action `"order"`**, param one of the fixed list `kRaidOrders`: `follow`, `come_back`,
  `stay`, `stack` (`target_guid` a member of his group, or 0 for him), `spread`, `run_out`,
  `back_off`, `hold`, `attack`, `kill_skull`, `taunt`, `lust`, `release`, and the mind's own
  `lift`. Anything else fails the parser (`bad field: param`); co/nc strategy strings keep their own
  signed whitelist. Each order is built from existing playerbots strategies, values and actions:
  the strategy strings of playerbots' Follow and Stay chat shortcuts (without their whisper), its
  `stay`/`return` positions (at the bot, at him or at the named member), the `rti` value `skull`,
  its `attack my target` action (a bot already on his target, or out of sight of it, counts as
  done), each class's taunt (`taunt`, `hand of reckoning`, `dark command`, `growl`) on his hostile
  selection unless that tank already holds it (`has_aggro`), the faction's `bloodlust`/`heroism`
  on itself, and for `release` the repop GroupRezJob uses (the one order, with `lift`, for a dead
  bot; `alive`/`released` otherwise). The job's terminal lists the bots it changed in `guids` and
  the rest in `skipped` with the reason; every "+x"/"-x" of an order is checked afterwards.
- **Only `hold` adds `passive`, and never on a tank or healer** (`PlayerbotAI::IsTank`/`IsHeal` by
  spec: `not_damage`). `spread` and `run_out` are one playerbots `move from group` of 10 and 30
  yards (a bot already that far from everyone is done); `back_off` is one move to just behind him.
  None leaves the passive `move from group` strategy behind. A hold is recorded on the world
  thread and lifted (`raidlead_hold_lifted guid=`) when his group's fight starts or ends after it,
  after 30 seconds (`kRaidHoldMs`), when the bot leaves his group, or when the switch is off; a
  later stay, stack, follow, come back, attack, kill skull or lift order clears it too. A bot out
  of the world (between maps, or logged out: playerbots keeps its strategies) keeps its record
  until it is back, then is lifted: a hold is never dropped unlifted.
- **Fences at the mutation boundary** (TickBotCommands, after the ack): he is online and leads a
  group that is no battleground or battlefield group, the bot is in that group and on his map
  (`not_leader`, `not_in_group`, `elsewhere`), no Realm lease on it or its group
  (`busy:realm_owner`), and `NativeOwnershipReason` (offline raid, dummy trial, training cleanup,
  journals, pending bot jobs: `busy:training_*`). `lift` is cleanup and is fenced by Realm leases
  only. A bot an order needs as his own gets him as master, as RaidHabits does.
- **`raid_lead`** `{action: "mark", guid, icon}` puts a raid icon (kRtiIcons, skull is 7) on his
  hostile selection, set server-side by `guid` (the lead he named, or him), who must be in his
  group (`not_in_group`, `no_target`). `{action: "probe"}` answers whether the switch is on.
  `{action: "sync"}` also publishes his group as a `group` event (nothing while he is in a
  battleground's raid), so the mind knows his group without waiting for a change. The ack is the
  result; the mind's `guid: 0` and `icon: ""` mean none. The ready check stays the existing
  `raid_control ready_check` with him as `leader_guid`.

Logs carry ids, guids and codes only. **Native rebuild required** (`ac-release.sh --native`).
# Ground hazards in the owner's group

Stock playerbots adds its `avoid aoe` strategy to the combat engine only (`AutoAvoidAoe` with a game-client
master). Between pulls the non-combat engine runs, including while a bot takes damage from a hazard it never
attacked, and `follow` walks it to its formation spot beside him, through and into fire or poison. Stock
`avoid aoe` is also reactive (a dynamic object's area only once its aura is on the bot; a trap or trigger npc
only when its centre is within 15 yards) and ignores anything above `Playerbots.MaxAoeAvoidRadius` (15).

`Leeroy.Hazards.Enable` (default 0, `.reload config`) changes that for the cast bots of his group only
(`HazardJob`, pure policy `LeeroyHazardPolicy`). A member qualifies while it is a cast bot (not a self-bot),
in his group (not a battleground or battlefield group) with him as its master; a Realm lease, offline raid,
training state or pending bot command leaves the body to its owner (nothing added or removed).

- **Out of combat too.** Every 500 ms the world tick adds `avoid aoe` to a qualifying member's non-combat engine
  where it is absent, and so does `RaidHabits` right after its strategy reset (the owner's `add5/10/25/40`, a
  raid night he leads). The journal holds only additions this job made, per body, AI and session: a strategy
  someone else added is never removed, a reset that dropped ours is forgotten and re-added while the member
  still qualifies (`hazard_avoid_readded`, the first and every 50th), and a member who stops qualifying (left
  his group, another master, key off) loses only ours (`hazard_avoid_off guid= reason=`). A logout forgets it;
  a member between maps is decided when it lands.
- **Zones** (needs the local playerbots patch `0011`, `PlayerbotHazardFilter`; without it the start log says the
  filter is unavailable and only the non-combat strategy works). Each pass scans 70 yards around him and around
  any member farther than 15 yards from him for the three kinds stock knows, measured as stock measures them:
  a hostile persistent area dealing damage (`DynamicObject` radius; dummy marker areas are left to stock's
  reactive case), a damaging trap that a player can see (half its diameter), and an unselectable hostile trigger
  npc whose periodic aura triggers a damage spell (that spell's radius plus the npc's own reach, as stock's edge
  to edge distance counts it). Each counts up to `Playerbots.MaxAoeAvoidRadius`, never one on
  `Playerbots.AoeAvoidSpellWhitelist`, plus the allow-list below, which matches a spell's difficulty versions
  (and, for a trigger npc, the spell its aura's first difficulty version triggers) as well as the listed id.
  Map threads read that snapshot; its version changes only when the zones do, so answers are reused between
  passes.
- **Follow spots and detours.** `FollowAction` lets the filter move a formation spot that lies within a zone's
  radius plus the bot's reach plus 1.5 yards to the nearest spot clear of every zone (straight out first, then
  rings of 2 yards up to 40 yards, ties toward the bot), checked against the navmesh. While the straight walk
  from the bot to that spot (moved or not) would cross a zone, follow walks to a waypoint round it instead: on a
  ring 1 yard beyond the clearance, the shorter way round first, as far round as a clear straight line reaches
  (at most 90 degrees a step), beyond the distance at which follow stops (its follow distance past the bot's reach, plus half a yard; at least 2.5), navmesh-checked
  and kept until the bot is near it. A bot behind a hazard he walked through goes round it. With no way round
  that makes progress (a second hazard shutting both ways), or once six waypoints in a row gain less than a
  yard on the goal (a pocket between hazards, where greedy steps would circle), it walks straight as stock until
  his spot moves 3 yards.
- **Exits.** `avoid aoe` (after its three stock cases, in combat as well) walks a bot standing in a zone to the
  nearest clear spot and keeps that exit while it stays clear. Out of combat the exit ranks by the follow goal:
  one from which the goal is in plain view (his side) beats a nearer one unless the nearer saves more than 4
  yards, so a bot just inside the near edge steps back and goes round, and one past the middle finishes the
  crossing. In combat, ties go toward him. An exit never loops with a lower-relevance mover (follow, or
  dungeon-clear's `.dc on` route at relevance 15-57) that walks the bot back into the same hazard: entries are
  counted per hazard, and a hazard's third entry within 20 seconds yields it to the bot's own movers for 20
  seconds; a bot standing only in yielded hazards is let through, overlapping ones included
  (`hazard_exit_yield guid= zones=`).
- Logs once a minute while it acts: `hazards_minute aware= zones= spot_members= detour_members= exit_members=`.

**Larger hazards (measured, 2026-11-14).** From the live `Spell.dbc`/`SpellRadius.dbc` and the core's raid
scripts and world SQL for Naxxramas, Ulduar, Trial of the Crusader, Icecrown Citadel, the Obsidian Sanctum, the
Eye of Eternity and the Vault of Archavon, the ground-effect damage spells with a radius above 15 and up to 35:

| Spell (aura -> damage) | Where | Radius | Decision |
|---|---|---|---|
| 28158/54362 -> 28241/54363 Poison | Naxx, Grobbulus's cloud (10/25) | 30 (script hits within the cloud's reach, 2 -> 17 over 60 s) | **allowed**, radius = the cloud's combat reach |
| 69788 -> 69789 Ooze Flood (71215, 71587, 71588) | ICC, Rotface's flooded corner | 25 | **allowed** (see below) |
| 57428 -> 57429 Static Field | EoE, Malygos phase 3 | 30 | no: the raid rides drakes |
| 70460 -> 70461 Coldflame Trap | ICC, jets after Saurfang | 25 (cone target) | no: a jet line, not a circle |
| 62016 -> 62017 Lightning Shock | Ulduar, Thorim's charged pillar | 35 | no: arena mechanic |
| 62393 -> 62392 Lightning Destruction | Ulduar, Thorim's gauntlet orb | 35 | no: an orb sweeping a corridor |
| 29350 -> 30122 Plague Cloud, 61887/61869 Tendrils/Overload, 62546 Scorch, 64167 Lunatic Gaze, 65084 Self Destruct | Heigan, Brundir, Ignis, Yogg-Saron, a mob | 18-30 | no: on a selectable boss or mob, not a ground hazard |

Rotface's own playerbots strategy (`IccRotfaceMultiplier`) zeroes every `avoid aoe` action, this exit included,
for a bot with Rotface on its threat list, so in that fight Ooze Flood mainly moves the follow spots of members
not yet engaged. No dynamic object in those raids exceeds 15 yards in the DBC (the largest are 13: Rune of Death,
Ulduar trash Blizzard). Putricide's Slime Puddle (70346, 2.5 x the puddle's scale) and the Lich King's Defile
(72754, 10 x its scale) are sized by their scripts with DBC radii of 100 and 200; playerbots' own ICC strategies
handle them, so the generic rule leaves them alone. Above 35 (Shroud of Sorrow 40, Remorseless Winter 45, Unholy
Aura 45, the spews 55, ...) nothing is ever fled. Allowed spells still stop at 35 yards.

Limits: the detour plans round one hazard at a time with straight legs and does not plan round walls (the
navmesh check refuses a waypoint it cannot reach straight, and then follow walks straight as stock, stepping out
again at the exit until the yield lets it through); a navmesh path between two clear points can still bend into
a hazard. `melee` and `queue` formations follow a unit, not a spot, so only the exit applies. Encounter
multipliers that silence `avoid aoe` during a mechanic silence the exit too, and in combat a melee bot whose
target stands in a hazard can step back in to reach it (after two exits the yield lets it stay). Logs carry
guids, counts and reason codes only. **Native rebuild required, with the playerbots patch applied first.**

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
