# Raid vehicle support

Patch 0014 adds `Playerbots.RaidVehicleSupport`, default `0`. Apply the numbered
playerbots series and rebuild the native server before enabling it in
`playerbots.conf`. This is a separate switch from the raidplay responder and
from the offline Ulduar bypass.

## Malygos

The boss script summons and seats each living participant on its own red drake.
The patch controls only ordinary bots in the raid, in their own control seat;
real players and self-bots are excluded. Stay/passive bots are excluded from the
new actions and healer allocation. Dead, non-present and other-instance group
members do not take healer slots.

Drakes gather on a safe human drake when possible, otherwise a deterministic
45-yard ring point outside Arcane Pulse. Static Fields exclude nearby points.
Roughly one in five eligible bot riders heals, preferring their normal healers;
at least one remains a damage dealer. Damage uses Flame Spike and stacked
Engulf in Flames; healers build Revivify on themselves and use Life Burst for
the stack. During a Surge warning, bots build/reserve combo points and energy
for Flame Shield. Shields wait for the actual damage aura: warning GUIDs remain set after
the damage, and a short shield cast early can expire before any damage. Reaction
can allow initial damage ticks through; the shield protects the remaining channel.
Blazing Speed helps a field escape. Costs, targets, cooldowns and normal cast
checks remain enforced. No health, phase, kill or achievement state is written.

The spell-data cross-check matters here: Flame Shield 57108 has an implicit
caster shield plus an explicit-target dummy effect. The cast therefore supplies
the current combo target (Malygos for a damage drake, itself for a healer),
while the protective aura still applies to the caster. Life Burst uses the
healer's self combo points. Revivify 57090 and Engulf 56092 directly apply their
periodic auras; those are the aura IDs checked for refresh, with no triggered
spell substitution. The normal cast validates the selected target and cost.

## Flame Leviathan with a human raid

A living real player in the same raid/instance must first board one of the
encounter's siege vehicles within 300 yards. Bots use the ordinary spell-click
handler. Empty siege-engine/demolisher driver seats take priority; melee/ranged
preferences do not strand vehicles when the roster is uneven. Bots may then
fill an empty gunner seat on a vehicle driven by their group. They do not evict
riders, enter another group's vehicle or fill a chopper's inactive passenger
seat. Chopper drivers are the final fallback.

Only a main control seat can drive. Drivers close to their weapon range while
the existing pursuit escape and turret/pyrite/interrupt actions remain in use.
Instant weapons and movement share each action tick so continuous kiting or
following cannot starve casting. If the human dies but remains in the raid and
instance, vehicles continue an active fight; new boarding still needs the cue.
Siege Ram leaves Electroshock's energy available. The default-off switch preserves
the stock actions when off. Offline Ulduar still uses its existing bypass and
does not receive a Flame Leviathan kill from this patch.

## Checks before rollout

The pure decision test runs in the native build workflow. It checks role
allocation for raid sizes 2–25, every energy/combo boundary, shield timing,
field/range decisions, and driver/gunner selection. The changed translation
units also receive the real core/playerbots headers in the full CI build.
These are code checks, not evidence of a successful live pull.

With a test raid after deployment, enable the switch and verify:

1. Malygos 10 and 25: every living bot gets the script's personal drake; movement
   leaves Static Fields and stays outside Arcane Pulse. Damage bots maintain
   their own Engulf stack; healer drakes refresh the stack's healing. Warning
   targets shield during the actual Surge damage. Finish through ordinary
   damage and verify the core's kill/achievement receipt.
2. Malygos without a human master: the same drake actions run. Leave one bot
   stay/passive and confirm it is not assigned a healer slot or driven by the
   new controller. Repeat after a dead or non-present group member.
3. Flame Leviathan: board first as the human; bots fill main drivers before
   group gunners, drive into range, interrupt Flame Vents and escape pursuit.
   A bot in your gunner seat must never turn or move your main vehicle.
4. Turn the switch off and confirm stock behavior. A bots-only Ulduar run must
   still take its existing bypass, with no earned Flame Leviathan kill.

No tower/hard-mode completion is claimed without a real pull. This patch does
not automate the human's vehicle or implement a tower-specific strategy.
