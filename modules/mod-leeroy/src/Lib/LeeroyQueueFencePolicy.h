// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_QUEUE_FENCE_POLICY_H
#define MOD_LEEROY_QUEUE_FENCE_POLICY_H

#include <cstdint>
#include <map>
#include <string>
#include <string_view>

namespace Leeroy
{
enum class QueueKind { Battleground, Arena, DungeonFinder, RaidBrowser, Wintergrasp };

struct QueueFenceState
{
    bool enabled = true, cast = false, human = false, humanGroupInInstance = false;
    // ownerRequest: the owner himself is the requester, so his whole group is
    // his to queue. ownerGroup: this member's group contains the owner.
    bool ownerRequest = false, ownerGroup = false;
    std::string reason;
};

// Empty means allow. The adapter supplies current ownership, never a wire flag.
std::string_view QueueFenceReason(QueueFenceState const& state, QueueKind kind);

// Only the exact synchronous native PvP call may cross its own queue fence.
// A queued packet, a different member/session, or a generic Realm hold cannot.
bool QueueFenceNativeCallMatches(QueueKind kind, bool wintergrasp, std::uint32_t guid,
    std::uint32_t callGuid, std::uintptr_t session, std::uintptr_t watchedSession,
    std::string_view callToken, std::string_view watchedToken);

// Playerbots' rated-arena captain gathers its team by taking each member out of its
// group and teleporting it (only a member whose master is a game client, or one in
// combat, a battleground or a queue, is skipped), so a cast member left in an old bot
// team can be pulled out of an offline raid. Playerbots' own pin guard only stops new
// teams. A pinned cast member leaves every team the owner does not captain; a member
// who captains a bot team cannot leave without handing the team on, so it is kept.
enum class CastArenaTeamExit { Keep, Captain, Wait, Leave };
struct CastArenaTeamFacts
{
    bool enabled = false, cast = false, pinned = false, human = false;
    bool ownerCaptain = false, memberCaptain = false, realmHeld = false;
    bool teamFighting = false, inBattleground = false, inQueue = false, groupQueued = false;
};
CastArenaTeamExit DecideCastArenaTeamExit(CastArenaTeamFacts const& facts);

// At most one log for each observed continuous hold, across all queue kinds.
// The adapter serializes access and calls Observe with an empty hold on release.
class QueueFenceLog
{
public:
    void Observe(std::uint32_t guid, std::string_view hold);
    bool Refuse(std::uint32_t guid, std::string_view hold);
    void Clear() { _holds.clear(); }
private:
    struct Hold { std::string key; bool logged = false; };
    std::map<std::uint32_t, Hold> _holds;
};
}
#endif
