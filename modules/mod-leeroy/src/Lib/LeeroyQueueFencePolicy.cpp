// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyQueueFencePolicy.h"

namespace Leeroy
{
std::string_view QueueFenceReason(QueueFenceState const& state, QueueKind kind)
{
    if (!state.enabled || !state.cast || state.human) return {};
    switch (kind)
    {
        case QueueKind::Battleground:
        case QueueKind::Arena:
        case QueueKind::DungeonFinder:
        case QueueKind::RaidBrowser:
        case QueueKind::Wintergrasp:
            // Characters that are not random bots, and bodies that are merely
            // unreachable right now (teleporting, self-bot), are not holds.
            if (state.reason == "not_bot" || state.reason == "busy:body_unavailable") return {};
            if (state.ownerRequest)
                return state.reason == "busy:raid_owner" ? std::string_view{} : state.reason;
            if (state.humanGroupInInstance) return "busy:human_instance";
            if (state.ownerGroup && state.reason == "busy:raid_owner") return {};
            return state.reason;
    }
    return {};
}

bool QueueFenceNativeCallMatches(QueueKind kind, bool wintergrasp, std::uint32_t guid,
    std::uint32_t callGuid, std::uintptr_t session, std::uintptr_t watchedSession,
    std::string_view callToken, std::string_view watchedToken)
{
    return guid && guid == callGuid && session && session == watchedSession
        && !callToken.empty() && callToken == watchedToken
        && kind == (wintergrasp ? QueueKind::Wintergrasp : QueueKind::Arena);
}

CastArenaTeamExit DecideCastArenaTeamExit(CastArenaTeamFacts const& f)
{
    if (!f.enabled || !f.cast || !f.pinned || f.human || f.ownerCaptain) return CastArenaTeamExit::Keep;
    if (f.memberCaptain) return CastArenaTeamExit::Captain;
    // Core refuses a leave while the team fights or the member is invited from a
    // queue; DelMember also pulls queued group-mates, so wait for all of those.
    if (f.realmHeld || f.teamFighting || f.inBattleground || f.inQueue || f.groupQueued)
        return CastArenaTeamExit::Wait;
    return CastArenaTeamExit::Leave;
}

void QueueFenceLog::Observe(std::uint32_t guid, std::string_view hold)
{
    if (hold.empty()) { _holds.erase(guid); return; }
    auto& state = _holds[guid];
    if (state.key != hold) state = {std::string(hold), false};
}
bool QueueFenceLog::Refuse(std::uint32_t guid, std::string_view hold)
{
    Observe(guid, hold);
    if (hold.empty()) return false;
    auto& state = _holds.at(guid);
    if (state.logged) return false;
    state.logged = true;
    return true;
}
}
