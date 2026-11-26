# Persona play styles

Enable both `[playstyle] enabled = true` in `mind.toml` and native
`Leeroy.PlayStyle.Enable = 1`. Both default on; invalid mind tables are off.
Restart the mind to attach a newly enabled controller. Once attached it rereads
the switch on hello/login, queuing restoration when off. Native reloads with
`.reload config`, invalidating previously queued additions even on an off/on pair.

The version-1 deterministic map uses only existing playerbots behavior:

| Trait | Requested behavior |
| --- | --- |
| Melee/ranged role | Wait `min(6, 2 + cautious + reliable + jitter)` seconds |
| Neuroticism at least 7 | `cautious=1`, else 0 |
| Conscientiousness at least 7 | `reliable=1`, else 0 |
| Guid | `jitter=((guid*1103515245+12345)&0xffffffff)%3` |
| Tank/healer role | No added wait |
| Conscientiousness at least 6, or clutch skill | Add `potions` combat strategy |
| Other consumable bucket | Keep stock use; never disable it |

These controls exist in playerbots `Ai/Base/Strategy/WaitForAttackStrategy.cpp`
and `UsePotionsStrategy.cpp`. Wait suppresses most actions until its timer ends,
so native rechecks actual tank/healer roles. Potions adds normal healthstone,
healing-potion and mana-potion triggers; it neither creates items nor bypasses
their cooldowns. No loot tendencies, gear, formation, duel policy, idle movement,
flasks or combat emotes are changed.

The mind requests styles only while the owner is in game: at his login (for the
online cast), then at member login, online-cast discovery and new owner-group
membership. While he is away a hello still restores every owned member (the
reconciliation below) and adds nothing back: a style changes how his bots fight
beside him, and the restore-then-reapply of about 190 `bot_do` at each of 46
hellos in three days ran for nobody. Repeated unchanged snapshots do not reapply. It rejects
known instance maps, raid groups and observed engaged members. Native is the final
combat/instance authority: it refuses every dungeon/raid instance, raid group,
combat, travel, casting, trade, PvP, foreign controller, Realm group/body hold,
offline-raid/dummy owner or training state. Missing boss telemetry cannot admit
an instance. The compact validated wire form is `bot_do`, `action="playstyle"`,
`param="<delay>:<potions>"` (delay 0 or 2–6, potions 0/1). A native hello advertises
`playstyle`; older servers get no requests.

A preexisting active hold is never shortened; one above six seconds is untouched.
The inactive stock value of ten seconds does not count as an active delay.
An independent journal captures the exact player/AI/session and original values
before writing, without enrolling in Training's off-state cleanup. Restore writes
only fields still equal to this controller's write. An externally replaced delay
also keeps its active wait strategy. Explicit `bot_do` and training strategy/value
writers unwind style ownership after validation, so later cleanup cannot undo even
an equal-value replacement. RaidHabits restores where possible, installs its own
stock/raid settings and discards the old style journal. Logout discards the retired
AI's journal; a replacement body/session/AI never inherits it.

Native checks retained journals once a second for off, role, instance, raid, PvP
and ownership changes; instance map changes also request restoration. Foreign
controller/Realm ownership or transit retains cleanup rather than mutating the
other owner's body. Ordinary outdoor combat uses the already-prepared style.
`bot_do restore` with `param="playstyle"` restores only these entries; `"*"`
restores style then training entries. Restoration is allowed with the style flag off.

The mind durably records possible ownership before sending; only an exact terminal
success releases it. An interrupted send, missing terminal or refusal retains the
receipt for hello/restart reconciliation. Pending work and receipts cap at 512
members. Shutdown attempts groups of at most 40 for at most 20 seconds; any
unconfirmed cleanup remains recorded. Set the native flag off to request its
independent cleanup even while the mind is stopped. A successful ack alone is never
reported as restoration. Each pass (a hello's reconciliation, his login, a group)
logs one `playstyle_pass` line: `commands`, `applied`, `restored`, `other` (counts by
`apply:`/`restore:` and reason: native's code, `skipped:<code>`, `refused:<ack error>`,
`no_job`, `guids`, `kind`, `transport:<class>`), `owner` and `owned`; the per-command
`playstyle_result`, `playstyle_refused` and `playstyle_transport` lines are DEBUG.
Logs contain ids, counts, numeric style codes and exception classes only; the off/no-receipt path attaches no hooks or tasks and changes no prompt.

## The table (`playstyle_table`)

A native whose hello lists `playstyle_table` gets the whole table in one command instead of one
`bot_do` a member: `{styles: [{guid, param}, ...]}`, every cast member with a persona (online or
not, at most 512), each `param` the `bot_do` form above. Native holds it for this connection only
and styles each member itself, once a second at most 16 tries, whenever she is free and he is in
game outside a raid group (the same admission as a single request: alive, out of combat, no
instance, raid group, PvP, foreign controller or other owner). A refused, offline or already-styled
member is checked again 15 seconds later, so a member in a battleground, dead or held by the Realm
at his login is styled once she is free, and again after a cleanup took the style back. Until
then the mind tried each member once at his login and at a few other events, and on 2026-11-11..25
63% of those attempts were refused for a passing state (busy:pvp 1,040, busy:body 322,
realm_owner 173, dead 66) and never retried: his login passes styled 34% of the cast on average.

The mind sends the table at hello (after restoring any receipts the one-`bot_do` path left), at
every login of his whatever native was last sent, and on a member login or cast when it differs
from the one native holds; unchanged it sends nothing. `[playstyle] enabled = false` sends an
empty table. Native takes back every style the table added once the table no longer lists that
member: an empty or smaller table, or the table dropped by the mind's disconnect or by
`Leeroy.Enable`, `Leeroy.PlayStyle.Enable` or `Leeroy.PlayStyle.Table` at 0 (as a config load
invalidates queued `bot_do` additions; the table comes back at his next login). A style from an explicit
`bot_do playstyle` stays that writer's; the table never takes it over. The table records no
receipt in `playstyle:owned`: native's journal is the only owner, and native restores it with the
connection that sent the table.

Fallback: refused `playstyle_table_off` (native `Leeroy.PlayStyle.Table = 0`, reloadable) or for
any reason of its own, the connection keeps one `bot_do` a member exactly as before; refused as one
`bot_do` would be (`playstyle_off`, `disabled`), it sends nothing until his next login or hello.
His login always tries the table again, first restoring the styles the one-`bot_do` path added
(native leaves another writer's style alone, so the table could not take those members over). Logs: `playstyle_table_sent styles= on= owner=`,
`playstyle_table_refused error= mode=` (`legacy` or `off`), `playstyle_table_failed error=` (an
exception class; native may hold the table all the same, so the mind's next send replaces it, an
empty one included); Server.log `mod-leeroy: playstyle_table held styles=`, `... dropped styles=
why=` (`mind`, `off`) and, at most once a minute when a style was added or the styled count moved,
`mod-leeroy: playstyle_table styles= styled= tried= applied= refused=<code=n,...>`.
