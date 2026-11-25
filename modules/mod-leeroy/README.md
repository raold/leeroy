# Ambient chat combat fence

Native advertises `combat_quiet` in `hello`. A `say` command may include
`"quiet_in_combat": true`; it is strictly boolean and defaults to false.
`ExecuteSay` checks the current owner and speaker `IsInCombat()` on the world
thread immediately before any chat transport. Combat refuses the command with
`combat_quiet` and publishes no chat echo. An offline owner does not block an
idle speaker. Commands without the flag retain their previous behavior.

The mind sets this for ambient game turns when `[talk] combat_quiet` is enabled;
direct answers, tactical callouts, urgent turns and Discord retain their own
delivery rules. See `mind/leeroy_mind/talk/README.md` for release/rollback steps.

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

# Offline raid admission reserves

The planner already fills all 10 or 25 raid seats. Members can become unavailable
before native grouping, which used to start at the 80% minimum without drawing
from the bench. `[guild] offline_reserves = 2` allows up to two eligible bench
members to fill those setup losses; `1` allows one, and the default `0` preserves
the current behavior. This requires an updated mind and a **native rebuild**
advertising `offline_raid_reserves`; the mind omits reserves on older servers.
No migration is needed.

Native tries the pre-approved reserves only while forming an offline raid, until
it has the minimum plus the requested reserve count (capped at the raid's normal
capacity). It never removes a usable planned member, lowers the running minimum,
replaces the required tank or loot lead, or brings in replacements during a run.
Owner lockouts, absence and the planner's exclusions apply to the bench too.
Native rechecks the exact bot session, Realm/day ownership, training/heroic
recovery, queued commands, existing groups, PvP, instances, combat and travel before admitting a
reserve. It does not preempt a reserve's day plan. Member heroics and benchmarks
do not use this feature.

The job's manifest accounts for each reserve: admitted members join ordinary
ownership, cleanup and attendance; unused members report `reserve_unused` and
remain on the bench. The log reports admitted counts as
`leeroy: raid <id> reserves=<n> members=<n> minimum=<n>`.

# Trial of the Crusader and Ulduar offline raids (patches/mod-dungeon-clear)

The local mod-dungeon-clear commits (`patches/mod-dungeon-clear`, exported by `scripts/ac-snapshot.sh` and applied
to its upstream by the `patches` and `build` workflows) teach dungeon-clear both raids, and advertise it in
`Ai/Dungeon/DungeonClear/DcCapabilities.h`. `mod-leeroy.cmake` probes that header (configure line
`[mod-leeroy] dungeon-clear offline raids: toc 1, ulduar 1`) and the hello lists `dc_toc` and `dc_ulduar`
(`LeeroyDcRaidFeatures`, pure `DcRaidFeatures`); the mind plans each raid only on a hello that lists its feature.

- **Trial of the Crusader (649).** Five arena objectives, one per stage, each held until the instance script's own boss
  slot reads DONE; Barrett Ramsey's gossip starts each stage and is clicked by a conditional driver whenever he offers
  it, the raid muster ready and the last kill looted (bounded); the faction champions and the twins, released 25-45 yd
  off and never coming on their own, are pulled when a stage is in progress and nothing is engaged; when the Lich King
  breaks the floor, the bots (who have no client to fall with) are put on Anub'arak's chamber floor; Anub'arak is an
  ordinary boss row. A wipe needs nothing new: the script summons Barrett again with his retry option. Every stage is a
  kill (boss states 0-4), so a full clear ends `cleared`. The Champions' Cache and heroic's Tribute Chest are looted
  like boss corpses (DcEncounterChests). Known gap: after an Anub'arak wipe the script resummons him only when a player
  enters the map, which a restart's same-map teleport is not, so such a run stalls until the mind stops it.
- **Ulduar (603).** Flame Leviathan is a siege-vehicle fight bots cannot drive (stock playerbots boards a vehicle only
  when the bot's master sits in one, and an offline raid's members are mastered by the driver outside the instance).
  The ruling: he is never fought, killed or credited (skipped by design); a raid of bots only, with no member a person
  controls, is moved past his gate to the Ulduar Teleporter's Colossal Forge destination (spell 64028's
  spell_target_position), which the instance itself offers only once he is dead. No loot, kill, achievement or boss
  state comes of it; every boss after him is fought for real; a group a person is in keeps the gate exactly as before.
  Razorscale is started at the Expedition Commander. Dungeon-clear's roster then reaches Ignis, Razorscale, XT-002,
  Kologarn and Auriaya; the rest are spell-credit encounters it never lists or lie behind the tram and the Keepers'
  gate, so with those five done native ends the run `cleared_reachable` (`DcUnreachableBosses(603)` = 0, 4, 7-13).
  Kologarn's Cache of Living Stone is looted.

Neither raid is in the default rotation: add `toc10`, `ulduar10` (or the 25-man keys) to `[guild] offline_rotation` once
a run has been watched live.

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
paused, door, stalled; `dc_phase=<to> from=<from>` is logged on change, first 20 then every 50th). The
heartbeat carries it as `dc_state`; entering Running (a start or a restart) resets the phase to
`unknown` and the hold, so the first heartbeat never repeats a pre-restart `off`. A pause out of
every boss fight (an encounter known to be disengaged) for `OfflineDcHoldStopMs` (5 minutes)
stops the run under its own name, `dc_door_blocked` (paused at a door), `dc_paused` (any other
pause) or `dc_unreachable` (`stalled`: an unpaused run whose leader carries a stall reason that is not
a door's, dungeon-clear's "Can't path to <boss>" / "Stuck near <boss>" waiting for a `dc skip`; it
clears the reason itself as soon as it moves again). On 2026-11-23 a Naxx 10 run read `on` for 24
minutes in front of Gothik until the mind's 30-minute watchdog stopped it. Nobody types `dc pause` in
an offline raid, so `paused` is dungeon-clear's null-door
fallback or, once the Training Ground acts (`[training] act` and 28 days of pulls), its boss hold:
the training listener sets WaitAtBoss on every offline run's tank and releases each hold, native
resuming it after 20 s, so `dc_phase=paused` then appears at every boss and only a stuck hold
reaches the 5 minutes. An engaged or unreadable encounter, or any other phase, restarts the clock;
every existing stop and restart rule still wins. Benchmarks keep their own rules (the hold stop,
the reachable clear and the phase are offline only; the roster check applies to both).

