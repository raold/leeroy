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
            if (state.humanGroupInInstance) return "busy:human_instance";
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
