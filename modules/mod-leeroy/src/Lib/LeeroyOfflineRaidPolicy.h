// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_OFFLINE_RAID_POLICY_H
#define MOD_LEEROY_OFFLINE_RAID_POLICY_H

#include <cstdint>
#include <string>
#include <vector>

namespace Leeroy::Guild
{
struct OfflineRaidLimits
{
    std::uint32_t maxWipes, maxWipesPerBoss, timeoutMs, minMembers, maxRestarts;
};

struct OfflineRaidState
{
    std::uint32_t elapsedMs;
    bool humanOnline, leaderPresent;
    std::uint32_t membersPresent, wipes, maxWipesOnOneBoss, bossesDone, bossesTotal;
    bool dcEnabled;
    std::uint32_t dcOffMs, restarts;
};

enum class Verdict { Continue, Restart, Done };
struct Decision { Verdict verdict; std::string reason; };

Decision Decide(OfflineRaidState const& state, OfflineRaidLimits const& limits);
bool IsWipe(std::uint8_t oldState, std::uint8_t newState);
std::uint32_t AdvanceElapsed(std::uint32_t elapsed, std::uint32_t diff);

// A new copy keeps each boss at TO_BE_DECIDED (5) until that boss's own script loads with its grid, so
// unloaded bosses are fresh. Only a recorded kill (DONE, 3) or a completed-encounter credit marks a used copy.
bool FreshEncounters(std::vector<std::uint8_t> const& bossStates, std::uint32_t completedEncounterMask);

// Configuration may release the cast pins while a job is running. These checks also apply during setup/restarts,
// before the running-only policy can meaningfully count members or inspect encounter state.
std::string OfflineRaidStopReason(bool humanOnline, bool moduleEnabled, bool keepCast,
    bool driverPresent, std::uint32_t elapsedMs, std::uint32_t timeoutMs, bool instanceStrategies = true);
}

#endif