Naxxramas's Sapphiron and Kel'Thuzad (instance bosses 13 and 14) are reached only by the hub's
teleporter after the four wings, which dungeon-clear does not use: its route stalls there with
the run still enabled. Gothik the Harvester (11) waits on his balcony (2642, -3387, z 285.5, 18
yards above his room's floor), passive until struck; dungeon-clear finds no navmesh route up to him
(2026-11-23: `path rebuilt -> Gothik the Harvester ... UNREACHABLE: no navigable route`) and stalls,
and the Four Horsemen (12) are a spell-credit encounter its roster never lists, behind the two gates
only Gothik's death opens. When every other boss is done and one of those is not (and none is
engaged, failed or mid-event, as a manual `dc skip` could make it), native ends the run
`cleared_reachable` (pure `DcUnreachableBosses`, `ClearedReachable`; logged
`cleared_reachable kills= out_of_reach=11,12,13,14`) instead of idling into the mind's stall stop or
the timeout. Gothik sits in the middle of dungeon-clear's order (Military Quarter, before the
Construct Quarter), so the job types the `dc skip` his stall asks for as soon as dungeon-clear heads
for him (pure `OfflineDcPassEntries`, `OfflineDcPassDue`: creature 16060, run enabled and unpaused,
no boss engaged, leader out of combat, checked once a second, at most `OfflineDcPassLimit` 16 times a
job since a restart's `dc on` forgets the skip; logged `dc_passed entry= passes= kills= enabled=`):
the run goes on to Patchwerk, Grobbulus, Gluth and Thaddius.

The mind never plans `toc` or `ulduar` on a worldserver whose hello lacks `dc_toc` /
`dc_ulduar` (`guild/offline.py` `UNSUPPORTED`, `RUNNABLE_WITH`; logged once per hello as
`offline_raid_unsupported raid= reason=`), whatever `[guild] offline_rotation` says (see the next
section for the build that lists them), and the default rotation is now `icc25, icc10, naxx10` (Naxxramas bosses all have static
spawns; none sits behind a vehicle event before it; `naxx25` is left out because he raids it
himself). A rotation of only unsupported raids holds the day. The planner also passes over, for
the day, any raid at a size he holds a permanent save for this lockout week (heroic or not;
`armory.saved_sizes`, the saves his add command reads; `offline_raid_skipped raid=
reason=owner_saved`): an offline run would save its members to another copy and his add command
would leave them out (`locked`) until the reset. `setup:starting:dc_no_bosses`, like
`no_fresh_encounters`, voids the run and passes the raid over for the rest of the day; so does a
native `dc_door_blocked` or `dc_unreachable` stop with no kill and no wipe, which also takes back the attendance and
its DKP recorded when the run reached running (`RaidSession.retract_attendance`; bench memories
already written stay) and marks the run not raided. A stall the mind stops names its cause from
the last fresh heartbeat: `offline_raid_stop raid_id= reason=stalled cause= idle_s= inside=
members=`, with `cause` one of `dc_door_blocked`, `dc_paused`, `dc_unreachable` (`dc_state=stalled`), `dc_off`, `members_out`,
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

# Offline raid roster rules and full wipes

A running offline raid counts a member as inside its copy while the body is in the job's group, in the world
and on the run's map and copy; a near teleport (same map, same copy) keeps it inside and only a worldport takes
it out until it arrives. Dungeon-clear moves members that way: its stranded recovery teleports strays to the
tank, its raid-wipe regroup revives and teleports the whole raid to the entrance, and the Trial of the
Crusader floor drop and Ulduar Leviathan pass move every member, the tank included. Each such tick used to
stop the run `too_few` or `leader_lost` (2026-11-14, Naxx 10, three members; 2026-11-16, ICC 25, sixteen;
2026-11-17, Naxx 10, the tank at the regroup). Setup gates, ownership proofs and cleanup still require no
teleport at all.

`leader_lost` and `too_few` now end a run only after the leader has been out, or the roster under the
minimum, for 10 s without a break (`short leader_present= present= min= members=` logs each time it goes
short, first 20 and every 50th); until then the run waits without restarting or claiming a clear. The
timeout and his login never wait.

When every member inside is dead, no boss is engaged and dungeon-clear is still on for 30 s, nobody is left to
run its rez recovery or regroup (2026-11-15, ICC 10: all nine died at Lady Deathwhisper and lay there until the
mind's 30-minute stall stop). The job switches dungeon-clear off (`full_wipe dead= inside= restarts= kills=`),
and its existing `dc_stopped` restart revives the raid at the entrance 30 s later, within the run's restart and
wipe limits. Benchmarks never take this full-wipe regroup. **Native rebuild required.**

# Rezzes in the owner's group

Stock playerbots leaves a member of a real player's group lying dead: it never releases
within 100 yards of him (or inside his instance), and its paladins, priests, shamans and druids
rez only out of combat, unmounted, unmoving, within 57 yards and in sight. While he rides,
his bots ride too, and a mounted player cannot cast, so nobody rezzes. The pinned bots'
revive-in-place timer skips every grouped bot.

`Leeroy.GroupRez.Enable` (default 1) checks his group once a second (`GroupRezJob`, pure
policy `LeeroyGroupRezPolicy`). A member lying dead (not released, not in a battleground)
waits while someone of his group fights near the body: a member within 100 yards of it
engaged with an enemy, or any member engaged with an enemy within 100 yards of it. Engaged
means a combat reference to a living, non-evading unit within 80 yards of the member; the
combat flag alone is not enough, since inside a dungeon the core never ends combat by distance
(`Player::Update` does that outside one only), so a creature a wing away, or a reset
encounter's trigger, can keep a member flagged for good.

Otherwise the rezzers are his group's paladin, priest, shaman and druid bots (alive, knowing a
rez) on the body's map within 100 yards of it. One that is casting, in a vehicle, on a taxi,
teleporting, mounted in the air, owned by the Realm or training, walking in to another body or
passed over after a refusal is busy. One still flagged in combat by nothing it is fighting has
its combat references beyond 80 yards ended, as the core does outside dungeons
(`CombatManager::EndCombatBeyondRange`, `group_rez_combat_cleared guid= refs= in_combat=`); one
still in combat after that is not used. A rezzer needs the mana its rez costs now
(`SpellInfo::CalcPowerCost`; 72% of base mana, more than a freshly rezzed healer has). Of the
free rezzers with that mana, the one already walking in to the body keeps it, unless another is
already within its spell's range and in sight: that one, the nearest, is put down from its mount
or Flight Form (only within 3 yards of the ground), out of Shadowform, Ghost Wolf or a druid form,
stopped and made to cast its highest-rank rez (`group_rez_cast`). With none in range and sight
the nearest walks to the body (`group_rez_approach guid= body= yards= path=`, once per pair): a
point move to the end of its navmesh path, with playerbots' AI held 1.5 s on each pass so its
follow does not take it back, until it is in range and sight. No path, or one that ends out of
the spell's range of the body (`path=0`; a plain point move would cut straight through walls or
off a ledge), or a walk-in that has not got there within 25 s, gives that rezzer up for the body
for a minute. A walk-in given up for any reason (a fight near the body, another rezzer's cast, the
body up) stops where it is when the walker is out of combat. With no free rezzer that has the mana, the nearest short of it is asked to
drink through playerbots' own `drink` action (its food cheat sits it down to regain mana; a
mount or form is dropped first; `group_rez_drink guid= body= mana= cost= ok=`, once per pair),
again after 25 s while still short, and is used once it has the mana. A refused cast passes that
rezzer over for every body for 30 s (a global cooldown, a cast in progress or moving: 3 s);
`group_rez_cast_refused guid= body= result=` is logged at most once a minute per rezzer and
result code. His own body is offered a rez the same way.

While a body gets no rez, `group_rez_blocked reason=<code> dead=<bodies> casters=<rezzers in
his group>` is logged once a minute per reason (`fence=<codes>` too for `fenced`): `combat` (a
fight near it), `fenced` (a Realm lease or training owns it), `awaiting_answer` (a rez landed
and waits to be accepted), `self_res` (a soulstone or reincarnation to use), `no_caster` (no
living rezzer on its map), `no_caster_in_range` (none within 100 yards), `all_oom` (every free
one in reach is short of mana; they are asked to drink), `unreachable` (every free one in reach
failed to walk in to range and sight), `caster_combat` (every one in reach is still in combat),
`casters_busy` (every one in reach is busy). A rez being cast or the 3 s gap between casts on
one body logs nothing.

A bot outside a dungeon or raid that has lain dead through `Leeroy.GroupRez.ReleaseSeconds`
(default 60, at most 600, 0 never) of quiet with no rez under way or waiting releases as
playerbots' own auto-release does, and runs back from the graveyard (`group_rez_released`);
a fight nearby restarts that clock. Inside an instance it waits for a rez, as before (stock
playerbots never releases a bot there while he is on the map). Reloadable with `.reload config`.
Logs carry guids, spell ids, counts and codes only. **Native rebuild required.**

# The owner's own auras (`Leeroy.OwnerAuras`)

