// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_OFFLINE_RAID_POLICY_H
#define MOD_LEEROY_OFFLINE_RAID_POLICY_H

#include <cstdint>
#include <optional>
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

// Empty error admits. Repeated stops during our teardown are no-ops; a finished or foreign job is refused.
std::string OfflineStopError(bool connected, bool owned, bool finished, bool tearingDown,
    bool stopped, std::optional<bool> bossEngaged);
bool BossProgress(std::uint8_t before, std::uint8_t after);

// A running offline raid may recall an unchanged, still-owned follower. These are
// per-member, per-run bounds; arriving never replenishes the attempt budget.
enum class RecallAction { Wait, Summon, Arrived };
struct OfflineRecall
{
    std::uint32_t absentMs = 0, sinceAttemptMs = 0, attempts = 0;
    bool pending = false;
    RecallAction Observe(bool inside, bool safe, std::uint32_t diffMs);
};
inline constexpr std::uint32_t OfflineRecallGraceMs = 30000;
inline constexpr std::uint32_t OfflineRecallRetryMs = 60000;
inline constexpr std::uint32_t OfflineRecallAttempts = 3;

// Configuration may release the cast pins while a job is running. These checks also apply during setup/restarts,
// before the running-only policy can meaningfully count members or inspect encounter state.
std::string OfflineRaidStopReason(bool humanOnline, bool moduleEnabled, bool keepCast,
    bool driverPresent, std::uint32_t elapsedMs, std::uint32_t timeoutMs, bool instanceStrategies = true);
}

#endif
