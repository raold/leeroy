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

Failing baseline: `20261124_213821-offline-dojo-152ecd20dcc1402c815e940bfba5816b`,
source `beebc340`, playerbots `072d8a7a`, dungeon-clear `e6654f52`. The run recorded
`boss_wipe_cap`, zero kills, three wipes, 92 Slime Pool damage events, and confirmed
cleanup and shutdown. The first pull reached Icehowl.