A GM aura such as 6560 "LeCraft Test Spell" (infinite duration, dispel type Poison) does not
stay on him: his healer bots' cleanses and poison totems take it as a poison, and it drops when
he dies like any aura. `Leeroy.OwnerAuras` (a comma list of spell ids, default empty = off,
`.reload config`) keeps the listed auras on the owner's character (`Leeroy.HumanGuid`) only
(`Guild/OwnerAuras`, pure policy `Lib/LeeroyOwnerAuras`).

- **When.** On the world tick about every 2 s, while he is in the world with a session, alive and
  not a ghost, not teleporting, not on a taxi, not in a vehicle and not in a battleground or arena
  (`OwnerAuraBlock`), each listed aura he lacks (any caster's counts) is put back with
  `AddAura(spell, him)`. Combat does not matter.
- **At once after a removal.** A `UnitScript` aura-remove hook (map threads) notes how a listed aura
  came off him (`AuraRemoveMode`: enemy spell = `dispelled`, `death`, `expired`, `cancelled`, the
  rest `unknown`) and wakes the next world tick; it never re-applies inside the removal (the core
  is iterating his aura lists there). Passes stay at least 500 ms apart, so an aura that cannot
  stay on is tried at most twice a second; a death waits until he is alive again.
- **Checks.** The ids are read at each config load and checked against the spell store on the
  next tick (at startup the config loads before the spells): an unknown id or one with no
  unit aura effect is skipped with one `owner_aura_invalid spell= reason=unknown_spell|not_an_aura`
  line, an entry that is not a positive number with `owner_aura_invalid entry= reason=not_a_spell_id`;
  the ids in force are logged `owner_auras spells=6560`.
- **Logs.** `owner_aura_reapplied spell= why=`, and `owner_aura_refused spell= why=` when
  `AddAura` gave nothing (immunity, a spell that cannot apply to him), each at most once a minute
  per spell.
- **Limits.** Nobody else is ever touched, and his bots still cleanse it: the aura is put back after
  each cleanse rather than shielded from it. `Leeroy.Enable = 0` stops it. Removing an id (then
  `.reload config`) is the only way to keep its aura off: `.unaura` or a cancel is undone.

**Native rebuild required.**

# Gear-up and the one-time regear

Playerbots' `autogear bis <ilvl>` destroys everything equipped and every weapon and armour
piece in the bags. It gears the empty bot at the cap, then puts on the BiS list of the nearest
tier at or below it. A tier's list holds items above it: 27 of the 204 entries of the 245 list
are 264-284. `PlayerbotFactory::AutoGear(bot, EPIC, ilvl, incremental)` picks the best item under
the cap for each slot and stores the replaced item in the bags. It keeps the old item when the
bags are full or nothing capped fits the slot. Incremental mode replaces an item only for a pick
scoring 1.2 times as much.

**Ordinary gear-up** (role change, under level, below average, theorycrafter; native whose hello
lists `regear`, `GearUpToCap`) never uses `autogear bis`. Its capped incremental `AutoGear`
fills empty slots and upgrades slots under the cap without destroying bag items. The separate
under-level step still calls `Randomize(true)` first, whose inventory reset can clear bag items;
that older behavior is outside the capped gear pass.
- Items already at or above the cap are protected before selection, including their enchants
  and gems. A two-handed main-hand candidate is refused while the offhand is protected, so the
  offhand stays equipped even with full bags. Compatible one-handers and unrelated slots can
  still upgrade. This needs playerbots patch `0015` and a native rebuild.
- Anything this gear-up put on above the cap is stripped, and the slot is refilled from the capped
  search with the same original protections (pure policies `GearUpSlotStep`, `GearUpProtectedSlots`).
- Nothing it equips is above its cap, and nothing the bot already holds at or above it is removed.
- Log `gear_up_member guid= ilvl= avg= stripped= returned= empty= kept=`.

**The regear** (`"regear": true`, ilvl 200-300) is the only gear-up that removes or destroys gear
above its cap (`RegearToCap`). It runs `autogear bis <cap>`, else the plain `AutoGear`, then:
- destroys weapons and armour above the cap in the backpack and four bags (never the bank, the
  mail or the guild bank);
- strips anything still equipped above the cap;
- refills emptied slots from the capped search;
- gives a slot that nothing capped can fill its old item back, never left empty, counted as `kept`.

Shirt and tabard are never touched. Only the cast's bots (pure policy `RegearRefusal`): the owner
(`owner:<guid>`), anyone outside the cast (`not_cast:<guid>`) or a character on a player's account
(`not_bot:<guid>`) refuses the whole command before anything moves. A regear while he is online is
refused `owner_online`, and his login mid-job ends it `owner_online`. Members log `regear_member`.

Fences, in every mode (`SkipFor`): each member is checked just before it is touched. It is skipped
for the body checks (grouped, battleground or queue, instance, teleport, dead, combat, flight,
trade), then for every Realm/training fence (`RealmModule::MutationReason`: a Realm lease, an
offline raid or dummy trial owning it, training cleanup, training journals, pending bot jobs, his
group). A held member is a skip in the job's terminal, never a refusal of the batch. **Native
rebuild required.**

The mind sends the regear once per value of `[guild] regear_to` (0 = never), meta
`guild:regear:<ilvl>`, only in gear-up's windows (he has been offline 10 minutes, no raid night or
offline raid).
- **Who and when.** The cast's members at 80 and above at its start (145 live). Theorycrafters go to
  `theorycrafter_ilvl` when that is higher. Batches of 40, two minutes apart.
- **Landing.** A member lands when its average is within 20 under and 6 over its target; its
  `geared`/`tc_geared` flags are replaced.
- **Retries.** Skipped members wait 30 minutes. Three landings out of bounds (or failures) give a
  member up and clear its `geared` flag, so gear-up revisits it by its average. Members still
  pending three days after the start are given up and reported. Members who leave the cast are
  dropped. With nobody eligible the regear is done at once.
- **Gear-up meanwhile.** It goes on for everyone the regear is not still holding.
- **Receipts.** Ordinary gear-ups take a floor of ilvl - 20 and no ceiling, since kept loot can lift
  the average. An older native keeps the old floors.
- **Restarts.** A hello from a restarted worldserver releases any gear-up batch the old process held.
- **Deadline.** A member still unavailable at the three-day deadline keeps its previous gear.
  Ordinary gear-up will not lower it later; a new `regear_to` value is needed to retry the regear.
- **Health.** The `regear` line shows members done of the total (a note), and FAILs a regear
  running over two days.
- **Logs.** `regear_started`, `regear_batch`, `regear_member`, `regear_out_of_bounds`,
  `regear_dropped`, `regear_gave_up reason=tries|deadline`, `regear_batch_done`,
  `regear_done ilvl= guids= avg= gave_up= dropped= kept=`.

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
## Members' heroic parties

`Leeroy.MemberDungeon.Enable = 1` enables the guild party runner after `.reload config`. It defaults to 0. The separate `Leeroy.Strings.Enable` key controls the owner's in-game strings. The mind's `[life] dungeons` switch admits a party after members agree on Discord. Halls of Lightning heroic uses normal entry and a dedicated DungeonClear test driver. The party tank leads. The native receipt records the original start, party, copy and return evidence in the characters database. Deploy the native module and mind together with `ac-release.sh --native`.

