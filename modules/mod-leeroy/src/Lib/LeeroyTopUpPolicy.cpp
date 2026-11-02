// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyTopUpPolicy.h"
#include <algorithm>

namespace Leeroy::Guild
{
bool TopUpAllowed(TopUpState const& s, std::uint64_t cooldownMs)
{
    return s.isCast && s.online && !s.inCombat && !s.dead && !s.grouped && !s.onInstanceMap
        && !s.inBattleground && !s.teleporting && !s.inFlight && !s.trading
        && s.nowMs >= s.lastTopUpMs && (!s.lastTopUpMs || s.nowMs - s.lastTopUpMs >= cooldownMs);
}

std::vector<std::uint32_t> TopUpOrder(std::vector<std::uint32_t> members, std::uint32_t lastHandled)
{
    std::sort(members.begin(), members.end());
    members.erase(std::unique(members.begin(), members.end()), members.end());
    members.erase(std::remove(members.begin(), members.end(), 0), members.end());
    std::rotate(members.begin(), std::upper_bound(members.begin(), members.end(), lastHandled), members.end());
    return members;
}

std::uint32_t TopUpMissing(std::uint32_t have, std::uint32_t target)
{
    return have < target ? target - have : 0;
}

std::vector<std::uint32_t> TopUpFoodCategories(bool hasFood, bool hasDrink, std::uint32_t maxMana)
{
    std::vector<std::uint32_t> categories;
    if (!hasFood)
        categories.push_back(11);
    if (maxMana && !hasDrink)
        categories.push_back(59);
    return categories;
}
}
