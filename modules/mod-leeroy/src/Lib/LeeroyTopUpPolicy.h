// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TOP_UP_POLICY_H
#define MOD_LEEROY_TOP_UP_POLICY_H

#include <cstdint>
#include <vector>

namespace Leeroy::Guild
{
struct TopUpState
{
    bool isCast = false, online = false, inCombat = false, dead = false, grouped = false;
    bool onInstanceMap = false, inBattleground = false;
    std::uint64_t nowMs = 0, lastTopUpMs = 0;
    bool teleporting = false, inFlight = false, trading = false;
};

bool TopUpAllowed(TopUpState const& state, std::uint64_t cooldownMs);
std::vector<std::uint32_t> TopUpOrder(std::vector<std::uint32_t> members, std::uint32_t lastHandled);
std::uint32_t TopUpMissing(std::uint32_t have, std::uint32_t target);
// Spell categories used by ordinary food (11) and drink (59).
std::vector<std::uint32_t> TopUpFoodCategories(bool hasFood, bool hasDrink, std::uint32_t maxMana);
}

#endif