Receipt reads, schema creation and checkpoint transactions run through the asynchronous Characters
database pool. A start's positive acknowledgement waits for its first durable receipt before changing any member;
each later checkpoint waits for an independent exact-ID/revision/payload readback. A slow or failed
read keeps that transition pending while the world thread continues. Partial party construction
resumes at its saved member index. Recovery polls at most four receipts per pass, retaining every
unresolved receipt and each member's verified handback; it never rewrites the first terminal event.
An initial failed insert receives a negative acknowledgement only after its absence is verified.
Pending status queries remain unproven, never `unknown_dungeon`. Repeated original starts do not
consume the prepared owner token twice. Deferred replies keep their originating bridge connection
serial; a disconnect before admission leaves untouched bodies for normal exact-ID reconciliation.
Database waits still count toward job/setup deadlines: a late verified start may immediately enter
teardown rather than begin grouping after its deadline.
Cancellation, admitted owner requests, deadlines and configuration shutdown remain live during pending receipt
I/O. They stop an independently proved DungeonClear run immediately; group changes and returns
still wait for persistence. Recovery publishes terminal and return transitions after asynchronous
verification, including the settled receipt's final event before dropping it from recovery.
Logging in alone still leaves an accepted member heroic active.

An unavailable initial store read refuses new heroic admission and retries. It does not claim the
entire cast: known outstanding, unreadable or unverified receipts still fence their own rosters
(an unreadable receipt without a recoverable roster remains conservative). Offline returns use a
0.01 coordinate tolerance for MySQL FLOAT round trips, while still requiring the same offline,
groupless character, map, instance, difficulty, flags and exact receipt revision. The character return
and its handback proof share one guarded transaction. No migration or configuration change is needed;
this follow-up requires a native rebuild before enabling member heroics.
Restart recovery still retains a partial party whose identity cannot be proved: a reused numeric
group ID, leader and roster subset do not authorize disband. Live cancellation can instead restore
each independently owned member through the existing handback path.

