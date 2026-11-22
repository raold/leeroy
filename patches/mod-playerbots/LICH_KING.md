# Lich King mechanics (experimental, opt-in)

Patch 0018 adds `Playerbots.LichKingMechanics = 0` to the playerbots config template.
It is off by default; shipping the patch does not enable it. An approved native
release and a supervised pull are required before enabling it in the installed
playerbots config. No live config or service is changed by this PR.

The encounter in this AzerothCore version has two different Frostmourne fights:

- **Normal:** one player enters with Terenas and a Spirit Warden. Healers heal
  Terenas with spells they know and can afford; bots interrupt Soul Rip when
  their class can, and damage the Warden through their ordinary rotation.
- **Heroic:** the raid enters together. Terenas is immune; there is no Warden
  healing objective. Every role dodges falling Spirit Bombs and approaching
  Wicked Spirits, while ranged damage roles shoot attackable spirits. The
  encounter script returns players to the platform.

The new action preserves the room boundary during transport, inside the room,
and after return. It prevents outside follow/assist targets from pulling a bot
back to the platform. The old LK actions cannot teleport victims out, snap their
height, kill spirits instantly, force a falling bot's death, grant Nitro Boosts,
or grant the old defensive/stun auras while this switch is on. Normal spell
costs, cooldowns, immunity and cast checks still apply. Other encounters are
unchanged. Real players, self-bots and bots explicitly ordered to stay/passive
are excluded. Emergency dodges use the reaction engine so an ongoing cast or
channel does not postpone Defile or bomb avoidance.

On the platform:

- Defile uses the core's `10 * object scale` damage radius, plus two yards,
  instead of estimating it from aura stacks. Paths cannot cross another
  puddle, deepen exposure to the current puddle, or leave the central platform.
  The actual navigation path is checked as well as its destination; missing or
  incomplete paths fail closed.
  The cast target moves away from the raid before the puddle spawns.
  The bounded search spans the whole platform diameter, so a grown puddle is
  not mistaken for a blocked route merely because its edge is over 28 yards away.
- Val'kyr rescue uses the actual passenger seat. Available damage bots and
  off-tanks split across carriers and use real learned stuns/slows. Main tanks
  and healers keep their duties. The core releases the passenger on heroic
  below 50%; an empty carrier stops being a rescue target immediately.
  The allocation uses the same eligibility checks as execution, so stay/passive
  and self-bots cannot consume rescue slots. Target acquisition consumes one
  tick; an already selected target yields to the ordinary damage rotation.
- Ranged damage bots shoot Vile Spirits. Low, approaching spirits are avoided
  by every role instead of assigning an off-tank to soak them with free auras.
- If no validated route exists, the bot reports `no_safe_path` and does not
  invent an unsafe fallback. This is a failure to investigate, not a successful
  avoidance result.

## Supervised checks before leaving it on

1. Start with a normal pull and verify the imported LK strategy is active.
   In Frostmourne, observe genuine Terenas healing, Soul Rip interrupts, and
   Warden health falling from combat damage. Check that return happens only
   through the encounter's Restore Soul.
2. Check Defile at the edge and with an existing puddle in the escape direction.
   The target must move before the drop; no bot or owner should receive a free
   speed aura. Watch for `no_safe_path`, excessive movement, or follow actions
   undoing a dodge.
3. Observe a carrying Val'kyr: damage/stuns must come from actual casts, healers
   must continue healing, and released carriers must stop stealing targets.
4. On a heroic pull, verify bomb avoidance for healers, melee, tanks and ranged,
   plus real Wicked Spirit damage. Check that normal class casts still run
   between dodges and nobody attempts to heal immune Terenas.
5. Check Vile Spirit collision damage and deaths against the raid debrief and
   training hurt, including return from heroic Frostmourne. Compare both raid
   sizes; movement geometry is shared, encounter damage is not.

`lk_mechanic` native log records include instance, bot GUID, room, code, success
and target entry. They are sampled at most once per five seconds per bot, not a
complete count of casts. Codes include `terenas_heal`, `soul_rip_interrupt`,
`frostmourne_dodge`, `defile_drop`, `platform_dodge`, `valkyr_cc`, `valkyr_rescue`,
`spirit_attack`, and `no_safe_path`. There are no player names or chat messages.

To roll back behavior, set `Playerbots.LichKingMechanics = 0` through the usual
approved native configuration procedure. That restores upstream LK behavior,
including its existing shortcuts; it does not undo an encounter in progress.

## Validation and limits

The patch includes a portable C++ policy harness (room/transport boundaries,
role/difficulty decisions, passenger release, Defile scale, route intersections,
platform edges and blocked routes). CI runs it beside the existing raid tests.
Native translation units are also checked with the Windows compiler.

These checks do not reproduce a full raid. A normal/heroic live pull is still
required to validate timing, spell throughput, navigation and encounter tuning.
This change neither awards nor guarantees an achievement or a Realm First.
