// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_DEATH_LOOP_POLICY_H
#define MOD_LEEROY_DEATH_LOOP_POLICY_H

#include <cstdint>
#include <deque>
#include <optional>

namespace Leeroy::Guild
{
inline constexpr std::uint64_t kDeathWindowMs = 15 * 60 * 1000;
inline constexpr std::uint64_t kDeathRescueCooldownMs = 60 * 60 * 1000;
inline constexpr unsigned kDeathRescuesPerWindow = 5;
inline constexpr std::uint64_t kDeathAreaClearMs = 60 * 1000;
inline constexpr float kDeathAreaRadius = 100.0f;

struct DeathLoopState
{
    bool cast = false, pinned = false, online = false;
    bool grouped = false, instance = false, battleground = false, owned = false;
    bool dead = false, combat = false, transit = false, trading = false, duel = false;
    bool invited = false, queued = false;
    std::uint32_t map = 0;
    float x = 0, y = 0, z = 0;
};

struct DeathArea { std::uint32_t map; float x, y, z; };

struct DeathLoopHistory
{
    std::deque<std::uint64_t> deaths;
    std::optional<std::uint64_t> lastAttempt;
    std::optional<std::uint64_t> outsideSince;
    std::optional<DeathArea> area;
    bool wasDead = false;
};

struct DeathRescueBudget
{
    std::deque<std::uint64_t> attempts;
    bool Claim(std::uint64_t now);
};

// Observe every member even after the rolling mutation allowance is spent.
void ObserveDeathLoop(DeathLoopHistory& history, DeathLoopState const& state, std::uint64_t now);
bool DeathLoopRescueReady(DeathLoopHistory const& history, DeathLoopState const& state, std::uint64_t now);
// Claim before attempting the teleport: refusals are bounded too. Returns the death count.
unsigned ClaimDeathLoopRescue(DeathLoopHistory& history, DeathLoopState const& state,
    std::uint64_t now, DeathRescueBudget& budget);
}
#endif
