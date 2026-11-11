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
inline constexpr unsigned kDeathRescuesPerPass = 5;

struct DeathLoopState
{
    bool cast = false, pinned = false, online = false;
    bool grouped = false, instance = false, battleground = false, owned = false;
    bool dead = false, combat = false, transit = false, trading = false, duel = false;
};

struct DeathLoopHistory
{
    std::deque<std::uint64_t> deaths;
    std::optional<std::uint64_t> lastAttempt;
    bool wasDead = false;
};

// Observe every member even after the pass's mutation allowance is spent.
void ObserveDeathLoop(DeathLoopHistory& history, DeathLoopState const& state, std::uint64_t now);
// Claim before attempting the teleport: refusals are bounded too. Returns the death count.
unsigned ClaimDeathLoopRescue(DeathLoopHistory& history, DeathLoopState const& state,
    std::uint64_t now, unsigned& attempted);
}
#endif
