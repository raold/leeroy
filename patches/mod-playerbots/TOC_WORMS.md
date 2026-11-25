# ToC worm cures

`Playerbots.TocWormSupport = 0` is the default. The candidate is not approved for
live enablement until the isolated offline ToC gate completes Northrend Beasts.

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
codestyle checker pass. Real candidate encounter acceptance is pending. The
change does not claim to fix Icehowl charges or every source of Slime Pool damage.

Failing baseline: `20261124_213821-offline-dojo-152ecd20dcc1402c815e940bfba5816b`,
source `beebc340`, playerbots `072d8a7a`, dungeon-clear `e6654f52`. The run recorded
`boss_wipe_cap`, zero kills, three wipes, 92 Slime Pool damage events, and confirmed
cleanup and shutdown. The first pull reached Icehowl.
