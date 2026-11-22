# Supervised vehicle rehearsal

**Status: not run.** This is a plan and diagnostic instrumentation, not raid
completion evidence. Do not enable either switch or restart anything as part of
merging this PR. Use a separately approved native release and a pull with the
owner present. The GPU is not involved.

Patch 0014 contains the controller. Patch 0020 adds observational diagnostics;
it does not board, move, cast, eject, heal or alter the encounter. Both switches
default to zero:

```ini
Playerbots.RaidVehicleSupport = 0
Playerbots.RaidVehicleDiagnostics = 0
```

## Prepare one test window

1. Record the released commit, applied module patches, raid size/difficulty,
   instance ID, start time and roster. Confirm nobody else has a release lock
   or native build in progress. Enable the two switches only in the agreed test
   window, through the usual native configuration/release procedure.
2. Keep an untouched copy of the pre-test config. Confirm the owner can control
   and exit their own vehicle. Do not simulate progress with `.die`, granted
   health/energy/auras, forced teleports or achievement commands.
3. Record an ordinary pull before drawing conclusions. A passing policy test,
   a queued action, a spell-click call or a vehicle helper returning true is not
   proof of boarding, damage, healing or a kill.
4. Preserve the `raid_vehicle` records for the full pull, together with the raid
   debrief, training hurt and the core encounter/achievement records. Stop the
   trial if vehicles move under the owner without input, casts continue from an
   invalid seat, field avoidance oscillates, or logs show suppressed records.

## Read the evidence correctly

Diagnostics emit one compact sample per rider every five seconds, plus seat and
lifecycle transitions. Records contain numeric identifiers only. At most 256
riders are tracked, with a global budget of 512 records per five seconds. A
nonzero `suppressed` value means the record is incomplete; do not count a quiet
interval as proof that nothing happened. Unknown spell IDs are excluded.

- `code=seated` is observed membership in an actual supported vehicle seat,
  not a requested boarding action. `entry`, `seat`, `control` and `can_cast`
  describe that seat. `seat` is the DBC seat ID, not a passenger index.
- `casts=56091:3,56092:1,` counts ordinary, non-triggered spells that reached
  the core's successful `OnSpellCast` hook in that window. The vehicle helper
  ignores the return value of `Spell::prepare`, so its boolean is deliberately
  not used as evidence. Accepted casts still do not prove a hit, aura, healing
  amount, interruption, target or achievement: inspect those effects separately.
- `cancels` counts observed ordinary cast cancellations. Zero cancels does not
  prove zero rejected attempts; failed prechecks and failed preparations are
  not successful cast events.
- `ready` is the existing controller's raid eligibility predicate. It does not
  claim that a particular spell is affordable or a passenger can drive.
  `energy`, `combo` and `x/y/z` are observed vehicle state at the sample time.
- `parent`, `parent_driver` and `parent_human` identify a nested gunner's parent
  vehicle. A turret's inherited movement alone cannot establish who steered
  its parent; also watch the owner's controls and the driver's seat.
- `seat_changed`, `seat_lost`, `vehicle_dead`, `died`, `logout` and `map_changed`
  attempt to flush the previous observed seat and clear its counters even when
  the log budget suppresses that flush. A new seat always starts a fresh window;
  counts from the old seat are never attributed to it. `expired` means no
  observation arrived for 30 seconds, not proven logout. There are no cached
  game pointers. Disabling diagnostics clears pending windows without emitting
  them; mark the end time before switching it off.

## Malygos: normal 10 first, then the intended raid size

| Check | Required evidence | Result |
| --- | --- | --- |
| Phase transition | Core seats every living participant on a red drake, entry 30161; each controlled bot has a `seated` record with a control/cast seat. Missing riders are failures, not excluded successes. | Not run |
| Damage and healing roles | Observe actual Flame Spike 56091, Engulf 56092, Revivify 57090 and Life Burst 57143 effects. Damage riders build their own Engulf; healer riders heal the group and use their own combo points. Compare casts with real energy/combo changes. | Not run |
| Surge | Compare the actual damage aura (57407/60936) and Flame Shield 57108 duration. A warning or a shield cast alone does not prove the shield covered the damage. Record initial ticks and deaths. | Not run |
| Static Fields and range | Watch real fields, Arcane Pulse and the drake stack. Compare sampled movement with field exposure and health loss. A movement request alone is insufficient. | Not run |
| Moving owner | Move the owner's drake within a safe ring position. Bots must keep casting while following, and never move the owner's drake. | Not run |
| Stay/passive | Put one bot on stay/passive in a controlled attempt. It must not take a new healer allocation or receive controller casts/movement; record its `ready=0` state. | Not run |
| Completion | Ordinary boss damage produces the core encounter/kill receipt. Check any achievement separately; this test does not grant one. | Not run |

Repeat role, Surge and field checks for 25-player mode before claiming that
mode works. For a deliberately excluded or dead rider, keep it in the roster
report so the denominator is honest.

## Flame Leviathan: owner present, ordinary difficulty first

1. Before the owner boards, bots must not begin new autonomous boarding. The
   owner then enters a real siege engine, demolisher or chopper in the same
   raid and instance. Record the time of this cue.
2. Check that empty main drivers (33060/33109) fill before nested gunners
   (33067/33167), and choppers (33062) are the last fallback. No occupied or
   foreign-group driver is evicted. `seated` must name the actual resulting
   seat, not just the vehicle clicked.
3. Keep one bot in the owner's nested gunner seat. Its caster entry must be the
   turret, with the owner's main vehicle in `parent`; it must not steer or move
   the owner's main vehicle. Observe this directly while changing the owner's
   direction, since inherited positions alone cannot prove control ownership.
4. Observe a normal pull: accepted weapons continue during pursuit movement;
   Siege Electroshock 62522 actually interrupts Flame Vents; demolishers use
   Pyrite 62490 and refresh its damage; gunners use their own turret spells.
   Preserve damage/interrupt evidence, not only spell counts.
5. A dead owner still present in the same raid/instance may allow the active
   fight to continue. That is intentional. New boarding still requires the
   living human riding cue. Logout or leaving the raid/instance is a different
   test: controller eligibility must become false, with no new controller
   casts. Already-issued motion or a cast in flight can finish; record it and
   distinguish it from newly issued work.
6. Repeat with the intended vehicle mix and raid size. Towers/hard modes and
   the autonomous offline bypass are outside this evidence; no tower-specific
   strategy is claimed.

## Cleanup and isolation checks for both raids

During a controlled failed attempt, inspect one bot death, one vehicle death,
one manual seat exit, a seat change, a logout and an instance transfer. Final
records must identify the old seat and flush its counts once. Re-entry must
start a new observation with no previous rider/vehicle counts. A bot in another
instance, a real player, or a self-bot must never be controlled by this feature.
Do not deliberately disconnect the owner during a valuable achievement attempt.

At the end, restore the agreed config and retain the exact log interval. Turning
support off restores upstream vehicle behavior; it is not a promise that stock
bots stop moving. Complete the result column with observed outcomes and links
to the preserved logs. Keep support off when a critical row is failed or unrun.
