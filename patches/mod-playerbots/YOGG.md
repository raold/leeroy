# Yogg-Saron brain team

Patch 0019 adds `Playerbots.YoggBrainSupport = 0` (default). It requires a
native rebuild. Do not enable it merely because the unit tests pass: the
supervised pull below is still required. Nothing in this PR enables the switch
or changes the live server, Keepers, sanity stacks, encounter state or achievements.

With it enabled, the Ulduar strategy uses real spell-click portals (34072),
normal attacks on influence tentacles and their disguises, the Brain's current
illusion and open door as proof it is safe to attack, and real Flee to the Surface
objects (194625). The floating Brain is approached using the room's floor.
Missing or incomplete mmap paths are refused. Legacy Yogg portal, illusion,
floor-recovery and movement cheats are disabled at their action entry points;
the other Yogg actions cannot use their direct-kill/teleport branches.

Portal teams are frozen per wave, with four slots in 10-player and ten in
25-player raids. Tanks stay outside, one healer enters if at least one/two bot
healers can remain outside, and melee damage dealers take precedence over ranged.
Present human non-tanks have reserved slots and choose whether to enter. The
controller never commands humans or self-bots. Stay/passive orders are respected.
Each bot claims one actual portal GUID; a human using that portal does not cause
the bot to steal a teammate's. Such a wave can be short one member.

Admission requires at least 40 Sanity and no Brain Link or Malady. No well or
Keeper is assumed. Adds are selected by gaze exposure before distance; skulls and
cinematic actors are never attacked. Attack/cast facing must have zero skull
exposure; otherwise the bot searches bounded offsets around the add for a safe
angle and walks there, or faces away and withholds combat. These checks use the
core spell radius and its actual line-of-sight rules. Surface Lunatic Gaze interrupts attacks and
casts and turns bots away until the channel ends. At 25 Sanity, at Brain 30%,
on loss of the actual Induce Madness cast, or when the remaining cast time is
less than the exit travel budget, the brain team stops combat and heads for the
exit. The budget uses distance, current run speed, a route allowance and a five
second margin, with at least twelve seconds reserved.
New casts must fit before that deadline; the emergency reaction can interrupt
an existing cast for exit or gaze even while the ordinary AI engine is waiting.

**Limits:** exit objects are beyond the illusion doors in this core. If the
team fails to kill every tentacle, a closed door can prevent escape; there is
deliberately no synthetic teleport rescue. Overlapping skull fields can have no
fully safe facing; a team can therefore withhold attacks rather than finish the
wave, and movement between safe attack points can still lose sanity. Portal timing,
skull line of sight, all three room paths, Brain melee reach,
healing throughput, and the real deadline margin still need live evidence. This
is not a claim that zero-Keeper Yogg or Death's Demise has been completed.

## Supervised validation

1. Keep the feature off while deploying the native build. Record current Keeper
   count and raid size. Do not add Keepers to make a zero-Keeper run pass.
2. Enable it only for an announced pull with the owner present. Start in 10-player
   before 25-player. Confirm the retained tanks and healers stay upstairs and that
   the human's portal choice is not controlled by a bot.
3. Watch a full chamber, Icecrown and Stormwind wave. Every selected bot should
   click exactly one real portal, damage only disguises/tentacles while the door
   is closed, then walk into the Brain room without waiting for the owner.
4. Confirm normal damage credit for the Brain. There must be no `path_blocked`
   on its approach, no instant tentacle kills and no fall-recovery teleports.
5. Check exits from the far end of each illusion and from the Brain with slowed
   movement. Every successful team must leave before 64059 finishes. A blocked
   door/path is a failed rehearsal, not a reason to force-teleport bots.
6. Confirm Sanity actually decreases when exposed; low-Sanity bots refuse the next
   wave. Check the upstairs 64163 gaze stops attacks/casts only during its real
   channel and normal combat resumes afterward. Check Brain Link and Malady
   continue using the existing surface responders.
7. Compare raid debrief/training hurt plus the `yogg_brain` logs. On path failure,
   wrong targets, late exits or runaway sanity loss, disable the switch after the
   pull and preserve the logs. Do not claim a Realm First from unit tests.

Logs contain instance/bot IDs, entries, counts and sanity only. `transition`
mode values are 1 portal, 2 exit, 3 doorway route, 4 combat, 5 gaze and 6 waiting
for a safe target. `path_blocked` is limited to once per ten seconds per bot.

## Tests

`tests/yogg_brain_test.cpp` in the generated module patch compiles standalone
with C++20. CI runs it with `-Wall -Wextra -Werror`, then compiles the full native
integration. Its production-policy checks cover 10/25-player team sizes, humans,
healer reserves, traversal order, sanity thresholds, every Brain/door/cast gate,
real add entries, slow/invalid-speed exit budgets, and gaze facing around the
full circle. Local validation also syntax-checks the modified production C++
against the configured core headers with MSVC, without changing the live build.
