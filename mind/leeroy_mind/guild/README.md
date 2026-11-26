# Guild loot receipts

Currency tokens (including all Wrath raid emblems) stay on the core's automatic
per-player distribution path. They do not open a DKP claim window, announce an
item or produce a “no takers” line. Gear and tier-set tokens retain master loot.

Durable hand-out proof is on by default: `[guild] loot_receipts = true` in `mind.toml`
and `Leeroy.LootReceipts.Enable = 1` in the native configuration. Deploy the native
and mind changes together and reconnect after enabling so the mind sees the new
hello capability. Both settings default on. When the mind setting is on but the
native capability is missing, new hand-outs wait rather than silently losing proof.

The native build requires core commit `c8bd1f28c20c72560fa5014eda3b0aa7c3d63dec`
(`fix(Core/Database): Restart lost transactions`, upstream #28020). The core owns
COMMIT and does not replay a transaction whose commit acknowledgement was lost;
exact persisted readback still decides whether the award completed.

The native Characters table `leeroy_loot_give_receipt` reserves the original
`give_loot` command ID, scoped by realm, guild and owner, before touching loot.
The award receipt and recipient inventory are committed in one transaction.
A positive acknowledgement follows an independent readback of the exact item,
recipient, source entry, instance, map and slot. Database work runs off the world
thread; delayed work rechecks ownership, connection and source generation.

After an unknown outcome, the mind uses `get_loot_receipt` with a fresh transport
ID and the original give ID and fields. It never repeats that give. Matching
persisted proof charges once, even after raid closure or a mind/server restart.
Recovery continues when new receipt awards are disabled. Outstanding bids remain
held across later raids; a persisted rejection releases them without a charge.

A reservation interrupted by a server crash, a missing row or a database failure
does **not** prove an award. Such records remain uncertain for operator review;
they are neither re-granted nor charged. In particular, a live inventory change
whose database transaction failed cannot be billed on the strength of an empty
corpse. Old pre-feature uncertain/gone awards have no native proof and are not
retroactively charged. Keep the Characters receipt table with database backups.

No live configuration or database is changed by this PR. No mind migration is
needed; native creates its table asynchronously on the first opted-in give.
