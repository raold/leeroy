// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyOfflineRaidPolicy.h"

#include <limits>

namespace Leeroy::Guild
{
Decision Decide(OfflineRaidState const& state, OfflineRaidLimits const& limits)
{
    if (state.humanOnline)
        return {Verdict::Done, "human_login"};
    if (state.elapsedMs >= limits.timeoutMs)
        return {Verdict::Done, "timeout"};
    if (!state.leaderPresent)
        return {Verdict::Done, "leader_lost"};
    if (state.membersPresent < limits.minMembers)
        return {Verdict::Done, "too_few"};
    if (state.maxWipesOnOneBoss >= limits.maxWipesPerBoss)
        return {Verdict::Done, "boss_wipe_cap"};
    if (state.wipes >= limits.maxWipes)
        return {Verdict::Done, "wipe_cap"};
    if (state.bossesTotal && state.bossesDone == state.bossesTotal)
        return {Verdict::Done, "cleared"};
    if (!state.dcEnabled && state.dcOffMs >= 30000)
        return state.restarts >= limits.maxRestarts ? Decision{Verdict::Done, "dc_stalled"}
                                                    : Decision{Verdict::Restart, "dc_stopped"};
    return {Verdict::Continue, {}};
}

bool IsWipe(std::uint8_t oldState, std::uint8_t newState)
{
    return oldState == 1 && (newState == 0 || newState == 2);
}

std::uint32_t AdvanceElapsed(std::uint32_t elapsed, std::uint32_t diff)
{
    auto const maximum = std::numeric_limits<std::uint32_t>::max();
    return diff > maximum - elapsed ? maximum : elapsed + diff;
}

std::string OfflineRaidStopReason(bool humanOnline, bool moduleEnabled, bool keepCast,
    bool driverPresent, std::uint32_t elapsedMs, std::uint32_t timeoutMs, bool instanceStrategies)
{
    if (humanOnline)
        return "human_login";
    if (elapsedMs >= timeoutMs)
        return "timeout";
    if (!moduleEnabled)
        return "module_disabled";
    if (!keepCast)
        return "keep_cast_off";
    if (!driverPresent)
        return "driver_lost";
    if (!instanceStrategies)
        return "instance_strategies_off";
    return {};
}
}
