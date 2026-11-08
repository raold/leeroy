# Queue fence

`Leeroy.QueueFence.Enable = 1` (the default, reloadable) prevents new random-bot
queues from taking cast members that `RealmModule::MutationReason` says are
owned. It also protects cast members in the human's group on an instance map.
The human and non-cast characters have no fence of their own. Group admission
checks every member, so a free leader cannot enqueue a held cast member.
Turning this key or `Leeroy.Enable` off disables both refusal and withdrawal.
A native build is needed; this does not change the running installation.

## Paths and hooks

Audited against core `e7e2e288e7d9ec30e2dfe6f417178a95986fcbb2` and the local
playerbots series ending at `3826b2f4db0c1086c51c97443749e0975b7fd788`. Paths below
are relative to those repositories. Line numbers describe that audit revision.

| Path | Admission and last check |
| --- | --- |
| Random battleground | playerbots `src/Ai/Base/Actions/BattleGroundJoinAction.cpp:554` queues `CMSG_BATTLEMASTER_JOIN`; core `src/server/game/Handlers/BattleGroundHandler.cpp:142` calls `OnPlayerCanJoinInBattlegroundQueue`. |
| Rated and skirmish arena | Same playerbots file, line 559, queues `CMSG_BATTLEMASTER_JOIN_ARENA`; the core handler's line 780 calls `OnPlayerCanJoinInArenaQueue`. |
| BG/arena invitation acceptance | Core handler line 430 calls `OnPlayerCanBattleFieldPort`, before resurrection, group/queue changes and teleport. This also catches playerbots' direct handler call in `AcceptBattlegroundInvitationAction.cpp:21`, which bypasses packet hooks. Declines remain allowed. |
| Dungeon Finder | playerbots `src/Ai/Base/Actions/LfgActions.cpp:184` queues `CMSG_LFG_JOIN`. Core `src/server/game/DungeonFinding/LFGMgr.cpp:614` calls `OnPlayerCanJoinLfg`. `LFGQueue.cpp:415` calls `CanCreateLfgProposal` before creating a proposal; the fence rechecks copied member/group holds there. |
| Raid Browser | playerbots `src/Bot/RandomPlayerbotMgr.cpp:1648` in `CheckRaidBrowser` queues the same `CMSG_LFG_JOIN`. It reaches the same LFG admission hook before `LFGMgr.cpp:826` calls `JoinRaidBrowser`. |
| Wintergrasp accept packets | Core `src/server/game/Battlefield/BattlefieldHandler.cpp:89,105` handles queue/war invite responses. `ServerScript::CanPacketReceive` rejects only WG accepts for held sessions, leaving declines and other battlefield packets alone. |

The audited playerbots BG, arena, LFG and Raid Browser enqueue paths do not
bypass these admission hooks. No playerbots or core changes are required for
them. Admission handlers run on the world thread and read current ownership.
Acceptance, packet and LFG compatibility hooks use a synchronized snapshot
published after the module's world-tick actions, because those hooks can run on
other threads. A hold created later by another module is visible to those copied
checks on the next world tick; admission checks already read current ownership.

## Intentional Realm PvP

Realm's arena continuation invokes the same core handler synchronously with a
thread-local native capability. The narrow `RealmPvpQueueCallOwns` accessor
recognizes only an exact nonempty operation token, member guid, watched session,
and PvP queue kind. Only that call crosses its fence. Ordinary queued packets,
another member/session and a mere Realm lease have no authority. Existing Realm
admission, body-identity and group-manifest validation still precede that call.
No new RealmModule accessor or change to Realm body/cleanup executors is needed.

## Existing queues and limits

At the end of each world tick the fence withdraws held members from solo,
non-rated battleground/skirmish queues using `RemovePlayer` plus the matching
player queue-slot/status update; solo queued LFG and Raid Browser via `LeaveLfg`;
and uninvited WG queues via `AskToLeaveQueue`. Refusals and withdrawals share one
`queue_fence_refused` log per member per observed hold (guid and owner code only).

It leaves group and rated BG/arena queues alone: core removal can remove other
members or change a rated invite's rating. Their held members cannot accept a
BG/arena invitation through the fenced handler. It leaves group LFG queues,
role checks and already-created proposals alone because cancellation affects
other members; new proposals are vetoed. It never alters an active match,
dungeon group or teleport in progress. Realm leases keep their queues for their
own exact cleanup protocol; the fence still refuses foreign admission/acceptance.
This is not a recovery tool for bodies already moved before the hold.

Wintergrasp is not a battleground queue. Its zone entry
(`Battlefield.cpp:89`), zone/queued war invitations (lines 228 and 241), and
relogin rejoin (line 636) are separate. The battlefield hooks return `void` and
cannot veto these paths. The audited playerbots source has no direct WG queue
or war-accept call: ordinary travel/teleports can enter zone 4197, where core
offers invitations. This audit does not establish which movement caused a
particular incident. Walking/flying into WG, generic zone teleports, automatic
rejoin, an already issued invite's private grace/kick state, and direct calls to
`PlayerAcceptInviteToQueue/War` remain outside this fence. Removing an uninvited
queue entry does not claim those private states are clean. A complete WG fence
would need a boolean core admission hook before queue insertion, war-group
mutation and auto-rejoin; any future playerbots direct call would need to honor
that hook. No such core/playerbots change is included here.