Heroic receipt and Realm mail transactions require the updated core database handling from
`c8bd1f28c20c72560fa5014eda3b0aa7c3d63dec` (`fix(Core/Database): Restart lost transactions`,
upstream #28020). Check that this commit is an ancestor of the core revision used for the native
build. The core retries connection losses before COMMIT, but treats a lost COMMIT acknowledgement
as an unknown outcome. These module transactions must leave COMMIT to the core: a raw COMMIT
inside the statement batch would look like a retryable query and could replay an already-committed
character, inventory or mail snapshot.

Independent persisted readback remains the proof of completion, including when the transaction
callback reports failure. Realm mail accepts only the existing exact mail, attachment, source and
money evidence; failed, absent or mismatched readback remains unknown without another transfer.
This compatibility fix needs a native rebuild before enabling member heroics or Realm mail;
it adds no migration or setting and does not enable either feature.

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
  A fourth kind stock never sees: an add (a selectable, attackable unit) whose own periodic aura triggers an
  allow-listed damage spell (`HeldAuraHazard`; today the Living Inferno's Blazing Aura, 75886/75887, heroic 25-man
  Halion), that spell's radius plus the add's reach, up to 35 yards. A tank holds such an add in its fire, so its
  zone concerns everyone but bots playerbots reads as tanks (`held`). A fifth: an allow-listed npc that goes off
  when a player comes near (`ProximityHazardFor`, by creature entry, below), its trigger distance or its blast's
  radius (this difficulty's version of the spell) when larger; nobody holds one, so tanks keep out too. Such npcs
  walk, so each pass places the zone where the npc is and, while its spline runs, stretches it a second ahead
  toward its next path point (`HazardLeadZone`: the circle round both). Each zone keeps its object's phase mask and
  a body meets only the zones of its own phases (Halion's two realms share the room; `HazardApplies`), where the
  scan used to keep only what the owner's phase saw. Map threads read that snapshot; its version changes only
  when the zones do, so answers are reused between passes.
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
- **Moving npcs** (2026-11-24). A zone that moves is never a pocket: while the first zone across the straight way
  moves (`HazardFirstCrossingMoves`) the detour never gives up, and with no way round it the bot waits for the way
  to clear, but only where it may stand: no moving npc comes within its own radius, the bot's reach and 1.5 yards in
  the next 2 s (`HazardStandClear`); else it steps aside, up to 10 yards, to the nearest such spot, rather than
  leave a bot waiting in a slime's way to the exit. While a moving zone lies within 20 yards, follow walks short
  legs (`HazardMovingLeg`): playerbots commits a bot to the leg it sets out on for that leg's walk time, up to
  `Playerbots.MaxWaitForMove` (5 s, 35 yards at run speed), so a short leg makes it plan again from each fresh pass.
  A leg is 4 yards, or longer to lie beyond where follow stops: the bot's own formation distance from its edge
  (`FollowStop`; playerbots' default chaos formation widens `Playerbots.FollowDistance` by up to 3.5 yards every
  3 s), and so is a detour step; a nearer spot made follow judge itself done, and the bot stood still beside the
  lanes. Across a lane a leg runs on to the first spot where the bot may stand, so it crosses in one walk instead of
  stopping in a slime's path. A walk (a leg, a detour step, a crossing) is judged as both move
  (`HazardSegmentClear`): the bot at run speed to its end and standing there a second, the npc on its way from a
  pass up to 500 ms old; it is not clear when they come within the npc's own radius plus the bot's reach, so
  stepping into a slime's way is refused and passing behind it is not. Map threads take only the bots of the master
  whose group the last pass made aware (before, `Leeroy.HumanGuid`'s, which left the dojo's synthetic leader's bot
  without zones).
- **Exits.** `avoid aoe` (after its three stock cases, in combat as well) walks a bot standing in a zone to the
  nearest clear spot and keeps that exit while it stays clear. Out of combat the exit ranks by the follow goal:
  one from which the goal is in plain view (his side) beats a nearer one unless the nearer saves more than 4
  yards, so a bot just inside the near edge steps back and goes round, and one past the middle finishes the
  crossing. In combat, ties go toward him. An exit never loops with a lower-relevance mover (follow, or
  dungeon-clear's `.dc on` route at relevance 15-57) that walks the bot back into the same hazard: entries are
  counted per hazard, into a hazard the bot did not stand in at its last answer (an exit planned again inside the
  same hazards counts nothing; a bot walked from one overlapping hazard into the other counts each) and never into
  a moving npc's zone, which walked onto the bot (`NoteHazardZoneEntry`; it is always stepped out of), and a
  hazard's third entry within 20 seconds yields it to the bot's own movers for 20
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

**Burning adds (Ruby Sanctum, 2026-11-20).** The Living Inferno (40681) that each heroic 25-man meteor wakes
carries Blazing Aura (75885, also its creature addon aura), triggering 75886 every 2 s (75887 on 25-man), 15
yards round it; the boss tank holds it beside Halion, where his melee stand. Stock's trigger npc case and this
scan's skip it (it is selectable), so it is allow-listed as a held hazard: 15 yards plus its reach, never for a
tank. Halion's meteor crater (40029, 74713 -> 74712) and the flames its script spreads (40041-40044, 40055,
74718 -> 74717), Combustion (40001) and Consumption (40135) are unselectable trigger npcs within 15 yards and
were measured already.

**Npcs that go off when a player comes near (2026-11-21).** In Naxxramas the owner's bots walked straight into
the Living Poison of the Construct Quarter's hall (`hazards_minute ... zones=0`): an unselectable npc with no
aura, so none of the four kinds saw it. From the core's scripts, the live `Spell.dbc`/`SpellRadius.dbc` with the
core's `SpellInfoCorrections` and the world database (`creature_template`, its addon auras and SmartAI rows):

| Npc | Where | Script: what sets it off | Then | Zone |
|---|---|---|---|---|
| 16027 Living Poison | Naxx, Construct Quarter hall | `npc_living_poison`: `SelectNearestTarget(1.5)`, a player's centre within 1.5 yards; it walks one of three lanes (`LivingPoisonDataList`, one every 5 s each, 2.5 yards a second, gone after ~15 s) | Explode 28433, 5 yards (radius index 8), ~30k nature, and it dies | **1.5**: the trigger, not the blast: 5 yards round slimes 12.5 yards apart would shut every lane, and a bot that sets none off is caught in none of its own |
| 34362 Proximity Mine | Ulduar, Mimiron phase 1 (ten from the Leviathan Mk II at a time) | `npc_ulduar_proximity_mine`: a player within 1.9 yards (`SelectTargetFromPlayerList`), checked every 500 ms; else it goes off after 35 s | Mine Explosion 66351 (63009 on 25-man), 3 yards (index 15) | **3**, the blast |
| 33292 Ominous Cloud | Ulduar, Yogg-Saron phase 1 | `boss_yoggsaron_cloud`: drifts in circles round the room (`MoveCirclePath`); every 500 ms a player within 6 yards of its edge (`SelectNearbyTarget(6)`) | Summon Guardian of Yogg-Saron 63031 | **6 + its reach** |
| 39137 Shadow Trap | ICC, the Lich King phase 1 | its periodic aura 73525 casts the searcher 74282 every 500 ms, 3 yards by the core's correction | 73529, 10 yards of damage (index 13) and a knockback (10 yards by correction) | **10**, the blast; playerbots' own Lich King strategy also keeps 12 yards |

A replay of the hall from `LivingPoisonDataList` (`Hazard_AFollowerCrossesTheLivingPoisonLanesWithoutSettingOneOff`:
a follower at run speed across all three lanes, 120 starts over every phase of the waves, the zones 500 ms old)
reaches the far side every time and never comes within 3 yards of a slime; walking straight sets one off in 57.
Its first run counted each re-planned exit as an entry (the job counted whenever its exit memo was not reusable),
so a slime walking onto a bot yielded the bot to follow within a second; hence the per-hazard entry rule above.
`Hazard_TheDojoHallCrossingSetsNoSlimeOff` replays the dojo's crossing (below) with legs committed as playerbots
commits them, a turn of the bot's AI every 0.1 s and both formations, over 200 phases of the waves: no slime goes
off and no crossing stalls; #424's rules alone set off 5, stock 954. A slime that appears on a bot standing at its
pipe (a lane's start) is not foreseen. While such an npc is within the scan the zones change every pass, so follow
spots and detours are worked out again every 500 ms (as when an area appears or ends).

**The dojo's crossing (#452, 2026-11-24).** The isolated raid dojo walks a stock bot after a held leader across the
hall for 70 s. As first measured it set off 3-6 slimes a run: the filter took only `Leeroy.HumanGuid`'s bots
(the dojo leaves it 0), so the bot walked straight. Given its zones (#424 as released) it still set off 2-5 a run:
long legs walked blind and crossings given up as pockets. With the rules above every crossing out of combat was
clean, but 7 of 24 runs still set off 2-4: a Sludge Belcher patrol pulls the leader into combat about 55 s in, and
the bot's combat engine, built under a bot master, had no `avoid aoe`, so no exit ran while it stood on lane 1.
Given it, as playerbots gives it under a game client master: 0 in 14 runs (43 crossings, two full gate runs among
them), nearest 1.7 yards; the stock control set off one in 9 of 10. The filter serves bots whose master is the
owner, a game client, whose AI takes a turn every 0.1 s; under a bot master playerbots waits 1-3 s between turns
out of combat (`GetReactDelay`), which no bot of his group meets, so the dojo holds its leader as a self-bot (its
own master) between teleports to measure that cadence.

Looked at and left out: Fallout Slime (16290, Grobbulus) and Sewage Slime (16375, Construct Quarter trash) carry
Disease Cloud (28156 -> 28153, 15 yards) and Embalming Slime (16024) casts Embalming Cloud in combat, but each is
an attackable add the raid kills inside its cloud; Mimiron's Bomb Bot (33836, SmartAI: explodes within 3 yards of
its victim), Lady Deathwhisper's Vengeful Shade (38222: Vengeful Blast procs on its one fixated target),
Putricide's Volatile Ooze and Gas Cloud and the Blood Princes' Kinetic Bomb chase or are struck by chosen players
(playerbots' ICC strategies); the Twin Val'kyr's orbs (34628/34630: `SelectNearestPlayer(2.75)`) are collected by
colour; Valithria's Dream Clouds (`SelectNearestPlayer(5)`) are flown through on purpose; Sapphiron's Blizzard
(16474, `trigger_periodic`) drops 3-yard Chill areas the dynamic object kind already sees; Algalon's black holes
are the mechanic responder's (`nearest` areas). The same search of the Northrend 5-man scripts and SmartAI rows
found none (Xevozz's Ethereal Spheres empower him, not a player).

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
target stands in a hazard can step back in to reach it (after two exits the yield lets it stay). A Living Poison
the owner himself sets off still catches the bots within 5 yards of it, and a moving npc's zone is up to a pass
(500 ms) old plus its one-second lead. Logs carry
guids, counts and reason codes only. **Native rebuild required, with the playerbots patch applied first.**

# Boss mechanics at cast start (the mechanic responder)

Stock playerbots meets most boss mechanics only after standing in them: `avoid aoe` steps out once an effect has
landed (and only with a real-player master), and nothing at all handles the mechanics the raid playbooks mark
`handled_by = "none"`. `Leeroy.Mechanics.Enable` (default 0, `.reload config`) moves the cast bots of the owner's
group and of a running offline raid when the boss **starts casting**, from the playbooks
(`mind/leeroy_mind/raidplay/README.md`, the mind's `[raidplay] respond`). The world side is `Guild/MechanicJob.*`,
pure policy and geometry `Lib/LeeroyMechanicPolicy.*` (unit tests `TestMechanicPolicy.cpp`); the bodies move
through the local playerbots patch `0012` (`PlayerbotMechanicGuard`; without it the start log says the guard is
unavailable and nobody moves). Hello lists `features: [..., "mechanics"]`.

- **`raid_mechanics {map, difficulty, mechanics: [{id, kind, area, roles, spells, radius, range, angle,
  duration, landing}]}`**: one instance's whole table (difficulty 0 10, 1 25, 2 10 heroic, 3 25 heroic),
  replacing the last one for that map and difficulty; at most 64 rules of at most 16 spell ids each, 16 tables,
  yards up to 100, `duration` up to 120 seconds. Kinds and their areas: `avoid_ground` (`dest`: a circle round the
  cast's destination, else its target; `caster`: round the caster, following it, ending with it; `line`: a band
  `radius` either side and `range` long from the caster toward its target, else its destination, else its facing;
  `ring`: a ring `range` from the destination, `radius` either side, as Infernal Eruption's blasts land 15 yards
  from the volcano), `move_out_cone` (`cone`: `angle` degrees wide and `range`, else `radius`, long, aimed the same
  way), `spread` (`group`), `stack` (`dest`: gather within `radius` of the cast's target, following it, else its
  destination), `run_out_debuff` (`target`: the member the cast or its aura lands on keeps `radius`, else
  `range`, from every other member) and `los` (`caster`: out of its sight); `stack` and `avoid_ground` also take
  `nearest` with `entries` (1-8 creature entries, required with it and refused elsewhere: `bad field: entries`):
  a circle `radius` round whichever live creature of those entries stands nearest the bot within 40 yards, which
  a stack gathers in from anywhere within those 40 yards and an avoid_ground leaves (Algalon's Black Holes at Big
  Bang, hello feature `mechanics_nearest`). `landing: true` means the place is
  known only when the cast completes (Shock Vortex lands 13 yards from its target in a random direction): such a
  rule binds nobody until then. A malformed field is refused `bad field: <key>`. The table is accepted with the
  switch off too (the switch gates only the responses), and it belongs to the mind connection that sent it: a new
  connection, or none, drops it (`mechanic_tables_dropped tables=`), and the new mind sends its own at hello, or
  nothing when its switch is off. Each accepted table logs `mechanic_table map= difficulty= rules=`.
- **Who moves.** Every 250 ms the world tick takes the members of his group (no battleground or battlefield
  group) and of a running offline raid (its members in its group inside its instance; never a benchmark) on a
  raid map whose map and difficulty have a table. A member responds while it is a cast bot (not a self-bot, never
  him), alive, in the world, not teleporting, on no vehicle or transport (the gunship's deck), and not fenced:
  for his group a Realm lease or `NativeOwnershipReason` (training, an offline raid, a pending bot job); for an
  offline raid a Realm lease, a dummy trial, the global training cleanup fence, a pending bot job, or a training
  DC run or job of any leader but the raid's own (`TrainingDcOwners`: every offline raid registers one over its
  whole group, and its training run's journals are its own). A bot with `stay` or `passive` in its combat
  strategies is held (his own stay, the raid lead's `stay` or `hold`, a dungeon-clear camp hold) and is left where
  it is. Its role is playerbots' reading of its spec (tank, healer, ranged, melee). Random bots and anyone outside
  his group or the offline raid are never in the snapshot, and a bot not in it is answered at once.
- **Cast start, completion and cancel.** On every spell's `OnSpellPrepare` (map thread; an atomic check first) a
  creature casting in a raid instance with responders looks the spell up in the table; each rule it starts becomes
  a mechanic in force for the cast time plus its hazard's linger: the rule's `duration`, else the spell's own up
  to 120 seconds, else 3 seconds, never under 1.5 (at most 64 at once). `OnSpellCast` reads the cast's destination
  and target again once its targets are chosen, so a destination offset from the target is where it really lands,
  and a `landing` rule binds from then on. `OnSpellCastCancel` (the boss stopped the cast, or broke off a channel
  such as Hellfire) ends what that cast started at once (`cancelled=1`; a debuff already on a member stays). A
  `run_out_debuff` needs a player target; when the debuff (a rule spell) lands on a player as an aura, that starts
  or extends it too. An instant cast has hit before any hook runs, so the playbooks leave instant cones out.
- **Moving.** Playerbots' `mechanic move` trigger (in every class combat strategy) asks the guard whether a
  mechanic in force binds the bot (its role; for a run out only the debuffed member; a stack only within 20 yards
  of its point, so a horseman's tank in another corner is left alone); the `mechanic move` action
  (`ACTION_RAID + 9`: above stock's raid positioning while it lasts, below every emergency and stock's own
  `ACTION_EMERGENCY` mechanic moves) then makes a forced move to the nearest spot that keeps every duty with 1.5
  yards to spare: out of each area (beyond the bot's reach), within the stack radius less 2 yards, beyond the
  spacing from every other member of the group or raid within 60 yards (a chain such as Holy Wrath or Fel
  Lightning jumps between both bodies' edges, so its 10 yards are 10 plus twice the reach, 13 for players; a burst
  such as Mana Detonation reaches the other's edge, 10 plus one reach), out of the caster's sight; straight out
  first, then rings of 2 yards up to 40 yards (spreading: away from the others' middle between equals), clear of
  the ground hazards `HazardJob` measures (scanned round the responders while a mechanic is in force, whatever
  `Leeroy.Hazards.Enable` says), and checked against the navmesh. A bound bot standing in one of those hazards is
  not "kept" and moves out with the rest. The chosen spot is kept while it still holds; a search that found
  nothing is tried again after a second. When the mechanic ends nothing is restored: no strategy, value or master
  was changed, and the bot plays on as stock.
- **Refusals.** Meanwhile `MoveTo` refuses an ordinary (not forced) move whose destination lies in an area a
  mechanic in force makes the bot leave, and nothing else: never while the bot stands in a known ground hazard
  (stock `avoid aoe`'s one way out comes first), never for spacing or a gather (the mechanic move serves those),
  and never a forced move.
- **Phases.** A mechanic binds only bots in its caster's phase while the caster is there (Halion's physical and
  twilight realms share the room: a meteor or an inferno in one never moves a body in the other), except a
  `nearest` area, whose refuge is what takes the bot out of the caster's phase (a Black Hole phases it); the world
  tick scans those creatures in every phase round the responders while one is in force. The known
  ground hazards a bot's spots keep clear of are those of its own phases; an add's own burning aura
  (`HeldAuraHazard`) is no hazard for a tank, who holds the add in it.
- **Never fight stock.** A bot whose last move is a forced move the guard did not issue, made since a mechanic
  began (a stock raid strategy's own positioning), is yielded from that mechanic for the rest of it: the guard
  stamps its own forced moves through `MechanicMoved`. The raid lead's `spread`, `run_out` and `back_off` orders
  (#299) yield their bots for 10 seconds first (`MechanicYield`), so neither refuses nor undoes the other, and
  neither reports the other as a failure.
- Logs: `mechanic_response id=<playbook mechanic> spell= kind= members=<moved> map= instance= cancelled=` when
  each ends, `members` counting the bots whose forced move was issued (the training ground's per-mechanic checks
  measure the effect); one an aura's periodic tick started (Blazing Aura, every 2 s) only when it moved someone
  or was cancelled (`MechanicEndLogged`). Once a minute while there are responders,
  `mechanics_minute responders= instances= held= fenced= dropped=`. Ids, counts and codes only.

Limits: a spell whose place is chosen after cast start without a landing to read (Putricide's bombs, the Beasts'
slime pools, Icehowl's charge, the twins' orbs), an instant cone (Grobbulus's Slime Spray, Arctic Breath, the
25-man Impale), and the mechanics where stock moves the same bodies with forced moves (Rotface's Sticky Ooze, the
heroic Lich King's Shadow Trap) are left out by the playbooks; `soak` (the off-tanks' Hateful Strike and the like)
and the non-movement kinds (`interrupt`, `dispel`, `tank_swap_at_stacks`, `switch_adds`, `kill_order`) are later
work. A hazard that outlives its response (a Fire Bomb's 60-second patch) is left to the hazard scan and stock
`avoid aoe` in his group; in an all-bot offline raid nothing walks a bot out of it after that. Spreading bots plan
against current positions, so neighbours may step twice; a bot with no clear reachable spot within 40 yards
stays. Encounter multipliers that zero movement actions zero this one too.
**Native rebuild required, with the playerbots patch applied first.**

# The owner's strings in game

His strings (D6 godmode, `mind/leeroy_mind/godmode/README.md`): from a member's detail in the guild console the
mind sends one `string_do {guid, kind, target_guid, emote}` (`Guild/Strings.cpp`, pure policy
`Lib/LeeroyStrings.h`). Native carries it out at once on the world thread and acks it, publishing first a done
`job` (kind `string_do`) whose `reason` says what she did (`came`, `joined`, `joined_far`, `following`,
`stopped`, `challenged`, `emoted`, `sat`); or it refuses with one code in the ack and changes nothing.

`Leeroy.Strings.Enable` (default 0, `.reload config`; `off` while 0). Every kind needs him online and alive, the
member a pinned cast member of his guild on a random-bot account run by playerbots, and her body free of every
ownership fence: a Realm lease, training, an offline raid, a pending bot command (`MutationReason`; a member of
his own group under every fence but the one that holds his group for him, as his add command treats her). Never
in a fight (`combat`: hers, his or his group's on his map), in or onto an instance (`in_instance`, hers or his), in a battleground or
arena (`in_bg`), a queue (`queued`) or while either travels (`travelling`). Then per kind:

- `come_to_me`: only while he stands in a city or rest area (a capital and its parts, a town with an inn, a
  faction rest zone, or wherever the core has him resting; else `bad_area`), never out of someone else's group
  (`grouped`). She is put beside him (the add command's teleport); one outside his group then keeps
  playerbots' `stay` for 10 minutes once she has landed, so her routine does not walk her off. For that window
  she is fenced: `NativeOwnershipReason` answers `busy:string` (`StringStayHeld`), so Realm moves (the bond's
  inn visits included), queues, offline raids and dummy trials (`busy:string:<guid>`, which the mind's planner
  retries in 15 minutes instead of leaving her out of the day) refuse her, while his own asks (his strings, his
  add command) pass it (`StringWaiver`). The fence ends with the stay: its 10 minutes, the key at 0, his
  logout, or her joining a group. If he is hovering on a flying mount she lands beside him in the air and falls,
  as the add command's teleport does.
- `join_group`: into the party he leads (made now when he has none), never a raid or a Dungeon Finder group,
  at most five (`in_group`, `lfg_group`, `not_leader`, `in_raid`, `group_full`), under the add command's own
  member fences for a party of his (`grouped`, `locked`, ...). She joins with the raid night's habits
  (`RaidHabits`: him as master, hazards) and comes to him.
- `follow`: in his party only (`not_in_group`; his raid `busy:raid_owner`). Him as her master with playerbots'
  `follow`; the same string again stops it and she waits where she is (playerbots' `stay`). Both last while that
  group does; a stop in a party that becomes a raid is lifted by the tick and her follow given back, since the
  string can no longer undo it there.
- `duel_me`: neither in a group with the other (`in_group`), nobody duelling (`busy:duel`), the same zone within
  30 yards (`too_far`), both where the core allows a duel and outside a sanctuary (`bad_area`). The core's duel
  spell is cast at him; accepting is his.
- `emote`: one of 16 friendly emotes (`bad_emote`), within 25 yards (`too_far`), aimed at him as bridge speech.
- `sit`: within 20 yards (`too_far`); #244's social seat keeps her sitting up to 5 minutes while he stays near
  and nobody in his group fights. A later come_to_me, join_group, follow or duel_me stands her up first.

None of these takes a Realm body-ledger rank (they are his own asks; come_to_me's window is a native fence
instead), and the Realm keeps refusing a body in his group as before. What a string changed is journalled per
body, AI and session (pure decisions `StringHoldFences`, `DecideStringHold`): the world tick (once a second) lifts
only the `stay` it added (after come_to_me's 10 minutes or his logout, when the follow's group ends or turns into a
raid, or when the key goes to 0) and stands a seated member up when her seat ends; a body another owner holds, or
in combat, is never touched and waits; a relog, an AI replacement or (come_to_me) a group join drops the hold
untouched. Logs: `string_done guid= kind=
outcome=`, `string_refused guid= kind= code=`, `string_stay_lifted guid=`. **Native rebuild required.**

## His invitations in game (`Leeroy.Invites.Enable`)

His invitations in game chat ("meet me at the bank", "let's go fishing in Dalaran"; the mind's
`[talk] invites`, `mind/leeroy_mind/invites/README.md`) are the same `string_do` marked `"via":
"invite"`, which answers to `Leeroy.Invites.Enable` (default 0, `.reload config`; `off` while 0)
instead of `Leeroy.Strings.Enable`. Hello's `features` lists `invites`. Every fence and shared
refusal above holds, and two kinds are added (pure policy `Lib/LeeroyStrings.h`, spots
`Lib/LeeroyInviteSpots.h`):

- `go_to {spot, slot}`: one of the compiled meeting spots (where a world NPC of that place stands,
  read from the world DB's creature spawns; a spot the places whitelist also names carries exactly
  its coordinates: `dal_fountain`, `dal_krasus`, `dal_inn`, `dal_horde_inn`, `dal_bank`, `org_inn`,
  `org_bank`, `uc_inn`, `uc_bank`, `tb_inn`, `tb_bank`, `smc_inn`, `smc_bank`, `booty_bay_docks`),
  slot 0 two yards in front of it, slot 1 two yards to its left. Never out of a group (`in_group`
  when she is in his, `grouped` in another's); `bad_spot` when the point is off its map or its zone
  (checked as the places' ValidPlace does). She is teleported there and then held as after
  come_to_me: playerbots' `stay` once she has landed at the spot, for 10 minutes, fenced as
  `busy:string`. Done job `went`.
- `fish`: playerbots' own `master fishing` (the strategy its bots take when their master fishes;
  a random bot without a pole is given one by playerbots). Needs the fishing skill (`no_fishing`),
  her within 40 yards of him or at the fishing spot a go_to of his sent her to (`too_far`), never
  in another group (`grouped`). In his group she fishes near him as his bots do; outside it
  playerbots' own water search (from where she stands, or a spot to walk to) gives the spot, her
  `stay` goes there (`no_water` when there is none) and she is fenced `busy:string` meanwhile.
  It ends after 15 minutes, once he has come within 40 yards and gone farther than 60 (or to
  another map, or offline), with the key at 0, or with any later string that moves her; only the
  `master fishing` and `stay` it added are taken back (`invite_fish_ended guid= why=`), and the
  main hand, off hand and ranged items she held when it began go back into their slots when they
  are still hers, in her bags and equippable there (playerbots' fishing puts its pole in her
  hands; a group join takes them back only while nobody else holds her and she is not fighting;
  a logout leaves the pole). Done job `fishing`.

A hold records whose string it was: an invitation's lifts with `Leeroy.Invites.Enable` at 0, a
console string's with `Leeroy.Strings.Enable`; a seat is kept while either is on. GuildFishing's
single proved cast (`bot_do` "go fishing") is not used for invitations: it refuses a member in a
group or with a master, and the stay a come_to_me or go_to leaves her fences her from its Realm
queue. **Native rebuild required.**

## His "you lead" (`Leeroy.Lead.Enable`)

His "you lead", "take the lead", "take us questing", "go grind", "lead us to the inn" in party,
raid or a whisper (the mind's `[talk] lead`, `mind/leeroy_mind/lead/README.md`) is `lead_do`
(`Guild/Lead.cpp`, pure policy `Lib/LeeroyLead.h`), answering to `Leeroy.Lead.Enable` (default 0,
`.reload config`; `off` while 0, and 0 ends the lead in force). Hello's `features` lists `lead`.
One lead at a time (a newer start ends the one in force, `replaced`).

- `{"action": "start", "guid", "activity": "quest"|"grind"|"travel", "spot"}` (`spot`: travel
  only, one of the meeting spots of `Lib/LeeroyInviteSpots.h`). Refused with one code
  (`LeadRefusal`): `off`, `human_offline`, `not_cast`, `not_online`, the first ownership fence of
  a member of his group (every one but `busy:raid_owner`, as his strings), `dead`, `combat` (hers
  or his group's fight), `in_instance` (either of them: dungeon-clear leads there), `in_bg`,
  `queued`, `travelling`, `human_dead`, `not_in_group`, `lfg_group`, `not_leader`, `too_far`
  (farther than 100 yards, or another map), `unsupported` (playerbots lacks the strategy); travel
  adds `bad_spot`, `other_map`, `here`. Carried out: she masters herself (a self-mastered bot, as
  an offline raid's driver: playerbots then keeps her as the others' master, and an arena captain's
  gather leaves them alone), playerbots' `follow` (and an older `stay`) come off her, and the
  activity's strategies go on: `new rpg` + `grind` (quest; new rpg starts idle and picks its own
  statuses), `grind` + `new rpg` (grind; wandering), `new rpg` alone (travel; `go camp` to the
  spot). The cast members of his group on her map whose playerbots master is him, free of fences,
  get her as their master, so their own follow and formation follow her. Done job `lead_do`,
  reason `leading`, `guids` the followers.
- `{"action": "end", "guid"}` (0 or absent: whoever leads): his end, whatever the switch says
  (`no_lead` when none is in force, or another member leads).

The world tick, once a second (`DecideLead`): **the wait-for-him rule**: once he is farther than 40
yards from her (or on another map, or being teleported) her activity's strategies come off and she
stops where she is; they go on again once he is within 20 yards (never in a fight: hers or his
group's). new rpg is kept to the activity (`LeadSteer`: a flight or outdoor PvP while questing is
turned into wandering, grinding keeps to idle, going to grind, wandering and resting, travel always
goes to the camp), and playerbots' stuck teleport is held off (its stuck clock is refreshed) so she
never teleports away from him; a travel that gets no 5 yards closer in 2 minutes of her own walking
ends instead. It ends (`lead_ended guid= why=`, published as the job `lead`, state `ended`, the
start's id, `guids` the followers given back), the first that holds: `gone` (another life, her
logout), `off`, `logout` (his), `dead`, `instance` (she or he on an instance map), `pvp`, `group`
(she left his group, he no longer leads it, a finder group), `taken` (her master is no longer
herself: someone else took her), `reset` (playerbots reset her strategies), `fenced`, `teleported`
(being teleported, a new map, or 250 yards in a tick: a self-mastered bot's teleport is
acknowledged by playerbots only once she is ours no more, so it ends at once), `timeout` (60
minutes), `arrived` (travel, within 15 yards), `no_path`, `left` (he stayed behind 10 minutes).
Restored from its journal, same body, AI and session only: the followers whose master is still her
follow him again (nobody if he left their group), then she loses the strategies the lead added
(and gets back her own that a wait took off), new rpg goes idle, her master is him again and her
`follow` comes back if the lead took it. A
follower whose body, group or master changed is let go untouched (`lead_follower_released`); one
that logs out is forgotten. A removed `stay` is not given back (leading means moving).

Not covered: members who join his group during the lead keep following him; addclass bots and
members of other guilds are never touched. **Native rebuild required.** Unverified in game at
release: new rpg's choices while self-mastered in his group, the followers' formation around her,
the wait and resume, and travel's walk through a city.

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

# Calendar writes take no body fence

A calendar post, update or signup changes CalendarMgr rows and sends calendar packets. It never
touches a body (strategies, group, master, position, DC settings), so native refuses one only for
identity (`CalendarMemberAdmission`, `LeeroyRealmCalendar.h`): `realm_off`; `no_event` unless the
character is a cast member, not the owner, and its character cache row, live body (a bot between
maps counts) and roster entry all name the configured guild and one account; `not_bot` off a
random-bot account. The event's creator is checked the same way (`CalendarAuthorityReason` first),
the event must exist, and each write's own checks stay (packet hooks, time, caps, invite lock,
`calendar_authority_changed` when an identity fence moved mid-write). No body fence refuses one:
not the training cleanup (`TrainingCleanupPending()`, `OfflineTrainingCleanupPending()`), not a
training, offline-raid, Realm or raid owner, not pending bot commands, not the queue fence.

Before this, `MemberReason` reused the body-move admission (`MutationReason`). On 2026-11-10 a
twelve-hour training cleanup refused all nine raid-night posts (three nights, three creators each)
and 361 signups `busy:training_cleanup`; the mind gave each post up after three tries, so none of
that week's raid nights reached the calendar. No offline-raid post ever succeeded either: its
creators were inside the running raid (`busy:training_owner`).

`calendar_signup` is now a direct command like a post: one synchronous write on the world thread
that reserves, pins and queues nothing (`IsQueued` no longer lists it). The signer and the event's
creator are each held to their identity fence (`CreatorFence`/`ExactCreator`) across the write.
It acks as before and still publishes `realm_job` `calendar_signup` (the member as `guid`, value 0)
once the write was attempted; a refusal is the ack's alone, as for a post. A write that threw or
failed its readback after native entered it answers `calendar_write_unknown` (ack and job); the
mind settles that as unknown and never sends it again. The first write of each
kind past each body fence code is logged once per process:
`realm calendar_signup by guid=<n> went past body fence busy:training_cleanup (...)`.

Body moves, `NativeOwnershipReason` and `OfflineTrainingCleanupPending()` (still unbounded) are
unchanged. Ship with a native rebuild (`ac-release.sh --native`); the mind needs no change for it,
and its desk separately never spends a try on an older worldserver's `busy:` refusal.

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

# The posted lineup in the owner's add picker

`guild_add_roster` is a bridge command (`members`, `bench`: positive guids, none twice, at most 200
each, none in both; `until_ms`: Unix ms, at most 12 hours ahead, else `bad field: until_ms`). The
mind sends it while a raid lineup the guild posted (its `[decisions]`) is at hand, and an empty one
when that night ends. Native holds it in memory only: a worldserver start clears it and the mind
sends it again on hello. With `Leeroy.GuildAdd.FollowRoster = 1` (default 0, `.reload config`) and
the roster unexpired, the owner's `.playerbots bot addN` takes, within each role, the roster's
members first and its bench only when no other free member fits; every existing fence, the size
template and the buff and gear rules still order the members within each tier (pure policy:
`PlanGuildAdd`'s roster, `GuildAddRosterInForce`). At 0, or with the roster expired or empty, it
picks exactly as before. The `guild_group` events of a group he built carry `roster_followed`:
true once a fill of that group added at least one of the roster's members or bench
(`GuildAddRosterFollowed`), so the mind can record the lineup in effect through his add. Server.log:
`guild_add_roster held (N members, M bench)` and `add<N> from the guild follows the posted roster
(...)`, counts only. **Native rebuild required.**

# The play-style table (`playstyle_table`)

`playstyle_table {styles: [{guid, param}, ...]}` (pure policy `Lib/LeeroyPlayStyle.h`, parser
`ParsePlayStyleTable`, adapter `Guild/PlayStyle.cpp`; hello feature `playstyle_table`) is the mind's
whole persona play-style table, at most 512 members, none twice, each `param` a `bot_do playstyle`
param. It replaces the last table whole and belongs to the bridge connection that sent it; a member
it repeats unchanged keeps its retry time. With `Leeroy.PlayStyle.Enable` and `Leeroy.PlayStyle.Table`
(default 1, `.reload config`) on, he online and his group no raid group, the play-style tick (once a
second) tries at most 16 due members, those waiting longest first, through the same admission and
journal as `bot_do playstyle`, and checks each tried member again 15 seconds later whatever came of it. A member whose
journal is the table's with the same request is not tried; one whose journal another writer made
(an explicit `bot_do playstyle`) is left to that writer. The tick takes back a journal the table made
once the table no longer lists its member: a smaller or empty table, the mind's disconnect (the
table is dropped, `why=mind`) or `Leeroy.Enable`, `Leeroy.PlayStyle.Enable` or `Leeroy.PlayStyle.Table`
at 0 (dropped, `why=off`, as a config load invalidates queued `bot_do` additions; the mind sends it
again at his next login or hello). The usual cleanup (PvP, instance, raid, role, another owner) still
applies, and the table then styles her again once she is free. So does a table journal whose fields no
longer hold what it wrote (`Journal::Drifted`): playerbots' `OnPlayerLogin` resets the strategies of his
group's bots at his relog without a new body or AI, so the tick restores what is still the table's and
styles her anew.

The handler refuses `disabled`, `playstyle_off` and `playstyle_table_off` (the mind then keeps one
`bot_do` a member), and `not_connected` for a table its mind queued before that connection closed (a
table is bound to the command's own connection serial, never to the one that replaced it). Server.log: `mod-leeroy: playstyle_table held styles=N`, `mod-leeroy:
playstyle_table dropped styles=N why=mind|off`, and at most once a minute, when a style was added or
the styled count moved, `mod-leeroy: playstyle_table styles= styled= tried= applied=
refused=<code=n,...>`; counts and codes only. **Native rebuild required.**
