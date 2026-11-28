# ToC worm cures

`Playerbots.TocWormSupport = 1` is the shipped default; `0` disables new cure and
spacing actions. The release must set the live option to `1` as well. A release
requires the isolated offline ToC gate to complete Northrend Beasts.

The baseline reproduced three wipes and zero encounter kills. On the first pull,
three poisoned ranged bots remained about 40 yards from Burning Bile and were
paralysed for roughly 43 seconds before a carrier finally reached them.

With the option enabled, mobile toxin victims approach an actual Bile carrier.
For an already rooted victim, one mobile bot carrier is elected, preferring a
non-tank. Actors wait within the real cure radius for the core's periodic effect;
the action never removes an aura or teleports a player. Afterward, unaffected bots
separate from Bile. Tank carriers retain position when another carrier can yield.

The trigger performs constant-time encounter and eligibility checks. Group scans
run only in the actions on the ToC map. `stay`, `passive`, self-bots, transports,
teleports, other maps/phases and dead players are excluded as appropriate. A human
can be a cure destination, but is never elected to perform a bot rescue.

Validation: native MSVC syntax checks, standalone policy tests and the module's
codestyle checker pass. The recorded `20261125_215236` full ToC run completed
Beasts, Jaraxxus, Champions and Twins before three Anub'arak wipes. The restack
behind main's Valithria patch needs its own candidate verification. This change
does not claim to fix Icehowl charges or every source of Slime Pool damage.

The contact-safety follow-up preserves casts and Auto Shot when a cure or spacing
move is refused, and ignores a non-combat-only `stay` during combat. Combat `stay`
and `passive` still hold the bot. The shared held-order check also applies to the
existing Anub'arak actions.

Cure actions try eight contact points. Native pool auras supply the growing
damage radius, with two ticks of growth plus a two-yard margin and actor reach.
Waiting and every route leg must respect that clearance, even in bot raids with
no mod-leeroy responder. Existing responder escapes and move refusals still win.
Routes use navmesh paths when enabled, or the core's collision and ground checks
on the encounter's floor when map 649 disables navmesh. Only half a second of a
checked route is committed before pools are checked again.

When all direct contacts are blocked, a bounded visibility graph searches around
expanded pools. It selects a shortest safe contact route, then proves each leg
against the core's ground/collision checks or complete navmesh paths. A refused
terrain leg is excluded before trying another route. The search admits at most
12 pools, eight contacts, 12 terrain attempts and 160 yards; exceeding those bounds
refuses movement. The final short spline retains an explicit midpoint so the
core cannot replace a checked two-point route with its own path calculation.

The recorded two-pool blockage from `20261127_143357` is a regression fixture:
all direct approaches to five paralysed victims fail, while the detours clear
both expanded pools. This is geometry proof only. Runtime acceptance must also
show actual detours and cures without a pool-crossing cure path. The dedicated
`playerbots.toc_worm` debug logger records only actor/move ids, route coordinates,
pool geometry and counts; acceptance enables it in the isolated config only.
The failed first pull remains evidence, even though a later pull cleared Beasts.

This follow-up needs its own isolated worm encounter and before/after performance
receipt before release. Portable policy tests and a production-header MSVC check
pass; they do not replace that runtime gate. The current-main full ToC release
gate remains separate and must run before these additional changes are included.

Failing baseline: `20261124_213821-offline-dojo-152ecd20dcc1402c815e940bfba5816b`,
source `beebc340`, playerbots `072d8a7a`, dungeon-clear `e6654f52`. The run recorded
`boss_wipe_cap`, zero kills, three wipes, 92 Slime Pool damage events, and confirmed
cleanup and shutdown. The first pull reached Icehowl.
