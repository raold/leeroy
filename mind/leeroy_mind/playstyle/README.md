# Persona play styles

Enable both `[playstyle] enabled = true` in `mind.toml` and native
`Leeroy.PlayStyle.Enable = 1`. Both default off; invalid mind tables are off.
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

The mind requests styles at initial online-cast discovery, member login and new
owner-group membership. Repeated unchanged snapshots do not reapply. It rejects
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
reported as restoration. Logs contain ids, counts, numeric style codes and exception
classes only; the off/no-receipt path attaches no hooks or tasks and changes no prompt.
